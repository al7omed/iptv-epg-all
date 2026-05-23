#!/usr/bin/env python3
"""Fetch beIN's TV-guide directly from bein.com and emit XMLTV.

The al7omed/bein-epg repo uses iptv-org/epg's beinsports.com scraper which
runs every 12 hours and doesn't capture the LIVE badge that bein.com puts
on each currently-airing live broadcast. This script bypasses that, hits
bein.com's epg-ajax-template endpoint directly, and emits XMLTV that:

  * Uses the bein-epg-style channel ids ('beINSports1.qa@MENA', etc.) so
    iptv-epg-all's override pass picks the data up the same way.
  * Adds <category>Live</category> on programmes bein.com flagged as live
    broadcasts (via their `<div class=live>` badge).
  * Prefixes those programmes' titles with '🔴 LIVE: ' so players that
    only render <title> still surface the flag.

Output XMLTV times are emitted in UTC ('+0000' suffix). Source times from
bein.com are GMT+3 (Bahrain wall-clock) since we hit the ?c=bh tv-guide.

Usage:
  python3 fetch_bein_live.py                  # writes XMLTV to stdout
  python3 fetch_bein_live.py -o out.xml       # writes to file
  python3 fetch_bein_live.py -d 5             # fetch 5 days ahead

Exits non-zero on irrecoverable fetch failure.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html as html_module
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
REFERER = "https://www.bein.com/en/tv-guide/?c=bh"
ENDPOINT = "https://www.bein.com/en/epg-ajax-template/"
# bein.com serves the Bahrain TV-guide in Asia/Bahrain time (GMT+3 year-round)
SOURCE_TZ_OFFSET = dt.timedelta(hours=3)

# Map bein.com logo basename (lowercased) -> (canonical channel display-name,
# XMLTV channel id matching bein-epg's id format). The basename is matched
# case-insensitively because bein.com mixes capitalisations
# ("beIN_SPORTS1_DIGITAL_Mono" vs "beIN_SPORTS1_ENGLISH_Digital_Mono" vs
# "bein_SPORTS_FTA_DIGITAL_Mono"). Anything not in this map gets skipped.
_LOGO_RAW: dict[str, tuple[str, str]] = {
    # Main sports (Arabic default)
    "beIN_SPORTS1_DIGITAL_Mono":   ("beIN SPORTS 1",  "beINSports1.qa@MENA"),
    "beIN_SPORTS2_DIGITAL_Mono":   ("beIN SPORTS 2",  "beINSports2.qa@MENA"),
    "beIN_SPORTS3_DIGITAL_Mono":   ("beIN SPORTS 3",  "beINSports3.qa@MENA"),
    "beIN_SPORTS4_DIGITAL_Mono":   ("beIN SPORTS 4",  "beINSports4.qa@MENA"),
    "beIN_SPORTS5_DIGITAL_Mono":   ("beIN SPORTS 5",  "beINSports5.qa@MENA"),
    "beIN_SPORTS6_DIGITAL_Mono":   ("beIN SPORTS 6",  "beINSports6.qa@MENA"),
    "beIN_SPORTS7_DIGITAL_Mono":   ("beIN SPORTS 7",  "beINSports7.qa@MENA"),
    "beIN_SPORTS8_DIGITAL_Mono":   ("beIN SPORTS 8",  "beINSports8.qa@MENA"),
    "beIN_SPORTS9_DIGITAL_Mono":   ("beIN SPORTS 9",  "beINSports9.qa@MENA"),
    # English language variants
    "beIN_SPORTS1_ENGLISH_Digital_Mono": ("beIN SPORTS EN 1", "beINSports1En.qa@MENA"),
    "beIN_SPORTS2_ENGLISH_Digital_Mono": ("beIN SPORTS EN 2", "beINSports2En.qa@MENA"),
    "beIN_SPORTS3_ENGLISH_Digital_Mono": ("beIN SPORTS EN 3", "beINSports3En.qa@MENA"),
    # French language variants
    "beIN_SPORTS1_FRENCH_Digital_Mono":  ("beIN SPORTS FR 1", "beINSports1Fr.qa@MENA"),
    "beIN_SPORTS2_FRENCH_Digital_Mono":  ("beIN SPORTS FR 2", "beINSports2Fr.qa@MENA"),
    # MAX tier
    "beIN_SPORTS_MAX1_DIGITAL_Mono":  ("beIN SPORTS MAX 1", "beINSportsMax1.qa@MENA"),
    "beIN_SPORTS_MAX2_DIGITAL_Mono":  ("beIN SPORTS MAX 2", "beINSportsMax2.qa@MENA"),
    "beIN_SPORTS_MAX3_DIGITAL_Mono":  ("beIN SPORTS MAX 3", "beINSportsMax3.qa@MENA"),
    "beIN_SPORTS_MAX4_DIGITAL_Mono":  ("beIN SPORTS MAX 4", "beINSportsMax4.qa@MENA"),
    "beIN_SPORTS_MAX5_DIGITAL_Mono":  ("beIN SPORTS MAX 5", "beINSportsMax5.qa@MENA"),
    "beIN_SPORTS_MAX6_DIGITAL_Mono":  ("beIN SPORTS MAX 6", "beINSportsMax6.qa@MENA"),
    # XTRA tier (numbered 1-9 in bein.com TV guide)
    "beIN_SPORTS_XTRA_DIGITAL_Mono":  ("beIN SPORTS XTRA",  "beINSportsXtra.qa@MENA"),
    "beIN_SPORTS_XTRA1_Digital_Mono": ("beIN SPORTS XTRA 1","beINSportsXtra1.qa@MENA"),
    "beIN_SPORTS_XTRA2_Digital_Mono": ("beIN SPORTS XTRA 2","beINSportsXtra2.qa@MENA"),
    "beIN_SPORTS_XTRA3_Digital_Mono": ("beIN SPORTS XTRA 3","beINSportsXtra3.qa@MENA"),
    "beIN_SPORTS_XTRA4_Digital_Mono": ("beIN SPORTS XTRA 4","beINSportsXtra4.qa@MENA"),
    "beIN_SPORTS_XTRA5_Digital_Mono": ("beIN SPORTS XTRA 5","beINSportsXtra5.qa@MENA"),
    "beIN_SPORTS_XTRA6_Digital_Mono": ("beIN SPORTS XTRA 6","beINSportsXtra6.qa@MENA"),
    "beIN_SPORTS_XTRA7_Digital_Mono": ("beIN SPORTS XTRA 7","beINSportsXtra7.qa@MENA"),
    "beIN_SPORTS_XTRA8_Digital_Mono": ("beIN SPORTS XTRA 8","beINSportsXtra8.qa@MENA"),
    "beIN_SPORTS_XTRA9_Digital_Mono": ("beIN SPORTS XTRA 9","beINSportsXtra9.qa@MENA"),
    # Specialty
    "beIN_SPORTS_AFC_DIGITAL_Mono":   ("beIN SPORTS AFC",  "beINSportsAFC.qa@MENA"),
    "beIN_SPORTS_NBA_DIGITAL_Mono":   ("beIN SPORTS NBA",  "beINSportsNBA.qa@MENA"),
    "beIN_SPORTS_NEWS_DIGITAL_Mono":  ("beIN SPORTS NEWS", "beINSportsNews.qa@MENA"),
    # FTA + 4K master
    "bein_SPORTS_FTA_DIGITAL_Mono":   ("beIN SPORTS",      "beINSports.qa@MENA"),
    "beIN_4K_DIGITAL_Mono":           ("beIN 4K",          "beIN4K.qa@MENA"),
    "beIN_SPORTS_4K_HDR_DIGITAL_Mono":("beIN SPORTS 4K HDR","beINSports4KHDR.qa@MENA"),
    # Entertainment / movies
    "beIN_MOVIES_DIGITAL_Mono":           ("beIN MOVIES",        "beINMovies.qa@MENA"),
    "beIN_MOVIES_PREMIERE_DIGITAL_Mono":  ("beIN MOVIES Premiere","beINMoviesPremiere.qa@MENA"),
    "beIN_MOVIES_ACTION_DIGITAL_Mono":    ("beIN MOVIES Action", "beINMoviesAction.qa@MENA"),
    "beIN_MOVIES_DRAMA_DIGITAL_Mono":     ("beIN MOVIES Drama",  "beINMoviesDrama.qa@MENA"),
    "beIN_MOVIES_FAMILY_DIGITAL_Mono":    ("beIN MOVIES Family", "beINMoviesFamily.qa@MENA"),
    "beIN_SERIES_DIGITAL_Mono":           ("beIN SERIES",        "beINSeries.qa@MENA"),
}
LOGO_TO_CHANNEL: dict[str, tuple[str, str]] = {k.lower(): v for k, v in _LOGO_RAW.items()}


# ─── regex toolkit ───
CHANNELS_BLOCK_RE = re.compile(
    # Each channel's block is wrapped in <div id=channels_N> ... </div>.
    # The next channel begins with another `id=channels_M` so we lookahead
    # for that or for the page footer marker.
    r"<div[^>]*\bid=channels_(\d+)\b[^>]*>(.*?)(?=<div[^>]*\bid=channels_\d+|<div[^>]*id='ruler_channels_[^']+'|$)",
    re.DOTALL | re.IGNORECASE,
)
# Logo URL inside a channel block tells us which beIN channel it is.
LOGO_RE = re.compile(r"src='[^']*?/([A-Za-z0-9_]+)\.(?:png|jpg|jpeg|gif)'", re.IGNORECASE)
# Each programme is one <li> containing title/format/start/end + optional
# LIVE badge. Capture them in one regex (non-greedy across multiline).
PROGRAMME_RE = re.compile(
    r"<p\s+class=title>([^<]*)</p>\s*"
    r"<p\s+class=format>([^<]*)</p>"
    r".*?data-start='([^']*)'\s+data-end='([^']*)'"
    r"(.*?)</li>",
    re.DOTALL | re.IGNORECASE,
)
# bein.com paints a <div class=live><img.../></div> on live broadcasts
LIVE_BADGE_RE = re.compile(r"<div\s+class=live\b", re.IGNORECASE)


def fetch_ajax(cdate: str, category: str = "sports",
               offset: int = 0, mins: int = 180) -> str:
    """Fetch one bein.com epg-ajax-template page. Returns HTML text."""
    q = urllib.parse.urlencode({
        "action": "epg_fetch",
        "offset": offset,
        "category": category,
        "serviceidentity": "bein.net",
        "mins": mins,
        "cdate": cdate,
        "language": "EN",
        "postid": 25356,
        "loadindex": 0,
    })
    url = f"{ENDPOINT}?{q}"
    req = urllib.request.Request(url, headers={
        "User-Agent": BROWSER_UA,
        "Referer": REFERER,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_local_to_utc(s: str) -> dt.datetime | None:
    """Parse a 'YYYY-MM-DD HH:MM:SS' local (GMT+3) string and return UTC datetime."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})", s.strip())
    if not m:
        return None
    y, mo, d, h, mi, sec = (int(x) for x in m.groups())
    try:
        local = dt.datetime(y, mo, d, h, mi, sec)
    except ValueError:
        return None
    return local - SOURCE_TZ_OFFSET  # GMT+3 -> UTC


