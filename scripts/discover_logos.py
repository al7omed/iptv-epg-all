#!/usr/bin/env python3
"""Auto-populate channels/logo_overrides.tsv with high-res channel logos.

~97% of channel logos in the upstream M3Us are 320px low-res
`photo-tmdb.com/stalker_portal/` thumbnails. Replace them by querying:

  1. TVMaze   — https://api.tvmaze.com/search/shows?q=<name>
                Free, no auth, has channel-level network/webChannel entries.
  2. Wikipedia API — pageimages on the channel article. Free, no auth.

For each unique canonical channel name in the M3U, queries both APIs and
records the first usable URL in `channels/logo_overrides.tsv`. Existing
lines are preserved verbatim — only NEW (canonical-name, URL) pairs are
appended. Run weekly via .github/workflows/discover-logos.yml.

Usage:
  python3 scripts/discover_logos.py                   # query all M3U sources
  python3 scripts/discover_logos.py --limit 50        # cap to N new lookups
  python3 scripts/discover_logos.py --dry-run         # don't write file
  python3 scripts/discover_logos.py --m3u <path>      # additional M3U source

Rate limits:
  TVMaze:    "Be nice" (no hard limit; we throttle at 4 req/sec).
  Wikipedia: 200 req/sec aggregate (we throttle at 4 req/sec).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_epg import (  # noqa: E402
    canonical_channel_name, clean_channel_name, parse_m3u,
)


USER_AGENT = (
    "iptv-epg-all/discover_logos (https://github.com/al7omed/iptv-epg-all)"
)
TVMAZE_SEARCH = "https://api.tvmaze.com/search/shows?q={q}"
WIKIPEDIA_PAGEIMAGE = (
    "https://en.wikipedia.org/w/api.php?"
    "action=query&format=json&prop=pageimages&pithumbsize=400&titles={q}"
)
WIKIPEDIA_OPENSEARCH = (
    "https://en.wikipedia.org/w/api.php?"
    "action=opensearch&format=json&limit=5&search={q}"
)
THROTTLE_SEC = 0.25  # 4 req/sec


def http_get(url: str, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def query_tvmaze(name: str) -> str | None:
    """Search TVMaze for a channel-style name. Return image URL or None.

    TVMaze indexes TV shows + networks; channels often appear as
    network entries on individual show records (e.g. 'BBC One' is
    show.network.name for many shows). We pick the FIRST result whose
    network.name OR webChannel.name OR show.name matches the query
    canonically.
    """
    q = urllib.parse.quote_plus(name)
    try:
        body = http_get(TVMAZE_SEARCH.format(q=q))
        data = json.loads(body)
    except (urllib.error.URLError, urllib.error.HTTPError,
            json.JSONDecodeError, TimeoutError):
        return None
    if not isinstance(data, list):
        return None
    canon = canonical_channel_name(name).lower()
    # Look at top 5 hits; prefer one whose network/webChannel/show name
    # canonicalises to the same canonical key.
    for hit in data[:5]:
        show = hit.get("show") if isinstance(hit, dict) else None
        if not isinstance(show, dict):
            continue
        candidates = []
        for key in ("network", "webChannel"):
            sub = show.get(key)
            if isinstance(sub, dict) and sub.get("name"):
                candidates.append(sub["name"])
        if show.get("name"):
            candidates.append(show["name"])
        for cand in candidates:
            if canonical_channel_name(cand).lower() == canon:
                img = show.get("image") or {}
                url = img.get("original") or img.get("medium")
                if url:
                    return url
    return None


def query_wikipedia(name: str) -> str | None:
    """Search Wikipedia for the channel's page, return pageimage URL or None."""
    q = urllib.parse.quote_plus(name)
    try:
        body = http_get(WIKIPEDIA_OPENSEARCH.format(q=q))
        data = json.loads(body)
    except (urllib.error.URLError, urllib.error.HTTPError,
            json.JSONDecodeError, TimeoutError):
        return None
    # opensearch returns [query, [titles], [descriptions], [urls]]
    if not (isinstance(data, list) and len(data) >= 2 and isinstance(data[1], list)):
        return None
    canon = canonical_channel_name(name).lower()
    chosen_title = None
    for title in data[1][:5]:
        if not isinstance(title, str):
            continue
        # Strip parenthetical disambiguators ('BBC One (TV channel)' -> 'BBC One')
        bare = re.sub(r"\s*\([^)]*\)\s*$", "", title).strip()
        if canonical_channel_name(bare).lower() == canon:
            chosen_title = title
            break
    if not chosen_title:
        # Fall back to first result if it canonicalises close enough — many
        # disambiguation pages still link the correct logo.
        if data[1]:
            chosen_title = data[1][0]
        else:
            return None

    # pageimages for the chosen title
    time.sleep(THROTTLE_SEC)
    q = urllib.parse.quote_plus(chosen_title)
    try:
        body = http_get(WIKIPEDIA_PAGEIMAGE.format(q=q))
        data = json.loads(body)
    except (urllib.error.URLError, urllib.error.HTTPError,
            json.JSONDecodeError, TimeoutError):
        return None
    pages = (data or {}).get("query", {}).get("pages", {})
    for _pid, page in pages.items():
        thumb = page.get("thumbnail")
        if isinstance(thumb, dict) and thumb.get("source"):
            return thumb["source"]
    return None


