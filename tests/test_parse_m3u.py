"""Unit tests for parse_m3u() — the M3U EXTINF/url parser."""
from build_epg import parse_m3u


SAMPLE = """#EXTM3U
#EXTINF:-1 tvg-id="bbc1.uk" tvg-name="BBC One HD" tvg-logo="http://x/bbc1.png" group-title="UK General",BBC One HD
http://example.com/stream/bbc1.ts
#EXTINF:-1 tvg-id="espn.us" tvg-name="ESPN" group-title="US Sports",ESPN
http://example.com/stream/espn.ts
#EXTINF:-1 group-title="No-ID Channel",Random Channel
http://example.com/stream/random.ts
"""


def test_parse_m3u_basic():
    entries = parse_m3u(SAMPLE)
    assert len(entries) == 3


def test_parse_m3u_attrs():
    entries = parse_m3u(SAMPLE)
    bbc = entries[0]
    assert bbc["tvg_id"] == "bbc1.uk"
    assert bbc["tvg_name"] == "BBC One HD"
    assert bbc["group"] == "UK General"
    assert bbc["title"] == "BBC One HD"
    assert bbc["url_line"] == "http://example.com/stream/bbc1.ts"


def test_parse_m3u_no_tvg_id():
    entries = parse_m3u(SAMPLE)
    random = entries[2]
    assert random["tvg_id"] == ""
    assert random["title"] == "Random Channel"
    assert random["group"] == "No-ID Channel"


def test_parse_m3u_empty_returns_empty():
    assert parse_m3u("") == []
    assert parse_m3u("#EXTM3U\n") == []


def test_parse_m3u_orphan_extinf_uses_next_url():
    """When two EXTINFs share a URL line, the parser walks the first EXTINF
    forward to the URL and treats the intermediate EXTINF as part of its
    block. (Documented behaviour — playlist authors shouldn't emit this
    shape, but if they do we keep it parseable.)"""
    text = "#EXTM3U\n#EXTINF:-1,Orphan\n#EXTINF:-1,Real\nhttp://x/y.ts\n"
    entries = parse_m3u(text)
    assert len(entries) == 1
    # First EXTINF wins the pairing
    assert entries[0]["title"] == "Orphan"
    assert entries[0]["url_line"] == "http://x/y.ts"
