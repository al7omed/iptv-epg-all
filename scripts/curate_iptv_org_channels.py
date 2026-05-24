#!/usr/bin/env python3
"""Curate iptv-org/epg channels.xml down to channels in our M3U.

iptv-org/epg ships channels.xml files with 1.7k-96k channel-region
permutations per site (freeview.co.uk, tvtv.us, tvpassport.com). Trying to
grab all of them in CI times out the 240s per-site budget, so we filter
the channels.xml down to ~150-300 channels that have a plausible match in
our actual M3U playlist before running `npm run grab`.

Filtering uses two scoring tiers:

  1. Strict normalized-name match (uppercased + decoration-stripped). Same
     `normalize_name()` as build_epg.py — so the curator agrees with what
     the build later considers a match.

  2. Token-set match: every token in the iptv-org channel's display-name
     must appear in some M3U channel's token-set, AND the iptv-org
     channel must carry ≥2 distinguishing tokens (single-token names like
     "Sports" or "News" match too loosely).

Usage (overwrite-in-place):
  python3 scripts/curate_iptv_org_channels.py \\
    --m3u epg-work/m3u_sub1.m3u --m3u epg-work/m3u_sub2.m3u \\
    --src epg-tool/sites/freeview.co.uk/freeview.co.uk.channels.xml \\
    --out epg-tool/sites/freeview.co.uk/freeview.co.uk.channels.xml \\
    --max-channels 300

Exit codes:
  0 — wrote curated file successfully (even if 0 channels matched; that's
      a valid result, the upstream grab will just no-op).
  2 — invalid CLI args or src/m3u path missing.
"""
from __future__ import annotations

import argparse
import html as html_module
import os
import re
import sys
from pathlib import Path

# Re-use the matching primitives from build_epg.py so curator and build
# agree on what counts as a name match.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_epg import normalize_name, name_tokens, parse_m3u  # noqa: E402


CHANNEL_RE = re.compile(
    r'<channel\s+([^>]*?)\s*>(.*?)</channel>',
    re.DOTALL | re.IGNORECASE,
)
ATTR_RE = re.compile(r'(\w[\w_-]*)="([^"]*)"')


def parse_channels_xml(text: str) -> list[dict]:
    """Return list of dicts: {attrs: {...}, name: 'Display Name'} per <channel>."""
    out = []
    for m in CHANNEL_RE.finditer(text):
        attr_text, inner = m.group(1), m.group(2)
        attrs = {k: html_module.unescape(v) for k, v in ATTR_RE.findall(attr_text)}
        name = html_module.unescape(inner).strip()
        out.append({"attrs": attrs, "name": name, "raw": m.group(0)})
    return out


def build_m3u_match_sets(m3u_paths: list[Path]) -> tuple[set, list[frozenset]]:
    """Read every M3U file, return:
       - set of normalized M3U channel names
       - list of token-frozenset per M3U entry (for token-set match).
    """
    norm_names: set = set()
    token_sets: list[frozenset] = []
    for p in m3u_paths:
        if not p.exists():
            print(f"  WARN: m3u {p} missing — skipping", file=sys.stderr)
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            print(f"  WARN: m3u {p} unreadable ({e}) — skipping", file=sys.stderr)
            continue
        for ch in parse_m3u(text):
            for nm in (ch.get("tvg_name"), ch.get("title"), ch.get("tvg_id")):
                if not nm:
                    continue
                nn = normalize_name(nm)
                if nn and len(nn) >= 3:
                    norm_names.add(nn)
                toks = name_tokens(nm)
                if len(toks) >= 2:
                    token_sets.append(toks)
    return norm_names, token_sets