def xmltv_time(t: dt.datetime) -> str:
    return t.strftime("%Y%m%d%H%M%S +0000")


def parse_response(html_text: str) -> list[dict]:
    """Return list of programme dicts:
       {channel_id, channel_name, start_utc, stop_utc, title, format, live}
    Only programmes for channels we recognise (LOGO_TO_CHANNEL) are returned.
    """
    out: list[dict] = []
    seen: set[tuple[str, dt.datetime]] = set()  # (channel_id, start_utc) dedup
    for cm in CHANNELS_BLOCK_RE.finditer(html_text):
        block = cm.group(2)
        # Find the logo basename → channel mapping (case-insensitive)
        ch_info: tuple[str, str] | None = None
        for lm in LOGO_RE.finditer(block):
            basename = lm.group(1).lower()
            if basename in LOGO_TO_CHANNEL:
                ch_info = LOGO_TO_CHANNEL[basename]
                break
        if ch_info is None:
            continue
        name, cid = ch_info
        for pm in PROGRAMME_RE.finditer(block):
            title = html_module.unescape(pm.group(1)).strip()
            fmt = html_module.unescape(pm.group(2)).strip()
            start_str = pm.group(3)
            end_str = pm.group(4)
            tail = pm.group(5)
            if not title:
                continue
            start_utc = parse_local_to_utc(start_str)
            end_utc = parse_local_to_utc(end_str)
            if start_utc is None or end_utc is None:
                continue
            if end_utc <= start_utc:
                # invalid window; skip
                continue
            is_live = bool(LIVE_BADGE_RE.search(tail))
            key = (cid, start_utc)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "channel_id": cid,
                "channel_name": name,
                "start_utc": start_utc,
                "stop_utc": end_utc,
                "title": title,
                "format": fmt,
                "live": is_live,
            })
    return out


