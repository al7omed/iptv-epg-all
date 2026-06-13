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
from build_epg import (  # noqa: E402
    BRAND_TOKENS,
    canon_identity_tokens,
    extract_callsign,
    foreign_tld_donor,
    name_tokens,
)

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
    # bein-override rows are exempt: bein.com is authoritative and
    # legitimately publishes one identical no-events filler block across
    # all XTRA variants.
    by_fp: dict[str, list[dict]] = defaultdict(list)
    for r in real_rows:
        if r["match_tier"] == "bein-override":
            continue
        if r["prog_fp"] and int(r["real_prog_count"]) >= 4:
            by_fp[r["prog_fp"]].append(r)
    def _donor_callsign(donor_cid: str) -> str | None:
        head = re.split(r"[.\-]", donor_cid)[0].upper() if donor_cid else ""
        return head if re.fullmatch(r"[KW][A-Z]{2,4}", head) else None

    for fp, group in by_fp.items():
        if len(group) < 2:
            continue
        toks = [canon_identity_tokens(name_tokens(r["display_name"]))
                for r in group]
        signs = [extract_callsign(r["display_name"]) for r in group]
        # A member whose name-callsign matches its donor's callsign is
        # verified-correct — never flag it ('CBS 3 (KYW)' fed by KYW.us).
        verified = [bool(signs[i]) and signs[i] ==
                    _donor_callsign(group[i]["donor_cid"])
                    for i in range(len(group))]
        conflicted: set[int] = set()
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                if toks[i] == toks[j]:
                    continue  # quality variants — legit shared feed
                if signs[i] and signs[i] == signs[j]:
                    continue  # same US station, different name styles
                if verified[i] and verified[j]:
                    continue
                if any(t.isdigit() or t in BRAND_TOKENS
                       for t in (toks[i] ^ toks[j])):
                    for k in (i, j):
                        if not verified[k]:
                            conflicted.add(k)
        if conflicted:
            names = ", ".join(sorted(group[i]["display_name"]
                                     for i in conflicted)[:4])
            for i in conflicted:
                add("high", "dup-feed", group[i],
                    f"fp={fp} shared by {len(conflicted)} conflicting "
                    f"channels: {names}")

    # --- brand-mismatch: loose tier, donor lacks a brand token we carry ---
    # Substring check against the squashed donor id ('beinsports3.fr' DOES
    # contain 'sports'; token-splitting concatenated ids misses that).
    # UUID-shaped donor ids carry no semantics at all — skip them; the
    # dup-feed and genre checks cover those channels instead.
    _uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-", re.I)
    for r in real_rows:
        if r["match_tier"] not in LOOSE_TIERS or not r["donor_cid"]:
            continue
        if _uuid_re.match(r["donor_cid"]):
            continue
        donor_blob = re.sub(r"[^a-z0-9]", "", r["donor_cid"].lower())
        ch_toks = name_tokens(r["display_name"])
        missing = {t for t in ch_toks
                   if t in BRAND_TOKENS
                   and t.lower() not in donor_blob
                   # SPORT/SPORTS singular-plural equivalence
                   and t.lower().rstrip("s") not in donor_blob}
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


def load_baseline(path: Path | None) -> set[tuple[str, str]]:
    """Accepted/known flags to suppress: TSV of effective_id<TAB>flag.
    Lets the weekly reporter stay quiet on benign same-network pairs and
    only surface NEWLY-appeared suspicious bindings."""
    out: set[tuple[str, str]] = set()
    if not path or not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            out.add((parts[0].strip(), parts[1].strip()))
    return out


