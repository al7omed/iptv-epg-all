"""Unit tests for the EPG-correctness heuristics added 2026-06:
degenerate shared tvg-ids, TimeShift junk filler, and the non-English
language scoring used by the backfill/rescue guards and the
wrong-language binding scrub."""
import pytest

from build_epg import (
    JUNK_PROG_TITLE_RE,
    find_degenerate_tvg_ids,
    non_english_title_ratio,
)


def _ch(tvg_id, name):
    return {"tvg_id": tvg_id, "tvg_name": name, "title": name}


class TestFindDegenerateTvgIds:
    def test_flag_id_shared_by_unrelated_channels(self):
        # Mirrors the real 'TS' case: one id stamped on totally unrelated
        # channels (provider timeshift/catch-up flag).
        names = [
            "UK: BBC 3 4K", "UK: ITV LONDON 4K", "UK: FILM 4 4K",
            "UK: MORE 4 4K", "AR: 2M Monde", "AFR: Sahel TV",
            "UK: FOOD NETWORK +1", "UK: HGTV +1", "US: DAYSTAR HD",
            "UK: PARAMOUNT HD", "National Geographic +1 [UK]",
            "UK: BBC Earth HD",
        ]
        chans = [_ch("TS", n) for n in names]
        assert find_degenerate_tvg_ids(chans) == {"TS"}

    def test_keep_quality_variant_group(self):
        # SkySportsCricket.uk shared by 14 variants of the SAME channel —
        # a brand token (SKY) is common to all, so the id is legitimate.
        names = [
            "UK: SKY SPORTS CRICKET HD", "UK: SKY SPORTS CRICKET HEVC 4K",
            "UK: SKY SPORTS CRICKET HEVC HD", "UK: SKY SPORTS CRICKET SD",
            "SKYGO: SKY SPORT CRICKET 4K", "SKYGO: SKY SPORT CRICKET HD",
            "UK: SKY SPORTS CRICKET RAW", "UK: SKY SPORTS CRICKET VIP",
            "UK: SKY SPORTS CRICKET FHD", "UK: SKY SPORTS CRICKET UHD",
            "UK: SKY SPORTS CRICKET 8K", "UK: SKY SPORTS CRICKET",
        ]
        chans = [_ch("SkySportsCricket.uk", n) for n in names]
        assert find_degenerate_tvg_ids(chans) == set()

    def test_small_groups_never_flagged(self):
        # <10 entries sharing an id is normal (quality variants).
        chans = [_ch("NatGeo.uk", n) for n in
                 ["National Geo HEVC 4K [UK]", "National Geographic 4K [UK]",
                  "National Geo HEVC HD [UK]"]]
        assert find_degenerate_tvg_ids(chans) == set()

    def test_empty_tvg_ids_ignored(self):
        chans = [_ch("", f"Channel {i}") for i in range(20)]
        assert find_degenerate_tvg_ids(chans) == set()


class TestJunkProgTitleRe:
    @pytest.mark.parametrize("block", [
        b'<programme channel="TS"><title>TimeShift 13</title>'
        b'<desc>TimeShift For Time 13 </desc></programme>',
        b'<programme channel="x"><title lang="en">timeshift 7</title></programme>',
        b'<programme channel="x"><title> Time Shift 22</title></programme>',
    ])
    def test_matches_filler(self, block):
        assert JUNK_PROG_TITLE_RE.search(block)

    @pytest.mark.parametrize("block", [
        # Real programmes about time travel must NOT be dropped.
        b'<programme channel="x"><title>The Time Shifters</title></programme>',
        b'<programme channel="x"><title>Doctor Who</title></programme>',
        # 'TimeShift' only in desc (not title) is not the filler pattern.
        b'<programme channel="x"><title>News</title><desc>TimeShift</desc></programme>',
    ])
    def test_keeps_real_programmes(self, block):
        assert not JUNK_PROG_TITLE_RE.search(block)


def _prog(title: str) -> bytes:
    return ('<programme channel="x"><title>%s</title></programme>' % title).encode("utf-8")


