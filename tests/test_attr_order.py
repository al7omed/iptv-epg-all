"""Regression test: programme start/stop/channel must be extracted
regardless of XML attribute ORDER.

epgshare sources disagree — AE1 emits `<programme start stop channel>`,
UK1 emits `<programme channel start stop>`. A fixed-order regex silently
dropped channel-first programmes in the gap-fill / overlap-dedup / lite
passes, which blanked every UK1-sourced channel (and any aliases.tsv pin
pointing at one). These extractors must match both forms.
"""

from build_epg import PROG_CHANNEL_RE, PROG_START_RE, PROG_STOP_RE

START_FIRST = (b'<programme start="20260613180000 +0000" '
               b'stop="20260613190000 +0000" channel="NatGeo.ae">'
               b'<title>Air Crash Investigation</title></programme>')
CHANNEL_FIRST = (b'<programme channel="U.and.Dave.HD.uk" '
                 b'start="20260613001000 +0000" stop="20260613005000 +0000">'
                 b'<title>Would I Lie To You?</title></programme>')


def _stc(p):
    ms, me, mc = (PROG_START_RE.search(p), PROG_STOP_RE.search(p),
                  PROG_CHANNEL_RE.search(p))
    assert ms and me and mc
    return ms.group(1), me.group(1), mc.group(1)


class TestAttributeOrder:
    def test_start_first(self):
        s, e, c = _stc(START_FIRST)
        assert s.startswith(b"20260613180000")
        assert e.startswith(b"20260613190000")
        assert c == b"NatGeo.ae"

    def test_channel_first(self):
        # The case that used to silently drop (UK1 / U& channels).
        s, e, c = _stc(CHANNEL_FIRST)
        assert s.startswith(b"20260613001000")
        assert e.startswith(b"20260613005000")
        assert c == b"U.and.Dave.HD.uk"

    def test_lite_window_slice_is_14_digits(self):
        # The lite filter compares the first 14 chars against a 14-digit
        # window bound; ensure the captured group starts with the digits.
        s, _, _ = _stc(CHANNEL_FIRST)
        assert s[:14] == b"20260613001000"
        assert s[:14].isdigit()
