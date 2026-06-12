"""Regression test: channels whose effective tvg-id contains an apostrophe
must receive programmes in the published guide.

Bug history (2026-06): the guide writer matched channel DEFS by their raw
dict key but programmes by their XML-escaped channel= attribute, and its
hand-rolled escape list didn't cover &#x27; (what html.escape(quote=True)
emits for '). Result: 12 live channels (e.g. "GO: AMERICA'S GOT TALENT")
shipped with a channel def and ZERO programmes — not even gap-fill dummies.
"""
import gzip
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent

# Real whitelisted category (generic fixture categories are filtered out of
# the playlist, and the guide only contains playlist channels).
UK_GENERAL = "UK| GENERAL ᴴᴰ/ᴿᴬᵂ"


@pytest.fixture(scope="module")
def apostrophe_build(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("apos")
    m3u = tmp_path / "fixture.m3u"
    m3u.write_text(
        "#EXTM3U\n"
        # No tvg-id -> effective id = tvg-name, which contains an apostrophe.
        f'#EXTINF:-1 tvg-id="" tvg-name="UK: BOB\'S BURGERS HD" '
        f'group-title="{UK_GENERAL}",UK: BOB\'S BURGERS HD\n'
        "http://example.com/s1\n"
        f'#EXTINF:-1 tvg-id="Plain.uk" tvg-name="UK: BBC ONE HD" '
        f'group-title="{UK_GENERAL}",UK: BBC ONE HD\n'
        "http://example.com/s2\n",
        encoding="utf-8",
    )
    epg = tmp_path / "fixture_epg.xml"
    epg.write_text(
        '<?xml version="1.0" encoding="UTF-8"?><tv>'
        '<channel id="Plain.uk"><display-name>BBC One</display-name></channel>'
        "</tv>",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["M3U_URL"] = m3u.resolve().as_uri()
    env["PROVIDER_EPG_URL"] = epg.resolve().as_uri()
    env["M3U_PATH_TOKEN"] = "test-token"
    env["ENRICH_EPG"] = "0"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "build_epg.py")],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        print("=== STDOUT ===\n", result.stdout)
        print("=== STDERR ===\n", result.stderr)
    return tmp_path, result


def test_build_exits_zero(apostrophe_build):
    _tmp, result = apostrophe_build
    assert result.returncode == 0


def test_apostrophe_channel_has_programmes(apostrophe_build):
    tmp_path, _ = apostrophe_build
    guide = tmp_path / "docs" / "guide.xml.gz"
    assert guide.exists(), "guide.xml.gz was not written"
    xml = gzip.open(guide).read().decode("utf-8")
    # The channel def must exist (apostrophe appears XML-escaped)...
    defs = re.findall(r'<channel id="(UK: BOB[^"]*)"', xml)
    assert defs, "apostrophe channel def missing from guide"
    cid = defs[0]
    # ...and it must have programmes (dummies at minimum — the guarantee
    # is that NO channel ships without EPG data).
    progs = re.findall(r'<programme[^>]*channel="%s"' % re.escape(cid), xml)
    assert len(progs) > 0, (
        f"apostrophe channel {cid!r} has a def but zero programmes "
        f"(escaping mismatch in the guide writer)"
    )