class TestNonEnglishTitleRatio:
    def test_english_feed_scores_low(self):
        progs = [_prog(t) for t in [
            "World's Deadliest", "Air Crash Investigation", "Drain the Oceans",
            "Vikings: The Rise and Fall", "Hunt for the Giant Squid",
            "Savage Kingdom", "Yukon Vet", "To Catch a Smuggler",
        ]]
        assert non_english_title_ratio(progs) < 0.3

    def test_spanish_feed_scores_high(self):
        # Real titles observed on the wrongly-bound Spanish Nat Geo feed.
        progs = [_prog(t) for t in [
            "Hermanos leones: de cachorros a reyes",
            "Jaguar: icono de la jungla de Guyana",
            "Felinos insólitos", "Safari letal",
            "Los secretos de la selva", "El reino del puma",
        ]]
        assert non_english_title_ratio(progs) >= 0.45

    def test_arabic_feed_scores_high_unless_latin_only(self):
        progs = [_prog(t) for t in ["مسلسل الليل", "برنامج الصباح", "نشرة الأخبار", "فيلم السهرة"]]
        assert non_english_title_ratio(progs) == 1.0
        assert non_english_title_ratio(progs, latin_only=True) == 0.0

    def test_occasional_accent_tolerated(self):
        # One Pokémon in an otherwise-English feed must not flag the channel.
        progs = [_prog(t) for t in [
            "Pokémon Horizons", "Breaking News", "Match of the Day",
            "The Chase", "Countdown", "News at Ten", "Question Time",
            "Top Gear",
        ]]
        assert non_english_title_ratio(progs) < 0.3

    def test_empty_input(self):
        assert non_english_title_ratio([]) == 0.0


from build_epg import strip_non_latin_id


class TestStripNonLatinId:
    def test_arabic_removed_from_id(self):
        assert strip_non_latin_id('#### DISCOVERY+ ديسكفري #####') == '#### DISCOVERY+ #####'

    def test_plain_ascii_untouched(self):
        assert strip_non_latin_id('SkySportsF1.uk') == 'SkySportsF1.uk'
        assert strip_non_latin_id('UK: BBC ONE HD') == 'UK: BBC ONE HD'

    def test_pure_arabic_becomes_empty(self):
        assert strip_non_latin_id('قناة العربية') == ''

    def test_empty_input(self):
        assert strip_non_latin_id('') == ''


class TestSwedishDetection:
    def test_swedish_feed_scores_high(self):
        # Real titles from the wrongly-bound Swedish Nat Geo Wild feed.
        progs = [_prog(t) for t in [
            "Den otroliga dr Pol", "Det vilda Filippinerna",
            "Det vilda Taiwan - Djungelön", "En tonårsleopards dagbok",
            "Kattkrig: Lejon vs gepard", "Komododrakarna",
            "Lejonbröder: Från ungar till kungar", "Världens största vithaj",
        ]]
        assert non_english_title_ratio(progs) >= 0.4

    def test_english_with_den_and_till_not_flagged(self):
        # 'Den'/'Till' appear in English titles too — the channel-level
        # ratio must absorb the occasional hit.
        progs = [_prog(t) for t in [
            "Den of Thieves", "Till Death Us Do Part", "Match of the Day",
            "The Chase", "News at Ten", "Top Gear", "Question Time",
            "Countdown", "The One Show", "Doctor Who",
        ]]
        assert non_english_title_ratio(progs) < 0.25


from build_epg import token_leftover_ok, MAX_CLONE_NORMS_PER_SOURCE


class TestTokenLeftoverOk:
    def test_digit_leftover_rejected(self):
        # 'BBC 3' must never take generic 'BBC' data
        assert not token_leftover_ok(frozenset({"BBC", "3"}), frozenset({"BBC"}))
        # 'beIN SPORTS 2' must never take generic 'beIN SPORTS'
        assert not token_leftover_ok(
            frozenset({"BEIN", "SPORTS", "2"}), frozenset({"BEIN", "SPORTS"}))

    def test_exact_and_nondigit_leftovers_allowed(self):
        assert token_leftover_ok(
            frozenset({"SKY", "CINEMA", "ACTION"}), frozenset({"SKY", "CINEMA", "ACTION"}))
        # City-name leftover allowed here — the per-source clone cap bounds it
        assert token_leftover_ok(
            frozenset({"SPECTRUM", "NEWS", "MILWAUKEE"}), frozenset({"SPECTRUM", "NEWS"}))

    def test_clone_cap_is_small(self):
        assert 2 <= MAX_CLONE_NORMS_PER_SOURCE <= 8


from build_epg import BRAND_TOKENS, name_tokens  # noqa: E402


