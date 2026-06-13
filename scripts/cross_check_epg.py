#!/usr/bin/env python3
"""Cross-check the built guide against an EXTERNAL reference EPG.

Our sources can be wrong or incomplete for a channel; an independent
reference (a curated third-party XMLTV) is a cheap second opinion. This
matches channels by BRAND IDENTITY (display-name tokens — the reference's
channel ids never line up with ours) and reports two things:

  incomplete  we ship dummies (no real EPG) for a channel the reference
              has a real schedule for  → go find it in a TRUSTED source
              (epgshare / iptv-org / provider) and pin via aliases.tsv
  wrong       we ship real EPG whose titles barely overlap the reference
              → POSSIBLE mis-binding. NOTE: sports/live-event channels
              legitimately disagree (titles are time/fixture-specific),
              so treat "wrong" as "look here", not proof.

The reference is NEVER ingested into the build — it's opaque-id, low-trust,
and mostly redundant. It's used here purely as a verification oracle.

Usage:
  python3 scripts/cross_check_epg.py --guide docs/guide.xml.gz \
      --audit docs/epg-audit.tsv --reference epg6.xml.gz --region UK,US
"""

from __future__ import annotations

import argparse
import gzip
import html
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_epg import canon_identity_tokens, name_tokens  # noqa: E402

_CHAN_RE = re.compile(r'<channel\s+id="([^"]*)"[^>]*>(.*?)</channel>', re.DOTALL)
_DN_RE = re.compile(r'<display-name[^>]*>([^<]*)</display-name>')
_PROG_RE = re.compile(r'<programme[^>]*channel="([^"]*)"[^>]*>(.*?)</programme>',
                      re.DOTALL)
_TITLE_RE = re.compile(r'<title[^>]*>([^<]*)</title>')
DUMMY_MARKER = "Programme guide unavailable"


def _read(path: Path) -> str:
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace")


def identity_titles(path: Path) -> dict[frozenset, set[str]]:
    """brand-identity -> union of lowercased programme titles."""
    data = _read(path)
    names_by_cid: dict[str, list[str]] = {}
    for m in _CHAN_RE.finditer(data):
        names_by_cid[html.unescape(m.group(1))] = [
            html.unescape(n) for n in _DN_RE.findall(m.group(2))]
    titles_by_cid: dict[str, set[str]] = defaultdict(set)
    for m in _PROG_RE.finditer(data):
        if DUMMY_MARKER in m.group(2):
            continue
        tm = _TITLE_RE.search(m.group(2))
        if tm:
            titles_by_cid[html.unescape(m.group(1))].add(
                html.unescape(tm.group(1)).strip().lower())
    out: dict[frozenset, set[str]] = defaultdict(set)
    for cid, names in names_by_cid.items():
        for n in names:
            ident = canon_identity_tokens(name_tokens(n))
            if len(ident) >= 2:
                out[ident] |= titles_by_cid.get(cid, set())
    return out


def our_titles(guide: Path) -> dict[str, set[str]]:
    data = _read(guide)
    out: dict[str, set[str]] = defaultdict(set)
    for m in _PROG_RE.finditer(data):
        if DUMMY_MARKER in m.group(2):
            continue
        tm = _TITLE_RE.search(m.group(2))
        if tm:
            out[html.unescape(m.group(1))].add(
                html.unescape(tm.group(1)).strip().lower())
    return out


def load_audit(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    hdr = lines[0].split("\t")
    rows = []
    for ln in lines[1:]:
        p = ln.split("\t")
        if len(p) >= len(hdr):
            rows.append(dict(zip(hdr, p)))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--guide", required=True, type=Path)
    ap.add_argument("--audit", required=True, type=Path)
    ap.add_argument("--reference", required=True, type=Path)
    ap.add_argument("--region", default="UK,US",
                    help="comma list of region tags to check (e.g. UK,US); "
                         "ALL to check every channel")
    ap.add_argument("--min-ref", type=int, default=4,
                    help="min reference titles to trust a comparison")
    ap.add_argument("--wrong-overlap", type=float, default=0.15,
                    help="title-overlap below this = 'wrong' candidate")
    args = ap.parse_args()

    regions = [r.strip().upper() for r in args.region.split(",") if r.strip()]
    all_regions = "ALL" in regions

    ref = identity_titles(args.reference)
    ours = our_titles(args.guide)
    rows = load_audit(args.audit)

    def in_region(r: dict) -> bool:
        if all_regions:
            return True
        n, e = r["display_name"].upper(), r["effective_id"].lower()
        return any(f"[{rg}]" in n or e.endswith(f".{rg.lower()}")
                   for rg in regions)

    incomplete, wrong = [], []
    for r in rows:
        if not in_region(r):
            continue
        ident = canon_identity_tokens(name_tokens(r["display_name"]))
        if len(ident) < 2:
            continue
        reft = ref.get(ident, set())
        if len(reft) < args.min_ref:
            continue
        cnt = int(r.get("real_prog_count") or 0)
        if cnt == 0:
            incomplete.append((len(reft), r))
        else:
            mine = ours.get(html.unescape(r["effective_id"]), set())
            if len(mine) >= args.min_ref:
                ov = len(mine & reft) / min(len(mine), len(reft))
                if ov < args.wrong_overlap:
                    wrong.append((round(ov, 2), r, sorted(mine)[:2],
                                  sorted(reft)[:2]))

    print(f"cross-check vs {args.reference.name} "
          f"(regions={','.join(regions)}): "
          f"{len(incomplete)} incomplete, {len(wrong)} wrong-candidate(s)")
    print("\n== INCOMPLETE (we dummy; reference has a schedule) — "
          "pin a TRUSTED source via aliases.tsv ==")
    for c, r in sorted(incomplete, reverse=True):
        print(f"  ref={c:4d}  {r['display_name']}  ({r['effective_id']})")
    print("\n== WRONG-CANDIDATE (low title overlap) — VERIFY; sports/live "
          "titles differ legitimately ==")
    for ov, r, mine, reft in sorted(wrong):
        print(f"  {ov:>4}  {r['display_name']}  "
              f"[{r['match_tier']}<-{r['donor_cid']}]")
        print(f"        ours: {mine}")
        print(f"        ref:  {reft}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
