"""Tests for scripts/audit_epg_bindings.py — the offline binding auditor
that flags suspicious channel→EPG bindings from the provenance artifact."""
import gzip

from audit_epg_bindings import donor_tokens, flag_rows, load_audit


def _row(effective_id, display_name, tier="direct", source="provider",
         donor="", count=10, fp="aaaa111111"):
    return {
        "effective_id": effective_id, "display_name": display_name,
        "match_tier": tier, "source": source, "donor_cid": donor,
        "real_prog_count": str(count), "prog_fp": fp, "sample_titles": "",
    }


class TestDonorTokens:
    def test_dotted_id(self):
        assert {"ABU", "DHABI"} <= donor_tokens("Abu.Dhabi.HD.ae")

    def test_camel_case_id(self):
        assert {"NAT", "GEO", "WILD"} <= donor_tokens("NatGeoWild.uk")

    def test_hyphenated_id(self):
        assert {"BEIN", "SPORTS", "1"} <= donor_tokens("bein-sports-1")


class TestDupFeedFlag:
    def test_different_brand_same_feed_flagged(self):
        rows = [
            _row("MbcFm.sa", "AR: MBC FM", fp="feed01"),
            _row("MbcPanorama.sa", "AR: MBC PANORAMA FM",
                 tier="token", donor="MbcFm.sa", fp="feed01"),
        ]
        flags = flag_rows(rows, {}, None)
        assert any(f["flag"] == "dup-feed" for f in flags)

    def test_quality_variants_not_flagged(self):
        # Same channel in 4K and HD shares one feed — same identity tokens
        # after quality-tag stripping, so never a dup-feed conflict.
        rows = [
            _row("NatGeo.uk", "UK: NAT GEO 4K", fp="feed02"),
            _row("NatGeo.uk.hd", "UK: NAT GEO HD", fp="feed02"),
        ]
        flags = flag_rows(rows, {}, None)
        assert not any(f["flag"] == "dup-feed" for f in flags)

    def test_brand_abbreviation_spellings_not_flagged(self):
        # 'NAT GEO' and 'NATIONAL GEOGRAPHIC' (and the provider typo
        # 'NATIOANL') are one identity — a shared feed is correct.
        rows = [
            _row("NatGeo.us", "US: NAT GEO HD", fp="feed05"),
            _row("NatGeoCh.us", "GO: NATIONAL GEOGRAPHIC CHANNEL",
                 tier="token", donor="NatGeo.us", fp="feed05"),
            _row("NationalGeographicAbuDhabi.ae",
                 "AR: Abu Dhabi Natioanl Geo 4K",
                 tier="alias", donor="Nat.Geo.Abu.Dhabi.HD.ae", fp="feed06"),
            _row("NatGeoAD2.ae", "X: National Geographic Abu Dhabi 4K",
                 tier="token", donor="NationalGeographicAbuDhabi.ae",
                 fp="feed06"),
        ]
        flags = flag_rows(rows, {}, None)
        assert not any(f["flag"] == "dup-feed" for f in flags)

    def test_same_callsign_not_flagged(self):
        # 'FOX (KDFW)' and 'FOX 4 (KDFW) DALLAS HD' are the same US station
        # under two naming styles — a shared feed is correct, the digit
        # difference is not a conflict.
        rows = [
            _row("Kdfw.us", "US: FOX (KDFW)", fp="feed03"),
            _row("Kdfw4.us", "US: FOX 4 (KDFW) DALLAS HD",
                 tier="callsign", donor="Kdfw.us", fp="feed03"),
        ]
        flags = flag_rows(rows, {}, None)
        assert not any(f["flag"] == "dup-feed" for f in flags)

    def test_different_callsigns_flagged(self):
        # Two different stations (Portland ME vs Portland OR) sharing one
        # feed is the wrong-city class — must flag.
        rows = [
            _row("Wgme.us", "US: CBS (WGME) PORTLAND MAINE", fp="feed04"),
            _row("Koin.us", "US: CBS 6 (KOIN) PORTLAND HD",
                 tier="token", donor="Wgme.us", fp="feed04"),
        ]
        flags = flag_rows(rows, {}, None)
        assert any(f["flag"] == "dup-feed" for f in flags)


