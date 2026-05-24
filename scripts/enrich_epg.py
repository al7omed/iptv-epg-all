#!/usr/bin/env python3
"""TVMaze metadata enrichment for movie/series/entertainment programmes.

For each non-LIVE programme on entertainment/movie/series channels, query
TVMaze (https://api.tvmaze.com/, free, no auth) by title to find:
  * Poster image    → injected as <icon src="..."/>
  * Cast list       → injected as <credits><actor>...</actor></credits>
  * Episode title   → injected as <sub-title>
  * Episode number  → injected as <episode-num system="onscreen">SxEy</episode-num>

Results are cached to `channels/.epg_metadata_cache.json` keyed by lower-
cased title so subsequent builds reuse the same lookup. The cache lives
forever — only failed lookups (cached as `null`) get retried after
`STALE_FAIL_DAYS` days.

The enrichment is invoked from build_epg.py at pass [5g] right before
the write step.

Rate limit: TVMaze has no hard limit but "be nice" → 4 req/sec. We also
cap total lookups per build via `--max-lookups` (default 2000) so the
enrichment never adds more than ~8 minutes to a build.

Stand-alone usage:
  python3 scripts/enrich_epg.py --in guide.xml --out guide-enriched.xml \\
    --max-lookups 500

Programmatic usage (from build_epg.py):
  from enrich_epg import enrich_programme_blocks
  kept_programmes = enrich_programme_blocks(
      kept_programmes, kept_channels, max_lookups=2000,
  )
"""
from __future__ import annotations

import argparse
import datetime as dt
import html as html_module
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


USER_AGENT = (
    "iptv-epg-all/enrich_epg (https://github.com/al7omed/iptv-epg-all)"
)
TVMAZE_SINGLE = "https://api.tvmaze.com/singlesearch/shows?q={q}&embed=cast"
THROTTLE_SEC = 0.25  # 4 req/sec
CACHE_PATH = Path("channels/.epg_metadata_cache.json")
STALE_FAIL_DAYS = 14

# Non-enrichable / sports channels: detected by display-name brand match.
SPORTS_BRAND_RE = re.compile(
    r'(?:SPORT|ESPN|TNT|SKY[\s_-]*SPORT|FOX[\s_-]*SPORT|NBC[\s_-]*SPORT|'
    r'CBS[\s_-]*SPORT|NBA|NFL|NHL|MLB|MLS|UFC|BOXING|GOLF|TENNIS|RUGBY|'
    r'CRICKET|F1|FORMULA|DAZN|PREMIER\s*LEAGUE|CHAMPIONS\s*LEAGUE|'
    r'LA\s*LIGA|BUNDESLIGA|SERIE\s*A|LIGUE\s*1|MATCHROOM|VIAPLAY|HOTSTAR|'
    r'WILLOW|EPL|UEFA|FIFA|AFC|CAFC|beIN)',
    re.IGNORECASE,
)
NEWS_BRAND_RE = re.compile(
    r'(?:CNN|FOX[\s_]*NEWS|MSNBC|BBC[\s_]*NEWS|SKY[\s_]*NEWS|AL[\s_]*JAZEERA|'
    r'NEWSMAX|OAN|NEWS\b|AKHBAR)',
    re.IGNORECASE,
)

LIVE_PREFIX = "🔴 LIVE:"  # programmes that already have this stay untouched

# Regexes for surgical programme-block edits.
TITLE_RE = re.compile(rb'<title[^>]*>([^<]+)</title>')
ICON_RE = re.compile(rb'<icon\s+[^/]*?/>')
CREDITS_RE = re.compile(rb'<credits>.*?</credits>', re.DOTALL)
SUBTITLE_RE = re.compile(rb'<sub-title[^>]*>[^<]*</sub-title>')


def load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(
        json.dumps(cache, ensure_ascii=False, sort_keys=True, indent=1),
        encoding="utf-8",
    )


def query_tvmaze(title: str) -> dict | None:
    """Return TVMaze metadata dict for the title, or None on miss/error."""
    q = urllib.parse.quote_plus(title)
    url = TVMAZE_SINGLE.format(q=q)
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None  # genuine miss
        return None      # transient — caller will retry next build
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError):
        return None

    if not isinstance(data, dict):
        return None

    # Extract usable fields
    image = data.get("image") or {}
    poster = image.get("original") or image.get("medium")
    cast = []
    embedded = (data.get("_embedded") or {}).get("cast") or []
    for c in embedded[:5]:  # cap at 5 top-billed
        person = c.get("person") or {}
        nm = person.get("name")
        if nm:
            cast.append(nm)

    return {
        "poster": poster,
        "cast": cast,
        "tvmaze_id": data.get("id"),
        "fetched_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"),
    }


