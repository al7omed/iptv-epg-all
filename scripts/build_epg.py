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
    r"GOBX|MBC|OSN|BEIN|ALL|ALL[ -]?PPV|PPV|"
    # Provider-specific source tokens used by the user's subscription
    # (sub2/opop's SS:/BE:/8K:/NM:/FM:/SA: prefixes etc.). Strip so the
    # normalized form matches upstream EPG channels that don't carry the
    # provider prefix.
    r"SS|BE|NM|FM|SA|8K|F|UHD)\s*[:|]+\s*",
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
_TVG_NAME_ATTR_RE = re.compile(r'(\stvg-name=")([^"]*)(")')
_TVG_LOGO_ATTR_RE = re.compile(r'(\stvg-logo=")([^"]*)(")')

# Logo URLs that are obviously broken/placeholder — get stripped from the
# patched M3U so the player falls back to its own default icon instead of
# showing a broken-image glyph.
_BROKEN_LOGO_RE = re.compile(
    r'^\s*$|'                                        # empty
    r'^\s*/+\s*$|'                                    # bare slash
    r'^https?://[^/]+/?$|'                            # bare host with no path
    r'\?ver=0(?:\D|$)|'                               # placeholder version
    r'/null(?:\.\w+)?(?:\?|$)|'                       # /null.png style placeholder
    r'/placeholder(?:\.\w+)?(?:\?|$)',
    re.IGNORECASE,
)


def sanitize_logo(url: str) -> str:
    """Return '' if the logo URL is obviously broken, else the URL unchanged."""
    if not url:
        return ""
    if _BROKEN_LOGO_RE.search(url):
        return ""
    return url


# ---------------- user-managed config files ----------------
# Three optional sidecar files let the user steer the build without code
# changes:
#   channels/favorites_extra.txt   pin extra channels into favorites
#   channels/category_hide.txt     drop categories from output
#   channels/logo_overrides.tsv    per-channel logo URL override
# All are read at build time and survive across runs.

