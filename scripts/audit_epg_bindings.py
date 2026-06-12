#!/usr/bin/env python3
"""Flag suspicious channel→EPG bindings in a built guide.

Consumes the binding-provenance artifact (docs/epg-audit.tsv, written by
build_epg.py) plus the built guide, and emits a ranked list of bindings
that deserve a human look. It does NOT modify anything — fixes go into
channels/aliases.tsv (pin the right upstream) or channels/dummy_override.txt
(force a blank guide when every candidate is wrong).

Flag categories (severity high → low):
  dup-feed          ≥2 channels with DIFFERENT identities share an identical
                    programme set (one of them is wrong)
  brand-mismatch    a loose-tier binding whose donor channel id lacks a
                    brand token the playlist channel name carries
  genre-conflict    channel name implies one genre, sampled titles read as
                    another (documentary channel airing news bulletins...)
  loose-brand-bind  big-name brand channel bound via the loose token tiers
                    (informational — worth a spot check, often fine)
  cross-source      optional (--sources DIR of xmltv files): a source
                    carries the same channel id with <30% title overlap

Usage:
  python3 scripts/audit_epg_bindings.py --summary \
      --audit docs/epg-audit.tsv --guide docs/guide-lite.xml.gz
  python3 scripts/audit_epg_bindings.py --audit ... --guide ... --out flags.tsv

Exit code is always 0 unless --strict is given (then >0 if any
high-severity flag is found). The hourly workflow runs --summary
warn-only: upstream data drift must never block a build.
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
from build_epg import BRAND_TOKENS, name_tokens  # noqa: E402

LOOSE_TIERS = {"norm-name", "token", "rescue-token"}

# Brands big enough that a loose-tier binding deserves a human glance.
KNOWN_BRANDS = (
    "NAT GEO", "NATIONAL GEOGRAPHIC", "DISCOVERY", "MBC", "BEIN",
    "SKY", "BBC", "ITV", "FOX", "CNN", "HBO", "ESPN", "TNT", "OSN",
    "ROTANA", "AL JAZEERA", "DUBAI", "ABU DHABI", "PARAMOUNT", "CINEMAX",
)

# Crude genre detection: channel-name keywords -> genre, title keywords ->
# genre. Only STRONG cross-genre conflicts are flagged (a sports channel
# airing a documentary is normal; a documentary channel airing news
# bulletins and soap dramas is the Nat-Geo-Abu-Dhabi failure mode).
NAME_GENRE = {
    "doc": re.compile(r"NAT\s*GEO|\bGEO\b|GEOGRAPHIC|DISCOVERY|DOCUMENTAR|"
                      r"HISTORY|NATURE|WILD|ANIMAL", re.I),
    "kids": re.compile(r"KIDS|JUNIOR|CARTOON|NICKELODEON|CBEEBIES|BARAEM", re.I),
    "movies": re.compile(r"CINEMA|MOVIES|FILM|PARAMOUNT", re.I),
    "music": re.compile(r"\bMUSIC\b|\bMTV\b", re.I),
}
TITLE_GENRE = {
    "news": re.compile(r"\bnews\b|headline|bulletin|press|\btonight\b|"
                       r"morning show|weather", re.I),
    "drama": re.compile(r"episode|series|season \d", re.I),
}
GENRE_CONFLICTS = {("doc", "news"), ("kids", "news"), ("movies", "news"),
                   ("music", "news")}

_TITLE_RE = re.compile(rb"<title\b[^>]*>([^<]*)</title>")
_PROG_RE = re.compile(rb'<programme\b[^>]*channel="([^"]+)"[^>]*>(.*?)</programme>',
                      re.DOTALL)
DUMMY_MARKER = b"Programme guide unavailable"


def read_xml(path: Path) -> bytes:
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw


def load_audit(path: Path) -> list[dict]:
    rows = []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    header = lines[0].split("\t")
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) < len(header):
            parts += [""] * (len(header) - len(parts))
        rows.append(dict(zip(header, parts)))
    return rows


def donor_tokens(donor_cid: str) -> frozenset:
    """Approximate identifying tokens from an upstream channel id.
    Ids come in several shapes ('Abu.Dhabi.HD.ae', 'NatGeoWild.uk',
    'bein-sports-1'): split on punctuation, then split camelCase."""
    base = donor_cid.rsplit(".", 1)[0] if "." in donor_cid else donor_cid
    parts = re.split(r"[^A-Za-z0-9]+", base)
    toks: set[str] = set()
    for p in parts:
        for sub in re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", p):
            toks.add(sub.upper())
    return frozenset(t for t in toks if t)


def guide_titles_by_cid(guide_path: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for m in _PROG_RE.finditer(read_xml(guide_path)):
        body = m.group(2)
        if DUMMY_MARKER in body:
            continue
        tm = _TITLE_RE.search(body)
        if not tm:
            continue
        cid = html.unescape(m.group(1).decode("utf-8", "replace"))
        out[cid].append(html.unescape(tm.group(1).decode("utf-8", "replace")))
    return out


def flag_rows(rows: list[dict], titles_by_cid: dict[str, list[str]],
              sources_dir: Path | None) -> list[dict]:
    flags: list[dict] = []

    def add(severity: str, flag: str, row: dict, detail: str) -> None:
        flags.append({
            "severity": severity, "flag": flag,
            "effective_id": row["effective_id"],
            "display_name": row["display_name"],
            "match_tier": row["match_tier"], "source": row["source"],
            "detail": detail,
        })

    real_rows = [r for r in rows if r["match_tier"] not in
                 ("dummy", "dummy-forced", "scrubbed-lang", "dup-feed-scrub")
                 and int(r["real_prog_count"] or 0) > 0]

    # --- dup-feed: identical fingerprints across different identities ---
    by_fp: dict[str, list[dict]] = defaultdict(list)
    for r in real_rows:
        if r["prog_fp"] and int(r["real_prog_count"]) >= 4:
            by_fp[r["prog_fp"]].append(r)
    for fp, group in by_fp.items():
        tokset = {r["effective_id"]: name_tokens(r["display_name"])
                  for r in group}
        distinct = set(tokset.values())
        if len(group) < 2 or len(distinct) < 2:
            continue
        if any(t.isdigit() or t in BRAND_TOKENS
               for a in distinct for b in distinct if a is not b
               for t in (a ^ b)):
            names = ", ".join(sorted(r["display_name"] for r in group)[:4])
            for r in group:
                add("high", "dup-feed", r,
                    f"fp={fp} shared by {len(group)} channels: {names}")

    # --- brand-mismatch: loose tier, donor lacks a brand token we carry ---
    for r in real_rows:
        if r["match_tier"] not in LOOSE_TIERS or not r["donor_cid"]:
            continue
        ch_toks = name_tokens(r["display_name"])
        d_toks = donor_tokens(r["donor_cid"])
        missing = {t for t in ch_toks
                   if t in BRAND_TOKENS and t not in d_toks}
        if missing:
            add("high", "brand-mismatch", r,
                f"donor {r['donor_cid']} lacks brand tokens "
                f"{sorted(missing)}")

    # --- genre-conflict on sampled titles ---
    for r in real_rows:
        ch_genre = next((g for g, pat in NAME_GENRE.items()
                         if pat.search(r["display_name"])), None)
        if not ch_genre:
            continue
        titles = titles_by_cid.get(r["effective_id"], [])[:40]
        if len(titles) < 4:
            continue
        for t_genre, pat in TITLE_GENRE.items():
            if (ch_genre, t_genre) not in GENRE_CONFLICTS:
                continue
            hits = sum(1 for t in titles if pat.search(t))
            if hits / len(titles) >= 0.3:
                add("high", "genre-conflict", r,
                    f"{ch_genre} channel, {hits}/{len(titles)} titles look "
                    f"like {t_genre}: {titles[:3]}")
                break

    # --- loose-brand-bind: informational ---
    for r in real_rows:
        if r["match_tier"] not in LOOSE_TIERS:
            continue
        up = r["display_name"].upper()
        brand = next((b for b in KNOWN_BRANDS if b in up), None)
        if brand:
            add("info", "loose-brand-bind", r,
                f"{brand} channel bound via {r['match_tier']} "
                f"from {r['donor_cid'] or r['source']}")

    # --- cross-source disagreement (optional) ---
    if sources_dir and sources_dir.is_dir():
        src_titles: dict[str, set[str]] = defaultdict(set)
        for f in sorted(sources_dir.iterdir()):
            if f.suffix not in (".xml", ".gz"):
                continue
            try:
                raw = read_xml(f)
            except Exception:
                continue
            for m in _PROG_RE.finditer(raw):
                tm = _TITLE_RE.search(m.group(2))
                if tm:
                    cid = html.unescape(m.group(1).decode("utf-8", "replace"))
                    src_titles[cid].add(
                        html.unescape(tm.group(1).decode("utf-8", "replace")))
        for r in real_rows:
            for cid in {r["effective_id"], r["donor_cid"]}:
                ours = set(titles_by_cid.get(r["effective_id"], []))
                theirs = src_titles.get(cid or "", set())
                if len(ours) >= 4 and len(theirs) >= 4:
                    overlap = len(ours & theirs) / min(len(ours), len(theirs))
                    if overlap < 0.3:
                        add("medium", "cross-source", r,
                            f"id {cid}: {overlap:.0%} title overlap with "
                            f"sources dir")
                        break

    sev_rank = {"high": 0, "medium": 1, "info": 2}
    flags.sort(key=lambda f: (sev_rank[f["severity"]], f["flag"],
                              f["effective_id"]))
    return flags


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--audit", required=True, type=Path)
    ap.add_argument("--guide", required=True, type=Path)
    ap.add_argument("--sources", type=Path, default=None,
                    help="dir of xmltv files for cross-source comparison")
    ap.add_argument("--out", type=Path, default=None,
                    help="write full flag list TSV here")
    ap.add_argument("--summary", action="store_true",
                    help="print per-flag counts + top findings (CI mode)")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 if any high-severity flag found")
    args = ap.parse_args()

    rows = load_audit(args.audit)
    titles = guide_titles_by_cid(args.guide)
    flags = flag_rows(rows, titles, args.sources)

    cols = ["severity", "flag", "effective_id", "display_name",
            "match_tier", "source", "detail"]
    if args.out:
        with args.out.open("w", encoding="utf-8") as f:
            f.write("\t".join(cols) + "\n")
            for fl in flags:
                f.write("\t".join(str(fl[c]) for c in cols) + "\n")
        print(f"wrote {args.out} ({len(flags)} flags)")

    counts: dict[str, int] = defaultdict(int)
    for fl in flags:
        counts[f"{fl['severity']}/{fl['flag']}"] += 1
    n_high = sum(1 for fl in flags if fl["severity"] == "high")

    if args.summary or not args.out:
        print(f"audited {len(rows)} channels "
              f"({sum(1 for r in rows if int(r['real_prog_count'] or 0) > 0)} "
              f"with real EPG) — {len(flags)} flags ({n_high} high)")
        for key in sorted(counts):
            print(f"  {key}: {counts[key]}")
        shown = [fl for fl in flags if fl["severity"] != "info"][:25]
        for fl in shown:
            print(f"  [{fl['severity']}] {fl['flag']}: "
                  f"{fl['display_name']} ({fl['effective_id']}, "
                  f"{fl['match_tier']}) — {fl['detail']}")
        if n_high:
            print("fix wrong bindings via channels/aliases.tsv (pin correct "
                  "upstream) or channels/dummy_override.txt (force blank)")

    if args.strict and n_high:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