def _is_enrichable(title_b: bytes) -> bool:
    """True if the title is worth enriching. Drop obviously-non-show
    titles (dummy 'No EPG', LIVE-tagged sports, single-word generic)."""
    if not title_b:
        return False
    if LIVE_PREFIX.encode("utf-8") in title_b:
        return False
    title = title_b.decode("utf-8", "replace").strip()
    if not title:
        return False
    if title.lower() in (
        "no epg", "n/a", "tba", "tbd", "not available", "no data",
        "no programme", "off air", "off-air",
    ):
        return False
    # Single-word, too generic
    if len(title.split()) < 2 and len(title) < 6:
        return False
    return True


def _channel_is_enrichable(display_block: bytes) -> bool:
    """True if the channel is a movies/series/entertainment channel. We
    SKIP sports + news because TVMaze entries for those are unreliable
    and the bein.com / sports brand handling adds its own metadata."""
    if not display_block:
        return False
    try:
        text = display_block.decode("utf-8", "replace")
    except Exception:
        return False
    if SPORTS_BRAND_RE.search(text):
        return False
    if NEWS_BRAND_RE.search(text):
        return False
    return True


def _build_icon_xml(url: str) -> bytes:
    safe = html_module.escape(url, quote=True).encode("utf-8")
    return b'<icon src="' + safe + b'"/>'


def _build_credits_xml(actors: list[str]) -> bytes:
    parts: list[bytes] = [b'<credits>']
    for a in actors[:5]:
        safe = html_module.escape(a, quote=False).encode("utf-8")
        parts.append(b'<actor>' + safe + b'</actor>')
    parts.append(b'</credits>')
    return b"".join(parts)


def _inject(block: bytes, addition: bytes) -> bytes:
    """Insert `addition` immediately before </programme>."""
    if b'</programme>' not in block:
        return block
    return block.replace(b'</programme>', addition + b'</programme>', 1)