def _load_config_lines(path: Path) -> list[str]:
    """Read a config file. Skip blank/# lines. Return list of stripped lines."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def load_favorites_extra() -> list[re.Pattern]:
    """Compiled regex patterns from channels/favorites_extra.txt.
    Substring-style lines fall back to escaped-literal regex."""
    raw = _load_config_lines(Path("channels/favorites_extra.txt"))
    patterns = []
    for line in raw:
        try:
            patterns.append(re.compile(line, re.IGNORECASE))
        except re.error:
            patterns.append(re.compile(re.escape(line), re.IGNORECASE))
    return patterns


def load_category_hide() -> set[str]:
    """Categories listed in channels/category_hide.txt are skipped on output."""
    return set(_load_config_lines(Path("channels/category_hide.txt")))


def load_logo_overrides() -> list[tuple[re.Pattern, str]]:
    """Parse channels/logo_overrides.tsv. Returns [(pattern, url), ...]."""
    path = Path("channels/logo_overrides.tsv")
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].rstrip("\n")
        if not line.strip():
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        pattern, url = parts[0].strip(), parts[1].strip()
        if not pattern or not url:
            continue
        try:
            out.append((re.compile(pattern, re.IGNORECASE), url))
        except re.error:
            out.append((re.compile(re.escape(pattern), re.IGNORECASE), url))
    return out


def apply_logo_override(name: str, original_url: str,
                         overrides: list[tuple[re.Pattern, str]]) -> str:
    """Return override URL if name matches a configured pattern, else original."""
    for pat, url in overrides:
        if pat.search(name):
            return url
    return original_url


_GROUP_TITLE_ATTR_RE = re.compile(r'(\sgroup-title=")([^"]*)(")')
_TVG_CHNO_ATTR_RE = re.compile(r'\s*tvg-chno="[^"]*"')

# Provider priority (user-set): 8K > NM > FM > BE > SS > UHD > SA.
# Provider source ordering (lower = better, applied as a secondary sort
# after quality_rank). VIP is the provider's flagship subscription tier
# (IPTV community convention — premium feeds live on VIP servers), so it
# leads. Then user-confirmed ordering: 8K > NM > FM > BE > SS > UHD > SA.
PROVIDER_PRIORITY = {
    "VIP": 0,
    "8K": 1, "NM": 2, "FM": 3, "BE": 4, "SS": 5, "UHD": 6, "SA": 7,
}


def extract_source_tag(name: str) -> str:
    """Return the [SRC] trailing tag, or '' if none."""
    m = re.search(r'\[([^\]]+)\]\s*$', name)
    return m.group(1) if m else ""


def extract_language(name: str) -> str:
    if re.search(r'\bArabic\b', name):
        return "Arabic"
    if re.search(r'\bEnglish\b', name):
        return "English"
    return ""


def extract_quality(name: str) -> str:
    base = re.sub(r'\s*\[[^\]]+\]\s*$', '', name)
    for tag in ("8K", "4K", "UHD", "3840p", "FHD", "HD", "HEVC", "RAW"):
        if re.search(r'\b' + re.escape(tag) + r'\b', base, re.IGNORECASE):
            return tag
    return ""


def provider_priority_rank(name: str) -> int:
    src = extract_source_tag(name)
    return PROVIDER_PRIORITY.get(src, 99)


def trim_category_redundancy(cat_name: str, uniform_source: str | None = None,
                              uniform_quality: str | None = None) -> str:
    """Aggressively trim category names for cleanest display:
      - Drop the region word from the suffix when the prefix already says it
        ('Arabic — Discovery+ Arabic RAW' → 'Arabic — Discovery+')
      - Drop trailing quality/codec/frame-rate tokens
      - Drop trailing source provider codes
      - Fix DirecTV casing
    """
    if " — " not in cat_name:
        return cat_name
    region, name = cat_name.split(" — ", 1)

    # Strip duplicate region word that bled into the suffix.
    if region == "Arabic":
        name = re.sub(r'\bArabic\b', '', name, flags=re.IGNORECASE)
    elif region in ("US", "USA"):
        name = re.sub(r'\b(?:USA|US)\b', '', name)
    elif region == "UK":
        name = re.sub(r'\bUK\b', '', name)
    elif region == "8K":
        # 8K — Sport On Air 8K → 8K — Sport On Air
        name = re.sub(r'\b8K\b', '', name)

    # Aggressive trailing-token trim. Run multiple passes for chained tokens.
    # Ordered specific-first so 'HD/RAW 60fps' is caught before 'HD/RAW' alone.
    quality_blobs = [
        r'HD/RAW\s+60fps', r'HD/RAW',
        r'RAW\s+VIP\s+Dolby\s+Audio', r'Dolby\s+Audio',
        r'RAW\s+60fps', r'60fps', r'VIP',
        r'UHD\s+3840p', r'3840p', r'2160p', r'1080p', r'720p',
        r'HEVC', r'RAW', r'4K', r'8K', r'UHD', r'FHD', r'HD',
        r'Original', r'Tk', r'Gold',
    ]
    for _ in range(3):
        for blob in quality_blobs:
            name = re.sub(r'\s+' + blob + r'\s*$', '', name, flags=re.IGNORECASE)

    # Strip trailing provider codes regardless of detected uniformity (safer
    # to over-strip in category name; channel names still preserve [SRC]).
    name = re.sub(r'\s+(?:8K|FM|NM|BE|SS|UHD|SA|F|M)\s*$', '', name, flags=re.IGNORECASE)

    # Strip trailing parenthesized noise + bracket noise.
    name = re.sub(r'\s*\([^)]*\)\s*$', '', name).strip()
    name = re.sub(r'\s*\[[^\]]*\]\s*$', '', name).strip()
    # Strip dangling '&' left over after operand removal (e.g. 'StarzPlay
    # Sport BE &' after stripping the RAW operand). Do NOT strip '+' here
    # because brand names like 'Discovery+', 'Paramount+', 'ESPN+' end in +.
    for _ in range(3):
        name = re.sub(r'\s+&\s*$', '', name).strip()
        name = re.sub(r'\s+(?:8K|FM|NM|BE|SS|UHD|SA|F|M|Tk)\s*$', '', name, flags=re.IGNORECASE)
    # Generic leading/trailing punctuation cleanup. '+' deliberately omitted.
    name = re.sub(r'^[\s:;|,.\-_~&]+|[\s:;|,.\-_~&]+$', '', name).strip()
    # DirecTV brand fix (split into two words by Unicode strip).
    name = re.sub(r'\bDirec\s+Tv\b', 'DirecTV', name, flags=re.IGNORECASE)
    # HBO MAX → HBO Max (max in this context is the streaming brand, not the
    # generic 'MAX' acronym). Same for 'TV+' brand-style fix.
    name = re.sub(r'\bHBO\s+MAX\b', 'HBO Max', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return f"{region} — {name}".strip(' —').strip()


def strip_uniform(name: str, uniform_source: str | None,
                  uniform_lang: str | None, uniform_quality: str | None) -> str:
    """Strip attributes that are uniform across the channel's category from the
    channel's display name. Reduces visual noise where a tag is redundant with
    its category label."""
    s = name
    if uniform_source:
        s = re.sub(r'\s*\[' + re.escape(uniform_source) + r'\]\s*$', '', s)
    if uniform_lang in ("Arabic", "English"):
        s = re.sub(r'\b' + re.escape(uniform_lang) + r'\b', '', s)
    if uniform_quality:
        s = re.sub(r'\b' + re.escape(uniform_quality) + r'\b', '', s, flags=re.IGNORECASE)
    return MULTI_SPACE_RE.sub(' ', s).strip(' -')


# ---- beIN merged-category classifier ----

def classify_to_merged_category(cleaned_name: str) -> str | None:
    """Classify a cleaned beIN channel name into one of the merged buckets,
    or return None to drop the channel entirely.

    Drops:
      * AFC channels — Asian Football Confederation, not relevant to MENA viewers
      * Al-Kass channels — Qatari sport, separate brand from beIN. Provider
        sometimes mixes them into beIN source categories; reject them.
      * Degenerate names — channels whose name is empty or contains no real
        identifier beyond a quality tag (e.g. 'SS 4K', '4K', 'HD').
      * Anything that doesn't actually mention 'beIN'/'bein' — final
        defensive filter so only legitimate beIN feeds enter the bucket.
    """
    n = cleaned_name
    if re.search(r'\bAFC\b', n, re.IGNORECASE):
        return None
    # Al-Kass — Qatari sport brand, not beIN.
    if re.search(r'\b(?:Al[-\s]?Kass|Alkass)\b', n, re.IGNORECASE):
        return None
    # Degenerate names: nothing left after stripping [SRC] suffix and quality
    # tokens. e.g. 'SS 4K' → stripped to nothing meaningful.
    bare = re.sub(r'\s*\[[^\]]+\]\s*$', '', n)
    bare = re.sub(r'\b(?:HEVC|UHD|8K|4K|FHD|HD|RAW|VIP|60FPS|3840P|2160P|1080P|720P|SS|NM|FM|BE|F|M|SA)\b',
                  '', bare, flags=re.I)
    bare = re.sub(r'\s+', ' ', bare).strip(" -:.,;|&+")
    if not bare or len(bare) < 3:
        return None
    # Final defensive filter — must mention beIN to belong in beIN bucket.
    if not re.search(r'\bbeIN\b|\bbein\b', n, re.I):
        return None
    if re.search(r'\bMAX\b', n, re.IGNORECASE):
        return "beIN Sports MAX"
    if re.search(r'\bXTRA\b', n, re.IGNORECASE):
        return "beIN Sports XTRA"
    # Default to main numbered/branded beIN Sports bucket
    return "beIN Sports"


# ---------------- name normalization for display ----------------
# Maps every Unicode modifier letter / superscript / subscript glyph we've
# seen in the provider's data back to its ASCII counterpart. Anything not in
# the map and not [a-zA-Z0-9] gets stripped.

UNICODE_LETTER_MAP = {
    "ᴬ": "A", "ᴮ": "B", "ᴰ": "D", "ᴱ": "E", "ᶠ": "F", "ᴳ": "G",
    "ᴴ": "H", "ᴵ": "I", "ᴶ": "J", "ᴷ": "K", "ᴸ": "L", "ᴹ": "M",
    "ᴺ": "N", "ᴼ": "O", "ᴾ": "P", "ᴿ": "R", "ˢ": "S", "ᵀ": "T",
    "ᵁ": "U", "ⱽ": "V", "ᵂ": "W",
    "ᵃ": "a", "ᵇ": "b", "ᶜ": "c", "ᵈ": "d", "ᵉ": "e",
    "ᵍ": "g", "ʰ": "h", "ᶦ": "i", "ʲ": "j", "ᵏ": "k", "ˡ": "l",
    "ᵐ": "m", "ⁿ": "n", "ᵒ": "o", "ᵖ": "p", "ʳ": "r",
    "ᵗ": "t", "ᵘ": "u", "ᵛ": "v", "ʷ": "w", "ˣ": "x", "ʸ": "y", "ᶻ": "z",
    "ᴬʳᵃᵇᶦᶜ": "Arabic", "ᶜᶦᵗʸ": "City", "ᴰᴼᴸᴮʸ": "Dolby", "ᴬᵁᴰᴵᴼ": "Audio",
    "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4",
    "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9",
    "₀": "0", "₁": "1", "₂": "2", "₃": "3", "₄": "4",
    "₅": "5", "₆": "6", "₇": "7", "₈": "8", "₉": "9",
    "⁽": "(", "⁾": ")",
}

DECORATIVE_CHARS_RE = re.compile(r'[⚽◉▶⎋▼◀●♦★☆▪►⏵⏴️]')
# U+0600..U+06FF Arabic; U+0750..U+077F Arabic Supplement; etc.
ARABIC_BLOCK_RE = re.compile(r'[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]+')
BORDER_CHARS_RE = re.compile(r'^[#*=\-_~\s]+|[#*=\-_~\s]+$')
MULTI_SPACE_RE = re.compile(r'\s+')

# Known brand casings — any word matching these (case-insensitive) is rendered
# with the canonical form regardless of how it appeared in the source.
BRAND_MAP = {
    "BEIN": "beIN", "BBC": "BBC", "ESPN": "ESPN", "ESPN+": "ESPN+",
    "ABC": "ABC", "CBS": "CBS", "NBC": "NBC", "FOX": "FOX", "CW": "CW",
    "TNT": "TNT", "ITV": "ITV", "NBA": "NBA", "NFL": "NFL", "MLB": "MLB",
    "NHL": "NHL", "MILB": "MiLB", "WNBA": "WNBA",
    "NCAAF": "NCAAF", "NCAAB": "NCAAB", "BTN": "BTN", "NFHS": "NFHS",
    "MLS": "MLS", "EPL": "EPL", "SPFL": "SPFL", "PDC": "PDC",
    "UFC": "UFC", "MBC": "MBC", "OSN": "OSN", "DAZN": "DAZN", "TUBI": "Tubi",
    "UEFA": "UEFA", "AFC": "AFC", "VIP": "VIP", "PPV": "PPV",
    "HBO": "HBO", "MGM": "MGM", "TLC": "TLC", "AMC": "AMC", "FX": "FX",
    "TV": "TV", "TV+": "TV+",
    "HEVC": "HEVC", "RAW": "RAW", "HD": "HD", "UHD": "UHD", "FHD": "FHD",
    "SD": "SD", "4K": "4K", "8K": "8K", "60FPS": "60fps",
    "USA": "USA", "UK": "UK", "AR": "Arabic", "EN": "English", "FR": "French",
    "DE": "German", "ES": "Spanish", "IT": "Italian", "TR": "Turkish",
    "ART": "ART", "GOBX": "GOBX", "F2": "F2", "F1": "F1",
    "PARAMOUNT+": "Paramount+", "PARAMOUNT": "Paramount",
    "DISCOVERY+": "Discovery+", "DISCOVERY": "Discovery",
    "NETFLIX": "Netflix", "PEACOCK": "Peacock", "PRIME": "Prime",
    "AMAZON": "Amazon", "SHAHID": "Shahid", "ROTANA": "Rotana",
    "STARZPLAY": "StarzPlay", "STARZ": "Starz",
    "MAX": "MAX", "PASS": "Pass", "ACTORS": "Actors", "ALWAN": "Alwan",
    "ALGERIA": "Algeria", "BAHRAIN": "Bahrain", "JEEM": "Jeem",
    "BARAEM": "Baraem", "ANGHAMI": "Anghami", "THMANYAH": "Thmanyah",
    "MYHD": "MyHD", "TOD": "TOD", "VIDIO": "Vidio", "TODD": "Todd",
    "FM": "FM", "NM": "NM", "BE": "BE", "SS": "SS", "F": "F", "OR": "OR",
    "RK": "RK", "SA": "SA", "TK": "TK", "PLATINUM": "Platinum",
    "SPORTS": "Sports", "SPORT": "Sport", "SOCCER": "Soccer",
    "EVENT": "Event", "FOOTBALL": "Football", "LIVE": "Live",
    "NEWS": "News", "GENERAL": "General", "ENTERTAINMENT": "Entertainment",
    "DOCUMENTARY": "Documentary", "NETWORK": "Network",
    "WORLD": "World", "COOKING": "Cooking", "CINEMA": "Cinema",
    "SPECTRUM": "Spectrum", "SKY": "Sky", "IPLAYER": "iPlayer",
    "DIREC": "Direc", "MENA": "MENA", "SERIES": "Series",
    "AUDIO": "Audio", "DOLBY": "Dolby", "REPLAY": "Replay",
    "CHAMPIONS": "Champions", "LEAGUE": "League", "ORIGINAL": "Original",
    "ULTIMATE": "Ultimate", "ULTRA": "Ultra", "EVENTS": "Events",
    "3840P": "3840p", "ASIA": "Asia", "CITY": "City",
    "WIPEOUT": "Wipeout", "AL-FAJER": "Al-Fajer", "AL-KASS": "Al-Kass",
    "AL-MAJD": "Al-Majd", "ALRABIAA": "Al Rabiaa",
    "TENNIS": "Tennis", "GOLF": "Golf", "FA": "FA", "TT": "TT",
    "F1": "F1", "MXGP": "MXGP", "EU": "EU",
    "HULU": "Hulu", "ROKU": "Roku", "VIA": "Via", "VIAPLAY": "Viaplay",
    "RACES": "Races", "BOARD": "Board", "TEAM": "Team", "STAN": "Stan",
    "MATCHROOM": "Matchroom", "TRILLER": "Triller", "FANATIZ": "Fanatiz",
    "FLORUGBY": "FloRugby", "BRAZIL": "Brazil", "ARGENTINA": "Argentina",
    "MIAMI": "Miami", "TELEMUNDO": "Telemundo", "FLO": "FLO",
    "PREMIER": "Premier", "POOL": "Pool", "RUGBY": "Rugby", "MUSIC": "Music",
    "KIDS": "Kids", "FAMILY": "Family", "MOVIES": "Movies", "REALITY": "Reality",
    "ACTION": "Action", "COMEDY": "Comedy", "CRIME": "Crime", "DRAMA": "Drama",
    "CARTOON": "Cartoon", "CHRISTIAN": "Christian", "QURAN": "Quran",
    "ISLAMIC": "Islamic", "SAUDI": "Saudi", "EMIRATES": "Emirates",
    "KUWAIT": "Kuwait", "QATAR": "Qatar", "OMAN": "Oman", "JORDAN": "Jordan",
    "IRAQ": "Iraq", "EGYPT": "Egypt", "MOROCCO": "Morocco", "LEBANON": "Lebanon",
    "SYRIA": "Syria", "PALESTINE": "Palestine", "YEMEN": "Yemen",
    "LIBYA": "Libya", "SUDAN": "Sudan", "TUNISIA": "Tunisia",
    "WATER": "Water", "POST": "Post", "ON": "On", "AIR": "Air", "AFRICA": "Africa",
    "GLOBAL": "Global", "HOLLYWOOD": "Hollywood", "ADVENTURE": "Adventure",
    "CLASSIC": "Classic", "SHOWS": "Shows", "ONEPLAY": "OnePlay",
    "ROYA": "Roya", "GROUP": "Group", "MAJESTIC": "Majestic",
    "BOX": "Box", "OFFICE": "Office", "CHEF": "Chef", "OMAR": "Omar",
    "WATCH": "Watch", "WEYYAK": "Weyyak", "SHOOF": "Shoof",
    "IT": "It", "ITUNES": "iTunes",
}


def _strip_unicode_glyphs(s: str) -> str:
    """Replace known Unicode modifier letters with ASCII equivalents. Drop
    decorative symbols AND Arabic-script text (the user wants ASCII-only
    display). Collapse remaining whitespace."""
    if not s:
        return ""
    for u, a in UNICODE_LETTER_MAP.items():
        s = s.replace(u, a)
    s = DECORATIVE_CHARS_RE.sub(" ", s)
    s = ARABIC_BLOCK_RE.sub(" ", s)
    s = BORDER_CHARS_RE.sub("", s)
    s = MULTI_SPACE_RE.sub(" ", s).strip()
    return s


def _title_word(w: str) -> str:
    up = w.upper()
    if up in BRAND_MAP:
        return BRAND_MAP[up]
    # Words mixing digits and letters (e.g. F1, MAX1, NBC4) — keep as upper
    if any(c.isdigit() for c in w) and any(c.isalpha() for c in w):
        return up
    return w.capitalize()


_WORD_RE = re.compile(r"[A-Za-z0-9+]+|[^A-Za-z0-9+]+")
_PARENS_CALLSIGN_RE = re.compile(r'\(([KW][a-z][a-z0-9]{1,4}(?:-[a-z]{1,3}\d?)?)\)', re.I)


def _smart_title_case(s: str) -> str:
    result = "".join(_title_word(part) if part.strip() else part for part in _WORD_RE.findall(s))
    # Re-upper any parenthesized callsigns (e.g. (KNBC), (WBAL)) that got
    # title-cased by mistake.
    result = _PARENS_CALLSIGN_RE.sub(lambda m: '(' + m.group(1).upper() + ')', result)
    return result


_SINGLE_LETTER_PARENS_RE = re.compile(r'\s*\([A-Z]{1,2}\d?\)\s*')


def clean_channel_name(raw: str) -> str:
    """Clean an M3U channel name for display. Preserves the source/region
    prefix (8K:, FM:, NM:, BE:, SS:, F:) as a [...] suffix so duplicates
    from different sources stay distinguishable."""
    if not raw:
        return ""
    s = _strip_unicode_glyphs(raw)
    source = None
    m = re.match(r'^([0-9A-Za-z]{1,4})\s*:\s*(.+)$', s)
    if m and 1 <= len(m.group(1)) <= 4:
        source = m.group(1).upper()
        s = m.group(2).strip()
    # Strip single-letter / 2-letter parenthesized provider codes like (H), (D), (TB)
    s = _SINGLE_LETTER_PARENS_RE.sub(" ", s)
    s = re.sub(r'\bSP\s*RTS\b', 'Sports', s, flags=re.I)
    s = _smart_title_case(s)
    s = MULTI_SPACE_RE.sub(" ", s).strip()
    # After decoration stripping, residual leading/trailing punctuation
    # (": " from "◉:" originals, dangling dashes etc.) needs cleanup.
    s = re.sub(r'^[\s:;|,.\-_~]+|[\s:;|,.\-_~]+$', '', s)
    if source:
        s = f"{s} [{source}]"
    return s


REGION_LABEL = {"AR": "Arabic", "US": "US", "UK": "UK", "8K": "8K",
                "F": "France", "DE": "Germany", "ES": "Spain", "IT": "Italy",
                "TR": "Turkey", "GR": "Greece", "PL": "Poland", "NL": "Netherlands",
                "SE": "Sweden", "DK": "Denmark", "NO": "Norway", "FI": "Finland",
                "BR": "Brazil", "CA": "Canada", "MX": "Mexico", "AU": "Australia",
                "VIP": "VIP", "ASIA": "Asia"}


def clean_category_name(raw: str) -> str:
    """Clean an M3U group-title for display. Format: 'Region — Subject'."""
    if not raw:
        return ""
    s = _strip_unicode_glyphs(raw)
    m = re.match(r'^\s*([A-Za-z0-9]{1,4})\s*\|\s*(.+)$', s)
    if m:
        prefix = m.group(1).upper()
        rest = m.group(2).strip()
        region = REGION_LABEL.get(prefix, prefix)
        rest = re.sub(r'\bSP\s*RTS\b', 'Sports', rest, flags=re.I)
        rest = _smart_title_case(rest)
        rest = MULTI_SPACE_RE.sub(" ", rest).strip()
        return f"{region} — {rest}"
    s = re.sub(r'\bSP\s*RTS\b', 'Sports', s, flags=re.I)
    s = _smart_title_case(s)
    return MULTI_SPACE_RE.sub(" ", s).strip()


# Quality scoring. Higher = better. Composite of resolution/codec tier plus
# RAW/VIP/Dolby bonuses.
#
# User preference: RAW + VIP combo is the provider's flagship tier (raw
# source bitrate, no transcoding, premium subscription). Even if a stream
# doesn't carry a resolution label, RAW+VIP is the highest-quality feed
# available. After that, 8K and 4K resolution claims, then HEVC codec,
# then FHD/HD.
_RE_8K     = re.compile(r'\b8K\b', re.I)
_RE_4K     = re.compile(r'\b(?:4K|UHD|2160P|3840P)\b', re.I)
_RE_HEVC   = re.compile(r'\bHEVC\b', re.I)
_RE_FHD    = re.compile(r'\bFHD\b', re.I)
_RE_HD     = re.compile(r'\bHD\b', re.I)
_RE_SD     = re.compile(r'\bSD\b', re.I)
_RE_RAW    = re.compile(r'\bRAW\b', re.I)
_RE_VIP    = re.compile(r'\bVIP\b', re.I)
_RE_DOLBY  = re.compile(r'\bDolby\b', re.I)


def _is_ambiguous_quality_category(cat: str) -> bool:
    """A source category is 'ambiguous' if its label uses '/' (or other
    OR-separator) to combine quality terms — meaning channels inside are
    a MIX of qualities, not all the same.

    Examples:
      'US| SPORT HD/RAW 60fps'         → ambiguous (HD/RAW = HD or RAW)
      'UK| SPORT RAW VIP DOLBY AUDIO'  → unambiguous (all RAW VIP Dolby)
      'AR| BEIN SPORTS 8K & RAW'       → unambiguous (every channel is 8K+RAW)

    When ambiguous, the category's RAW/VIP/Dolby tags are NOT inherited
    by channels — each channel is scored only by its own quality tags.
    """
    s = _strip_unicode_glyphs(cat or "")
    if "/" in s:
        return True
    if re.search(r"\bor\b", s, re.I):
        return True
    return False


def quality_rank(name: str, source_category: str = "") -> int:
    """Composite quality score (higher = better).

    Either RAW or VIP alone is treated as the provider's flagship tier
    — RAW means uncompressed/non-transcoded source passthrough (no
    quality loss from re-encoding), and VIP is the standard IPTV
    community convention for the premium subscription source. Both
    indicate top-tier quality regardless of resolution label.

    Scoring:
      RAW + VIP combo  → 110 (flagship)  + resolution bonus
      RAW alone        → 100
      VIP alone        → 100
      8K               →  95
      4K / UHD         →  85
      HEVC             →  75
      FHD              →  65
      HD               →  55
      SD               →  20
    Additional bonuses (stacked):
      +10 if combined with 8K
      +5  if combined with 4K
      +2  if Dolby Audio

    VIP detection includes bracketed source tags like '[VIP]' since the
    VIP source IS the provider's flagship tier. (Other source codes like
    [SS], [NM], [BE] are just regular providers and don't trigger this.)

    When the channel name doesn't carry quality tags but its SOURCE
    CATEGORY does, the category is used as a fallback.
    """
    # Keep the bracketed tail when scanning so a '[VIP]' source tag is
    # detected. Other bracket contents like '[SS]', '[NM]' don't match VIP.
    n_full = _strip_unicode_glyphs(name)
    src = _strip_unicode_glyphs(source_category or "")

    # If the source category is AMBIGUOUS (e.g. 'HD/RAW' = mix of HD and
    # RAW channels), don't propagate its quality tags. Each channel is
    # scored on its own name only.
    if _is_ambiguous_quality_category(source_category):
        combined = n_full
    else:
        combined = n_full + " " + src

    has_raw   = bool(_RE_RAW.search(combined))
    has_vip   = bool(_RE_VIP.search(combined))
    has_dolby = bool(_RE_DOLBY.search(combined))
    has_8k    = bool(_RE_8K.search(combined))
    has_4k    = bool(_RE_4K.search(combined))

    def _res_bonus() -> int:
        if has_8k:
            return 10
        if has_4k:
            return 5
        return 0

    # Flagship tier: RAW + VIP combo
    if has_raw and has_vip:
        score = 110 + _res_bonus()
        if has_dolby:
            score += 2
        return score

    # RAW alone or VIP alone — either is top tier
    if has_raw or has_vip:
        score = 100 + _res_bonus()
        if has_dolby:
            score += 2
        return score

    # Resolution claims (no RAW or VIP signal)
    if has_8k:
        return 95 + (2 if has_dolby else 0)
    if has_4k:
        return 85 + (2 if has_dolby else 0)

    # Codec/resolution fallbacks
    if _RE_HEVC.search(combined):
        return 75
    if _RE_FHD.search(combined):
        return 65
    if _RE_HD.search(combined):
        return 55
    if _RE_SD.search(combined):
        return 20
    return 0


_NATURAL_SPLIT_RE = re.compile(r'(\d+)')


def natural_key(s: str):
    """Split a string into alternating text/number chunks for natural sort."""
    return [int(p) if p.isdigit() else p.lower() for p in _NATURAL_SPLIT_RE.split(s)]


def language_rank(name: str) -> int:
    """Lower comes first.

    Within beIN categories the convention is:
      * 'Arabic' or no explicit language → Arabic feed (rank 0)
      * 'English' → English commentary feed (rank 1)
    So untagged channels (e.g. 'beIN Sports 1 HD') are sorted with the
    Arabic ones, matching how beIN MENA labels their primary feeds.
    """
    if re.search(r'\bEnglish\b', name):
        return 1
    return 0


# Channels whose tvg-name matches one of these word-boundary tokens are
# dropped — they're explicitly non-English and non-Arabic. Arabic-region
# country names (Algeria, Morocco, Egypt, etc.) are deliberately NOT here.
_EXCLUDE_LANG_TOKENS = [
    # 2/3-letter language/country codes
    "FR", "FRA", "DE", "GER", "ES", "ESP", "IT", "ITA", "TR", "TUR",
    "NL", "NED", "PL", "POL", "SE", "SWE", "NO", "NOR", "DK", "DAN",
    "FI", "FIN", "RU", "RUS", "GR", "GRE", "PT", "POR", "HU", "HUN",
    "RO", "ROM", "CZ", "CZE", "SK", "SVK", "UA", "UKR", "BG", "BUL",
    "HR", "CRO", "RS", "SRB", "BA", "BIH", "SI", "SLO", "MK", "MKD",
    "JP", "JPN", "CN", "CHN", "KR", "KOR", "HE", "HEB", "VN", "VIE",
    "TH", "THA", "MY", "MYS", "PH", "PHL", "ID", "IDN", "TW", "TWN",
    "IL", "ISR", "BR", "BRA",
    # NOT "AR" — that means Arabic in our channel names, must stay.
    "MX", "MEX", "CL", "CHL", "CO", "COL", "VE", "VEN", "BO", "BOL",
    "PE", "PER", "AT", "AUT", "CH", "CHE", "BY", "BLR",
    # Full language/country names
    "FRANCE", "FRENCH", "FRANCAIS", "GERMANY", "GERMAN", "DEUTSCH",
    "SPAIN", "SPANISH", "ESPANA", "ITALY", "ITALIAN", "ITALIA",
    "TURKEY", "TURKISH", "TURKIYE", "NETHERLANDS", "DUTCH",
    "POLAND", "POLISH", "POLSKA", "SWEDEN", "SWEDISH",
    "NORWAY", "NORWEGIAN", "DENMARK", "DANISH", "FINLAND", "FINNISH",
    "RUSSIA", "RUSSIAN", "GREECE", "GREEK", "PORTUGAL", "PORTUGUESE",
    "HUNGARY", "HUNGARIAN", "ROMANIA", "ROMANIAN", "CZECHIA", "CZECH",
    "SLOVAKIA", "SLOVAK", "UKRAINE", "UKRAINIAN", "BULGARIA", "BULGARIAN",
    "CROATIA", "CROATIAN", "SERBIA", "SERBIAN", "BOSNIA", "BOSNIAN",
    "SLOVENIA", "SLOVENIAN", "MACEDONIA", "MACEDONIAN", "ALBANIA",
    "ALBANIAN", "MONTENEGRO", "ESTONIA", "LATVIA", "LITHUANIA",
    "JAPAN", "JAPANESE", "CHINA", "CHINESE", "MANDARIN", "CANTONESE",
    "KOREA", "KOREAN", "HEBREW", "VIETNAM", "VIETNAMESE",
    "THAILAND", "THAI", "MALAYSIA", "MALAY", "INDONESIA", "INDONESIAN",
    "INDIA", "HINDI", "URDU", "PUNJABI", "TAMIL", "TELUGU", "MARATHI",
    "BENGALI", "GUJARATI", "BRAZIL", "BRAZILIAN", "PORTUGUES",
    "ARGENTINA", "MEXICAN", "CHILE", "CHILEAN", "COLOMBIA", "VENEZUELA",
    "PERU", "BOLIVIA", "ECUADOR", "URUGUAY", "PARAGUAY", "AUSTRIA",
    "BELGIUM", "FLEMISH", "WALLOON", "SWISS", "SWITZERLAND", "BELARUS",
    "LATIN", "LATINO", "ESPANOL", "JAPONES", "AUSTRIAN",
    "EXYU", "EX-YU", "YU", "YUGO",
]
_EXCLUDE_LANG_RE = re.compile(
    r'\b(' + "|".join(re.escape(t) for t in _EXCLUDE_LANG_TOKENS) + r')\b',
    re.IGNORECASE,
)


def is_english_or_arabic(name: str) -> bool:
    """Return True unless the channel name has an explicit non-English/
    non-Arabic language or country tag. Arabic countries (Algeria, Egypt,
    Morocco, etc.) and English-speaking countries (US/UK/Canada/Australia)
    pass through. Normalizes Unicode modifiers (ˢᴰ → SD) first so the
    word-boundary patterns hit consistently."""
    if not name:
        return True
    normalized = _strip_unicode_glyphs(name).upper()
    return _EXCLUDE_LANG_RE.search(normalized) is None


_LOW_QUALITY_RE = re.compile(r'\b(SD|LQ|LOW)\b|▼', re.IGNORECASE)


def is_acceptable_quality(name: str) -> bool:
    """Drop channels whose quality marker is SD or LQ. Normalizes Unicode
    modifier letters first (ˢᴰ → SD) so the regex hits regardless of how the
    provider encoded the quality tag."""
    if not name:
        return True
    normalized = _strip_unicode_glyphs(name)
    return _LOW_QUALITY_RE.search(normalized) is None


# User-curated category whitelist in display order.
#
# Order strategy (after user input):
#   1. beIN Sports / MAX / XTRA               (always on top)
#   2. Sports — Major Events PPV              (NFL/MLB/NHL/F1/UEFA/UFC/etc.)
#   3. AR sports + premium                    (UEFA, Thmanyah, DAZN MENA, ...)
#   4. AR entertainment + lifestyle           (MBC, OSN, Shahid, Rotana, ...)
#   5. 8K — Sport On Air                      (catch-all 8K sports)
#   6. UK sports                              (Sport, TNT, Live Football, ...)
#   7. UK premium movies & series             (Sky Cinema, BBC iPlayer, ...)
#   8. UK general / news / streaming services (General, News, ITV X, ...)
#   9. US sports                              (Sport, ESPN+, NBA, UFC, MLS, ...)
#   10. US broadcast networks                 (ABC, CBS, NBC, FOX, CW, ...)
#   11. US streaming networks                 (Prime, HBO Max, Hulu, ...)
#   12. US 24/7 catch-up archives             (Prime/Disney+/PPV/Movies/etc.)
#
# Source categories with the same root name (e.g. 'UK SPORT HEVC' + 'UK SPORT
# RAW VIP DOLBY AUDIO') merge into one display via auto-merge. Major-PPV
# sources are forced into a single 'Sports — Major Events PPV' bucket via
# CATEGORY_REMAP above.
ALLOWED_CATEGORIES_ORDER = [
    # ───────────── 1. beIN (Sports / MAX / XTRA) ─────────────
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
    "AR| BEIN SPORTS ᵁᴴᴰ ⚽",
    "AR| BEIN SPORTS ᴮᴱ ⚽",
    "AR| BEIN SPORTS ᴺᴹ ⚽",
    "AR| BEIN SPORTS ᴺᴹ & ASIA ⚽",
    "AR| BEIN SPORTS SA ⚽",

    # ───────────── 2. Sports — Major Events PPV (consolidated bucket) ─────────────
    # All these sources route via CATEGORY_REMAP to a single
    # 'Sports — Major Events PPV' display bucket. Major sport-event hitters
    # consolidated into one easy-to-find category (NFL, MLB, NHL, F1, UEFA,
    # La Liga, Tennis, Golf, Boxing/Matchroom, UFC, etc.).
    "US| NFL PPV",
    "US| MLB PPV",
    "US| NHL PPV",
    "US| NCAAF PPV",
    "US| NCAAB PPV",
    "US| THE MASTERS PPV",
    "US| APPLE TV F1 PPV",
    "US| MATCHROOM PPV",
    "US| GOLF PPV",
    "US| TENNIS PPV",
    "US| FIFA+ PPV",
    "US| UEFA PPV",
    "UK| FORMULA 1 PPV",
    "UK| UEFA PPV",
    "UK| LA LIGA TEAM PPV",
    "UK| LIGUE 1 PPV",
    "UK| SERIE A TEAM PPV",
    "UK| UFC PPV",
    "UK| PPV EVENT",

    # ───────────── 3. Sports — 8K Live (was '8K Sport On Air') ─────────────
    # Renamed via CATEGORY_REMAP for consistency with the 'Sports — XXX' pattern.
    "8K| SPORT ON AIR ⁸ᴷ",

    # ───────────── 4. Arabic — sports (non-beIN) ─────────────
    "AR| UEFA CHAMPIONS LEAGUE ⚽",
    "AR| ALWAN SPORT ᴿᴬᵂ ⚽",
    "AR| ARABIC SPORT 4K ▶ رياضه ⚽️",
    "AR| THMANYAH ⁸ᴷ ⚽",
    "AR| SHAHID PPV ⚽",
    "AR| SPORTS PPV ᴺᴹ ⚽",

    # ───────────── 5. Arabic — entertainment + lifestyle ─────────────
    "AR| DAZN MENA PPV ⁸ᴷ",
    "AR| MBC 4K",
    "AR| OSN PLATINUM ᴿᴬᵂ",
    "AR| GOBX PLATINUM 4K",
    "AR| ROTANA & ART 4K ▶ روتانا",
    "AR| SHAHID VIP 4K ▶ شاهد الاصلية",
    "AR| WORLD OF COOKING 4K ▶ عالم الطبخ",
    "AR| ️ACTORS 4K ▶ الفنانون",
    "AR| BAHRAIN 4K ▶ البحرين",
    "AR| DISCOVERY+ ᴬʳᵃᵇᶦᶜ ᴿᴬᵂ ديسكفري",
    "AR| DOCUMENTARY 4K ▶ وثائقي",

    # ───────────── 6. UK — sports ─────────────
    "UK| SPORT ʰᵉᵛᶜ",
    "UK| SPORT ᴿᴬᵂ ⱽᴵᴾ ᴰᴼᴸᴮʸ ᴬᵁᴰᴵᴼ",
    "UK| TNT SPORT ᴿᴬᵂ ⱽᴵᴾ ᴰᴼᴸᴮʸ ᴬᵁᴰᴵᴼ",
    "UK| TNT SPORT EVENT",
    "UK| LIVE FOOTBALL PPV",
    "UK| SOCCER REPLAY ᴿᴬᵂ",
    "UK| AMAZON PRIME PPV",

    # ───────────── 7. UK — live channels + cinema ─────────────
    "UK| SKY CINEMA ʰᵉᵛᶜ",
    "UK| BBC IPLAYER ᴿᴬᵂ",
    "UK| ITV X VIP",

    # ───────────── 8. UK — general / news / documentary ─────────────
    "UK| GENERAL ʰᵉᵛᶜ",
    "UK| NEWS ʰᵉᵛᶜ",
    "UK| DOCUMENTARY ʰᵉᵛᶜ",
    "UK| DISCOVERY+ ᴴᴰ/ᴿᴬᵂ",

    # ───────────── 9. US — sports + events ─────────────
    "US| SPORT ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ",
    "US| ESPN+ PPV ⱽᴵᴾ",
    "US| NBA PPV",
    "US| NBA PASS PPV ⁸ᴷ",
    "US| UFC PPV",
    "US| PPV EVENT ⁽ᴮᴷ⁾",
    "US| NETFLIX PPV",
    "US| DAZN PPV",

    # ───────────── 10. US — broadcast / live cable networks ─────────────
    "US| ABC ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ",
    "US| CBS ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ",
    "US| CW ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ",
    "US| FOX ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ",
    "US| NBC ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ",
    "US| NEWS ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ",
    "US| SPECTRUM NETWORK ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ",
    "US| ENTERTAINMENT ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ",
    "US| DIREC TV ᴿᴬᵂ ⁶⁰ᶠᵖˢ",
    "US| DIREC TV ᶜᶦᵗʸ ᴿᴬᵂ ⁶⁰ᶠᵖˢ",
]


# ----------------- category emoji prefixes -----------------
# Tiny visual cue per category — helps scan the grid in UHF.
# Palette is intentionally limited and consistent: same emoji = same content
# type across regions.

CATEGORY_EMOJI = {
    # beIN
    "beIN Sports":                       "⚽",
    "beIN Sports MAX":                   "🏆",
    "beIN Sports XTRA":                  "🌟",
    # Top-level sports buckets
    "Sports — Major Events PPV":         "🏟️",
    "Sports — 8K Live":                  "✨",
    # Arabic — sports
    "Arabic — UEFA Champions League":    "🏆",
    "Arabic — Thmanyah":                 "⚽",
    "Arabic — Sport":                    "⚽",
    "Arabic — Alwan Sport":              "⚽",
    "Arabic — Sports PPV":               "🏟️",
    "Arabic — DAZN MENA PPV":            "🥊",
    "Arabic — Shahid PPV":               "🎬",
    # Arabic — entertainment/lifestyle
    "Arabic — MBC":                      "📺",
    "Arabic — OSN Platinum":             "🎬",
    "Arabic — GOBX Platinum":            "🎬",
    "Arabic — Shahid":                   "🎬",
    "Arabic — Rotana & ART":             "🎵",
    "Arabic — World Of Cooking":         "🍳",
    "Arabic — Actors":                   "🎭",
    "Arabic — Bahrain":                  "🇧🇭",
    "Arabic — Discovery+":               "🌍",
    "Arabic — Documentary":              "🎥",
    # UK
    "UK — Sport":                        "⚽",
    "UK — TNT Sport":                    "⚽",
    "UK — TNT Sport Event":              "🏟️",
    "UK — Live Football PPV":            "⚽",
    "UK — Soccer Replay":                "⚽",
    "UK — Amazon Prime PPV":             "🏟️",
    "UK — Sky Cinema":                   "🎬",
    "UK — BBC iPlayer":                  "📺",
    "UK — ITV X":                        "📺",
    "UK — General":                      "📺",
    "UK — News":                         "📰",
    "UK — Documentary":                  "🎥",
    "UK — Discovery+":                   "🌍",
    # US
    "US — Sport":                        "🏈",
    "US — ESPN+ PPV":                    "🏈",
    "US — NBA PPV":                      "🏀",
    "US — NBA Pass PPV":                 "🏀",
    "US — UFC PPV":                      "🥊",
    "US — PPV Event":                    "🏟️",
    "US — Netflix PPV":                  "🎬",
    "US — DAZN PPV":                     "🥊",
    "US — ABC":                          "📺",
    "US — CBS":                          "📺",
    "US — NBC":                          "📺",
    "US — FOX":                          "📺",
    "US — CW":                           "📺",
    "US — News":                         "📰",
    "US — Spectrum Network":             "📺",
    "US — Entertainment":                "🎭",
    "US — DirecTV":                      "📡",
}


def add_category_emoji(name: str) -> str:
    """Prepend a relevant emoji to the category name for visual scanning.
    If no mapping exists, return the name unchanged."""
    emoji = CATEGORY_EMOJI.get(name)
    if emoji:
        return f"{emoji} {name}"
    return name


# ----------------- category remap & major-PPV consolidation -----------------
# Some source categories should NOT use the auto-trimmed display name and
# instead be forced into a specific bucket. Two reasons:
#   1. Merging similar UI groups (e.g. US DirecTV + DirecTV City → DirecTV).
#   2. Consolidating dozens of major-PPV sports sources into ONE 'Major Events
#      PPV' bucket per user request — keeps the playlist easier to browse.
#
# Key: exact source-category string. Value: display name override.
CATEGORY_REMAP = {
    # DirecTV merge
    "US| DIREC TV ᴿᴬᵂ ⁶⁰ᶠᵖˢ":           "US — DirecTV",
    "US| DIREC TV ᶜᶦᵗʸ ᴿᴬᵂ ⁶⁰ᶠᵖˢ":      "US — DirecTV",
    # 8K Sport On Air renamed to match the 'Sports — XXX' pattern
    "8K| SPORT ON AIR ⁸ᴷ":               "Sports — 8K Live",
    # Major Events PPV consolidation — US side
    "US| NFL PPV":                       "Sports — Major Events PPV",
    "US| NFL REPLAY":                    "Sports — Major Events PPV",
    "US| NFHS PPV":                      "Sports — Major Events PPV",
    "US| NHL PPV":                       "Sports — Major Events PPV",
    "US| NHL TEAM PPV":                  "Sports — Major Events PPV",
    "US| NHL REPLAY":                    "Sports — Major Events PPV",
    "US| VIAPLAY NHL PPV":               "Sports — Major Events PPV",
    "US| MILB PPV":                      "Sports — Major Events PPV",
    "US| MLB PPV":                       "Sports — Major Events PPV",
    "US| MLB TEAM PPV":                  "Sports — Major Events PPV",
    "US| WNBA PPV":                      "Sports — Major Events PPV",
    "US| NBA TEAM PPV":                  "Sports — Major Events PPV",
    "US| NCAAF PPV":                     "Sports — Major Events PPV",
    "US| NCAAB PPV":                     "Sports — Major Events PPV",
    "US| B/R MAX SPORTS PPV":            "Sports — Major Events PPV",
    "US| BTN+ PPV":                      "Sports — Major Events PPV",
    "US| THE MASTERS PPV":               "Sports — Major Events PPV",
    "US| APPLE TV F1 PPV":               "Sports — Major Events PPV",
    "US| MATCHROOM PPV":                 "Sports — Major Events PPV",
    "US| GOLF PPV":                      "Sports — Major Events PPV",
    "US| TENNIS PPV":                    "Sports — Major Events PPV",
    "US| RUGBY PPV":                     "Sports — Major Events PPV",
    "US| SUPERCROSS PPV":                "Sports — Major Events PPV",
    "US| DIRTVISION PPV":                "Sports — Major Events PPV",
    "US| FLO COLLEGE PPV":               "Sports — Major Events PPV",
    "US| FLO RACING PPV":                "Sports — Major Events PPV",
    "US| FLO SPORTS PPV":                "Sports — Major Events PPV",
    "US| FIFA+ PPV":                     "Sports — Major Events PPV",
    "US| UEFA PPV":                      "Sports — Major Events PPV",
    "US| SOCCER PPV":                    "Sports — Major Events PPV",
    "US| BALLY SPORTS PPV":              "Sports — Major Events PPV",
    # Major Events PPV consolidation — UK side
    "UK| FORMULA 1 PPV":                 "Sports — Major Events PPV",
    "UK| APPLE TV F1 PPV":               "Sports — Major Events PPV",
    "UK| UEFA PPV":                      "Sports — Major Events PPV",
    "UK| LA LIGA TEAM PPV":              "Sports — Major Events PPV",
    "UK| LIGUE 1 PPV":                   "Sports — Major Events PPV",
    "UK| SERIE A TEAM PPV":              "Sports — Major Events PPV",
    "UK| CHAMPIONSHIP PPV":              "Sports — Major Events PPV",
    "UK| LEAGUE ONE PPV":                "Sports — Major Events PPV",
    "UK| LEAGUE TWO PPV":                "Sports — Major Events PPV",
    "UK| SCOTTISH CUP PPV":              "Sports — Major Events PPV",
    "UK| SPFL/SCOTTISH PPV":             "Sports — Major Events PPV",
    "UK| SUPER LEAGUE PLUS PPV":         "Sports — Major Events PPV",
    "UK| HUB PREMIER PPV":               "Sports — Major Events PPV",
    "UK| FA PLAYER PPV":                 "Sports — Major Events PPV",
    "UK| EU GAME PASS PPV":              "Sports — Major Events PPV",
    "UK| NATIONAL LEAGUE PPV":           "Sports — Major Events PPV",
    "UK| VIDIO EPL PPV":                 "Sports — Major Events PPV",
    "UK| MXGP PPV":                      "Sports — Major Events PPV",
    "UK| RALLY PPV":                     "Sports — Major Events PPV",
    "UK| TT RACES PPV":                  "Sports — Major Events PPV",
    "UK| ULTIMATE POOL PPV":             "Sports — Major Events PPV",
    "UK| VOLLEY BALL WORLD PPV":         "Sports — Major Events PPV",
    "UK| PDC BOARD PPV":                 "Sports — Major Events PPV",
    "UK| MONO MAX PPV":                  "Sports — Major Events PPV",
    "UK| TRILLER TV PPV":                "Sports — Major Events PPV",
    "UK| FLORUGBY PPV":                  "Sports — Major Events PPV",
    "UK| UFC PPV":                       "Sports — Major Events PPV",
    "UK| NETFLIX PPV":                   "Sports — Major Events PPV",
    "UK| ESPN+ PPV":                     "Sports — Major Events PPV",
    "UK| MAX PPV":                       "Sports — Major Events PPV",
    "UK| HBO MAX PPV":                   "Sports — Major Events PPV",
    "UK| PPV EVENT":                     "Sports — Major Events PPV",
    "UK| PPV EVENT ⁽ᴮᴷ⁾":                "Sports — Major Events PPV",
}


# ----------------- favorites M3U -----------------
# A curated subset playlist: one of each beIN feed at top quality, the strongest
# documentary lineup, plus must-have news/movies/kids. All channels here go
# through the same language/quality/dead filters as the main M3U, then are
# de-duped by canonical name (best quality wins) and grouped into clean
# Favorites buckets.

_CANONICAL_STRIP_RE = re.compile(
    r'\s*\[[^\]]+\]\s*|'                            # [SS], [NM] etc.
    r'\b(?:HEVC|UHD|8K|4K|FHD|HD|RAW|VIP|60FPS|'
    r'ORIGINAL|DOLBY\s+AUDIO|3840P|2160P|1080P|720P|'
    r'2K|FM|NM|BE|SS|SA|F)\b',
    re.IGNORECASE,
)

# Leading-prefix words that aren't part of the channel name itself —
# they're region/group markers some providers prepend. Stripped from the
# canonical key so 'Hub beIN Sports 1' dedupes against 'beIN Sports 1'.
_CANONICAL_PREFIX_RE = re.compile(
    r'^(?:hub|sa|us|uk|ar|me|mena|asia|asian|global)\s+', re.IGNORECASE,
)
_CANONICAL_LEAD_ZERO_RE = re.compile(r'\b0(\d)\b')


def canonical_channel_name(name: str) -> str:
    """Canonical key for de-duplicating the same logical channel across
    sources/qualities/regions.

      * '[SS]', '[NM]' etc.        → stripped
      * 'HEVC', '4K', '8K', etc.   → stripped
      * 'Hub beIN Sports 1'        → 'bein sports 1'  (Hub prefix stripped)
      * 'beIN Sports 01'           → 'bein sports 1'  (zero-padded number)
      * '(Event Only)' / '(East)'  → KEPT (meaningful distinction)
    """
    s = _CANONICAL_STRIP_RE.sub(' ', name)
    s = _CANONICAL_PREFIX_RE.sub('', s)
    s = _CANONICAL_LEAD_ZERO_RE.sub(r'\1', s)
    # Strip orphan '&' left behind after stripping the operands around it
    # (e.g. 'Sky Sports F1 4K & 3840p' → 'Sky Sports F1  &  ' → 'sky sports f1').
    s = re.sub(r'\s+&\s*$', '', s)
    s = re.sub(r'\s+&\s+', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip(' -:.,;|&').lower()
    return s


# Favorite-bucket classifier. Returns the display group title for the favorites
# M3U, or None if the channel isn't a favorite.
#
# Brand patterns use word boundaries so 'BBC One' doesn't catch 'CBBC One' and
# 'AMC HD' doesn't need a trailing space. Order matters: more-specific brands
# first (catch 'beIN' before generic 'sports').

# Substring lookups (lowercase). Used for multi-word brand names that don't
# need precise boundary matching.
_FAV_DOC_SUBSTR = (
    'national geographic', 'nat geo', 'natgeo',
    'discovery channel', 'discovery science', 'discovery turbo',
    'discovery+', 'investigation discovery', 'animal planet',
    'history channel', 'bbc earth', 'smithsonian',
    'science channel', 'curiosity stream', 'travel channel',
    'crime+investigation', 'crime + investigation', 'crime investigation',
    'motortrend', 'love nature', 'love history', 'love documentary',
)
_FAV_NEWS_SUBSTR = (
    'bbc news', 'bbc world', 'sky news', 'cnn international',
    'al jazeera', 'aljazeera', 'al arabiya', 'alarabiya',
    'france 24', 'france24', 'dw news', 'dw english',
    'euronews', 'cnbc world', 'fox news',
)
_FAV_MOVIE_SUBSTR = (
    'sky cinema', 'sky movies', 'sky atlantic',
    'max original', 'max hits',
    'paramount+', 'paramount plus',
    'osn first', 'osn movies', 'osn rotana', 'osn ya hala',
    'mbc max', 'mbc drama', 'mbc action', 'mbc bollywood', 'mbc 4',
)
_FAV_KIDS_SUBSTR = (
    'disney channel', 'disney jr', 'disney junior', 'disney xd',
    'cartoon network', 'boomerang', 'nickelodeon', 'nick jr',
    'baby tv', 'cbeebies', 'cbbc',
)
# General Sports: ONLY the national flagships, not regional affiliates.
# 'FOX Sports Arizona/Carolinas/Detroit/...' get rejected via the
# regional-name blocklist below.
_FAV_SPORT_SUBSTR = (
    'sky sports main', 'sky sports premier', 'sky sports football',
    'sky sports f1', 'sky sports cricket', 'sky sports news',
    'sky sports racing', 'sky sports golf', 'sky sports arena',
    'sky sports action', 'sky sports mix',
    'tnt sports',
)
# Tight word-boundary patterns for flagship-only matches.
_FAV_SPORT_WORD_RE = re.compile(
    r'\b(?:espn(?:\s*[u23]|\s*news|\s*usa)?|fox\s*sports\s*[12]|'
    r'fs1|fs2|nbc\s*sports\s*(?:network|nbc)|nbcsn)\b',
    re.IGNORECASE,
)
# Drop FOX Sports / Bally Sports regionals — Arizona, Carolinas, Detroit, etc.
_FAV_SPORT_REGIONAL_RE = re.compile(
    r'\b(?:arizona|carolinas?|college|detroit|florida|kansas|'
    r'midwest|mid\s*west|netbase|north|ohio|oklahoma|pacific|'
    r'pittsburgh|prime\s*ticket|san\s*diego|south|southeast|southwest|'
    r'sun|tennessee|texas|utah|west|wisconsin|atlantic|central)\b',
    re.IGNORECASE,
)
# Word-boundary brand regexes for single-word brands prone to false matches.
_FAV_DOC_WORD_RE = re.compile(r'\b(?:discovery|documentary|tlc|h2|pbs|history)\b', re.I)
_FAV_NEWS_WORD_RE = re.compile(r'\b(?:cnn|bloomberg|msnbc|cnbc)\b', re.I)
_FAV_MOVIE_WORD_RE = re.compile(r'\b(?:hbo|max|showtime|cinemax|starz|amc|fx|fxx|mgm\+?)\b', re.I)


def classify_favorite(display: str) -> str | None:
    d = display.lower()
    # 1. beIN — every beIN feed is a favorite (Sports / Movies / News if any)
    if 'bein' in d:
        return "Favorites — beIN"
    # 2. Documentaries
    if any(b in d for b in _FAV_DOC_SUBSTR):
        return "Favorites — Documentaries"
    if _FAV_DOC_WORD_RE.search(d) and 'discovery+' not in d:
        return "Favorites — Documentaries"
    # 3. News
    if any(n in d for n in _FAV_NEWS_SUBSTR):
        return "Favorites — News"
    if _FAV_NEWS_WORD_RE.search(d):
        return "Favorites — News"
    # 4. Movies & premium series. Check substrings first (Sky Cinema etc),
    # then word-boundary single-word brands.
    if any(m in d for m in _FAV_MOVIE_SUBSTR):
        return "Favorites — Movies & Series"
    if _FAV_MOVIE_WORD_RE.search(d):
        return "Favorites — Movies & Series"
    # 5. Kids
    if any(k in d for k in _FAV_KIDS_SUBSTR):
        return "Favorites — Kids"
    # 6. General sports (national flagships only). Reject regional FOX/Bally
    # affiliates so we don't pull in 'FOX Sports Detroit', 'FOX Sports
    # Carolinas', etc.
    if _FAV_SPORT_REGIONAL_RE.search(d):
        return None
    if any(s in d for s in _FAV_SPORT_SUBSTR):
        return "Favorites — General Sports"
    if _FAV_SPORT_WORD_RE.search(d):
        return "Favorites — General Sports"
    return None


FAVORITES_SECTION_ORDER = [
    "Favorites — Pinned",          # user-pinned via channels/favorites_extra.txt
    "Favorites — Tonight",         # primetime live PPV (16:00-23:00 GMT+3)
    "Favorites — beIN",
    "Favorites — Documentaries",
    "Favorites — News",
    "Favorites — Movies & Series",
    "Favorites — General Sports",
    "Favorites — Kids",
]


def write_favorites_m3u(m3u_channels, dest: Path, epg_url: str,
                          live_now_ids: set | None = None) -> int:
    """Emit a curated favorites playlist:
      * One entry per logical beIN feed (highest quality, best provider)
      * Top documentary networks (Nat Geo, Discovery, History, BBC Earth, ...)
      * Top news (BBC News, Sky News, CNN, Al Jazeera, France 24, ...)
      * Premium movies/series (Sky Cinema, HBO/Max, Showtime, ...)
      * Premium sports (ESPN, Fox Sports, Sky Sports flagships)
      * Kids (Disney, Cartoon Network, Nickelodeon)
    All channels go through the same filters as the main M3U.
    """
    # Same blacklist as main M3U.
    bl_path = Path("channels/dead_channels.txt")
    dead_ids: set[str] = set()
    if bl_path.exists():
        for line in bl_path.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                dead_ids.add(line)

    allowed = set(ALLOWED_CATEGORIES_ORDER)
    extra_patterns = load_favorites_extra()
    logo_overrides = load_logo_overrides()
    if extra_patterns:
        print(f"      favorites_extra.txt: {len(extra_patterns)} extra patterns loaded")

    # Primetime detection: build runs hourly, so the favorites M3U dynamically
    # gains a 'Favorites — Tonight' section at the top during 16:00–23:00
    # local time (GMT+3). Contains live PPV / sport channels currently airing.
    local_now = dt.datetime.now(dt.timezone.utc) + USER_TZ_OFFSET
    is_primetime = 16 <= local_now.hour <= 22  # inclusive of 22:xx, exclusive 23:00
    live_set = live_now_ids or set()
    live_set_str = {
        (i.decode("utf-8", "replace") if isinstance(i, (bytes, bytearray)) else i)
        for i in live_set
    }
    primetime_patterns = (
        # Categories considered "primetime worthy" — sport-event flagships.
        # A channel is auto-pinned into Tonight if its display name OR its
        # source category matches AND it has a current EPG programme.
        re.compile(r'\b(?:PPV|Event\s*Only|Live\s+Event|Match)\b', re.I),
        re.compile(r'beIN\s+Sports', re.I),
        re.compile(r'\bUEFA\b|\bChampions\s+League\b', re.I),
        re.compile(r'\bTNT\s+Sport|Sky\s+Sport(?:s)?\b', re.I),
        re.compile(r'\bNFL\b|\bMLB\b|\bNHL\b|\bUFC\b|\bF1\b|\bFormula\s+1\b', re.I),
    )
    primetime_categories = {
        "Sports — Major Events PPV", "Sports — 8K Live",
        "UK — Sport", "UK — TNT Sport", "UK — Live Football PPV",
        "US — Sport", "US — ESPN+ PPV", "US — UFC PPV", "US — NBA PPV",
        "Arabic — UEFA Champions League", "Arabic — Sports PPV",
    }
    if is_primetime:
        print(f"      primetime favorites ACTIVE (local hour {local_now.hour:02d}:xx)")

    # bucket[(favorites_section, canonical_name)] = list of (rank_tuple, display, ch)
    # We'll then keep the best per canonical key inside each section.
    candidates: "dict[tuple[str, str], list]" = defaultdict(list)
    for ch in m3u_channels:
        cat = ch.get("group", "")
        if cat not in allowed:
            continue
        raw = ch.get("tvg_name") or ch.get("title") or ""
        if not is_english_or_arabic(raw):
            continue
        if not is_acceptable_quality(raw):
            continue
        if ch.get("effective_id") in dead_ids:
            continue
        display = clean_channel_name(raw)
        # Drop AFC channels — user removed this category entirely.
        if re.search(r'\bAFC\b', display, re.I):
            continue
        # Check classifier first, then favorites_extra.txt as a manual pin.
        section = classify_favorite(display)
        if section is None:
            # User-managed pin: any favorites_extra pattern that matches the
            # display name routes the channel into 'Favorites — Pinned'.
            if any(p.search(display) for p in extra_patterns):
                section = "Favorites — Pinned"
        # Primetime override: live PPV / Event-Only channels airing now get
        # pinned to 'Favorites — Tonight'. Strict criteria to avoid bloat:
        #   1. We must be in primetime (16:00–22:59 GMT+3)
        #   2. The channel must have a current EPG programme (live_set_str)
        #   3. The channel name OR source category must explicitly say PPV /
        #      Event Only / Live Event — generic sport channels don't qualify.
        if is_primetime and ch.get("effective_id") in live_set_str:
            name_has_ppv = bool(_PPV_PIN_RE.search(display))
            cat_has_ppv = bool(re.search(r'\b(?:PPV|EVENT[\s_-]?ONLY|LIVE\s+EVENT)\b',
                                          cat, re.I))
            # Special-case: Sports — Major Events PPV bucket is always Tonight-worthy
            cat_is_major = "MAJOR EVENTS PPV" in cat.upper() or cat in (
                "Sports — Major Events PPV",
            )
            if name_has_ppv or cat_has_ppv or cat_is_major:
                tonight_canon = canonical_channel_name(display)
                if tonight_canon:
                    q_t = -quality_rank(display, source_category=cat)
                    prov_t = provider_priority_rank(display)
                    rank_t = (q_t, prov_t, natural_key(display))
                    candidates[("Favorites — Tonight", tonight_canon)].append(
                        (rank_t, display, ch)
                    )
        if not section:
            continue
        canon = canonical_channel_name(display)
        if not canon:
            continue
        # Pass the source category so RAW/VIP/Dolby tags carried at the
        # category level (e.g. 'UK| SPORT RAW VIP DOLBY AUDIO') still feed
        # quality_rank when the channel name itself lost those tags after
        # cleanup.
        q = -quality_rank(display, source_category=cat)
        prov = provider_priority_rank(display)
        rank = (q, prov, natural_key(display))
        candidates[(section, canon)].append((rank, display, ch))

    # Pick the single best entry per (section, canonical name).
    best: "dict[str, list[tuple[list, str, dict]]]" = defaultdict(list)
    for (section, canon), items in candidates.items():
        items.sort(key=lambda x: x[0])
        rank, display, ch = items[0]
        best[section].append((rank, display, ch))

    # Emit in the curated section order, sorted within each section.
    out = [f'#EXTM3U x-tvg-url="{epg_url}"']
    written = 0
    section_log: list[tuple[str, int]] = []
    # Per-section cap so 'Tonight' (live PPV) can't bloat to hundreds.
    SECTION_CAP = {
        "Favorites — Tonight": 30,
        "Favorites — Pinned": 50,
    }
    for section in FAVORITES_SECTION_ORDER:
        items = best.get(section, [])
        if not items:
            continue
        items.sort(key=lambda x: x[0])
        cap = SECTION_CAP.get(section)
        if cap and len(items) > cap:
            items = items[:cap]
        chno = 1
        for _, display, ch in items:
            line = ch.get("extinf_line", "")
            if not line:
                continue
            line = _TVG_ID_ATTR_RE.sub("", line)
            line = _TVG_CHNO_ATTR_RE.sub("", line)
            line = _TVG_NAME_ATTR_RE.sub(
                lambda m: m.group(1) + display.replace('"', "'") + m.group(3), line,
            )
            line = _GROUP_TITLE_ATTR_RE.sub(
                lambda m: m.group(1) + section.replace('"', "'") + m.group(3), line,
            )
            # Strip obviously-broken logo URLs, then apply per-channel override.
            def _logo_repl_fav(m: re.Match) -> str:
                cleaned = sanitize_logo(m.group(2))
                cleaned = apply_logo_override(display, cleaned, logo_overrides)
                return m.group(1) + cleaned + m.group(3)
            line = _TVG_LOGO_ATTR_RE.sub(_logo_repl_fav, line)
            comma_idx = line.find(",")
            if comma_idx > 0:
                line = line[: comma_idx + 1] + display
            eff = ch["effective_id"].replace('"', "'")
            m = re.match(r'(#EXTINF[^\s,]*)\s*(.*?,.*)$', line, re.DOTALL)
            if m:
                head, tail = m.group(1), m.group(2)
                line = f'{head} tvg-id="{eff}" tvg-chno="{chno}" {tail}'
            out.append(line)
            out.extend(ch.get("extra_lines", []))
            if ch.get("url_line"):
                out.append(ch["url_line"])
            written += 1
            chno += 1
        section_log.append((section, len(items)))
    print(f"      Favorites M3U: {written} entries across {len(section_log)} sections")
    for s, n in section_log:
        print(f"        [{n:>3}]  {s}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(out) + "\n", encoding="utf-8")
    return written


# Channel name patterns that mark a PPV / event-driven channel. When such a
# channel has a current programme in the EPG (live_now_ids set), it gets
# pinned to the top of its category.
_PPV_PIN_RE = re.compile(
    r'\b(?:PPV|Event\s*Only|Event-Only|Live\s+Event)\b|'
    r'\(Event\s*Only\)|\(Live\)|\(PPV\)',
    re.IGNORECASE,
)


def write_patched_m3u(m3u_channels, dest: Path, epg_url: str,
                       live_now_ids: set | None = None):
    """Returns (count, set_of_effective_ids_used).

    live_now_ids: set of channel ids (bytes or str) that currently have an
    airing programme with a non-blank title. PPV/Event-pattern channels that
    are in this set get pinned to #1 of their category.
    """
    live_set = live_now_ids or set()
    # Normalize all to str for comparison against ch['effective_id']
    live_set_str = {
        (i.decode('utf-8', 'replace') if isinstance(i, (bytes, bytearray)) else i)
        for i in live_set
    }
    """Emit a patched M3U where every entry's tvg-id is set to its effective_id.

    Filtered to ALLOWED_CATEGORIES_ORDER, with beIN categories collapsed into
    4 merged buckets (MAX / Numbered / XTRA / AFC). Channels sorted by quality
    desc → language (Arabic first in beIN) → provider priority → natural alpha.
    Names are normalized; uniform-across-category source/quality/language tags
    are auto-stripped from channel names. tvg-chno added per category.

    SECURITY: contains stream URLs with credentials.
    """
    # Load optional user-managed config files.
    category_hide = load_category_hide()
    logo_overrides = load_logo_overrides()
    if category_hide:
        print(f"      category_hide.txt: {len(category_hide)} categories will be skipped")
    if logo_overrides:
        print(f"      logo_overrides.tsv: {len(logo_overrides)} per-channel overrides loaded")
    # Load health-check blacklist if present.
    bl_path = Path("channels/dead_channels.txt")
    dead_ids: set[str] = set()
    if bl_path.exists():
        for line in bl_path.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                dead_ids.add(line)

    # Failover URL map: canonical channel name → ordered list of distinct
    # stream URLs (best first). After URL dedup below collapses duplicates
    # per URL, channels with the same logical name but different URLs still
    # remain in the playlist; this map lets the emit pass attach backup-URL
    # hints to each entry so players that support failover can switch on
    # primary-stream failure. Built from the PRE-dedup channel list.
    failover_map: "dict[str, list[str]]" = defaultdict(list)
    for ch in m3u_channels:
        url = ch.get("url_line", "")
        if not url:
            continue
        raw = ch.get("tvg_name") or ch.get("title") or ""
        if not is_english_or_arabic(raw) or not is_acceptable_quality(raw):
            continue
        canon = canonical_channel_name(clean_channel_name(raw))
        if canon and url not in failover_map[canon]:
            failover_map[canon].append(url)

    # URL dedup: collapse channels with the same stream URL to a single
    # channel (highest quality_rank wins). Providers sometimes expose the
    # same stream under multiple group-titles or display names. Keeping only
    # the best per URL prevents the same channel showing up two or three
    # times in the playlist.
    url_best: "dict[str, tuple[int, dict]]" = {}
    no_url: list = []
    for ch in m3u_channels:
        url = ch.get("url_line", "")
        if not url:
            no_url.append(ch)
            continue
        raw = ch.get("tvg_name") or ch.get("title") or ""
        score = quality_rank(raw, source_category=ch.get("group", ""))
        cur = url_best.get(url)
        if cur is None or score > cur[0]:
            url_best[url] = (score, ch)
    m3u_channels = [ch for _, ch in url_best.values()] + no_url
    url_dedup_dropped = sum(1 for ch in m3u_channels if ch is None)  # 0 by construction
    print(f"      M3U URL dedup: kept {len(url_best)} unique URLs from {len(url_best)+0} entries (+ {len(no_url)} without URL)")

    by_cat: dict[str, list] = defaultdict(list)
    for ch in m3u_channels:
        cat = ch.get("group", "")
        by_cat[cat].append(ch)

    # First pass: filter + clean + bucket into NEW (possibly merged) categories.
    # The new category for an entry = beIN-merged name if it lives in a beIN
    # source category, else its cleaned original.
    out = [f'#EXTM3U x-tvg-url="{epg_url}"']
    written = 0
    used_ids: set = set()  # effective_ids of channels actually emitted
    # Per-region used_ids for region-specific EPG outputs:
    used_ids_by_region: "dict[str, set]" = defaultdict(set)
    lang_dropped_total = 0
    quality_dropped_total = 0
    dead_dropped_total = 0
    new_buckets: "dict[str, list]" = defaultdict(list)  # new_cat -> list of (display, ch)
    new_cat_order: list[str] = []  # in source-list order, deduped
    seen_new_cats: set = set()


    def _region_for(cat: str) -> str:
        """Map an emitted category name to a region key for the EPG splits."""
        if cat.startswith("beIN"):
            return "bein"
        if cat.startswith("Sports —"):
            return "sports"
        if cat.startswith("Arabic —"):
            return "ar"
        if cat.startswith("UK —"):
            return "uk"
        if cat.startswith("US —"):
            return "us"
        return "other"

    for cat in ALLOWED_CATEGORIES_ORDER:
        entries = by_cat.get(cat, [])
        if not entries:
            continue
        # Apply explicit category remap (DirecTV merge, Major-PPV consolidation).
        # If a source category has a remap entry, that's the display name —
        # auto-trim is bypassed for it.
        remapped_display = CATEGORY_REMAP.get(cat)
        clean_cat = remapped_display if remapped_display else clean_category_name(cat)
        is_bein_src = ("beIN" in clean_cat or "BEIN" in cat.upper())
        # Skip any source category whose name carries AFC — user removed AFC
        # entirely from the playlist.
        if re.search(r'\bAFC\b', clean_cat, re.I) or re.search(r'\bAFC\b', cat, re.I):
            continue

        for ch in entries:
            raw = ch.get("tvg_name") or ch.get("title") or ""
            if not is_english_or_arabic(raw):
                lang_dropped_total += 1
                continue
            if not is_acceptable_quality(raw):
                quality_dropped_total += 1
                continue
            if ch.get("effective_id") in dead_ids:
                dead_dropped_total += 1
                continue
            display = clean_channel_name(raw)
            # Re-bucket beIN channels into one of the merged categories. Some
            # branches (AFC) return None — drop those channels entirely.
            if is_bein_src:
                new_cat = classify_to_merged_category(display)
                if new_cat is None:
                    continue
            else:
                # Drop non-beIN source categories whose source name carries
                # 'AFC' as well — user doesn't want it anywhere.
                if re.search(r'\bAFC\b', clean_cat, re.I) or re.search(r'\bAFC\b', raw, re.I):
                    continue
                new_cat = clean_cat
            if new_cat not in seen_new_cats:
                seen_new_cats.add(new_cat)
                new_cat_order.append(new_cat)
            new_buckets[new_cat].append((display, ch))

    # Enforce a stable display order for the merged beIN categories ahead of
    # the rest (which keep their source order from ALLOWED_CATEGORIES_ORDER).
    # AFC removed per user request.
    BEIN_DISPLAY_ORDER = ["beIN Sports", "beIN Sports MAX", "beIN Sports XTRA"]
    bein_present = [c for c in BEIN_DISPLAY_ORDER if c in seen_new_cats]
    non_bein = [c for c in new_cat_order if c not in set(BEIN_DISPLAY_ORDER)]
    new_cat_order = bein_present + non_bein

    # Second pass: compute the FINAL emitted category name for each bucket,
    # then re-bucket by that name so source categories that trim to the same
    # display label get merged. Example:
    #   'UK — Sport HEVC' + 'UK — Sport RAW VIP Dolby Audio' → 'UK — Sport'
    # Channels from both source buckets end up in one combined display group.
    final_buckets: "dict[str, list]" = defaultdict(list)
    final_order: list[str] = []  # preserves first-occurrence order
    seen_final: set = set()
    # Set of CATEGORY_REMAP target display names — these came from the remap,
    # so we skip the trim step for them (they're already the final name).
    _remap_target_displays = set(CATEGORY_REMAP.values())
    for new_cat in new_cat_order:
        entries = new_buckets[new_cat]
        if not entries:
            continue
        is_bein_cat = "beIN" in new_cat
        is_remap_target = new_cat in _remap_target_displays
        if is_bein_cat or is_remap_target:
            emitted_cat = new_cat
            display_entries = entries
        else:
            sources_in = {extract_source_tag(d) for d, _ in entries}
            langs_in = {extract_language(d) for d, _ in entries}
            qualities_in = {extract_quality(d) for d, _ in entries}
            uniform_source = next(iter(sources_in)) if len(sources_in) == 1 else None
            uniform_lang = next(iter(langs_in)) if len(langs_in) == 1 else None
            uniform_quality = next(iter(qualities_in)) if len(qualities_in) == 1 else None
            display_entries = [
                (strip_uniform(d, uniform_source, uniform_lang, uniform_quality), c)
                for d, c in entries
            ]
            emitted_cat = trim_category_redundancy(new_cat, uniform_source, uniform_quality)
        if emitted_cat not in seen_final:
            seen_final.add(emitted_cat)
            final_order.append(emitted_cat)
        final_buckets[emitted_cat].extend(display_entries)

    # Re-run uniform-suffix stripping on each MERGED bucket — a tag that was
    # uniform inside a source bucket may no longer be uniform after merging,
    # and vice versa. This guarantees the final channel names match what's
    # actually shared across the displayed group.
    cleaned_final: "dict[str, list]" = {}
    for emitted_cat, entries in final_buckets.items():
        if "beIN" in emitted_cat:
            cleaned_final[emitted_cat] = entries
            continue
        sources_in = {extract_source_tag(d) for d, _ in entries}
        langs_in = {extract_language(d) for d, _ in entries}
        qualities_in = {extract_quality(d) for d, _ in entries}
        uniform_source = next(iter(sources_in)) if len(sources_in) == 1 else None
        uniform_lang = next(iter(langs_in)) if len(langs_in) == 1 else None
        uniform_quality = next(iter(qualities_in)) if len(qualities_in) == 1 else None
        cleaned_final[emitted_cat] = [
            (strip_uniform(d, uniform_source, uniform_lang, uniform_quality), c)
            for d, c in entries
        ]

    # Third pass: per final bucket, sort and emit.
    seen_cats_log = []
    hidden_cats_dropped = 0
    for emitted_cat in final_order:
        entries = cleaned_final[emitted_cat]
        if not entries:
            continue
        # User-managed hide list — drop entire category.
        if emitted_cat in category_hide:
            hidden_cats_dropped += 1
            continue
        is_bein_cat = "beIN" in emitted_cat
        decorated = []
        seen_display_per_cat: set = set()
        for display, ch in entries:
            # Drop within-category dupes (same exact display name) — keeps the
            # first occurrence which, after sort below, will be the best one.
            # For now we just collect; dedup happens after sort.
            # Source-category passes the RAW/VIP/Dolby tier signal that may
            # only live on the original group-title (e.g. 'UK| SPORT RAW VIP
            # DOLBY AUDIO').
            src_cat = ch.get("group", "")
            # PPV pin: if this is a PPV/Event-pattern channel AND it's
            # currently airing real content (in live_set_str), prepend pin=0
            # so it sorts to the very top of the category. Otherwise pin=1.
            is_ppv_pattern = bool(_PPV_PIN_RE.search(display))
            is_live_now = ch.get("effective_id") in live_set_str
            pin = 0 if (is_ppv_pattern and is_live_now) else 1
            q = -quality_rank(display, source_category=src_cat)
            lang = language_rank(display) if is_bein_cat else 0
            prov = provider_priority_rank(display)
            decorated.append((pin, q, lang, natural_key(display), prov, display, ch))
        decorated.sort(key=lambda x: (x[0], x[1], x[2], x[3], x[4]))
        # Dedup by exact display name within a category (keep highest-quality).
        dedup = []
        for tup in decorated:
            display = tup[5]  # display is index 5 now (pin, q, lang, nat, prov, display, ch)
            if display in seen_display_per_cat:
                continue
            seen_display_per_cat.add(display)
            dedup.append(tup)
        # Emit with tvg-chno.
        chno = 1
        # Apply emoji prefix once per category for the player-visible name.
        # Internal logic (dedup, seen_cats_log, region mapping) still uses
        # the bare name so emoji changes don't break lookups.
        display_cat = add_category_emoji(emitted_cat)
        for _, _, _, _, _, display, ch in dedup:
            line = ch.get("extinf_line", "")
            if not line:
                continue
            line = _TVG_ID_ATTR_RE.sub("", line)
            line = _TVG_CHNO_ATTR_RE.sub("", line)
            line = _TVG_NAME_ATTR_RE.sub(
                lambda m: m.group(1) + display.replace('"', "'") + m.group(3), line,
            )
            line = _GROUP_TITLE_ATTR_RE.sub(
                lambda m: m.group(1) + display_cat.replace('"', "'") + m.group(3), line,
            )
            # Strip obviously-broken logo URLs, then apply per-channel override.
            def _logo_repl(m: re.Match) -> str:
                cleaned = sanitize_logo(m.group(2))
                cleaned = apply_logo_override(display, cleaned, logo_overrides)
                return m.group(1) + cleaned + m.group(3)
            line = _TVG_LOGO_ATTR_RE.sub(_logo_repl, line)
            comma_idx = line.find(",")
            if comma_idx > 0:
                line = line[: comma_idx + 1] + display
            eff = ch["effective_id"].replace('"', "'")
            m = re.match(r'(#EXTINF[^\s,]*)\s*(.*?,.*)$', line, re.DOTALL)
            if m:
                head, tail = m.group(1), m.group(2)
                line = f'{head} tvg-id="{eff}" tvg-chno="{chno}" {tail}'
            out.append(line)
            out.extend(ch.get("extra_lines", []))
            # Failover URL hints — emit up to 3 backup URLs for the same
            # canonical channel as #EXTVLCOPT lines. Compatible players
            # (VLC, some IPTV apps) try the next URL if the primary fails.
            # Others safely ignore these lines.
            primary_url = ch.get("url_line", "")
            if primary_url:
                canon = canonical_channel_name(display)
                backups = [u for u in failover_map.get(canon, []) if u != primary_url][:3]
                for i, backup in enumerate(backups, 1):
                    out.append(f'#EXTVLCOPT:backup-url-{i}={backup}')
                out.append(primary_url)
            written += 1
            used_ids.add(ch["effective_id"])
            used_ids_by_region[_region_for(emitted_cat)].add(ch["effective_id"])
            chno += 1
        seen_cats_log.append((display_cat, len(dedup)))

    print(f"      M3U filters: language-dropped {lang_dropped_total} (non-EN/AR), quality-dropped {quality_dropped_total} (SD/LQ), dead-dropped {dead_dropped_total}")
    print(f"      M3U category filter: {written} entries across {len(seen_cats_log)} merged categories")
    for clean_cat, n in seen_cats_log:
        print(f"        [{n:>4}]  {clean_cat}")
    matched_cats = {orig for orig in ALLOWED_CATEGORIES_ORDER if by_cat.get(orig)}
    missing = [c for c in ALLOWED_CATEGORIES_ORDER if c not in matched_cats]
    if missing:
        print(f"      not found in M3U ({len(missing)}):")
        for c in missing:
            print(f"        ?? {c}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(out) + "\n", encoding="utf-8")

    # Write a sidecar failover.json mapping canonical channel name →
    # ordered list of stream URLs. Easier for custom clients to parse than
    # the #EXTVLCOPT lines embedded in the M3U.
    import json as _json
    fover = {k: v for k, v in failover_map.items() if len(v) > 1}
    if fover:
        (dest.parent / "failover.json").write_text(
            _json.dumps(fover, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        n_fb = sum(len(v) - 1 for v in fover.values())
        print(f"      wrote failover.json: {len(fover)} channels with {n_fb} total backup URLs")
    return written, used_ids, dict(used_ids_by_region)


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
    # has an effective_id (original tvg-id or auto-generated). Apply the
    # same prettification (clean_channel_name) used when writing the patched
    # M3U so EPG cell titles match what the user sees in their player.
    m3u_display = {}
    for ch in m3u_channels:
        tid = ch["effective_id"]
        raw = ch["tvg_name"] or ch["title"]
        name = clean_channel_name(raw) if raw else ""
        if not name:
            name = raw  # fallback for edge cases where cleaning produces ""
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
        # Use the channel display name as the dummy programme title so the
        # player shows the channel's own name in the EPG cell instead of
        # rendering "No EPG" for blank/whitespace titles. XML-escape to be
        # safe. Also emit a <desc> — UHF on tvOS (and some other players)
        # render programmes without a description as "Data Unavailable",
        # so a placeholder desc keeps the detail pane populated.
        title_xml = html.escape(m3u_display[tid], quote=True)
        desc_xml = title_xml + " — live channel. Programme guide unavailable."
        for s_str, e_str in block_times:
            p = (
                f'<programme start="{s_str}" stop="{e_str}" channel="{tid_xml}">'
                f'<title lang="en">{title_xml}</title>'
                f'<desc lang="en">{desc_xml}</desc></programme>'
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

    # Build a fast lookup: channel id (raw) → first display-name text.
    # Used by gap_blocks to set a sensible title for filler programmes (the
    # channel's own display name) instead of leaving them blank — many
    # players render blank titles as "No EPG".
    _DISPLAY_NAME_RE = re.compile(rb'<display-name[^>]*>([^<]+)</display-name>')
    cid_to_display_xml: dict[str, str] = {}
    for cid_raw, blk in kept_channels.items():
        m = _DISPLAY_NAME_RE.search(blk)
        if m:
            # Keep the XML-escaped form ready to drop straight into <title>.
            try:
                txt = m.group(1).decode("utf-8")
            except UnicodeDecodeError:
                txt = m.group(1).decode("utf-8", "replace")
            # The block was emitted XML-escaped already; reuse as-is.
            cid_to_display_xml[cid_raw] = txt

    def gap_blocks(start: dt.datetime, end: dt.datetime, cid_xml: str,
                   cid_raw: str = "") -> list[bytes]:
        """Split [start, end) into BLOCK_HOURS-sized chunks of dummy programmes.

        Title prefers the M3U's tvg-name (cleaner, mixed-case) over the
        provider's display-name (often the all-caps tvg-id form). Falls back
        to the provider's display-name, then to the raw channel id. Each
        programme also carries a <desc> — UHF on tvOS renders programmes
        without a description as 'Data Unavailable'."""
        out = []
        cur = start
        title_raw = (
            m3u_display.get(cid_raw)
            or html.unescape(cid_to_display_xml.get(cid_raw, ""))
            or cid_raw
        )
        title_xml = html.escape(title_raw, quote=True)
        desc_xml = title_xml + " — live channel. Programme guide unavailable."
        while cur < end:
            nxt = min(cur + dt.timedelta(hours=BLOCK_HOURS), end)
            s_str = fmt_xmltv_time(cur)
            e_str = fmt_xmltv_time(nxt)
            out.append(
                (
                    f'<programme start="{s_str}" stop="{e_str}" channel="{cid_xml}">'
                    f'<title lang="en">{title_xml}</title>'
                    f'<desc lang="en">{desc_xml}</desc></programme>'
                ).encode("utf-8")
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
                gap_fill_programmes.extend(gap_blocks(cursor, min(start, series_stop_utc), cid_xml, cid))
                had_gap = True
            cursor = max(cursor, stop)
            if cursor >= series_stop_utc:
                break
        if cursor < series_stop_utc:
            gap_fill_programmes.extend(gap_blocks(cursor, series_stop_utc, cid_xml, cid))
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

    # Post-process: scrub provider-supplied placeholder titles ("No EPG",
    # "N/A", "TBA", etc.). REPLACE them with the channel's own display name
    # — many players interpret an empty/whitespace title as "no data" and
    # render "No EPG" themselves, so emptying the cell makes the problem
    # WORSE. Filling with the channel name keeps the cell looking informative.
    NO_EPG_TITLE_RE = re.compile(
        rb'(<programme\b[^>]*channel="([^"]+)"[^>]*>[^<]*<title\b[^>]*>)\s*'
        rb'(?:No\s*EPG|N/?A|TBA|TBD|Not Available|Data Unavailable|'
        rb'Pas de programme|No Programa|To Be Announced)\s*'
        rb'(</title>)',
        re.IGNORECASE,
    )
    def _scrub(match: re.Match) -> bytes:
        cid_xml = match.group(2).decode("utf-8", "replace")
        # Try both XML-escaped form and html-unescaped form for lookup.
        cid_raw = html.unescape(cid_xml)
        name = cid_to_display_xml.get(cid_raw) or cid_to_display_xml.get(cid_xml) or ""
        if not name:
            # Fall back to the channel id itself, sans any leading punctuation.
            name = cid_xml.lstrip("#: ").strip()
        # Escape since we'll inject into XML title text.
        safe = html.escape(name, quote=False).encode("utf-8")
        return match.group(1) + safe + match.group(3)

    # Also catch empty / whitespace-only titles — these come either from the
    # provider (some XMLTV sources emit '<title></title>') or from our own
    # legacy gap-fill blocks. Player renders them as 'No EPG' too.
    EMPTY_TITLE_RE = re.compile(
        rb'(<programme\b[^>]*channel="([^"]+)"[^>]*>[^<]*<title\b[^>]*>)\s*(</title>)',
    )
    scrubbed = 0
    scrubbed_empty = 0
    for i, p in enumerate(kept_programmes):
        new_p = p
        # Cheap pre-check to avoid running the heavy regex on every programme.
        if (b'No EPG' in new_p or b'N/A' in new_p or b'TBA' in new_p or b'TBD' in new_p
                or b'Not Available' in new_p or b'Data Unavailable' in new_p
                or b'To Be Announced' in new_p):
            new_p, n = NO_EPG_TITLE_RE.subn(_scrub, new_p)
            scrubbed += n
        # Empty-title pass (no pre-filter; cheap regex)
        new_p, n2 = EMPTY_TITLE_RE.subn(_scrub, new_p)
        scrubbed_empty += n2
        if new_p is not p:
            kept_programmes[i] = new_p
    if scrubbed or scrubbed_empty:
        print(f"      scrubbed {scrubbed} placeholder titles + {scrubbed_empty} empty titles (all replaced with channel name)")

    # Fill missing <desc> on every programme (real-EPG or dummy). UHF on tvOS
    # renders programmes whose <desc> is absent or empty as "Data Unavailable"
    # in the detail pane. Mirror the title into <desc> so the detail pane
    # always has something to render. Cheap regex: only touches programmes
    # that are actually missing a <desc>.
    DESC_PRESENT_RE = re.compile(rb'<desc\b[^>]*>[^<]')
    PROG_TITLE_TEXT_RE = re.compile(rb'<title\b[^>]*>([^<]*)</title>')
    desc_added = 0
    for i, p in enumerate(kept_programmes):
        if DESC_PRESENT_RE.search(p):
            continue
        m = PROG_TITLE_TEXT_RE.search(p)
        if not m:
            continue
        title_bytes = m.group(1).strip()
        if not title_bytes:
            continue
        desc_block = b'<desc lang="en">' + title_bytes + b'</desc>'
        # Inject just before </programme>. Falls back to no-op if the
        # programme is somehow self-closing or otherwise unexpected.
        if b'</programme>' in p:
            kept_programmes[i] = p.replace(b'</programme>', desc_block + b'</programme>', 1)
            desc_added += 1
    if desc_added:
        print(f"      added <desc> to {desc_added} programmes missing one (mirrors <title>)")

    # NOTE: guide.xml.gz used to include ALL channels we scraped (~58k incl.
    # ~55k channels NOT in the user's playlist). That made the file ~50MB
    # gzipped / ~570MB uncompressed and crashed memory-constrained players
    # like UHF on tvOS. It's now written further below alongside the lite
    # version, filtered to playlist channels only (same content as lite),
    # producing a ~10MB file that all players load comfortably.

    # Strip an old guide.xml if it was committed before this change so Pages
    # stops serving stale content.
    if out_xml.exists():
        out_xml.unlink()

    # Publish the non-sensitive tvg-id map. The user uses patch_m3u.py locally
    # to inject these tvg-ids into their private M3U. We do NOT publish the
    # full M3U because it embeds the user's stream auth tokens.
    out_map = out_dir / "tvg-id-map.tsv"
    written = write_tvg_id_map(m3u_channels, out_map)
    print(f"      wrote {out_map} ({out_map.stat().st_size//1024} KB, {written} rows)")

    # OPTIONAL: also write a patched M3U with tvg-ids injected, behind a
    # random URL token (env M3U_PATH_TOKEN). The token doubles as the only
    # access key — anyone with the URL can use the user's IPTV subscription.
    # === Compute "live now" channels for the PPV pin ===
    # Channels whose EPG has a currently-airing programme with a real (non-
    # blank, non-placeholder) title. write_patched_m3u uses this to pin PPV
    # /Event channels that ARE airing to the top of their category.
    now_utc_now = dt.datetime.now(dt.timezone.utc)
    PROG_LIVE_RE = re.compile(
        rb'<programme\s+start="([^"]+)"\s+stop="([^"]+)"[^>]*channel="([^"]+)"[^>]*>'
        rb'.*?<title\b[^>]*>([^<]*)</title>',
        re.DOTALL,
    )
    LIVE_TIME_RE = re.compile(rb'(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})')
    def _parse_xmltv_b(s: bytes):
        m = LIVE_TIME_RE.match(s)
        if not m:
            return None
        try:
            y, mo, d, h, mi, sec = (int(m.group(i)) for i in range(1, 7))
            return dt.datetime(y, mo, d, h, mi, sec, tzinfo=dt.timezone.utc)
        except (ValueError, OverflowError):
            return None
    live_now_ids: set = set()
    for p in kept_programmes:
        m = PROG_LIVE_RE.search(p)
        if not m:
            continue
        start = _parse_xmltv_b(m.group(1))
        stop = _parse_xmltv_b(m.group(2))
        if not start or not stop:
            continue
        if not (start <= now_utc_now < stop):
            continue
        title = m.group(4).strip()
        if not title:
            continue
        # Strip XML entities for the blank check
        if title.lower() in (b'no epg', b'n/a', b'tba', b'tbd', b'not available'):
            continue
        # Channel id may be XML-escaped; keep both forms
        cid = m.group(3)
        live_now_ids.add(cid)
        live_now_ids.add(html.unescape(cid.decode('utf-8', 'replace')).encode('utf-8'))
    print(f"      {len(live_now_ids)} channels are live-now (EPG has current programme with title)")

    token = os.environ.get("M3U_PATH_TOKEN", "").strip()
    if token:
        pages_base = os.environ.get("PAGES_BASE", "https://al7omed.github.io/iptv-epg-all")
        # Patched M3U uses the LITE EPG to keep player load times snappy.
        epg_lite_link = f"{pages_base}/guide-lite.xml.gz"
        m3u_out = out_dir / token / "playlist.m3u"

        # Channel-name diff: snapshot the OLD playlist's {tvg-id: display_name}
        # mapping BEFORE the new write overwrites it. After the new playlist
        # is written, we'll diff and log renames.
        old_names: dict[str, str] = {}
        if m3u_out.exists():
            old_extinf = re.compile(r'tvg-id="([^"]+)"[^,]*,(.*)$')
            for line in m3u_out.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("#EXTINF"):
                    m = old_extinf.search(line)
                    if m:
                        old_names[html.unescape(m.group(1))] = m.group(2).strip()

        n, used_ids, used_ids_by_region = write_patched_m3u(
            m3u_channels, m3u_out, epg_lite_link, live_now_ids=live_now_ids,
        )
        print(f"      wrote patched M3U at {m3u_out} ({m3u_out.stat().st_size//1024} KB, {n} entries)")

        # Now compute the new {tvg-id: display_name} and diff.
        new_names: dict[str, str] = {}
        new_extinf = re.compile(r'tvg-id="([^"]+)"[^,]*,(.*)$')
        for line in m3u_out.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("#EXTINF"):
                m = new_extinf.search(line)
                if m:
                    new_names[html.unescape(m.group(1))] = m.group(2).strip()

        renames: list[tuple[str, str, str]] = []
        for tid, new_name in new_names.items():
            old = old_names.get(tid)
            if old is not None and old != new_name:
                renames.append((tid, old, new_name))
        if renames:
            ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            log_path = Path("channels/name_changes.log")
            log_path.parent.mkdir(parents=True, exist_ok=True)
            # Append-only log; user can inspect/clear as needed.
            with log_path.open("a", encoding="utf-8") as f:
                f.write(f"\n=== {ts} — {len(renames)} channel name change(s) ===\n")
                for tid, old, new in renames[:200]:  # cap to avoid bloat
                    f.write(f"  {tid}\n    OLD: {old}\n    NEW: {new}\n")
                if len(renames) > 200:
                    f.write(f"  ... and {len(renames) - 200} more\n")
            print(f"      channel renames detected: {len(renames)} (logged to {log_path})")
        # Curated favorites playlist behind the same access token.
        fav_out = out_dir / token / "favorites.m3u"
        fn = write_favorites_m3u(m3u_channels, fav_out, epg_lite_link,
                                   live_now_ids=live_now_ids)
        print(f"      wrote favorites M3U at {fav_out} ({fav_out.stat().st_size//1024} KB, {fn} entries)")

        # === EPG lite + region splits ===
        # The full guide.xml.gz is ~47 MB. The lite version only includes the
        # channels actually exposed in the playlist (much faster cold-start).
        # Region-specific files (guide-bein, guide-uk, guide-us, guide-ar,
        # guide-sports) let advanced users mix multiple sub-playlists with
        # tiny EPG payloads.
        prog_chan_re = re.compile(rb'channel="([^"]+)"')

        def _write_epg_subset(out_path: Path, want_ids: set[str], label: str) -> None:
            if not want_ids:
                return
            want_bytes = {i.encode("utf-8") for i in want_ids}
            want_escaped = {
                i.replace(b"&", b"&amp;").replace(b'"', b"&quot;")
                 .replace(b"<", b"&lt;").replace(b">", b"&gt;")
                for i in want_bytes
            }
            want_all = want_bytes | want_escaped
            kept_chans: list[bytes] = []
            kept_chan_ids: set = set()
            for cid in sorted(kept_channels):
                cid_b = cid.encode("utf-8") if isinstance(cid, str) else cid
                cid_escaped = cid_b.replace(b"&", b"&amp;").replace(b'"', b"&quot;")
                if cid_b in want_all or cid_escaped in want_all:
                    kept_chans.append(kept_channels[cid])
                    kept_chan_ids.add(cid_b)
                    kept_chan_ids.add(cid_escaped)
            kept_progs = [
                p for p in kept_programmes
                if (m := prog_chan_re.search(p)) and m.group(1) in kept_chan_ids
            ]
            with gzip.open(out_path, "wb", compresslevel=6) as f:
                f.write(header)
                for block in kept_chans:
                    f.write(block)
                    f.write(b"\n")
                for p in kept_progs:
                    f.write(p)
                    f.write(b"\n")
                f.write(footer)
            print(f"      wrote {out_path.name} ({out_path.stat().st_size//1024} KB, "
                  f"{len(kept_chans)} channels, {len(kept_progs)} programmes) — {label}")

        # Full EPG (same playlist-filtered content as lite). Previously
        # guide.xml.gz was the unfiltered ~50MB / ~570MB-uncompressed dump of
        # every channel epgshare01 carries, which crashed UHF on tvOS. Now
        # it carries only the channels actually in the playlist — identical
        # to lite in content, kept as a separate file for backwards-compat
        # with player configs pointing at guide.xml.gz.
        _write_epg_subset(out_dir / "guide.xml.gz", used_ids, "full = all playlist channels")
        # Lite (same content as full now; both are the recommended file)
        _write_epg_subset(out_dir / "guide-lite.xml.gz", used_ids, "lite = all playlist channels")
        # Per-region splits
        _write_epg_subset(out_dir / "guide-bein.xml.gz",
                          used_ids_by_region.get("bein", set()), "beIN only")
        _write_epg_subset(out_dir / "guide-sports.xml.gz",
                          used_ids_by_region.get("sports", set()), "Sports — bucket only")
        _write_epg_subset(out_dir / "guide-ar.xml.gz",
                          used_ids_by_region.get("ar", set()) | used_ids_by_region.get("bein", set()),
                          "Arabic (incl. beIN MENA)")
        _write_epg_subset(out_dir / "guide-uk.xml.gz",
                          used_ids_by_region.get("uk", set()), "UK only")
        _write_epg_subset(out_dir / "guide-us.xml.gz",
                          used_ids_by_region.get("us", set()), "US only")

        # === Subscribe bundle: setup.json + README.txt + QR SVG ===
        # Helps onboarding a new device: one URL/QR can be scanned/copied
        # to set up the player without manually typing the long random token.
        token_dir = out_dir / token
        setup_payload = {
            "playlist": f"{pages_base}/{token}/playlist.m3u",
            "favorites": f"{pages_base}/{token}/favorites.m3u",
            "epg_full": f"{pages_base}/guide.xml.gz",
            "epg_lite_recommended": f"{pages_base}/guide-lite.xml.gz",
            "epg_bein": f"{pages_base}/guide-bein.xml.gz",
            "epg_sports": f"{pages_base}/guide-sports.xml.gz",
            "epg_arabic": f"{pages_base}/guide-ar.xml.gz",
            "epg_uk": f"{pages_base}/guide-uk.xml.gz",
            "epg_us": f"{pages_base}/guide-us.xml.gz",
            "failover_data": f"{pages_base}/{token}/failover.json",
            "tvg_id_map": f"{pages_base}/tvg-id-map.tsv",
            "channel_count": n,
            "favorites_count": fn,
            "generated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "timezone": "GMT+3 (user local)",
            "build_cron": "every hour at :15",
            "health_check_cron": "Sundays 03:00 UTC",
        }
        import json as _json2
        (token_dir / "setup.json").write_text(
            _json2.dumps(setup_payload, indent=2), encoding="utf-8",
        )

        readme_lines = [
            "IPTV — Auto-generated playlist + EPG bundle",
            "=" * 50,
            "",
            f"Generated: {setup_payload['generated_utc']}",
            f"Channels: {n}    Favorites: {fn}",
            "",
            "MAIN URLS (paste into your player)",
            "-" * 50,
            f"Playlist:  {setup_payload['playlist']}",
            f"Favorites: {setup_payload['favorites']}",
            f"EPG (recommended, smaller, faster): {setup_payload['epg_lite_recommended']}",
            "",
            "REGION-SPECIFIC EPG (advanced — mix multiple sub-playlists)",
            "-" * 50,
            f"beIN MENA only: {setup_payload['epg_bein']}",
            f"Sports cats:    {setup_payload['epg_sports']}",
            f"Arabic (+beIN): {setup_payload['epg_arabic']}",
            f"UK only:        {setup_payload['epg_uk']}",
            f"US only:        {setup_payload['epg_us']}",
            f"All channels:   {setup_payload['epg_full']}",
            "",
            "DATA",
            "-" * 50,
            f"Stream failover (canonical → backup URLs): {setup_payload['failover_data']}",
            f"tvg-id mapping (channel → EPG id):         {setup_payload['tvg_id_map']}",
            "",
            "BUILD SCHEDULE",
            "-" * 50,
            f"Playlist refresh: {setup_payload['build_cron']}",
            f"Dead-channel scan: {setup_payload['health_check_cron']}",
            "",
            "CONFIG FILES (committed to repo, edit to customize)",
            "-" * 50,
            "channels/favorites_extra.txt   pin extra channels into favorites",
            "channels/category_hide.txt     hide entire categories from output",
            "channels/logo_overrides.tsv    per-channel logo URL override",
            "channels/dead_channels.txt     dead-stream blacklist (auto-updated)",
            "channels/name_changes.log      append-only log of channel rename events",
        ]
        (token_dir / "README.txt").write_text(
            "\n".join(readme_lines) + "\n", encoding="utf-8",
        )

        # QR code as SVG (no external deps). Encodes the playlist URL so a
        # phone camera can grab it and the user can paste into their player.
        # Minimal pure-Python QR — fall back to a text file if qrcode pkg
        # isn't installed.
        try:
            import qrcode
            qr = qrcode.QRCode(
                version=None, error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=10, border=4,
            )
            qr.add_data(setup_payload["playlist"])
            qr.make(fit=True)
            from qrcode.image.svg import SvgImage
            img = qr.make_image(image_factory=SvgImage)
            img.save(str(token_dir / "playlist-qr.svg"))
            print(f"      wrote subscribe bundle: setup.json, README.txt, playlist-qr.svg")
        except Exception as e:
            # Fallback: write a tiny placeholder noting why QR couldn't be generated.
            (token_dir / "playlist-qr.svg").write_text(
                f"<!-- QR generation skipped: {e}. Install 'qrcode' to enable. -->\n",
                encoding="utf-8",
            )
            print(f"      wrote subscribe bundle: setup.json, README.txt (QR skipped: {e})")

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