def load_existing_patterns(path: Path) -> set[str]:
    """Return the set of patterns already present in logo_overrides.tsv.
    Patterns are normalized lowercased to prevent duplicate adds when the
    file mixes casing.
    """
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].rstrip("\n")
        if not line.strip():
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        out.add(parts[0].strip().lower())
    return out


def collect_m3u_canonical_names(m3u_paths: list[Path]) -> list[str]:
    """Return a deduplicated list of canonical channel names from M3U(s)."""
    seen: set[str] = set()
    out: list[str] = []
    for p in m3u_paths:
        if not p.exists():
            print(f"  WARN: m3u {p} missing — skipping", file=sys.stderr)
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for ch in parse_m3u(text):
            raw = ch.get("tvg_name") or ch.get("title")
            if not raw:
                continue
            display = clean_channel_name(raw)
            if not display:
                continue
            canon = canonical_channel_name(display)
            if not canon or canon in seen:
                continue
            seen.add(canon)
            out.append(display)  # keep ORIGINAL casing for the TSV
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--m3u", action="append",
                    default=["epg-work/m3u_sub1.m3u",
                             "epg-work/m3u_sub2.m3u"],
                    help="M3U source(s) (default: epg-work/m3u_sub*.m3u)")
    ap.add_argument("--out", default="channels/logo_overrides.tsv",
                    help="logo_overrides.tsv path (default: %(default)s)")
    ap.add_argument("--limit", type=int, default=2000,
                    help="Cap new lookups per run (default: %(default)s)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print discovered URLs but don't write the file")
    args = ap.parse_args()

    m3u_paths = [Path(p) for p in args.m3u]
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    existing = load_existing_patterns(out_path)
    print(f"logo-discover: {len(existing)} patterns already in {out_path}",
          file=sys.stderr)

    candidates = collect_m3u_canonical_names(m3u_paths)
    print(f"logo-discover: {len(candidates)} unique canonical channels in M3U",
          file=sys.stderr)

    # Skip canonical names that already have an override entry. We compare
    # against the lower-case canonical form of the existing pattern so we
    # don't repeat work after multiple runs.
    new_pairs: list[tuple[str, str, str]] = []  # (pattern, url, source)
    looked_up = 0
    hits_tvmaze = 0
    hits_wiki = 0
    misses = 0
    for name in candidates:
        canon = canonical_channel_name(name).lower()
        if canon in existing:
            continue
        if looked_up >= args.limit:
            print(f"logo-discover: hit --limit ({args.limit}), stopping",
                  file=sys.stderr)
            break
        looked_up += 1
        time.sleep(THROTTLE_SEC)
        url = query_tvmaze(name)
        src = "tvmaze"
        if not url:
            time.sleep(THROTTLE_SEC)
            url = query_wikipedia(name)
            src = "wikipedia"
        if not url:
            misses += 1
            continue
        # Use the canonical name (lowercased) as the pattern — it's a
        # substring match against cleaned display names so 'bein sports 1'
        # finds 'beIN Sports 1 RAW', 'beIN SPORTS 1 HD', etc.
        new_pairs.append((canon, url, src))
        existing.add(canon)
        if src == "tvmaze":
            hits_tvmaze += 1
        else:
            hits_wiki += 1
        if looked_up % 50 == 0:
            print(f"  progress: {looked_up} looked up, "
                  f"{hits_tvmaze} tvmaze + {hits_wiki} wiki, {misses} misses",
                  file=sys.stderr)

    print(f"\nlogo-discover: done — {looked_up} lookups, "
          f"{hits_tvmaze} tvmaze + {hits_wiki} wiki = {len(new_pairs)} new, "
          f"{misses} misses", file=sys.stderr)

    if not new_pairs:
        print("logo-discover: nothing new to append", file=sys.stderr)
        return 0

    if args.dry_run:
        print("logo-discover: --dry-run — would append:")
        for pat, url, src in new_pairs:
            print(f"  {pat}\t{url}\t# {src}")
        return 0

    # Append-only. Existing lines (comments, user-edited rows, etc.) are
    # preserved untouched.
    with out_path.open("a", encoding="utf-8") as f:
        if out_path.stat().st_size and not out_path.read_text(encoding="utf-8").endswith("\n"):
            f.write("\n")
        f.write(f"\n# Auto-discovered by scripts/discover_logos.py\n")
        for pat, url, src in new_pairs:
            f.write(f"{pat}\t{url}\t# auto:{src}\n")
    print(f"logo-discover: appended {len(new_pairs)} entries to {out_path}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