class TestBrandMismatchFlag:
    def test_nat_geo_bound_to_general_channel(self):
        rows = [_row("NationalGeographicAbuDhabi.ae",
                     "AR: Abu Dhabi Natioanl Geo 4K",
                     tier="token", donor="Abu.Dhabi.HD.ae")]
        flags = flag_rows(rows, {}, None)
        assert any(f["flag"] == "brand-mismatch" for f in flags)

    def test_matching_donor_not_flagged(self):
        rows = [_row("NatGeoWild.uk", "UK: NAT GEO WILD HD",
                     tier="token", donor="NatGeoWild.uk")]
        flags = flag_rows(rows, {}, None)
        assert not any(f["flag"] == "brand-mismatch" for f in flags)

    def test_direct_tier_skipped(self):
        # Direct-id bindings have no donor mismatch by construction.
        rows = [_row("NatGeo.uk", "UK: NAT GEO HD", tier="direct",
                     donor="NatGeo.uk")]
        flags = flag_rows(rows, {}, None)
        assert not any(f["flag"] == "brand-mismatch" for f in flags)


class TestGenreConflictFlag:
    def test_doc_channel_with_news_titles(self):
        rows = [_row("NationalGeographicAbuDhabi.ae",
                     "AR: Abu Dhabi Natioanl Geo HD")]
        titles = {"NationalGeographicAbuDhabi.ae": [
            "Home News Tonight", "Points Of View", "Evening News Bulletin",
            "Morning Show Live", "Weather Update", "My Home My Destiny",
        ]}
        flags = flag_rows(rows, titles, None)
        assert any(f["flag"] == "genre-conflict" for f in flags)

    def test_doc_channel_with_doc_titles_clean(self):
        rows = [_row("NatGeo.uk", "UK: NAT GEO HD")]
        titles = {"NatGeo.uk": [
            "Air Crash Investigation", "Ice Road Rescue",
            "Wonders Of The Ocean", "Meet The Chimps", "Dog: Impossible",
        ]}
        flags = flag_rows(rows, titles, None)
        assert not any(f["flag"] == "genre-conflict" for f in flags)


class TestLoadAudit:
    def test_roundtrip(self, tmp_path):
        p = tmp_path / "epg-audit.tsv"
        p.write_text(
            "effective_id\tdisplay_name\tmatch_tier\tsource\tdonor_cid\t"
            "real_prog_count\tprog_fp\tsample_titles\n"
            "NatGeo.uk\tUK: NAT GEO HD\tdirect\tepgshare-UK1\tNatGeo.uk\t"
            "42\tabc123def0\tAir Crash Investigation | Ice Road Rescue\n",
            encoding="utf-8")
        rows = load_audit(p)
        assert len(rows) == 1
        assert rows[0]["effective_id"] == "NatGeo.uk"
        assert rows[0]["real_prog_count"] == "42"


class TestGuideTitles:
    def test_dummy_blocks_skipped(self, tmp_path):
        from audit_epg_bindings import guide_titles_by_cid
        xml = (
            b'<?xml version="1.0"?><tv>'
            b'<programme start="20260612000000 +0000" channel="A.uk">'
            b'<title lang="en">Real Show</title><desc>x</desc></programme>'
            b'<programme start="20260612040000 +0000" channel="A.uk">'
            b'<title lang="en">A</title>'
            b'<desc>A \xe2\x80\x94 live channel. Programme guide unavailable.</desc>'
            b'</programme></tv>'
        )
        p = tmp_path / "guide.xml.gz"
        p.write_bytes(gzip.compress(xml))
        titles = guide_titles_by_cid(p)
        assert titles["A.uk"] == ["Real Show"]
