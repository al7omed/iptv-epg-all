"""Unit tests for clean_channel_name() — the display-name prettifier."""
import pytest

from build_epg import clean_channel_name


@pytest.mark.parametrize("inp,expected", [
    ("", ""),
    # Source prefix preserved as [SS] suffix
    ("SS: BBC One", "BBC One [SS]"),
    ("NM: ESPN", "ESPN [NM]"),
    # No source prefix → no suffix added
    ("BBC One HD", "BBC One HD"),
    # Single-letter parens stripped
    ("ESPN (H)", "ESPN"),
    ("FOX (D)", "FOX"),
    # SP RTS leftover restored
    ("BBC SP RTS", "BBC Sports"),
    # Smart title case
    ("bbc one", "BBC One"),
    # Trailing punctuation stripped (smart-case may lowercase acronyms)
    ("CNN :", "Cnn"),
    # Multiple spaces collapsed
    ("BBC    One", "BBC One"),
])
def test_clean_channel_name(inp, expected):
    assert clean_channel_name(inp) == expected
