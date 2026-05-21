#!/usr/bin/env python3
"""One-shot stream URL health check.

Reads M3U_URL_1 (and optionally M3U_URL_2 / Xtream creds for sub2), HEAD-probes
every stream URL with a short timeout, and writes channels/dead_channels.txt
with one effective_id per line for every URL that returned non-200 or timed
out. The next build_epg.py run reads this file and drops listed channels from
the patched M3U.

Run by manually triggering `.github/workflows/check-streams.yml` or locally:

    M3U_URL=... PROVIDER_EPG_URL=... python3 scripts/check_streams.py

Concurrency-limited to avoid rate-limiting the provider. The whole pass takes
~5-15 min for 7k streams.
"""
from __future__ import annotations

import concurrent.futures as cf
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Reuse helpers from build_epg in the same dir.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_epg import (  # noqa: E402
    parse_m3u, assign_effective_ids, fetch, ALLOWED_CATEGORIES_ORDER,
)

UA = "VLC/3.0.18 LibVLC/3.0.18"
TIMEOUT = 8
MAX_WORKERS = 16
DEAD_FILE = Path("channels/dead_channels.txt")


def check_one(stream_url: str) -> bool:
    """Return True if URL responds with 2xx within TIMEOUT seconds."""
    try:
        req = urllib.request.Request(stream_url, method="HEAD", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        # 4xx / 5xx — likely dead. But some streams refuse HEAD; try a small
        # ranged GET as a fallback before declaring dead.
        if e.code in (405, 501):
            try:
                req2 = urllib.request.Request(stream_url, headers={
                    "User-Agent": UA, "Range": "bytes=0-1024",
                })
                with urllib.request.urlopen(req2, timeout=TIMEOUT) as resp2:
                    return resp2.status in (200, 206)
            except Exception:
                return False
        return False
    except Exception:
        return False


def main() -> int:
    m3u_urls = [u.strip() for u in (os.environ.get("M3U_URL") or "").split(",") if u.strip()]
    if not m3u_urls:
        print("ERROR: M3U_URL env var required", file=sys.stderr)
        return 2
    workdir = Path("epg-work")
    workdir.mkdir(exist_ok=True)
    DEAD_FILE.parent.mkdir(parents=True, exist_ok=True)

    all_channels = []
    for idx, url in enumerate(m3u_urls):
        path = workdir / f"check_m3u_{idx}.m3u"
        try:
            fetch(url, path)
            all_channels.extend(parse_m3u(path.read_text(encoding="utf-8", errors="replace")))
        except Exception as e:
            print(f"FAIL source[{idx}]: {e}", file=sys.stderr)
    assign_effective_ids(all_channels)

    # Filter to channels whose category is in our published list — no point
    # probing streams we don't expose.
    allowed = set(ALLOWED_CATEGORIES_ORDER)
    to_check = [
        ch for ch in all_channels
        if ch.get("group", "") in allowed and ch.get("url_line")
    ]
    print(f"channels in published categories: {len(to_check)}")

    dead = []
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futs = {pool.submit(check_one, ch["url_line"]): ch for ch in to_check}
        done = 0
        for fut in cf.as_completed(futs):
            ch = futs[fut]
            done += 1
            try:
                ok = fut.result()
            except Exception:
                ok = False
            if not ok:
                dead.append(ch["effective_id"])
            if done % 500 == 0:
                elapsed = int(time.time() - t0)
                print(f"  progress {done}/{len(to_check)}, dead so far: {len(dead)} ({elapsed}s)")

    dead_unique = sorted(set(dead))
    DEAD_FILE.write_text(
        "# Channels that failed HEAD probe. effective_id per line.\n"
        f"# Generated: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} UTC\n"
        f"# {len(dead_unique)} dead out of {len(to_check)} checked.\n"
        + "\n".join(dead_unique) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {DEAD_FILE} with {len(dead_unique)} dead channels (of {len(to_check)} checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
