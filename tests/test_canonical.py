"""Unit tests for canonical_channel_name() — the dedup-key generator."""
import pytest

from build_epg import canonical_channel_name


@pytest.mark.parametrize("inp,expected", [
    # Bracketed source tags stripped
    ("beIN Sports 1 [SS]", "bein sports 1"),
    ("beIN Sports 1 [NM]", "bein sports 1"),
    # Quality tags stripped
    ("BBC One HD", "bbc one"),
    ("Sky Sports Premier League 4K", "sky sports premier league"),
    ("ESPN HEVC", "espn"),
    ("ESPN HEVC 1080p", "espn"),
    # 'Hub' prefix stripped
    ("Hub beIN Sports 1", "bein sports 1"),
    # Zero-padded numbers normalized
    ("beIN Sports 01", "bein sports 1"),
    ("Channel 04", "channel 4"),
    # Parenthesized region/event qualifiers KEPT
    ("ESPN (East)", "espn (east)"),
    ("Sky Sports F1 (Event Only)", "sky sports f1 (event only)"),
    # Orphan ampersands stripped
    ("Sky Sports F1 4K & 3840p", "sky sports f1"),
    ("Discovery & Science HD", "discovery science"),
    # Trailing punctuation stripped
    ("MTV |", "mtv"),
    # Lowercased
    ("FOX NEWS", "fox news"),
])
def test_canonical_channel_name(inp, expected):
    assert canonical_channel_name(inp) == expected