class TestFinnishDutchDetection:
    def test_finnish_feed_scores_high(self):
        # Observed live: 'FI: EUROSPORT 1' shipped Finnish titles — Finnish
        # has few accents in short titles and slipped the older stopword set.
        progs = [b"<programme><title>Motocross: MM-sarja Latvia</title></programme>",
                 b"<programme><title>Tennis: ATP-sarja, 1. jakso</title></programme>",
                 b"<programme><title>Snooker: MM 2026, kausi 3</title></programme>",
                 b"<programme><title>Ravit: Toto76 suorana</title></programme>"]
        assert non_english_title_ratio(progs) >= 0.4

    def test_dutch_feed_scores_high(self):
        progs = [b"<programme><title>Het Klokhuis</title></programme>",
                 b"<programme><title>Een huis vol</title></programme>",
                 b"<programme><title>Wie is de Mol? Aflevering 4</title></programme>",
                 b"<programme><title>De slimste mens</title></programme>"]
        assert non_english_title_ratio(progs) >= 0.4

    def test_english_sports_titles_unaffected(self):
        progs = [b"<programme><title>Motocross World Championship: Latvia</title></programme>",
                 b"<programme><title>Live: Premier League Darts</title></programme>",
                 b"<programme><title>Season 3 Episode 4</title></programme>",
                 b"<programme><title>The Open Championship</title></programme>"]
        assert non_english_title_ratio(progs) < 0.25

    def test_matchup_listings_with_foreign_names_unaffected(self):
        # Real titles from beIN Sports 2 English that scored 0.57 before the
        # matchup exemption — 'De Minaur' hit the 'de' stopword, 'Libéma'
        # the accent rule. English channel, must never be scrubbed.
        progs = [
            "<programme><title>Benjamin Bonzi (FRA) vs Alex De Minaur (AUS)  -  ATP 250 Libéma Open 2026</title></programme>".encode(),
            "<programme><title>Flavio Cobolli (ITA) vs Alexander Zverev (GER)  -  Men's Singles Final</title></programme>".encode(),
            "<programme><title>Zhizhen Zhang (CHN) vs Adrian Mannarino (FRA)</title></programme>".encode(),
            "<programme><title>Felix Auger - Aliassime (CAN) vs Kamil Majchrzak (POL)</title></programme>".encode(),
        ]
        assert non_english_title_ratio(progs, latin_only=True) < 0.25


from build_epg import callsign_id_contradiction  # noqa: E402


class TestCallsignIdContradiction:
    def test_wrong_station_id_neutralized(self):
        # Real provider data: Honolulu's id stamped on the Hartford channel,
        # Oregon's id on the Maine channel, Grand Junction's on Goldsboro.
        assert callsign_id_contradiction("KHON.us", "US: FOX HARTFORD (WTIC)")
        assert callsign_id_contradiction("KOIN.us", "US: CBS (WGME) PORTLAND MAINE")
        assert callsign_id_contradiction("KREX.us", "US: CBS (WNCN) GOLDSBORO NORTH CAROLINA")

    def test_matching_callsign_kept(self):
        assert not callsign_id_contradiction("KHON.us", "US: FOX 2 (KHON) HONOLULU HD")
        assert not callsign_id_contradiction("WGME.us", "US: CBS 13 (WGME) PORTLAND HD")

    def test_non_callsign_ids_ignored(self):
        # Brand ids that merely look vaguely station-shaped never fire
        # without an unambiguous callsign on BOTH sides.
        assert not callsign_id_contradiction("WWE.us", "WWE Network HD")
        assert not callsign_id_contradiction("NatGeo.uk", "UK: NAT GEO (WILD)")
        assert not callsign_id_contradiction("KHON.us", "US: FOX HARTFORD")
        assert not callsign_id_contradiction("", "US: FOX HARTFORD (WTIC)")


from build_epg import foreign_tld_donor, load_aliases  # noqa: E402


class TestForeignTldDonor:
    def test_foreign_market_ids_blocked(self):
        # The real case: French beIN 3 rescued onto 18 MENA beIN variants.
        assert foreign_tld_donor("beinsports3.fr")
        assert foreign_tld_donor("NatGeo.de")
        assert foreign_tld_donor("natgeo.es@HD")
        assert foreign_tld_donor("SPORT1.tr")

    def test_english_and_mena_markets_allowed(self):
        for cid in ("BBCOne.uk", "NatGeo.us", "beINSports3En.qa",
                    "MBC.2.ae", "Rotana.Drama.sa", "skysport1.nz",
                    "WGME-DT.us_locals1", "MbcMasr2.eg"):
            assert not foreign_tld_donor(cid), cid

    def test_uuid_and_plain_ids_allowed(self):
        assert not foreign_tld_donor("d2d0ba03-cbf0-40ec-a604-010a95390a9e")
        assert not foreign_tld_donor("6609")
        assert not foreign_tld_donor("")