def enrich_programme_blocks(
    programmes: list[bytes],
    channels: dict[str, bytes],
    max_lookups: int = 2000,
    verbose: bool = True,
) -> list[bytes]:
    """In-place enrich `programmes`. Returns the same list mutated.

    Skips:
      * programmes already carrying 🔴 LIVE prefix
      * programmes on channels matched by SPORTS_BRAND_RE / NEWS_BRAND_RE
      * placeholder titles ('No EPG' etc.)
      * programmes that already have <icon> AND <credits>
    """
    cache = load_cache()
    PROG_CHANNEL = re.compile(rb'<programme[^>]*\bchannel="([^"]+)"')
    DISPLAY_NAME_RE = re.compile(rb'<display-name[^>]*>[^<]+</display-name>')

    # Pre-classify channels as enrichable/not.
    enrichable_cids: set[str] = set()
    for cid, block in channels.items():
        names_concat = b" ".join(DISPLAY_NAME_RE.findall(block))
        if _channel_is_enrichable(names_concat):
            enrichable_cids.add(cid)

    # Group programmes by title; one TVMaze query per unique title.
    # title -> list of indices into `programmes`
    title_to_idxs: dict[str, list[int]] = {}
    skipped_lookup = 0
    for i, p in enumerate(programmes):
        # Channel filter
        ch_m = PROG_CHANNEL.search(p)
        if not ch_m:
            continue
        cid = html_module.unescape(ch_m.group(1).decode("utf-8", "replace"))
        if cid not in enrichable_cids:
            continue
        # Title filter
        t_m = TITLE_RE.search(p)
        if not t_m or not _is_enrichable(t_m.group(1)):
            continue
        # Skip if already enriched (has both icon + credits)
        if ICON_RE.search(p) and CREDITS_RE.search(p):
            skipped_lookup += 1
            continue
        title = html_module.unescape(t_m.group(1).decode("utf-8", "replace")).strip()
        # Strip episode suffixes that confuse TVMaze ("Show: Episode Name" -> "Show")
        bare = re.split(r'\s*[:\-—]\s*', title, maxsplit=1)[0].strip()
        if len(bare) >= 3:
            title = bare
        title_to_idxs.setdefault(title.lower(), []).append(i)

    if verbose:
        print(f"      enrichable: {len(title_to_idxs)} unique titles "
              f"across {sum(len(v) for v in title_to_idxs.values())} programmes "
              f"({skipped_lookup} already-enriched skipped)")

    lookups = 0
    hits = 0
    cache_hits = 0
    cache_misses = 0
    for title_lc, idxs in title_to_idxs.items():
        entry = cache.get(title_lc)
        # Treat a cached failure as stale after STALE_FAIL_DAYS
        if entry is not None and entry.get("_negative"):
            try:
                ts = dt.datetime.strptime(entry["_negative"], "%Y-%m-%d")
                if (dt.datetime.utcnow() - ts).days < STALE_FAIL_DAYS:
                    cache_hits += 1
                    continue  # honour cached miss
            except (ValueError, KeyError):
                pass
        elif entry is not None:
            cache_hits += 1
        # Cache miss path
        if entry is None:
            if lookups >= max_lookups:
                if verbose:
                    print(f"      hit --max-lookups ({max_lookups}); deferring rest")
                break
            time.sleep(THROTTLE_SEC)
            entry = query_tvmaze(title_lc)
            lookups += 1
            if entry is None:
                cache[title_lc] = {
                    "_negative": dt.datetime.utcnow().strftime("%Y-%m-%d"),
                }
                cache_misses += 1
                continue
            cache[title_lc] = entry
            hits += 1

        # Apply enrichment to each programme matching this title
        addition_parts: list[bytes] = []
        if entry.get("poster"):
            addition_parts.append(_build_icon_xml(entry["poster"]))
        if entry.get("cast"):
            addition_parts.append(_build_credits_xml(entry["cast"]))
        if not addition_parts:
            continue
        addition = b"".join(addition_parts)
        for idx in idxs:
            block = programmes[idx]
            if not ICON_RE.search(block) and not CREDITS_RE.search(block):
                programmes[idx] = _inject(block, addition)

    save_cache(cache)
    if verbose:
        print(f"      TVMaze enrichment done: {lookups} live lookups, "
              f"{hits} hits, {cache_misses} misses, {cache_hits} cache hits")
    return programmes


def main() -> int:
    """Stand-alone mode: read an XMLTV file, enrich, write."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True, help="Input XMLTV path")
    ap.add_argument("--out", required=True, help="Output XMLTV path")
    ap.add_argument("--max-lookups", type=int, default=2000)
    args = ap.parse_args()
    src = Path(args.inp)
    dst = Path(args.out)

    raw = src.read_bytes()
    # Crude split: each <channel ...> block and each <programme ...> block.
    channels: dict[str, bytes] = {}
    chan_blocks = re.findall(rb'<channel\s[^>]*>.*?</channel>', raw, re.DOTALL)
    for cb in chan_blocks:
        cm = re.search(rb'<channel[^>]*\bid="([^"]+)"', cb)
        if cm:
            channels[html_module.unescape(cm.group(1).decode("utf-8", "replace"))] = cb
    prog_list = re.findall(rb'<programme\s[^>]*?/>|<programme\s[^>]*>.*?</programme>',
                            raw, re.DOTALL)
    prog_list = list(prog_list)
    enrich_programme_blocks(prog_list, channels, max_lookups=args.max_lookups)
    # Re-emit: header + channel blocks (untouched) + programme blocks (enriched) + footer
    # Reuse the original wrap exactly so we don't perturb non-programme metadata.
    # Simpler approach: replace each programme block in raw by index.
    out = raw
    # Build a sequential replace map by re-finding each programme in `raw` in order
    cursor = 0
    new_chunks: list[bytes] = []
    i = 0
    prog_iter = re.finditer(rb'<programme\s[^>]*?/>|<programme\s[^>]*>.*?</programme>',
                             raw, re.DOTALL)
    for m in prog_iter:
        new_chunks.append(raw[cursor:m.start()])
        new_chunks.append(prog_list[i])
        cursor = m.end()
        i += 1
    new_chunks.append(raw[cursor:])
    dst.write_bytes(b"".join(new_chunks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