def suggest_aliases(flags: list[dict], sources_dir: Path) -> list[dict]:
    """For each high-severity flag, look for a source channel whose brand
    IDENTITY exactly matches the flagged channel and that carries real
    programmes — a higher-confidence donor the user can pin in one line.
    Emits {effective_id, donor_cid, reason}; English-market/MENA donors
    win over foreign-market ones. Pure suggestion: never auto-applied."""
    if not sources_dir or not sources_dir.is_dir():
        return []
    # Build identity index from every source channel that has programmes.
    has_progs: set[str] = set()
    id_index: dict[frozenset, list[str]] = defaultdict(list)
    chan_names: dict[str, str] = {}
    _CHAN_RE = re.compile(
        rb'<channel id="([^"]+)"[^>]*>(.*?)</channel>', re.DOTALL)
    _DN_RE = re.compile(rb'<display-name[^>]*>([^<]*)</display-name>')
    for f in sorted(sources_dir.iterdir()):
        if f.suffix not in (".xml", ".gz"):
            continue
        try:
            raw = read_xml(f)
        except Exception:
            continue
        for m in _PROG_RE.finditer(raw):
            if DUMMY_MARKER in m.group(2):
                continue
            has_progs.add(html.unescape(m.group(1).decode("utf-8", "replace")))
        for m in _CHAN_RE.finditer(raw):
            cid = html.unescape(m.group(1).decode("utf-8", "replace"))
            names = [html.unescape(n.decode("utf-8", "replace"))
                     for n in _DN_RE.findall(m.group(2))]
            # Identity comes from the display name (reliable, quality-
            # stripped). The id is a poor signal — lowercase-concatenated
            # ids like 'beinsports3' tokenize to junk and quality suffixes
            # ('.HD') leak in — so only fall back to it when a channel has
            # no usable display name.
            idents: set[frozenset] = set()
            for n in names:
                ident = canon_identity_tokens(name_tokens(n))
                if len(ident) >= 2:
                    idents.add(ident)
            if not idents:
                ident = canon_identity_tokens(donor_tokens(cid))
                if len(ident) >= 2:
                    idents.add(ident)
            for ident in idents:
                id_index[ident].append(cid)
            if names:
                chan_names[cid] = names[0]

    suggestions: list[dict] = []
    seen: set[str] = set()
    for fl in flags:
        if fl["severity"] != "high" or fl["effective_id"] in seen:
            continue
        want = canon_identity_tokens(name_tokens(fl["display_name"]))
        if len(want) < 2:
            continue
        cands = [c for c in id_index.get(want, [])
                 if c in has_progs and c != fl["donor_cid"]]
        if not cands:
            continue
        # Prefer English-market / MENA donors over foreign-market ones.
        cands.sort(key=lambda c: foreign_tld_donor(c))
        donor = cands[0]
        seen.add(fl["effective_id"])
        suggestions.append({
            "effective_id": fl["effective_id"],
            "display_name": fl["display_name"],
            "donor_cid": donor,
            "donor_name": chan_names.get(donor, ""),
            "reason": f"{fl['flag']} (currently {fl['match_tier']} "
                      f"from {fl['donor_cid'] or fl['source']})",
        })
    return suggestions


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
                    help="exit 1 if any NEW high-severity flag found "
                         "(after baseline suppression)")
    ap.add_argument("--baseline", type=Path, default=None,
                    help="TSV of accepted effective_id<TAB>flag to suppress")
    ap.add_argument("--new-only", action="store_true",
                    help="only report flags absent from --baseline")
    ap.add_argument("--suggest", action="store_true",
                    help="emit suggested aliases.tsv lines for high flags "
                         "(needs --sources)")
    ap.add_argument("--github-summary", type=Path, default=None,
                    help="write a markdown report of NEW high flags here "
                         "(for opening a GitHub issue); empty file if none")
    args = ap.parse_args()

    rows = load_audit(args.audit)
    titles = guide_titles_by_cid(args.guide)
    flags = flag_rows(rows, titles, args.sources)

    baseline = load_baseline(args.baseline)
    for fl in flags:
        fl["is_new"] = (fl["effective_id"], fl["flag"]) not in baseline
    report_flags = [fl for fl in flags if fl["is_new"]] if args.new_only else flags

    cols = ["severity", "flag", "effective_id", "display_name",
            "match_tier", "source", "detail"]
    if args.out:
        with args.out.open("w", encoding="utf-8") as f:
            f.write("\t".join(cols) + "\n")
            for fl in report_flags:
                f.write("\t".join(str(fl[c]) for c in cols) + "\n")
        print(f"wrote {args.out} ({len(report_flags)} flags)")

    counts: dict[str, int] = defaultdict(int)
    for fl in report_flags:
        counts[f"{fl['severity']}/{fl['flag']}"] += 1
    high = [fl for fl in report_flags if fl["severity"] == "high"]
    new_high = [fl for fl in flags if fl["severity"] == "high" and fl["is_new"]]

    if args.summary or not args.out:
        print(f"audited {len(rows)} channels "
              f"({sum(1 for r in rows if int(r['real_prog_count'] or 0) > 0)} "
              f"with real EPG) — {len(report_flags)} flags ({len(high)} high; "
              f"{len(new_high)} new high vs baseline)")
        for key in sorted(counts):
            print(f"  {key}: {counts[key]}")
        shown = [fl for fl in report_flags if fl["severity"] != "info"][:25]
        for fl in shown:
            tag = "NEW " if fl["is_new"] else ""
            print(f"  [{tag}{fl['severity']}] {fl['flag']}: "
                  f"{fl['display_name']} ({fl['effective_id']}, "
                  f"{fl['match_tier']}) — {fl['detail']}")
        if high:
            print("fix wrong bindings via channels/aliases.tsv (pin correct "
                  "upstream) or channels/dummy_override.txt (force blank)")

    suggestions: list[dict] = []
    if args.suggest:
        suggestions = suggest_aliases(flags, args.sources)
        if suggestions:
            print("\n# suggested channels/aliases.tsv pins "
                  "(VERIFY before adding):")
            for s in suggestions:
                print(f"{s['effective_id']}\t{s['donor_cid']}"
                      f"\t# {s['reason']} -> {s['donor_name']}")
        else:
            print("\n# no confident alias suggestions found")

    if args.github_summary is not None:
        _write_github_summary(args.github_summary, new_high, suggestions)

    if args.strict and new_high:
        return 1
    return 0


