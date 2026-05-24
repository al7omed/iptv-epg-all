"""Unit tests for normalize_name() — the aggressive fuzzy-match normalizer."""
import pytest

from build_epg import normalize_name


@pytest.mark.parametrize("inp,expected", [
    ("", ""),
    (None, ""),
    ("BBC One", "BBCONE"),
    ("BBC One HD", "BBCONE"),
    ("bbc one fhd", "BBCONE"),
    # Source prefix stripped
    ("SS: BBC One", "BBCONE"),
    ("UK: BBC One", "BBCONE"),
    # Hash borders / decorations stripped
    ("### BBC ONE ###", "BBCONE"),
    # Unicode superscripts (ᴴᴰ / ᴿᴬᵂ) stripped
    ("BBC Oneᴴᴰ", "BBCONE"),
    # Parenthesized provider codes at end stripped
    ("Sky Sports F1 (S)", "SKYSPORTSF1"),
    # Quality tags stripped
    ("ESPN HD", "ESPN"),
    ("ESPN UHD", "ESPN"),
    # Language tokens RETAINED (they distinguish EN from AR variants)
    ("beIN Sports 1 EN", "BEINSPORTS1EN"),
    # Decorative emoji in middle of word
    ("SP⚽RTS 1", "SPORTS1"),
    # Punctuation collapsed
    ("FOX-NEWS!", "FOXNEWS"),
])
def test_normalize_name(inp, expected):
    assert normalize_name(inp) == expected
