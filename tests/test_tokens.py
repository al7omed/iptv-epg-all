"""Unit tests for name_tokens() — token-set generator for fuzzy match."""
import pytest

from build_epg import name_tokens


def _tokens(s):
    return name_tokens(s)


@pytest.mark.parametrize("inp,expected_subset,not_expected", [
    # Basic tokenization
    ("BBC One", {"BBC", "ONE"}, {"HD"}),
    # Quality tokens dropped (HD/4K/FHD/HEVC/RAW/VIP etc.)
    ("beIN Sports 1 HD RAW", {"BEIN", "SPORTS", "1"}, {"HD", "RAW"}),
    ("BBC One 4K HEVC", {"BBC", "ONE"}, {"4K", "HEVC"}),
    # Language tokens KEPT as short codes
    ("beIN Sports 1 EN", {"BEIN", "SPORTS", "1", "EN"}, {}),
    # Decorative emoji + superscripts stripped
    ("SP⚽RTS 1", {"SPORTS", "1"}, {}),
    ("BBC Oneᴴᴰ", {"BBC", "ONE"}, {}),
    # Source prefix stripped
    ("SS: BBC One", {"BBC", "ONE"}, {"SS"}),
    # Empty input
    ("", set(), set()),
    # Stand-alone quality words = empty set (everything dropped)
    ("HEVC RAW HD", set(), {"HEVC", "RAW", "HD"}),
])
def test_name_tokens_subsets(inp, expected_subset, not_expected):
    toks = _tokens(inp)
    # Expected tokens should be present
    assert expected_subset.issubset(toks), \
        f"missing tokens in {inp!r}: expected {expected_subset}, got {toks}"
    # Dropped tokens should not appear
    for bad in not_expected:
        assert bad not in toks, f"unwanted token {bad!r} in {toks}"


def test_name_tokens_position_independent():
    """tokens are position-independent for matching."""
    a = name_tokens("beIN Sports EN 1")
    b = name_tokens("beIN 1 Sports English")
    # English -> EN by the language map, so 'EN' should be in both
    assert "EN" in a
    assert "EN" in b
    # Both should share {BEIN, SPORTS, 1}
    assert {"BEIN", "SPORTS", "1"}.issubset(a)
    assert {"BEIN", "SPORTS", "1"}.issubset(b)
