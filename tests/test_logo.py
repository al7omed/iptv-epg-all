"""Tests for logo sanitization: animated-GIF rewrites/stripping and the
broken-URL guard (scripts/build_epg.sanitize_logo)."""

from build_epg import LOGO_URL_REWRITES, sanitize_logo


class TestAnimatedFlagRewrite:
    def test_usa_and_uk_flags_become_static(self):
        usa = sanitize_logo("https://upload.wikimedia.org/wikipedia/commons/"
                            "4/42/Animated-Flag-USA.gif")
        uk = sanitize_logo("https://upload.wikimedia.org/wikipedia/commons/"
                           "2/2d/Animated-Flag-United-Kingdom.gif")
        assert usa.endswith("Flag_of_the_United_States.svg?width=512")
        assert uk.endswith("Flag_of_the_United_Kingdom.svg?width=512")
        assert ".gif" not in usa and ".gif" not in uk

    def test_all_flag_rewrites_are_static_images(self):
        for needle, repl in LOGO_URL_REWRITES.items():
            assert not repl.lower().endswith(".gif"), needle
            assert repl.startswith("http")


class TestBrandGifRewrite:
    def test_bein_gif_to_provider_logo(self):
        out = sanitize_logo("https://i.ibb.co/QjQY3M2P/Bein-Sports-GIF.gif")
        assert out.endswith("12782.png")

    def test_dazn_osn_to_static(self):
        assert sanitize_logo(
            "https://i.ibb.co/vCPR3CSF/DAZN-Logo-Gif.gif").endswith("12983.png")
        assert sanitize_logo(
            "https://i.ibb.co/tTTQLnP6/OSN.gif").endswith("6623.jpg")


class TestUnmappedGifStripped:
    def test_unknown_gif_blanked(self):
        # No clean static known -> blank, so the player shows its default
        # icon rather than a frozen GIF frame.
        assert sanitize_logo(
            "https://cdn.pixabay.com/animation/x/16-43-28-59_512.gif") == ""
        assert sanitize_logo(
            "https://i.postimg.cc/x/UEFA-Champions-League-GIF.gif") == ""

    def test_static_logo_untouched(self):
        url = "http://photo-tmdb.com/stalker_portal/misc/logos/320/12782.png?95032"
        assert sanitize_logo(url) == url

    def test_png_with_gif_in_path_segment_kept(self):
        # Only a real .gif extension is stripped, not 'gif' inside a name.
        url = "http://host/logos/gifford-tv.png"
        assert sanitize_logo(url) == url


class TestBrokenStillStripped:
    def test_empty_and_placeholder(self):
        assert sanitize_logo("") == ""
        assert sanitize_logo("http://host/null.png") == ""
        assert sanitize_logo("https://host") == ""
