#!/usr/bin/env python3
"""POST the latest channel-rename diff to Discord/Slack webhook.

build_epg.py maintains an append-only `channels/name_changes.log` recording
every channel rename detected across builds (provider switched quality
variant, swapped languages, etc.). When the RENAME_WEBHOOK_URL secret is
set, this script reads the MOST RECENT block from the log and POSTs it
to that webhook. The format is detected from the URL:

  https://discord.com/api/webhooks/...        → Discord embed payload
  https://hooks.slack.com/services/...        → Slack incoming-webhook
  anything else                               → generic JSON POST

Run this script AFTER build_epg.py has finished (the log is updated
mid-build), and ONLY if the webhook secret is configured. Silent no-op
otherwise.

Usage (from workflow):
  RENAME_WEBHOOK_URL=... python3 scripts/notify_renames.py

Exit codes:
  0 — posted successfully, or no new renames, or webhook unset (no-op)
  1 — HTTP error from the webhook server
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

LOG_PATH = Path("channels/name_changes.log")
MAX_BODY_CHARS = 3500  # below both Discord (4096) + Slack (~4k) message caps


def latest_block(text: str) -> str:
    """Return the LAST '=== ... ===' block from the log (newest entry)."""
    # Find every '=== <ts> ===' header line; the latest is the last one.
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith("=== ") and line.endswith(" ==="):
            if current:
                blocks.append(current)
            current = [line]
        else:
            if current:  # only collect lines after we've seen a header
                current.append(line)
    if current:
        blocks.append(current)
    if not blocks:
        return ""
    return "\n".join(blocks[-1]).rstrip()


def truncate_body(body: str, limit: int = MAX_BODY_CHARS) -> str:
    if len(body) <= limit:
        return body
    return body[:limit - 60].rstrip() + "\n  ... (truncated — see channels/name_changes.log)"


def post_discord(url: str, body: str) -> bool:
    """Discord embed POST. Returns True on 2xx."""
    payload = {
        "username": "iptv-epg-all",
        "embeds": [{
            "title": "Channel renames detected",
            "description": "```\n" + body + "\n```",
            "color": 0x5865F2,
        }],
    }
    return _post_json(url, payload)


def post_slack(url: str, body: str) -> bool:
    """Slack incoming-webhook POST. Returns True on 2xx."""
    payload = {
        "text": "*Channel renames detected*\n```\n" + body + "\n```",
        "username": "iptv-epg-all",
    }
    return _post_json(url, payload)


def post_generic(url: str, body: str) -> bool:
    return _post_json(url, {"text": body, "source": "iptv-epg-all"})


def _post_json(url: str, payload: dict) -> bool:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json",
                  "User-Agent": "iptv-epg-all/notify_renames"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            ok = 200 <= resp.status < 300
            print(f"notify_renames: POST {url[:60]}... -> {resp.status}",
                  file=sys.stderr)
            return ok
    except urllib.error.HTTPError as e:
        print(f"notify_renames: HTTP {e.code} {e.reason}", file=sys.stderr)
        return False
    except urllib.error.URLError as e:
        print(f"notify_renames: URL error {e.reason}", file=sys.stderr)
        return False
    except OSError as e:
        print(f"notify_renames: OS error {e}", file=sys.stderr)
        return False


def main() -> int:
    url = os.environ.get("RENAME_WEBHOOK_URL", "").strip()
    if not url:
        print("notify_renames: RENAME_WEBHOOK_URL not set — no-op", file=sys.stderr)
        return 0
    if not LOG_PATH.exists():
        print(f"notify_renames: {LOG_PATH} doesn't exist — no-op", file=sys.stderr)
        return 0
    text = LOG_PATH.read_text(encoding="utf-8", errors="replace")
    block = latest_block(text)
    if not block:
        print("notify_renames: no log blocks found — no-op", file=sys.stderr)
        return 0

    # State guard: only POST if this block is different from the last one
    # we already notified about. The "last-notified" marker is a small file
    # outside `channels/` so the build's commit step doesn't touch it.
    marker = Path(".epg-build-state/notify_renames.last")
    marker.parent.mkdir(parents=True, exist_ok=True)
    header_line = block.splitlines()[0] if block else ""
    if marker.exists():
        try:
            last_seen = marker.read_text(encoding="utf-8").strip()
            if last_seen == header_line:
                print(f"notify_renames: already notified about {header_line!r} — no-op",
                      file=sys.stderr)
                return 0
        except OSError:
            pass

    body = truncate_body(block)
    if "discord.com/api/webhooks" in url:
        ok = post_discord(url, body)
    elif "hooks.slack.com" in url:
        ok = post_slack(url, body)
    else:
        ok = post_generic(url, body)

    if ok:
        try:
            marker.write_text(header_line, encoding="utf-8")
        except OSError as e:
            print(f"notify_renames: warning, couldn't write marker: {e}", file=sys.stderr)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