class TestLoadAliasesManyToOne:
    def test_many_m3u_ids_share_one_upstream(self, tmp_path, monkeypatch):
        # Every beIN 3 quality variant pins the SAME verified feed — the
        # map must be keyed by the (unique) M3U id, not the upstream id.
        monkeypatch.chdir(tmp_path)
        (tmp_path / "channels").mkdir()
        (tmp_path / "channels" / "aliases.tsv").write_text(
            "# comment\n"
            "BE: beIN 3 HD\tbeIN_SPORTS3_EN.bein\n"
            "SA: beIN 3 RAW\tbeIN_SPORTS3_EN.bein\n"
            "NationalGeographicAbuDhabi.ae\tNat.Geo.Abu.Dhabi.HD.ae\n",
            encoding="utf-8")
        out = load_aliases()
        assert out == {
            "BE: beIN 3 HD": "beIN_SPORTS3_EN.bein",
            "SA: beIN 3 RAW": "beIN_SPORTS3_EN.bein",
            "NationalGeographicAbuDhabi.ae": "Nat.Geo.Abu.Dhabi.HD.ae",
        }


class TestJunkChannelUnavailable:
    def test_no_longer_available_filler_dropped(self):
        # Provider filler observed parked on Sky Showcase + ROOT Sports —
        # blocks real data exactly like the TimeShift blocks.
        for t in (b"Channel No Longer Available",
                  b"Channel Is No Longer Available",
                  b"channel no longer available"):
            assert JUNK_PROG_TITLE_RE.search(
                b"<programme><title>" + t + b"</title></programme>")

    def test_real_titles_kept(self):
        for t in (b"The Channel Tunnel Story", b"No Time to Die",
                  b"Available Light"):
            assert not JUNK_PROG_TITLE_RE.search(
                b"<programme><title>" + t + b"</title></programme>")


class TestBrandTokenGuard:
    def test_nat_geo_abu_dhabi_regression(self):
        # The real 2026-06 bug: M3U 'Abu Dhabi Natioanl Geo 4K' (provider
        # typo) token-matched the generic 'Abu Dhabi' (Abu Dhabi TV) feed
        # and shipped news + drama schedules on a Nat Geo channel. The
        # leftover {NATIOANL, GEO} marks a different channel identity.
        m3u = name_tokens("AR: Abu Dhabi Natioanl Geo 4K")
        src = name_tokens("Abu Dhabi")
        assert src.issubset(m3u)
        assert not token_leftover_ok(m3u, src)

    def test_genre_suffix_rejected(self):
        # 'SKY SPORTS GOLF' must never take a generic 'SKY SPORTS' feed.
        assert not token_leftover_ok(
            frozenset({"SKY", "SPORTS", "GOLF"}), frozenset({"SKY", "SPORTS"}))
        # 'SKY CINEMA ACTION' must never take generic 'SKY CINEMA'.
        assert not token_leftover_ok(
            frozenset({"SKY", "CINEMA", "ACTION"}), frozenset({"SKY", "CINEMA"}))
        # 'MBC PANORAMA FM' must never take the 'MBC FM' feed.
        assert not token_leftover_ok(
            frozenset({"MBC", "PANORAMA", "FM"}), frozenset({"MBC", "FM"}))

    def test_city_and_provider_leftovers_still_allowed(self):
        # Non-brand leftovers (cities, provider prefixes) remain allowed —
        # the clone cap and language guard bound the damage there.
        assert token_leftover_ok(
            frozenset({"SPECTRUM", "NEWS", "MILWAUKEE"}),
            frozenset({"SPECTRUM", "NEWS"}))
        assert token_leftover_ok(
            frozenset({"SKYGO", "SKY", "WITNESS"}), frozenset({"SKY", "WITNESS"}))

    def test_brand_tokens_exclude_quality_and_junk(self):
        # Quality/junk tokens are stripped by name_tokens() before the
        # guard ever sees them; keeping them out of BRAND_TOKENS documents
        # that they are NOT identity markers.
        for tok in ("HD", "FHD", "UHD", "4K", "8K", "RAW", "VIP", "HEVC",
                    "BACKUP", "FEED", "MIRROR", "LIVE"):
            assert tok not in BRAND_TOKENS
