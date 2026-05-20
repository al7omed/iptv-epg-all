#!/usr/bin/env python3
"""
Build a unified XMLTV EPG by merging epgshare01.online per-region files with the
provider EPG, filtering to channels referenced by the user's M3U playlist.

Inputs (env vars):
  M3U_URL          (required) URL to user's M3U playlist
  PROVIDER_EPG_URL (optional) URL to provider's existing XMLTV (gzipped or raw)

Output:
  docs/guide.xml      uncompressed XMLTV
  docs/guide.xml.gz   gzip-compressed

The matcher uses two strategies:
  1) Direct tvg-id match
  2) Normalized display-name match (handles US callsigns, prefixes, suffixes,
     unicode superscripts)
Plus: every channel from the provider EPG is kept verbatim, since the provider
already curated alias mappings for ~168 channels.
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import gzip
import hashlib
import html
import io
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

# User-local timezone for snapping dummy programme blocks to a clean midnight.
USER_TZ_OFFSET = dt.timedelta(hours=3)  # GMT+3


# ---------------- display-name variants ----------------
# UHF (and many players) normalize the channel name before matching it against
# EPG display-names. We don't know the exact normalization, so we emit several
# variants per channel. The player will match whichever variant fits its rules.

UNICODE_ASCII_MAP = {
    # Unicode "modifier letter" / superscript glyphs -> ASCII equivalents.
    # UHF and similar players appear to normalize these in display, so we
    # produce matching variants. Pictographs (⚽ ◉ ⚾) and other "real" glyphs
    # are intentionally NOT mapped — players preserve them.
    "ᴿᴬᵂ": "RAW", "ʳᵃʷ": "RAW",
    "ᴴᴰ": "HD", "ʰᵈ": "HD",
    "ʰᵉᵛᶜ": "hevc", "ᴴᴱᵛᶜ": "HEVC",
    "ᶠᴴᴰ": "FHD", "ᶠʰᵈ": "FHD",
    "ᵁᴴᴰ": "UHD", "ᵘʰᵈ": "UHD",
    "⁴ᵏ": "4K", "⁴ᴷ": "4K",
    "⁸ᴷ": "8K", "⁸ᵏ": "8K",
    "⁶⁰ᶠᵖˢ": "60fps", "⁶⁰ᶠᴾˢ": "60FPS",
    "ᶠ²": "F2",
    "ᴺᴹ": "NM", "ᴮᴱ": "BE",
    "ᴾ": "P", "ˢ": "S", "ᵖ": "p", "ᵏ": "k",
    "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4",
    "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9",
}

BORDER_DECOR_RE = re.compile(r'^[#*=\-_~\s]+|[#*=\-_~\s]+$')
COLLAPSE_WS_RE = re.compile(r'\s+')
PREFIX_STRIP_RE = re.compile(
    r'^(?:[0-9]{1,2}[KkRr]|UK|US|AR|FR|DE|ES|IT|TR|EN|NM|BE|SS|FM|VIP|NOW|NEW|'
    r'BACKUP|MAIN|F|D|H|S|A|OR|EXYU|GOBX|MBC|OSN|BEIN|ALL|PPV)\s*[:|]\s*',
    re.IGNORECASE,
)


def _ascii_normalize(name: str) -> str:
    for u, a in UNICODE_ASCII_MAP.items():
        name = name.replace(u, a)
    return name


def display_variants(raw: str) -> list[str]:
    """Return a list of display-name variants suitable for emitting under a
    <channel>. Players that normalize the M3U title in various ways can match
    against any of these."""
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    def add(s: str) -> None:
        s = s.strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    add(raw)
    ascii_form = _ascii_normalize(raw)
    add(ascii_form)
    no_border = BORDER_DECOR_RE.sub("", ascii_form).strip()
    add(no_border)
    no_prefix = PREFIX_STRIP_RE.sub("", no_border).strip()
    add(no_prefix)
    add(COLLAPSE_WS_RE.sub(" ", no_prefix))
    # Without the colon (some players strip "X:" delimiter when normalizing)
    add(no_border.replace(":", ""))
    add(no_prefix.replace(":", ""))
    return out


def display_name_block(name: str) -> bytes:
    """Return concatenated <display-name>...</display-name> elements for all variants."""
    parts = []
    for v in display_variants(name):
        parts.append(b"<display-name>" + html.escape(v, quote=True).encode("utf-8") + b"</display-name>")
    return b"".join(parts)

# ---------------- epgshare01 sources ----------------

EPGSHARE_BASE = "https://epgshare01.online/epgshare01"
EPGSHARE_FILES = [
    "US2",
    "US_LOCALS1",
    "US_SPORTS1",
    "UK1",
    "BEIN1",
    "ALJAZEERA1",
    "AE1",
]

# Note: SA1 is stale (last update 2024) so omitted. Add country files as needed
# by editing this list.

# ---------------- name normalization ----------------

# Unicode superscript characters used in the user's M3U (RAW, HD, 60fps, hevc, etc.)
SUPERSCRIPT_CHARS = "ᴿᴬᵂᴴᴰᶠʰᵉᵛᶜᵘᵏ⁴⁶⁰⁸⁵ᵖˢ⁷⁸⁹⁰¹²³"

PREFIX_PATTERN = re.compile(
    r"^\s*(?:UK|US|AR|FR|DE|ES|IT|TR|EN|NL|PT|RU|SE|NO|FI|PL|CA|AU|NZ|IN|ZA|"
    r"MENA|VIP|NOW|NEW|BACK[ -]?UP|MAIN|EXYU|EX-YU|YU|"
    r"GOBX|MBC|OSN|BEIN|ALL|ALL[ -]?PPV|PPV)\s*[:|]+\s*",
    re.IGNORECASE,
)

# Suffixes/qualifiers to strip
SUFFIX_TOKENS_PATTERN = re.compile(
    r"\b("
    r"HD|FHD|UHD|4K|SD|HEVC|H265|H\.?264|RAW|"
    r"BACKUP|BACK[ -]?UP|MULTI[ -]?AUDIO|MULTI[ -]?AUDIO|HQ|LQ|"
    r"PLATINUM|VIP|EVENT|EVENTS|LIVE|PLUS1|\+1|TIMESHIFT|"
    r"60FPS|60[ -]?FPS|MAIN|MIRROR|FEED"
    r")\b",
    re.IGNORECASE,
)

CALLSIGN_PATTERN = re.compile(r"\(([KW][A-Z0-9]{2,5}(?:-(?:DT|LD|LP|CD|CA|TV)\d?)?)\)")
BARE_CALLSIGN_PATTERN = re.compile(r"^([KW][A-Z0-9]{2,5})(?:-(?:DT|LD|LP|CD|CA|TV)\d?)?$")
US_AFFILIATE_PREFIX = re.compile(r"^(?:NBC|FOX|CBS|ABC|CW|PBS|MNT|TELEMUNDO|UNIVISION|MYTV)\s*\d*\s*", re.IGNORECASE)


def _strip_us_callsign_suffix(cs: str) -> str:
    """Strip -DT, -LD, -LP, -CD, -CA, -TV (with optional digit) from a US callsign."""
    return re.sub(r"-(?:DT|LD|LP|CD|CA|TV)\d?$", "", cs)


def normalize_name(s: str) -> str:
    """Aggressive normalization for fuzzy matching. Returns '' for empty/junk."""
    if not s:
        return ""
    # Strip surrounding hash-borders (####### NAME #######)
    s = re.sub(r"^[#*=\-_\s]+|[#*=\-_\s]+$", "", s)
    # Strip unicode superscripts
    s = re.sub(f"[{SUPERSCRIPT_CHARS}]+", "", s)
    # Strip prefixes like "US:", "UK:"
    s = PREFIX_PATTERN.sub("", s)
    # Strip parenthesized qualifiers like (D), (H), (A), (S) at the end
    s = re.sub(r"\(([A-Z]{1,3}(?:\d?))\)\s*$", "", s)
    # Strip suffix tokens
    s = SUFFIX_TOKENS_PATTERN.sub("", s)
    # Collapse
    s = s.upper()
    s = re.sub(r"[^A-Z0-9]+", "", s)
    return s


def extract_callsign(name: str) -> str | None:
    """Extract a US broadcast callsign from a channel name. Handles two forms:
       'NBC 4 (KNBC) LOS ANGELES' -> 'KNBC'
       'KNBC-DT' or 'KNBC'        -> 'KNBC'
    Returns the canonical form (no -DT/-LD/-LP/etc suffix).
    """
    m = CALLSIGN_PATTERN.search(name)
    if m:
        cs = _strip_us_callsign_suffix(m.group(1).upper())
        if 3 <= len(cs) <= 5 and cs[0] in ("K", "W"):
            return cs
    bare = BARE_CALLSIGN_PATTERN.match(name.strip().upper())
    if bare:
        return bare.group(1)
    return None


# ---------------- M3U parsing ----------------

EXTINF_LINE_RE = re.compile(r'#EXTINF[^,\n]*,([^\n]+)')
ATTR_RE = re.compile(r'(\b[\w-]+)="([^"]*)"')


def parse_m3u(text: str):
    """Return list of dicts: tvg_id, tvg_name, group, title, extinf_line, url_line, line_index.

    Order-independent attribute parsing. Captures the original lines so we can
    rewrite the M3U later while preserving everything except tvg-id.
    """
    out = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.startswith("#EXTINF"):
            i += 1
            continue
        m = EXTINF_LINE_RE.match(line)
        title = m.group(1).strip() if m else ""
        comma_idx = line.find(",")
        attr_str = line[: comma_idx if comma_idx > 0 else len(line)]
        attrs = dict(ATTR_RE.findall(attr_str))
        # The following non-EXTINF lines (vlcopt, kodiprop, etc.) plus the URL
        # belong to this entry. Collect them all.
        following = []
        j = i + 1
        while j < len(lines) and lines[j].startswith("#"):
            following.append(lines[j])
            j += 1
        url_line = lines[j] if j < len(lines) else ""
        out.append({
            "tvg_id": attrs.get("tvg-id", "").strip(),
            "tvg_name": attrs.get("tvg-name", "").strip(),
            "group": attrs.get("group-title", "").strip(),
            "title": title,
            "extinf_line": line,
            "extra_lines": following,
            "url_line": url_line,
        })
        i = j + 1
    return out


# ---------------- auto tvg-id ----------------

AUTO_ID_INVALID = re.compile(r"[^a-z0-9]+")
AUTO_ID_BORDER = re.compile(r"^[#*=\-_\s]+|[#*=\-_\s]+$")


def auto_tvg_id(channel: dict) -> str:
    """Generate a stable, URL-safe tvg-id from a channel's name.

    Deterministic per name. Suffix '.auto' marks these as generated (so they
    never collide with the M3U's existing namespace).
    """
    name = channel["tvg_name"] or channel["title"]
    if not name:
        name = channel["title"] or "channel"
    s = AUTO_ID_BORDER.sub("", name).lower()
    s = AUTO_ID_INVALID.sub("-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    if not s:
        s = "ch" + hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
    # Append a 4-char hash for stability across name collisions
    h = hashlib.md5(name.encode("utf-8")).hexdigest()[:4]
    return f"{s}-{h}.auto"


MAX_ID_LEN = 96


def _shorten_id(s: str) -> str:
    """Cap channel ids at MAX_ID_LEN chars. Append a short hash if truncated so
    multiple long ids with the same prefix don't collide."""
    if len(s) <= MAX_ID_LEN:
        return s
    h = hashlib.md5(s.encode("utf-8")).hexdigest()[:8]
    return s[: MAX_ID_LEN - 9].rstrip() + "~" + h


def assign_effective_ids(m3u_channels):
    """Set 'effective_id' on every M3U channel.

    Priority:
      1. M3U's tvg-id, if set
      2. M3U's tvg-name verbatim, if set — many players (Kodi pvr.iptvsimple,
         and apparently UHF) fall back to tvg-name as the EPG lookup key when
         tvg-id is empty. They look for <channel id="<tvg-name>">. So we use
         the M3U title as the EPG channel id.
      3. Auto-generated id from name hash (last-resort).

    Long ids are capped at MAX_ID_LEN to avoid parser issues.
    """
    auto_count = 0
    name_count = 0
    for ch in m3u_channels:
        if ch["tvg_id"]:
            ch["effective_id"] = _shorten_id(ch["tvg_id"])
        elif ch["tvg_name"]:
            ch["effective_id"] = _shorten_id(ch["tvg_name"])
            name_count += 1
        else:
            ch["effective_id"] = _shorten_id(auto_tvg_id(ch))
            auto_count += 1
    return auto_count, name_count


# ---------------- tvg-id map (replacement for M3U republish) ----------------
#
# We do NOT publish the M3U publicly because it contains the user's IPTV
# stream URLs with embedded auth tokens. Instead we publish a CSV mapping of
# (channel-name -> effective tvg-id) which is non-sensitive. The user runs
# scripts/patch_m3u.py locally to inject these tvg-ids into their local M3U.


def write_tvg_id_map(m3u_channels, dest: Path) -> int:
    """Write a CSV: tvg_name|title|tvg_id|effective_id, one row per M3U entry.
    Uses tab separator since channel names contain commas. Returns row count.
    """
    rows = ["tvg_name\ttitle\toriginal_tvg_id\teffective_tvg_id"]
    for ch in m3u_channels:
        rows.append("\t".join([
            ch["tvg_name"].replace("\t", " ").replace("\n", " "),
            ch["title"].replace("\t", " ").replace("\n", " "),
            ch["tvg_id"],
            ch["effective_id"],
        ]))
    dest.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return len(m3u_channels)


_TVG_ID_ATTR_RE = re.compile(r'\s*tvg-id="[^"]*"')


# User-curated category whitelist in display order. Channels in any other
# group-title are dropped from the patched M3U. Comments are the truncated
# names visible in UHF's category grid.
ALLOWED_CATEGORIES_ORDER = [
    # Row 1: beIN Sports MAX + 8K variants + F + SS + ◉
    "AR| BEIN SPORTS MAX ⁸ᴷ ⚽",
    "AR| BEIN SPORTS MAX F ⚽",
    "AR| BEIN SPORTS MAX ᴺᴹ ⚽",
    "AR| BEIN SPORTS ⁸ᴷ & ³⁸⁴⁰ᴾ ⚽",
    "AR| BEIN SPORTS ⁸ᴷ & ᴴᴰ ⚽",
    "AR| BEIN SPORTS ⁸ᴷ & ʰᵉᵛᶜ ⚽",
    "AR| BEIN SPORTS ⁸ᴷ & ᴿᴬᵂ ⚽",
    "AR| BEIN SPORTS ⁸ᴷ & AFC ⚽",
    "AR| BEIN SPORTS F ⚽",
    "AR| BEIN SPORTS F & AFC ⚽",
    "AR| BEIN SPORTS ˢˢ ⚽",
    "AR| BEIN SPORTS ◉ ⚽",
    # Row 2: more beIN variants + 8K Sport + UEFA + Alwan + Arabic Sport + Thmanyah + Shahid PPV + Sports PPV
    "AR| BEIN SPORTS ᵁᴴᴰ ⚽",
    "AR| BEIN SPORTS ᴮᴱ ⚽",
    "AR| BEIN SPORTS ᴺᴹ ⚽",
    "AR| BEIN SPORTS ᴺᴹ & ASIA ⚽",
    "AR| BEIN SPORTS SA ⚽",
    "8K| SPORT ON AIR ⁸ᴷ",
    "AR| UEFA CHAMPIONS LEAGUE ⚽",
    "AR| ALWAN SPORT ᴿᴬᵂ ⚽",
    "AR| ARABIC SPORT 4K ▶ رياضه ⚽️",
    "AR| THMANYAH ⁸ᴷ ⚽",
    "AR| SHAHID PPV ⚽",
    "AR| SPORTS PPV ᴺᴹ ⚽",
    # Row 3: DAZN MENA, MBC, OSN, GOBX, Rotana, Shahid, Cooking, Actors, Bahrain, Discovery, Documentary, US Sport
    "AR| DAZN MENA PPV ⁸ᴷ",
    "AR| MBC 4K",
    "AR| OSN PLATINUM ᴿᴬᵂ",
    "AR| GOBX PLATINUM 4K",
    "AR| ROTANA & ART 4K ▶ روتانا",
    "AR| SHAHID VIP 4K ▶ شاهد الاصلية",
    "AR| WORLD OF COOKING 4K ▶ عالم الطبخ",
    "AR| ️ACTORS 4K ▶ الفنانون",  # note: contains invisible variation selector
    "AR| BAHRAIN 4K ▶ البحرين",
    "AR| DISCOVERY+ ᴬʳᵃᵇᶦᶜ ᴿᴬᵂ ديسكفري",
    "AR| DOCUMENTARY 4K ▶ وثائقي",
    "US| SPORT ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ",
    # Row 4: US PPV/streaming
    "US| ESPN+ PPV ⱽᴵᴾ",
    "US| NBA PPV",
    "US| NBA PASS PPV ⁸ᴷ",
    "US| UFC PPV",
    "US| PPV EVENT ⁽ᴮᴷ⁾",
    "US| PRIME ᴿᴬᵂ ⁶⁰ᶠᵖˢ",
    "US| PARAMOUNT+ ORIGINAL ᴿᴬᵂ ⁶⁰ᶠᵖˢ",
    "US| NETFLIX PPV",
    "US| DAZN PPV",
    "US| ABC ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ",
    "US| CBS ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ",
    "US| CW ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ",
    # Row 5: US broadcast + UK General/Sport
    "US| FOX ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ",
    "US| NBC ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ",
    "US| NEWS ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ",
    "US| SPECTRUM NETWORK ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ",
    "US| ENTERTAINMENT ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ",
    "US| DIREC TV ᴿᴬᵂ ⁶⁰ᶠᵖˢ",
    "US| DIREC TV ᶜᶦᵗʸ ᴿᴬᵂ ⁶⁰ᶠᵖˢ",
    "US| PEACOCK ᴿᴬᵂ ⁶⁰ᶠᵖˢ",
    "US| TUBI ᴿᴬᵂ ⁶⁰ᶠᵖˢ",
    "UK| GENERAL ʰᵉᵛᶜ",
    "UK| SPORT ʰᵉᵛᶜ",
    "UK| SPORT ᴿᴬᵂ ⱽᴵᴾ ᴰᴼᴸᴮʸ ᴬᵁᴰᴵᴼ",
    # Row 6: UK Live Football + TNT + News + ITV + BBC + Prime + Amazon + Documentary + Discovery + Soccer + Sky
    "UK| LIVE FOOTBALL PPV",
    "UK| TNT SPORT ᴿᴬᵂ ⱽᴵᴾ ᴰᴼᴸᴮʸ ᴬᵁᴰᴵᴼ",
    "UK| TNT SPORT EVENT",
    "UK| NEWS ʰᵉᵛᶜ",
    "UK| ITV X VIP",
    "UK| BBC IPLAYER ᴿᴬᵂ",
    "UK| PRIME ᴿᴬᵂ ⁶⁰ᶠᵖˢ",
    "UK| AMAZON PRIME PPV",
    "UK| DOCUMENTARY ʰᵉᵛᶜ",
    "UK| DISCOVERY+ ᴴᴰ/ᴿᴬᵂ",
    "UK| SOCCER REPLAY ᴿᴬᵂ",
    "UK| SKY CINEMA ʰᵉᵛᶜ",
]


def write_patched_m3u(m3u_channels, dest: Path, epg_url: str) -> int:
    """Emit a patched M3U where every entry's tvg-id is set to its effective_id.

    Filtered to ALLOWED_CATEGORIES_ORDER (in that order); entries in other
    groups are dropped. Within a category, original M3U order is preserved.

    SECURITY: this file contains the user's stream URLs with credentials.
    Caller is responsible for placing it at a non-guessable URL path.
    """
    by_cat: dict[str, list] = defaultdict(list)
    for ch in m3u_channels:
        cat = ch.get("group", "")
        by_cat[cat].append(ch)

    out = [f'#EXTM3U x-tvg-url="{epg_url}"']
    written = 0
    seen_cats = []
    for cat in ALLOWED_CATEGORIES_ORDER:
        entries = by_cat.get(cat, [])
        if not entries:
            continue
        seen_cats.append((cat, len(entries)))
        for ch in entries:
            line = ch.get("extinf_line", "")
            if not line:
                continue
            line = _TVG_ID_ATTR_RE.sub("", line)
            eff = ch["effective_id"].replace('"', "'")
            m = re.match(r'(#EXTINF[^\s,]*)\s*(.*?,.*)$', line, re.DOTALL)
            if m:
                head, tail = m.group(1), m.group(2)
                line = f'{head} tvg-id="{eff}" {tail}'
            out.append(line)
            out.extend(ch.get("extra_lines", []))
            if ch.get("url_line"):
                out.append(ch["url_line"])
            written += 1

    print(f"      M3U category filter: kept {written} entries from {len(seen_cats)}/{len(ALLOWED_CATEGORIES_ORDER)} categories")
    for cat, n in seen_cats:
        print(f"        [{n:>3}]  {cat}")
    missing = [c for c in ALLOWED_CATEGORIES_ORDER if c not in {c2 for c2, _ in seen_cats}]
    if missing:
        print(f"      not found in M3U ({len(missing)}):")
        for c in missing:
            print(f"        ?? {c}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(out) + "\n", encoding="utf-8")
    return written


def build_m3u_index(m3u_channels):
    """Build the matching index used to decide whether to keep an upstream channel."""
    tvg_ids = set()
    norm_names = set()
    callsigns = set()
    for ch in m3u_channels:
        if ch["tvg_id"]:
            tvg_ids.add(ch["tvg_id"])
        for name in (ch["tvg_name"], ch["title"]):
            n = normalize_name(name)
            if n and len(n) > 2:
                norm_names.add(n)
            cs = extract_callsign(name)
            if cs:
                callsigns.add(cs)
    return tvg_ids, norm_names, callsigns


# ---------------- upstream EPG handling ----------------

def fetch(url: str, dest: Path):
    """Download with retries."""
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "iptv-epg-builder/1.0"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                with open(dest, "wb") as f:
                    while True:
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
            return dest
        except Exception as e:
            last_err = e
            time.sleep(2 + attempt * 2)
    raise RuntimeError(f"failed to fetch {url}: {last_err}")


def read_xmltv(path: Path) -> bytes:
    """Read possibly gzipped XMLTV from disk, return raw bytes."""
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    return raw


# Streaming XMLTV parser: extract channel and programme elements without
# loading the whole tree into memory. Returns iter of (tag, attrs, inner_xml).
CHANNEL_RE = re.compile(rb"<channel\b[^>]*>.*?</channel>", re.DOTALL)
PROGRAMME_RE = re.compile(rb"<programme\b[^>]*?/>|<programme\b[^>]*?>.*?</programme>", re.DOTALL)
DISPLAY_NAME_RE = re.compile(rb"<display-name[^>]*>([^<]+)</display-name>")
CHANNEL_ID_RE = re.compile(rb'<channel\b[^>]*?\bid="([^"]+)"')
PROG_CHANNEL_RE = re.compile(rb'<programme\b[^>]*?\bchannel="([^"]+)"')


def iter_channels(xml_bytes: bytes):
    """Yield (id, display_names, block) — id is unescaped (raw) form."""
    for m in CHANNEL_RE.finditer(xml_bytes):
        block = m.group(0)
        idm = CHANNEL_ID_RE.search(block)
        if not idm:
            continue
        cid = html.unescape(idm.group(1).decode("utf-8", errors="replace"))
        names = [html.unescape(n.decode("utf-8", errors="replace"))
                 for n in DISPLAY_NAME_RE.findall(block)]
        yield cid, names, block


def iter_programmes(xml_bytes: bytes):
    """Yield (channel_id, block) — channel_id is unescaped (raw) form."""
    for m in PROGRAMME_RE.finditer(xml_bytes):
        block = m.group(0)
        chm = PROG_CHANNEL_RE.search(block)
        if not chm:
            continue
        yield html.unescape(chm.group(1).decode("utf-8", errors="replace")), block


# ---------------- channel matching ----------------

def channel_matches(cid: str, display_names: list[str], tvg_ids: set, norm_names: set, callsigns: set) -> bool:
    if cid in tvg_ids:
        return True
    for n in display_names:
        nn = normalize_name(n)
        if nn and nn in norm_names:
            return True
        cs = extract_callsign(n)
        if cs and cs in callsigns:
            return True
    # channel id may itself be a callsign (e.g. 'KNBC-DT.us_locals1' or 'knbc.us')
    cid_head = cid.split(".")[0].upper()
    if _strip_us_callsign_suffix(cid_head) in callsigns:
        return True
    return False


# ---------------- main build ----------------

def _split_csv(env_value: str) -> list[str]:
    """Comma-separated env value -> list of trimmed non-empty strings."""
    return [s.strip() for s in (env_value or "").split(",") if s.strip()]


def main():
    m3u_urls = _split_csv(os.environ.get("M3U_URL", ""))
    if not m3u_urls:
        print("ERROR: M3U_URL env var required (comma-separated for multiple)", file=sys.stderr)
        return 2
    provider_urls = _split_csv(os.environ.get("PROVIDER_EPG_URL", ""))

    workdir = Path("epg-work")
    workdir.mkdir(exist_ok=True)
    out_dir = Path("docs")
    out_dir.mkdir(exist_ok=True)

    print(f"[1/6] fetching M3U ({len(m3u_urls)} source(s))...")
    m3u_channels = []
    for idx, m3u_url in enumerate(m3u_urls):
        m3u_path = workdir / f"playlist_{idx}.m3u"
        try:
            fetch(m3u_url, m3u_path)
            text = m3u_path.read_text(encoding="utf-8", errors="replace")
            entries = parse_m3u(text)
            print(f"      source[{idx}]: {len(entries)} entries")
            m3u_channels.extend(entries)
        except Exception as e:
            print(f"      FAIL source[{idx}]: {e}")
    print(f"      M3U entries: {len(m3u_channels)} (combined)")
    auto_n, name_n = assign_effective_ids(m3u_channels)
    tvg_id_n = len(m3u_channels) - auto_n - name_n
    print(f"      effective ids assigned: {tvg_id_n} from tvg-id, {name_n} from tvg-name (fallback), {auto_n} auto-generated")
    tvg_ids, norm_names, callsigns = build_m3u_index(m3u_channels)
    # Include auto-generated effective ids in the matcher set so an upstream
    # source with that exact id (rare) still binds.
    tvg_ids |= {ch["effective_id"] for ch in m3u_channels}
    print(f"      index: {len(tvg_ids)} tvg-ids (incl. effective), {len(norm_names)} norm-names, {len(callsigns)} US callsigns")

    print(f"[2/6] fetching upstream EPGs from epgshare01...")
    upstream_paths = []
    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        futs = {}
        for name in EPGSHARE_FILES:
            url = f"{EPGSHARE_BASE}/epg_ripper_{name}.xml.gz"
            dest = workdir / f"{name}.xml.gz"
            futs[pool.submit(fetch, url, dest)] = name
        for fut in cf.as_completed(futs):
            name = futs[fut]
            try:
                p = fut.result()
                upstream_paths.append((name, p))
                print(f"      OK {name}: {p.stat().st_size//1024} KB")
            except Exception as e:
                print(f"      FAIL {name}: {e}")

    print(f"[3/6] fetching provider EPG sources ({len(provider_urls)})...")
    provider_paths = []
    for idx, p_url in enumerate(provider_urls):
        try:
            p = workdir / f"provider_{idx}.xml"
            fetch(p_url, p)
            provider_paths.append((f"provider_{idx}", p))
            print(f"      OK provider[{idx}]: {p.stat().st_size//1024} KB")
        except Exception as e:
            print(f"      FAIL provider[{idx}]: {e}")

    print(f"[4/6] filtering and merging channels...")
    # Output: build channel and programme dicts keyed by channel id.
    # Provider EPGs take priority in the order configured (first wins).
    kept_channels: dict[str, bytes] = {}
    kept_ids: set[str] = set()
    source_stats = {}

    for src_name, p_path in provider_paths:
        raw = read_xmltv(p_path)
        before = len(kept_ids)
        skipped_auto = 0
        for cid, names, block in iter_channels(raw):
            # Skip provider channels whose id ends with .auto — these are stale
            # echoes scraped from our own previously-published guide. They have
            # no programmes and their multi-alias blocks confuse name matching.
            if cid.endswith(".auto"):
                skipped_auto += 1
                continue
            if cid not in kept_ids:
                kept_channels[cid] = block
                kept_ids.add(cid)
        added = len(kept_ids) - before
        source_stats[src_name] = added
        suffix = f" (skipped {skipped_auto} stale .auto echoes)" if skipped_auto else ""
        print(f"      {src_name}: +{added} channels{suffix}")

    for name, path in upstream_paths:
        raw = read_xmltv(path)
        before = len(kept_ids)
        count = 0
        for cid, names, block in iter_channels(raw):
            count += 1
            if cid in kept_ids:
                continue  # already have it from provider
            if channel_matches(cid, names, tvg_ids, norm_names, callsigns):
                kept_channels[cid] = block
                kept_ids.add(cid)
        added = len(kept_ids) - before
        source_stats[name] = added
        print(f"      {name}: scanned={count}, added={added}")

    print(f"      total kept channels: {len(kept_ids)}")

    print(f"[5/6] filtering and merging programmes...")
    kept_programmes: list[bytes] = []
    prog_count_by_source = {}

    for src_name, p_path in provider_paths:
        raw = read_xmltv(p_path)
        n = 0
        for chan_id, block in iter_programmes(raw):
            if chan_id in kept_ids:
                kept_programmes.append(block)
                n += 1
        prog_count_by_source[src_name] = n

    for name, path in upstream_paths:
        raw = read_xmltv(path)
        n = 0
        for chan_id, block in iter_programmes(raw):
            if chan_id in kept_ids:
                kept_programmes.append(block)
                n += 1
        prog_count_by_source[name] = n

    # Dedupe by (channel, start) — provider EPG was added first, so its
    # programmes win when the same slot appears in multiple sources.
    seen_keys = set()
    deduped = []
    for block in kept_programmes:
        m = re.search(rb'<programme\s+start="([^"]+)"[^>]*channel="([^"]+)"', block)
        if m:
            key = (m.group(1), m.group(2))
            if key in seen_keys:
                continue
            seen_keys.add(key)
        deduped.append(block)
    kept_programmes = deduped
    print(f"      total kept programmes (after dedupe): {len(kept_programmes)}")

    # ---------- dummy entries for uncovered tvg-ids ----------
    # Force-dummy list: tvg-ids the user marked as inaccurate. These get their
    # real EPG removed and replaced with dummies.
    override_path = Path("channels/dummy_override.txt")
    forced_ids: set[str] = set()
    if override_path.exists():
        for line in override_path.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                forced_ids.add(line)
    print(f"[5b]  dummy override list: {len(forced_ids)} entries")

    if forced_ids:
        kept_channels = {cid: blk for cid, blk in kept_channels.items() if cid not in forced_ids}
        before = len(kept_programmes)
        kept_programmes = [
            blk for blk in kept_programmes
            if not (m := PROG_CHANNEL_RE.search(blk)) or m.group(1).decode("utf-8", "replace") not in forced_ids
        ]
        removed = before - len(kept_programmes)
        print(f"      removed {removed} programmes from overridden channels")
        kept_ids = set(kept_channels.keys())

    # Build map from effective_id -> M3U display name. Every M3U entry now
    # has an effective_id (original tvg-id or auto-generated).
    m3u_display = {}
    for ch in m3u_channels:
        tid = ch["effective_id"]
        name = ch["tvg_name"] or ch["title"]
        if name and tid not in m3u_display:
            m3u_display[tid] = name

    # ---------- backfill pass ----------
    # An upstream/provider channel often matches an M3U entry via callsign or
    # name, but the player binds by tvg-id strictly. Find those mismatches and
    # clone the upstream's channel+programmes under the M3U's effective_id so
    # real EPG data is actually displayed.
    print(f"[5c]  backfill pass (rewire upstream data to M3U effective ids)")
    backfill_cs: dict[str, str] = {}
    backfill_nn: dict[str, str] = {}
    for cid, block in kept_channels.items():
        names = [n.decode("utf-8", "replace") for n in DISPLAY_NAME_RE.findall(block)]
        for n in names:
            cs = extract_callsign(n)
            if cs:
                backfill_cs.setdefault(cs, cid)
            nn = normalize_name(n)
            if nn and len(nn) > 3:
                backfill_nn.setdefault(nn, cid)
        cid_cs = _strip_us_callsign_suffix(cid.split(".")[0].upper())
        if 3 <= len(cid_cs) <= 5 and cid_cs[0] in ("K", "W"):
            backfill_cs.setdefault(cid_cs, cid)

    progs_by_chan: dict[str, list[bytes]] = {}
    for p in kept_programmes:
        m = PROG_CHANNEL_RE.search(p)
        if m:
            sid = m.group(1).decode("utf-8", "replace")
            progs_by_chan.setdefault(sid, []).append(p)

    def rewrite_prog_channel(block: bytes, old: str, new: str) -> bytes:
        # Both `old` and `new` are raw Python strings. The XML in `block` has
        # `old` written in escaped form. Match the escaped form and replace
        # with the escaped form of `new`.
        old_xml = html.escape(old, quote=True).encode("utf-8")
        new_xml = html.escape(new, quote=True).encode("utf-8")
        return re.sub(
            rb'(<programme\b[^>]*?\bchannel=")' + re.escape(old_xml) + rb'(")',
            lambda m: m.group(1) + new_xml + m.group(2),
            block, count=1,
        )

    DISPLAY_ANY_RE = re.compile(rb'<display-name\b[^>]*>[^<]*</display-name>')

    def clone_channel_for_m3u(src_block: bytes, new_id: str, m3u_display_name: str) -> bytes:
        """Clone a source <channel> block under a new id. The cloned channel
        carries display-name variants of the M3U's own name (so any name-based
        normalization the player applies still matches). Original aliases from
        the source provider are dropped to avoid cross-variant ambiguity."""
        new_id_xml = html.escape(new_id, quote=True).encode("utf-8")
        out = re.sub(
            rb'(<channel\b[^>]*?\bid=")[^"]+(")',
            lambda m: m.group(1) + new_id_xml + m.group(2),
            src_block, count=1,
        )
        out = DISPLAY_ANY_RE.sub(b"", out)
        new_dn = display_name_block(m3u_display_name)
        out = re.sub(
            rb'(<channel\b[^>]*?>)',
            lambda m: m.group(1) + new_dn,
            out, count=1,
        )
        return out

    backfilled = 0
    backfill_progs = 0
    backfill_added_programmes: list[bytes] = []
    for ch in m3u_channels:
        tid = ch["effective_id"]
        if tid in kept_ids or tid in forced_ids:
            continue
        candidate_cid = None
        for nm in (ch["tvg_name"], ch["title"]):
            if not nm:
                continue
            cs = extract_callsign(nm)
            if cs and cs in backfill_cs:
                candidate_cid = backfill_cs[cs]
                break
            nn = normalize_name(nm)
            if nn and nn in backfill_nn:
                candidate_cid = backfill_nn[nn]
                break
        if not candidate_cid:
            continue
        src_block = kept_channels[candidate_cid]
        m3u_name = ch["tvg_name"] or ch["title"] or tid
        new_block = clone_channel_for_m3u(src_block, tid, m3u_name)
        kept_channels[tid] = new_block
        kept_ids.add(tid)
        backfilled += 1
        for src_prog in progs_by_chan.get(candidate_cid, []):
            backfill_added_programmes.append(rewrite_prog_channel(src_prog, candidate_cid, tid))
            backfill_progs += 1
    kept_programmes.extend(backfill_added_programmes)
    print(f"      backfilled {backfilled} M3U ids (+{backfill_progs} cloned programmes)")

    uncovered_ids = (set(m3u_display.keys()) - kept_ids) | forced_ids
    uncovered_ids = {tid for tid in uncovered_ids if tid in m3u_display}
    print(f"      dummy entries to add: {len(uncovered_ids)} (covers every remaining M3U channel)")

    # Generate dummy programme blocks. Many IPTV players (UHF on tvOS, some
    # TiviMate builds) refuse to render programmes longer than ~24h and show
    # "data unavailable" instead. So we emit 4-hour blocks for 8 days = 48
    # blocks per channel. Snapped to GMT+3 hour boundaries.
    BLOCK_HOURS = 4
    DAYS_AHEAD = 5  # was 8 — cut to ease memory pressure on tvOS
    now_utc = dt.datetime.now(dt.timezone.utc)
    local_now = now_utc + USER_TZ_OFFSET
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    series_start_utc = local_midnight - USER_TZ_OFFSET - dt.timedelta(days=1)
    n_blocks = (DAYS_AHEAD * 24) // BLOCK_HOURS

    def fmt_xmltv_time(t: dt.datetime) -> str:
        return t.strftime("%Y%m%d%H%M%S +0000")

    block_times = []
    for i in range(n_blocks):
        s = series_start_utc + dt.timedelta(hours=i * BLOCK_HOURS)
        e = s + dt.timedelta(hours=BLOCK_HOURS)
        block_times.append((fmt_xmltv_time(s), fmt_xmltv_time(e)))

    dummy_count = 0
    dummy_programmes: list[bytes] = []
    for tid in sorted(uncovered_ids):
        tid_xml = html.escape(tid, quote=True)
        dn = display_name_block(m3u_display[tid])
        ch_block = (
            b'<channel id="' + tid_xml.encode("utf-8") + b'">' + dn + b'</channel>'
        )
        # Key by RAW tid (not extracted from escaped XML bytes) to keep
        # kept_ids consistent with the rest of the pipeline.
        kept_channels[tid] = ch_block
        kept_ids.add(tid)
        dummy_count += 1
        for s_str, e_str in block_times:
            p = (
                f'<programme start="{s_str}" stop="{e_str}" channel="{tid_xml}">'
                f'<title lang="en">No EPG</title></programme>'
            ).encode("utf-8")
            dummy_programmes.append(p)

    kept_programmes.extend(dummy_programmes)
    print(f"      added {dummy_count} dummy channels × {n_blocks} blocks = {len(dummy_programmes)} programmes")

    # ---------- gap-fill pass ----------
    # Channels with REAL EPG sometimes have coverage gaps (e.g. provider EPG
    # is missing a programme for "now" but has entries before and after).
    # Many players show "data unavailable" during such gaps. We fill every
    # gap with a "No EPG" dummy programme so the grid stays uniform.
    print(f"[5d]  gap-fill pass")
    PROG_TIMES_RE = re.compile(
        rb'<programme\s+start="(\d{14}[^"]*)"\s+stop="(\d{14}[^"]*)"[^>]*channel="([^"]+)"'
    )
    TIME_PARSE_RE = re.compile(r"(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(?:\s*([+-]\d{4}|Z))?")

    def parse_xmltv(s: str) -> dt.datetime:
        m = TIME_PARSE_RE.match(s)
        if not m:
            return None
        y, mo, d, h, mi, sec = (int(m.group(i)) for i in range(1, 7))
        offset_str = m.group(7) or "+0000"
        t = dt.datetime(y, mo, d, h, mi, sec, tzinfo=dt.timezone.utc)
        if offset_str == "Z":
            return t
        sign = 1 if offset_str[0] == "+" else -1
        oh, om = int(offset_str[1:3]), int(offset_str[3:5])
        return t - sign * dt.timedelta(hours=oh, minutes=om)

    series_stop_utc = series_start_utc + dt.timedelta(days=DAYS_AHEAD)

    ch_progs: dict[str, list] = defaultdict(list)
    for p in kept_programmes:
        m = PROG_TIMES_RE.search(p)
        if not m:
            continue
        start = parse_xmltv(m.group(1).decode())
        stop = parse_xmltv(m.group(2).decode())
        if start is None or stop is None:
            continue
        # html.unescape because channel attr in XML may have &amp;/&#x27; that
        # need decoding to match the raw Python form in kept_ids.
        cid = html.unescape(m.group(3).decode("utf-8", "replace"))
        ch_progs[cid].append((start, stop))

    def gap_blocks(start: dt.datetime, end: dt.datetime, cid_xml: str) -> list[bytes]:
        """Split [start, end) into BLOCK_HOURS-sized chunks of dummy programmes."""
        out = []
        cur = start
        while cur < end:
            nxt = min(cur + dt.timedelta(hours=BLOCK_HOURS), end)
            s_str = fmt_xmltv_time(cur)
            e_str = fmt_xmltv_time(nxt)
            out.append(
                f'<programme start="{s_str}" stop="{e_str}" channel="{cid_xml}">'
                f'<title lang="en">No EPG</title></programme>'.encode("utf-8")
            )
            cur = nxt
        return out

    gap_fill_programmes: list[bytes] = []
    channels_with_gaps = 0
    fully_empty = 0
    # Iterate ALL channels in kept_ids, not just those with programmes — a
    # channel can land in kept_ids via backfill from a provider channel that
    # had zero programmes, leaving the .auto id with no schedule data.
    for cid in kept_ids:
        items = ch_progs.get(cid, [])
        items.sort(key=lambda x: x[0])
        cid_xml = html.escape(cid, quote=True)
        had_gap = False
        cursor = series_start_utc
        for start, stop in items:
            if start > cursor:
                gap_fill_programmes.extend(gap_blocks(cursor, min(start, series_stop_utc), cid_xml))
                had_gap = True
            cursor = max(cursor, stop)
            if cursor >= series_stop_utc:
                break
        if cursor < series_stop_utc:
            gap_fill_programmes.extend(gap_blocks(cursor, series_stop_utc, cid_xml))
            had_gap = True
            if not items:
                fully_empty += 1
        if had_gap:
            channels_with_gaps += 1
    kept_programmes.extend(gap_fill_programmes)
    print(f"      ({fully_empty} channels were fully empty pre-fill)")
    print(f"      filled gaps in {channels_with_gaps} channels (+{len(gap_fill_programmes)} dummy programmes)")

    # ---------- normalize all programme times to UTC ----------
    # Sources publish mixed TZ offsets (+0000, +0200, +0100, -0400). Different
    # timezones with the same wall-clock time look like overlaps in players
    # that compare by raw string. Convert everything to +0000 once so the
    # wall-time IS the UTC time and downstream comparisons are unambiguous.
    print(f"[5d.4] normalize programme timezones to UTC")
    NON_UTC_TIME_RE = re.compile(rb'(start|stop)="(\d{14})\s*([+-])(\d{2})(\d{2})"')

    def _to_utc_str(wall: str, sign: str, oh: int, om: int) -> str:
        y = int(wall[0:4]); mo = int(wall[4:6]); d = int(wall[6:8])
        h = int(wall[8:10]); mi = int(wall[10:12]); sec = int(wall[12:14])
        delta = dt.timedelta(hours=oh, minutes=om)
        if sign == "+":
            t = dt.datetime(y, mo, d, h, mi, sec) - delta
        else:
            t = dt.datetime(y, mo, d, h, mi, sec) + delta
        return t.strftime("%Y%m%d%H%M%S")

    converted = 0
    def _repl(m):
        nonlocal converted
        sign = m.group(3).decode()
        oh = int(m.group(4))
        om = int(m.group(5))
        if oh == 0 and om == 0:
            return m.group(0)
        new_wall = _to_utc_str(m.group(2).decode(), sign, oh, om)
        converted += 1
        return m.group(1) + b'="' + new_wall.encode() + b' +0000"'

    for i, p in enumerate(kept_programmes):
        new_p = NON_UTC_TIME_RE.sub(_repl, p)
        if new_p is not p:
            kept_programmes[i] = new_p
    print(f"      converted {converted} time attributes to UTC")

    # ---------- overlap dedup pass ----------
    # Different upstream sources can publish slightly-shifted programme times
    # for the same channel (e.g. 09:30 vs 09:55). Both pass the (start, channel)
    # dedup. The result confuses players (overlapping cells). Drop programmes
    # that overlap an already-kept earlier programme on the same channel.
    print(f"[5d.5] overlap dedup pass")
    chan_to_programmes: dict[str, list] = defaultdict(list)
    for p in kept_programmes:
        m = PROG_TIMES_RE.search(p)
        if not m:
            continue
        start = parse_xmltv(m.group(1).decode())
        stop = parse_xmltv(m.group(2).decode())
        if start is None or stop is None:
            continue
        cid = html.unescape(m.group(3).decode("utf-8", "replace"))
        chan_to_programmes[cid].append((start, stop, p))

    deduped: list[bytes] = []
    overlap_dropped = 0
    for cid, items in chan_to_programmes.items():
        items.sort(key=lambda x: (x[0], x[1]))
        last_stop = None
        for start, stop, p in items:
            if last_stop is not None and start < last_stop:
                overlap_dropped += 1
                continue
            deduped.append(p)
            last_stop = stop
    kept_programmes = deduped
    print(f"      dropped {overlap_dropped} overlapping programmes; kept {len(kept_programmes)}")

    # ---------- orphan prune pass ----------
    # Provider EPGs contain channels neither subscription's M3U references.
    # They bloat the file with no benefit (the player can't display them).
    # Keep only channels whose id is an M3U effective_id or original tvg-id.
    print(f"[5e]  orphan prune pass")
    m3u_id_set = set()
    for ch in m3u_channels:
        m3u_id_set.add(ch["effective_id"])
        if ch["tvg_id"]:
            m3u_id_set.add(ch["tvg_id"])

    before_ch = len(kept_channels)
    kept_channels = {cid: blk for cid, blk in kept_channels.items() if cid in m3u_id_set}
    kept_ids = set(kept_channels.keys())
    dropped_channels = before_ch - len(kept_channels)

    before_prog = len(kept_programmes)
    new_progs = []
    for p in kept_programmes:
        m = PROG_CHANNEL_RE.search(p)
        if m and html.unescape(m.group(1).decode("utf-8", "replace")) in kept_ids:
            new_progs.append(p)
    kept_programmes = new_progs
    dropped_programmes = before_prog - len(kept_programmes)
    print(f"      dropped {dropped_channels} orphan channels, {dropped_programmes} orphan programmes")
    print(f"      final: {len(kept_channels)} channels, {len(kept_programmes)} programmes")

    print(f"[6/6] writing output...")
    out_xml = out_dir / "guide.xml"
    out_gz = out_dir / "guide.xml.gz"

    header = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<tv generator-info-name="iptv-epg-unified" '
        b'source-info-name="epgshare01.online + provider">\n'
    )
    footer = b"</tv>\n"

    # Full version (gzipped). Lite uncompressed version was dropped — every
    # modern player accepts .gz, and the file would otherwise exceed GitHub
    # Pages' 100 MB per-file limit once dummies are split into many blocks.
    with gzip.open(out_gz, "wb", compresslevel=6) as f:
        f.write(header)
        for cid in sorted(kept_channels):
            f.write(kept_channels[cid])
            f.write(b"\n")
        for p in kept_programmes:
            f.write(p)
            f.write(b"\n")
        f.write(footer)

    # Strip an old guide.xml if it was committed before this change so Pages
    # stops serving stale content.
    if out_xml.exists():
        out_xml.unlink()

    print(f"      wrote {out_gz} ({out_gz.stat().st_size//1024} KB) — full data, gzipped")

    # Publish the non-sensitive tvg-id map. The user uses patch_m3u.py locally
    # to inject these tvg-ids into their private M3U. We do NOT publish the
    # full M3U because it embeds the user's stream auth tokens.
    out_map = out_dir / "tvg-id-map.tsv"
    written = write_tvg_id_map(m3u_channels, out_map)
    print(f"      wrote {out_map} ({out_map.stat().st_size//1024} KB, {written} rows)")

    # OPTIONAL: also write a patched M3U with tvg-ids injected, behind a
    # random URL token (env M3U_PATH_TOKEN). The token doubles as the only
    # access key — anyone with the URL can use the user's IPTV subscription.
    token = os.environ.get("M3U_PATH_TOKEN", "").strip()
    if token:
        pages_base = os.environ.get("PAGES_BASE", "https://al7omed.github.io/iptv-epg-all")
        epg_link = f"{pages_base}/guide.xml.gz"
        m3u_out = out_dir / token / "playlist.m3u"
        n = write_patched_m3u(m3u_channels, m3u_out, epg_link)
        print(f"      wrote patched M3U at {m3u_out} ({m3u_out.stat().st_size//1024} KB, {n} entries)")

    print()
    print("=== source breakdown (channels) ===")
    for src, n in source_stats.items():
        print(f"  {src:15s} {n:>6}")
    print()
    print("=== source breakdown (programmes) ===")
    for src, n in prog_count_by_source.items():
        print(f"  {src:15s} {n:>6}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