def _write_github_summary(path: Path, new_high: list[dict],
                          suggestions: list[dict]) -> None:
    """Markdown report of NEW high-severity flags for a GitHub issue.
    Writes an EMPTY file when nothing new — the workflow treats empty as
    'no issue needed'."""
    if not new_high:
        path.write_text("", encoding="utf-8")
        return
    lines = [
        "## EPG audit: new suspicious bindings",
        "",
        f"The weekly audit found **{len(new_high)} new high-severity "
        "binding(s)** not in `channels/.audit_baseline.tsv`. Each is a "
        "channel whose EPG may be wrong. Fix with a one-line pin in "
        "`channels/aliases.tsv` (correct upstream) or "
        "`channels/dummy_override.txt` (force blank), or — if benign — add "
        "`<effective_id><TAB><flag>` to `channels/.audit_baseline.tsv` to "
        "silence it.",
        "",
        "| Channel | flag | tier | detail |",
        "| --- | --- | --- | --- |",
    ]
    sugg_by_id = {s["effective_id"]: s for s in suggestions}
    for fl in new_high[:40]:
        detail = fl["detail"].replace("|", "\\|")[:140]
        lines.append(f"| {fl['display_name']} (`{fl['effective_id']}`) "
                     f"| {fl['flag']} | {fl['match_tier']} | {detail} |")
    if len(new_high) > 40:
        lines.append(f"\n…and {len(new_high) - 40} more (see workflow log).")
    if sugg_by_id:
        lines += ["", "### Suggested pins (verify first)", "```"]
        for fl in new_high:
            s = sugg_by_id.get(fl["effective_id"])
            if s:
                lines.append(f"{s['effective_id']}\t{s['donor_cid']}"
                             f"\t# -> {s['donor_name']}")
        lines.append("```")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
