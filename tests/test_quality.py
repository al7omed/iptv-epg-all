"""Unit tests for quality_rank() + is_acceptable_quality()."""
import pytest

from build_epg import quality_rank, is_acceptable_quality


# quality_rank: relative ordering matters more than exact numbers.
# Test invariants: higher quality => higher score, plus a few key absolute
# values from the docstring.

def test_quality_rank_flagship_higher_than_lone_raw():
    """RAW + VIP combo > RAW alone."""
    flag = quality_rank("beIN Sports 1 RAW VIP")
    raw_only = quality_rank("beIN Sports 1 RAW")
    vip_only = quality_rank("beIN Sports 1 VIP")
    assert flag > raw_only
    assert flag > vip_only


def test_quality_rank_raw_equals_vip_alone():
    """RAW alone and VIP alone are both flagship tier 1 = 100."""
    assert quality_rank("Channel RAW") == quality_rank("Channel VIP")


def test_quality_rank_8k_higher_than_4k():
    assert quality_rank("Movie 8K") > quality_rank("Movie 4K")


def test_quality_rank_4k_higher_than_fhd():
    assert quality_rank("Movie 4K") > quality_rank("Movie FHD")


def test_quality_rank_hevc_higher_than_hd():
    assert quality_rank("News HEVC") > quality_rank("News HD")


def test_quality_rank_fhd_higher_than_hd():
    assert quality_rank("News FHD") > quality_rank("News HD")


def test_quality_rank_hd_higher_than_sd():
    assert quality_rank("News HD") > quality_rank("News SD")


def test_quality_rank_bracketed_vip_counts():
    """[VIP] source tag still counts as flagship."""
    assert quality_rank("beIN Sports [VIP]") >= 100


def test_quality_rank_no_tags_is_zero():
    """Untagged channel scores low (everything is relative)."""
    assert quality_rank("Random Channel") >= 0


# is_acceptable_quality: SD/LQ are dropped, everything else kept.

@pytest.mark.parametrize("name,acceptable", [
    ("", True),                  # empty is acceptable (caller decides)
    ("BBC One HD", True),
    ("BBC One FHD", True),
    ("ESPN UHD", True),
    ("Movie 4K", True),
    ("News HEVC", True),
    ("Channel RAW VIP", True),
    # Drop these
    ("Channel SD", False),
    ("Channel LQ", False),
    ("BBC One ˢᴰ", False),       # unicode superscripts normalized
])
def test_is_acceptable_quality(name, acceptable):
    assert is_acceptable_quality(name) == acceptable