def write_xmltv(programmes: list[dict], out_stream) -> None:
    """Emit XMLTV with one <channel> per unique id and one <programme> per row.

    LIVE-flagged programmes get:
      * '🔴 LIVE: ' prefix on the title
      * <category lang="en">Live</category>
    bein.com's `format` (e.g. 'Spanish League - Primera División') maps to
    a regular <category> too so players that filter by category benefit.
    """
    by_chan: dict[str, str] = {}  # cid -> display-name
    for p in programmes:
        by_chan.setdefault(p["channel_id"], p["channel_name"])

    out_stream.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    out_stream.write(
        '<tv generator-info-name="iptv-epg-all/fetch_bein_live" '
        'source-info-name="bein.com/en/tv-guide">\n'
    )
    for cid, name in sorted(by_chan.items()):
        name_esc = html_module.escape(name, quote=True)
        cid_esc = html_module.escape(cid, quote=True)
        out_stream.write(
            f'<channel id="{cid_esc}"><display-name lang="en">{name_esc}</display-name></channel>\n'
        )

    programmes.sort(key=lambda p: (p["channel_id"], p["start_utc"]))
    for p in programmes:
        cid_esc = html_module.escape(p["channel_id"], quote=True)
        start = xmltv_time(p["start_utc"])
        stop = xmltv_time(p["stop_utc"])
        title = p["title"]
        if p["live"]:
            title = "🔴 LIVE: " + title
        title_esc = html_module.escape(title, quote=False)
        out_stream.write(
            f'<programme start="{start}" stop="{stop}" channel="{cid_esc}">'
            f'<title lang="en">{title_esc}</title>'
        )
        if p["format"]:
            fmt_esc = html_module.escape(p["format"], quote=False)
            out_stream.write(f'<category lang="en">{fmt_esc}</category>')
        if p["live"]:
            out_stream.write('<category lang="en">Live</category>')
        out_stream.write('</programme>\n')
    out_stream.write('</tv>\n')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", help="Write XMLTV to this file (default: stdout)")
    ap.add_argument("-d", "--days", type=int, default=5,
                    help="Days ahead to fetch (default: 5)")
    ap.add_argument("--mins", type=int, default=720,
                    help="Window per AJAX call in minutes (default: 720 = 12h)")
    args = ap.parse_args()

    all_progs: list[dict] = []
    seen_keys: set[tuple[str, dt.datetime]] = set()
    # We hit bein.com using cdate in the source TZ (GMT+3).
    today_local = (dt.datetime.utcnow() + SOURCE_TZ_OFFSET).date()
    fail_dates: list[str] = []
    for offset_days in range(args.days):
        cdate = (today_local + dt.timedelta(days=offset_days)).strftime("%Y-%m-%d")
        for category in ("sports", "entertainment"):
            try:
                resp = fetch_ajax(cdate, category, offset=0, mins=args.mins)
            except urllib.error.HTTPError as e:
                sys.stderr.write(f"  HTTP {e.code} on {cdate}/{category}: {e.reason}\n")
                fail_dates.append(f"{cdate}/{category}")
                continue
            except Exception as e:
                sys.stderr.write(f"  fetch error on {cdate}/{category}: {e}\n")
                fail_dates.append(f"{cdate}/{category}")
                continue
            chunk = parse_response(resp)
            new_n = 0
            for p in chunk:
                key = (p["channel_id"], p["start_utc"])
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                all_progs.append(p)
                new_n += 1
            sys.stderr.write(f"  {cdate}/{category}: {new_n} new programmes ({len(chunk)} total in response)\n")

    if not all_progs:
        sys.stderr.write("fetch_bein_live: no programmes parsed; aborting\n")
        return 1

    live_n = sum(1 for p in all_progs if p["live"])
    sys.stderr.write(f"\nTotal: {len(all_progs)} programmes across "
                     f"{len({p['channel_id'] for p in all_progs})} channels, "
                     f"{live_n} flagged LIVE\n")
    if fail_dates:
        sys.stderr.write(f"WARNING: {len(fail_dates)} date/category fetches failed\n")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            write_xmltv(all_progs, f)
        sys.stderr.write(f"wrote XMLTV to {args.out}\n")
    else:
        write_xmltv(all_progs, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
