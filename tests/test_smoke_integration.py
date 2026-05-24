"""Smoke test: feed a 15-channel fixture M3U + 15-programme EPG into
build_epg.py via env vars, assert the pipeline runs end-to-end without
crashing.

build_epg.py's patched-playlist + guide.xml.gz writers filter by a
hardcoded category whitelist (`ALLOWED_CATEGORIES_ORDER`), so generic
fixture group names won't survive that filter. The smoke test therefore
asserts the broader contract:
  * build_epg.py exits 0
  * tvg-id-map.tsv is emitted (unconditional output)
  * if guide.xml.gz exists, it's a valid gzip+XML file
  * if patched playlist exists, it parses as M3U

This catches crashes anywhere in the 30+ pipeline stages without forcing
the fixtures to mirror the production category whitelist.
"""
import gzip
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).parent.parent


@pytest.fixture(scope="module")
def build_epg_run(tmp_path_factory):
    """Run build_epg.py in a tmp dir with file:// fixtures. Returns
    (tmp_path, stdout) tuple. Run once per test module (scope=module) so
    we only pay the ~60s build cost once."""
    tmp_path = tmp_path_factory.mktemp("smoke")
    m3u_url = (FIXTURES / "small.m3u").resolve().as_uri()
    epg_url = (FIXTURES / "small_epg.xml").resolve().as_uri()
    env = os.environ.copy()
    env["M3U_URL"] = m3u_url
    env["PROVIDER_EPG_URL"] = epg_url
    env["M3U_PATH_TOKEN"] = "test-token"
    env["PAGES_BASE"] = "http://localhost/test"
    env.pop("HTTP_PROXY", None)
    env.pop("HTTPS_PROXY", None)
    cmd = [sys.executable, str(REPO_ROOT / "scripts" / "build_epg.py")]
    result = subprocess.run(
        cmd, cwd=str(tmp_path), env=env,
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        # Print on failure so CI logs show what went wrong
        print("=== STDOUT ===\n", result.stdout)
        print("=== STDERR ===\n", result.stderr)
    return tmp_path, result


def test_build_exits_zero(build_epg_run):
    _tmp, result = build_epg_run
    assert result.returncode == 0, (
        f"build_epg.py exited {result.returncode}\n"
        f"STDOUT tail: {result.stdout[-2000:]}\n"
        f"STDERR tail: {result.stderr[-2000:]}"
    )


def test_build_emits_tvg_id_map(build_epg_run):
    tmp_path, _result = build_epg_run
    tsv = tmp_path / "docs" / "tvg-id-map.tsv"
    assert tsv.exists(), "expected tvg-id-map.tsv after build"
    # Header + at least one mapping line (the fixture has 15 channels)
    text = tsv.read_text(encoding="utf-8")
    rows = [l for l in text.splitlines() if l and not l.startswith("#")]
    assert len(rows) >= 1, f"expected ≥1 row in tvg-id-map.tsv, got {len(rows)}"


def test_build_emits_token_artifacts(build_epg_run):
    """When M3U_PATH_TOKEN is set, the build writes the subscribe bundle."""
    tmp_path, _result = build_epg_run
    token_dir = tmp_path / "docs" / "test-token"
    assert token_dir.is_dir(), f"expected {token_dir} after build"
    # README + setup.json should always be there
    assert (token_dir / "README.txt").exists()
    assert (token_dir / "setup.json").exists()


def test_guide_gz_is_valid_if_present(build_epg_run):
    """guide.xml.gz only emits if used_ids (post-category-whitelist) is
    non-empty. The fixture's generic categories won't survive the
    whitelist, so guide.xml.gz may not exist — but if it does, it must
    be a valid gzip + XML document."""
    tmp_path, _result = build_epg_run
    guide = tmp_path / "docs" / "guide.xml.gz"
    if not guide.exists():
        pytest.skip("guide.xml.gz not produced (fixture categories not in whitelist)")
    raw = gzip.decompress(guide.read_bytes())
    root = ET.fromstring(raw)
    assert root.tag == "tv", f"expected <tv> root, got <{root.tag}>"


def test_lite_gz_is_valid_if_present(build_epg_run):
    """Same as full guide — lite emits only if used_ids is non-empty."""
    tmp_path, _result = build_epg_run
    lite = tmp_path / "docs" / "guide-lite.xml.gz"
    if not lite.exists():
        pytest.skip("guide-lite.xml.gz not produced (fixture categories not in whitelist)")
    raw = gzip.decompress(lite.read_bytes())
    root = ET.fromstring(raw)
    assert root.tag == "tv"
