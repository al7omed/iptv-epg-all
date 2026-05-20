#!/usr/bin/env python3
"""Synthesize an M3U from the Xtream Codes player_api.

Used as a fallback when the provider's /get.php endpoint is blocked by the
CDN's WAF (some IPs receive a non-standard HTTP 884). The Xtream API endpoint
is usually unaffected, but Cloudflare sometimes blocks even that with HTTP 511.
This script retries with delays and uses browser-like headers to slip past
intermittent WAF rules.

Environment:
  XTREAM_HOST  e.g. http://mil79711.wd.business-cdn-8k.com
  XTREAM_USER  e.g. ac50ff82173e
  XTREAM_PASS  e.g. 0b9695fafe

Writes the synthesized M3U to stdout. Exits non-zero on irrecoverable failure.
"""
from __future__ import annotations

import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}


def get_json(url: str, max_attempts: int = 5):
    last_err = None
    for attempt in range(max_attempts):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    import gzip as _gz
                    raw = _gz.decompress(raw)
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            last_err = e
            sys.stderr.write(f"  attempt {attempt+1}/{max_attempts}: HTTP {e.code} {e.reason}\n")
            time.sleep(8 + attempt * 6 + random.uniform(0, 4))
        except Exception as e:
            last_err = e
            sys.stderr.write(f"  attempt {attempt+1}/{max_attempts}: {e}\n")
            time.sleep(4 + attempt * 4)
    raise RuntimeError(f"all {max_attempts} attempts failed: {last_err}")


def main():
    host = os.environ["XTREAM_HOST"].rstrip("/")
    user = os.environ["XTREAM_USER"]
    pwd = os.environ["XTREAM_PASS"]
    q = urllib.parse.urlencode({"username": user, "password": pwd})

    sys.stderr.write(f"fetching categories...\n")
    try:
        cats = get_json(f"{host}/player_api.php?{q}&action=get_live_categories")
    except Exception as e:
        sys.stderr.write(f"  categories failed: {e}; continuing without names\n")
        cats = []
    cat_name = {c["category_id"]: c["category_name"] for c in cats}
    sys.stderr.write(f"  {len(cat_name)} categories\n")

    sys.stderr.write(f"fetching streams...\n")
    streams = get_json(f"{host}/player_api.php?{q}&action=get_live_streams")
    sys.stderr.write(f"  {len(streams)} streams\n")

    out = sys.stdout
    out.write("#EXTM3U\n")
    for s in streams:
        name = (s.get("name") or "").replace('"', '\\"').replace("\n", " ")
        icon = (s.get("stream_icon") or "").replace('"', '\\"')
        cid = s.get("epg_channel_id") or ""
        cat_id = s.get("category_id") or ""
        cat_label = cat_name.get(cat_id, "")
        stream_id = s["stream_id"]
        attrs = f'tvg-id="{cid}" tvg-name="{name}" tvg-logo="{icon}" group-title="{cat_label}"'
        out.write(f"#EXTINF:-1 {attrs},{name}\n")
        out.write(f"{host}/live/{user}/{pwd}/{stream_id}.ts\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