def score_channel(name: str,
                  m3u_norm: set,
                  m3u_token_sets: list[frozenset]) -> tuple[int, str]:
    """Score how well an iptv-org channel name matches our M3U.
    Returns (score, reason). Higher is better. 0 = no match.

    Tiers:
      3 — exact normalize match
      2 — token-set subset match (≥2 tokens, channel tokens ⊆ some M3U
          channel's tokens)
      1 — token-set partial (≥2 shared tokens with some M3U channel)
      0 — none
    """
    nn = normalize_name(name)
    if nn and nn in m3u_norm:
        return 3, "norm-exact"
    toks = name_tokens(name)
    if len(toks) < 2:
        return 0, "too-few-tokens"
    best_score = 0
    best_reason = ""
    for m_toks in m3u_token_sets:
        if toks.issubset(m_toks):
            # Subset is strong: every iptv-org token is in the M3U
            return 2, "token-subset"
        shared = toks & m_toks
        if len(shared) >= 2 and len(shared) > best_score - 1:
            best_score = 1
            best_reason = f"token-partial ({len(shared)})"
    return best_score, best_reason


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--m3u", action="append", required=True,
                    help="M3U playlist file (repeat for multiple).")
    ap.add_argument("--src", required=True,
                    help="Source iptv-org/epg channels.xml.")
    ap.add_argument("--out", required=True,
                    help="Output curated channels.xml.")
    ap.add_argument("--max-channels", type=int, default=300,
                    help="Cap output at N best matches. Default 300.")
    ap.add_argument("--min-score", type=int, default=1,
                    help="Drop entries with score < N. Default 1 (any partial match).")
    args = ap.parse_args()

    src_path = Path(args.src)
    out_path = Path(args.out)
    m3u_paths = [Path(p) for p in args.m3u]
    if not src_path.exists():
        print(f"ERROR: --src not found: {src_path}", file=sys.stderr)
        return 2
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"curate: src={src_path} out={out_path}", file=sys.stderr)
    print(f"curate: m3u sources: {[str(p) for p in m3u_paths]}", file=sys.stderr)

    m3u_norm, m3u_token_sets = build_m3u_match_sets(m3u_paths)
    print(f"curate: M3U index — {len(m3u_norm)} norm-names, "
          f"{len(m3u_token_sets)} token-sets", file=sys.stderr)
    if not m3u_norm and not m3u_token_sets:
        print("curate: empty M3U index — refusing to write 0-channel output",
              file=sys.stderr)
        return 2

    src_text = src_path.read_text(encoding="utf-8", errors="replace")
    src_channels = parse_channels_xml(src_text)
    print(f"curate: src has {len(src_channels)} channels", file=sys.stderr)

    scored: list[tuple[int, str, dict]] = []
    score_hist: dict[int, int] = {0: 0, 1: 0, 2: 0, 3: 0}
    for ch in src_channels:
        score, reason = score_channel(ch["name"], m3u_norm, m3u_token_sets)
        score_hist[score] = score_hist.get(score, 0) + 1
        if score >= args.min_score:
            scored.append((score, reason, ch))
    print(f"curate: score distribution: "
          f"3 (norm-exact)={score_hist.get(3,0)}, "
          f"2 (subset)={score_hist.get(2,0)}, "
          f"1 (partial)={score_hist.get(1,0)}, "
          f"0 (no match)={score_hist.get(0,0)}", file=sys.stderr)

    # Sort by score desc, then by name length asc (shorter = less specific
    # = generally more useful as a high-quality canonical match).
    scored.sort(key=lambda t: (-t[0], len(t[2]["name"])))
    if args.max_channels > 0:
        scored = scored[:args.max_channels]
    print(f"curate: keeping top {len(scored)} channels", file=sys.stderr)

    # Emit. Preserve the original site's XML attrs verbatim; only the
    # filter is applied. iptv-org/epg's grabber cares about site_id +
    # site + xmltv_id, so we don't rewrite anything inside.
    with out_path.open("w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write('<channels>\n')
        for _score, _reason, ch in scored:
            f.write(f"  {ch['raw']}\n")
        f.write('</channels>\n')
    print(f"curate: wrote {out_path} ({len(scored)} channels)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
