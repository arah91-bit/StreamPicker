"""How foreign works are actually named: regional titles and season years."""

import time
import unittest
from unittest.mock import AsyncMock, patch

from app import content_identity, meta, private_trackers, usenet

# TMDB's record for 乡村爱情 (tt1395015), trimmed to the fields that matter.
_FIND = {"tv_results": [{"id": 49036, "original_language": "zh",
                         "name": "Love Stories in the Countryside",
                         "original_name": "乡村爱情"}]}
_DETAILS = {
    "origin_country": ["CN"],
    "alternative_titles": {"results": [
        {"iso_3166_1": "CN", "title": "Country Love", "type": ""},
        {"iso_3166_1": "CN", "title": "Rural Love Story", "type": ""},
        {"iso_3166_1": "CN", "title": "乡村爱情故事", "type": ""},
        {"iso_3166_1": "CN", "title": "Xiang Cun Ai Qing", "type": ""},
        {"iso_3166_1": "CN", "title": "Country.Love", "type": ""},
        {"iso_3166_1": "CN", "title": "乡村爱情18", "type": "season 18"},
        {"iso_3166_1": "CN", "title": "CL", "type": ""},
        {"iso_3166_1": "RU", "title": "Деревенские истории", "type": ""},
    ]},
    "translations": {"translations": [
        {"iso_639_1": "en", "iso_3166_1": "US",
         "data": {"name": "Love Stories in the Countryside"}},
        {"iso_639_1": "zh", "iso_3166_1": "CN", "data": {"name": "乡村爱情"}},
        {"iso_639_1": "zh", "iso_3166_1": "TW", "data": {"name": "鄉村愛情"}},
        {"iso_639_1": "zh", "iso_3166_1": "HK", "data": {"name": "鄉村愛情"}},
    ]},
}


def _clear_meta_caches():
    for cache in (meta._aliases_cache, meta._identity_cache, meta._find_cache,
                  meta._tv_cache, meta._cinemeta_cache):
        cache.clear()


def _tmdb(find=_FIND, details=_DETAILS):
    async def get(path, **params):
        if path.startswith("/find/"):
            return find
        assert params.get("append_to_response") == ("alternative_titles,"
                                                    "translations")
        return details
    return AsyncMock(side_effect=get)


class TitleAliasTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _clear_meta_caches()
        self._key = patch.object(meta, "TMDB_KEY", "k")
        self._key.start()

    def tearDown(self):
        self._key.stop()
        _clear_meta_caches()

    async def test_origin_country_aliases_are_kept(self):
        with patch.object(meta, "_get", _tmdb()):
            aliases = await meta.title_aliases("series", "tt1395015")
        # Season-specific, too-short, foreign-market and punctuation-duplicate
        # entries are all dropped.
        self.assertEqual(("Country Love", "Rural Love Story", "乡村爱情故事",
                          "Xiang Cun Ai Qing", "鄉村愛情"), aliases)

    async def test_english_original_gets_none(self):
        get = _tmdb(find={"tv_results": [{"id": 1, "original_language": "en"}]})
        with patch.object(meta, "_get", get):
            self.assertEqual((), await meta.title_aliases("series", "tt1"))
        self.assertEqual(1, get.await_count)        # no details request

    async def test_movie_uses_production_countries_and_titles_key(self):
        find = {"movie_results": [{"id": 7, "original_language": "ko"}]}
        details = {"production_countries": [{"iso_3166_1": "KR"}],
                   "alternative_titles": {"titles": [
                       {"iso_3166_1": "KR", "title": "Gisaengchung", "type": ""},
                       {"iso_3166_1": "US", "title": "Parasite", "type": ""}]}}
        with patch.object(meta, "_get", _tmdb(find, details)):
            self.assertEqual(("Gisaengchung",),
                             await meta.title_aliases("movie", "tt6751668"))

    async def test_failure_is_cached_only_briefly(self):
        with patch.object(meta, "_get", AsyncMock(side_effect=RuntimeError)):
            self.assertEqual((), await meta.title_aliases("series", "tt1395015"))
        expires, _ = meta._aliases_cache[("series", "tt1395015")]
        self.assertLessEqual(expires - time.monotonic(), meta._NEG_TTL)

    async def test_imdb_title_leads_the_aliases_of_a_foreign_work(self):
        record = type("Record", (), {"title": "Country Love"})()
        with patch.object(meta, "_get", _tmdb()), \
                patch.object(meta.omdb, "enabled", return_value=True), \
                patch.object(meta.omdb, "lookup", AsyncMock(return_value=record)):
            aliases = await meta.title_aliases("series", "tt1395015")
        self.assertEqual(("Country Love", "Rural Love Story", "乡村爱情故事",
                          "Xiang Cun Ai Qing", "鄉村愛情"), aliases)
        # An English original gets no aliases, IMDb title included.
        _clear_meta_caches()
        english = _tmdb(find={"tv_results": [{"id": 1,
                                              "original_language": "en"}]})
        with patch.object(meta, "_get", english), \
                patch.object(meta.omdb, "enabled", return_value=True), \
                patch.object(meta.omdb, "lookup",
                             AsyncMock(return_value=record)) as lookup:
            self.assertEqual((), await meta.title_aliases("series", "tt1"))
        lookup.assert_not_awaited()

    def test_generic_aliases_are_not_usable(self):
        for title in ("Home", "Mother", "Signal", "CL", "Up"):
            self.assertFalse(meta._alias_usable(title), title)
        for title in ("Country Love", "Country.Love", "Gisaengchung",
                      "Xiang Cun Ai Qing", "乡村爱情", "母亲"):
            self.assertTrue(meta._alias_usable(title), title)

    async def test_identity_profile_accepts_an_alias_named_release(self):
        with patch.object(meta, "_title_details", AsyncMock(return_value=(
                    "Love Stories in the Countryside", "乡村爱情", 2006, "zh"))), \
                patch.object(meta, "title_aliases", AsyncMock(return_value=(
                    "Country Love", "Rural Love Story"))), \
                patch.object(meta.omdb, "enabled", return_value=False):
            profile = await meta.identity_profile("series", "tt1395015:1:1")
        self.assertIn("Rural Love Story", profile.aliases)
        self.assertNotEqual(
            content_identity.CONTRADICTION,
            content_identity.classify(
                profile, "Rural.Love.Story.2006.S01E01.1080p.WEB-DL.H264.AAC"))


class UsenetTitleTests(unittest.IsolatedAsyncioTestCase):
    async def test_expected_titles_put_canonical_and_native_first(self):
        with patch("app.meta.title_year", AsyncMock(return_value=(
                    "Love Stories in the Countryside", "乡村爱情", 2006))), \
                patch("app.meta.title_aliases", AsyncMock(return_value=(
                    "Country Love", "Rural Love Story"))):
            titles, year = await usenet._expected_info(
                "series", "tt1395015:1:1")
        self.assertEqual(["Love Stories in the Countryside", "乡村爱情",
                          "Country Love", "Rural Love Story"], titles)
        self.assertEqual(2006, year)

    async def test_imdb_title_comes_from_the_cached_omdb_record(self):
        record = type("Record", (), {"title": "Country Love"})()
        with patch.object(meta.omdb, "enabled", return_value=True), \
                patch.object(meta.omdb, "lookup", AsyncMock(return_value=record)):
            self.assertEqual("Country Love",
                             await meta.imdb_title("series", "tt1395015:17:1"))
        with patch.object(meta.omdb, "enabled", return_value=False):
            self.assertIsNone(await meta.imdb_title("series", "tt1395015"))

    def test_text_queries_reach_past_a_native_title(self):
        queries = usenet._text_queries(
            "series", ["tt1395015", "1", "1"],
            ["Love Stories in the Countryside", "乡村爱情", "Country Love",
             "Rural Love Story"], 2006)
        self.assertIn("Country Love S01E01", queries)
        self.assertNotIn("Rural Love Story S01E01", queries)
        self.assertFalse(any("乡村" in q for q in queries))


# 乡村爱情 began in 2006; each season is released under its own air year.
_SEASONS = {"seasons": [{"season_number": 0, "air_date": "2007-01-01"},
                        {"season_number": 1, "air_date": "2006-09-30"},
                        {"season_number": 17, "air_date": "2025-01-28"},
                        {"season_number": 18, "air_date": ""}]}


class SeasonYearTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _clear_meta_caches()
        self._key = patch.object(meta, "TMDB_KEY", "k")
        self._key.start()

    def tearDown(self):
        self._key.stop()
        _clear_meta_caches()

    async def test_season_year_reads_every_season_from_one_record(self):
        get = AsyncMock(return_value=_SEASONS)
        with patch.object(meta, "_find", AsyncMock(return_value=49036)), \
                patch.object(meta, "_get", get), \
                patch.object(meta, "_cinemeta_season_years",
                             AsyncMock(return_value={17: 2025, 18: 2026})):
            self.assertEqual(2025, await meta.season_year("tt1395015", 17))
            self.assertIsNone(await meta.season_year("tt1395015", 18))
            self.assertIsNone(await meta.season_year("tt1395015", 0))
        self.assertEqual(1, get.await_count)

    async def test_season_year_needs_tmdb_and_cinemeta_to_agree(self):
        # Cinemeta's S17 (the numbering a Stremio id uses) began a year later
        # than TMDB's: the extra year would belong to a different season.
        with patch.object(meta, "_find", AsyncMock(return_value=49036)), \
                patch.object(meta, "_get", AsyncMock(return_value=_SEASONS)), \
                patch.object(meta, "_cinemeta_season_years",
                             AsyncMock(return_value={17: 2026})):
            self.assertIsNone(await meta.season_year("tt1395015", 17))

    async def test_tmdb_failure_returns_none_and_is_not_cached(self):
        with patch.object(meta, "_find", AsyncMock(side_effect=RuntimeError)):
            self.assertIsNone(await meta.season_year("tt1395015", 17))
        self.assertNotIn(49036, meta._tv_cache)

    async def test_cinemeta_years_use_its_own_numbering_and_fail_briefly(self):
        payload = {"meta": {"videos": [
            {"season": 17, "episode": 2, "released": "2025-01-29T00:00:00Z"},
            {"season": 17, "episode": 1, "released": "2025-01-28T00:00:00Z"},
            {"season": 0, "episode": 1, "released": "2007-01-01T00:00:00Z"},
            {"season": 18, "episode": 1, "released": None}]}}
        ok = AsyncMock(return_value=type("R", (), {
            "raise_for_status": lambda self: None,
            "json": lambda self: payload})())
        with patch.object(meta._client, "get", ok):
            self.assertEqual({17: 2025},
                             await meta._cinemeta_season_years("tt1395015:17:1"))
        _clear_meta_caches()
        with patch.object(meta._client, "get",
                          AsyncMock(side_effect=RuntimeError)):
            self.assertEqual({}, await meta._cinemeta_season_years("tt1395015"))
        expires, _ = meta._cinemeta_cache["tt1395015"]
        self.assertLessEqual(expires - time.monotonic(), meta._NEG_TTL)

    async def test_accepted_years_add_the_season_year_after_season_one(self):
        lookup = AsyncMock(return_value=2025)
        with patch.object(meta, "season_year", lookup):
            self.assertEqual({2006, 2025}, await usenet._accepted_years(
                "series", "tt1395015:17:1", 2006))
            self.assertEqual({2006}, await usenet._accepted_years(
                "series", "tt1395015:1:1", 2006))
            self.assertEqual({2006}, await usenet._accepted_years(
                "movie", "tt1395015", 2006))
            self.assertEqual(frozenset(), await usenet._accepted_years(
                "series", "tt1395015:17:1", None))
        lookup.assert_awaited_once_with("tt1395015", 17)

    def test_title_year_gate_accepts_the_season_year_only(self):
        titles = ["Country Love"]
        years = frozenset({2006, 2025})
        self.assertTrue(usenet._release_year_match(
            "Country Love 2025 S17 Complete 2160p WEB-DL", titles, years))
        self.assertTrue(usenet._release_year_match(
            "Country Love 2006 S01 Complete 2160p WEB-DL", titles, years))
        self.assertFalse(usenet._release_year_match(
            "Country Love 2019 S17 2160p WEB-DL", titles, years))
        self.assertFalse(usenet._release_year_match(
            "Country Love 2025 S17 Complete 2160p WEB-DL", titles, 2006))

    def test_mounted_basename_may_carry_the_season_year(self):
        href = "/content/tv/pack/Country.Love.2025.S17E02.2160p.WEB-DL.mkv"
        release = {"_nzb_expected": {"media": "series",
                                     "titles": ["Country Love"], "year": 2006,
                                     "years": [2006, 2025]}}
        self.assertEqual("strong", usenet._basename_identity(
            href, release, (17, 2))[0])
        wrong = "/content/tv/pack/Country.Love.2019.S17E02.2160p.WEB-DL.mkv"
        self.assertEqual("wrong-year", usenet._basename_identity(
            wrong, release, (17, 2))[2])
        # A release stored before "years" existed keeps the old strict check.
        legacy = {"_nzb_expected": {"media": "series",
                                    "titles": ["Country Love"], "year": 2006}}
        self.assertEqual("wrong-year", usenet._basename_identity(
            href, legacy, (17, 2))[2])

    def test_reused_pack_restores_the_season_year(self):
        row = {"release_key": "nzb:abc", "titles": ["Country Love"],
               "title": "Country.Love.2025.S17.2160p.WEB-DL", "size": 1,
               "scope": "series:tt1395015:17", "year": 2006}
        with patch("app.candidate_health.pack_seeds", return_value=[row]), \
                patch("app.usenet_health.should_skip", return_value=False):
            reused = usenet._verified_pack_releases("tt1395015:17:2", 2025)
            plain = usenet._verified_pack_releases("tt1395015:17:2")
        self.assertEqual([2006, 2025], reused[0]["_nzb_expected"]["years"])
        self.assertEqual([2006], plain[0]["_nzb_expected"]["years"])

    async def test_identity_profile_accepts_the_season_year(self):
        with patch.object(meta, "_title_details", AsyncMock(return_value=(
                    "Love Stories in the Countryside", "乡村爱情", 2006, "zh"))), \
                patch.object(meta, "title_aliases", AsyncMock(return_value=(
                    "Country Love",))), \
                patch.object(meta, "season_year", AsyncMock(return_value=2025)), \
                patch.object(meta.omdb, "enabled", return_value=False):
            profile = await meta.identity_profile("series", "tt1395015:17:1")
        self.assertEqual({2006, 2025}, set(profile.years))
        self.assertFalse(profile.metadata_conflict)
        self.assertNotEqual(
            content_identity.CONTRADICTION,
            content_identity.classify(
                profile, "Country.Love.2025.S17E01.2160p.WEB-DL.H265.AAC"))
        self.assertEqual(
            content_identity.CONTRADICTION,
            content_identity.classify(
                profile, "Country.Love.2019.S17E01.2160p.WEB-DL.H265.AAC"))

    async def test_private_search_admits_a_season_year_pack(self):
        def row(title):
            return {"indexerId": 45, "indexer": "M-Team - TP",
                    "protocol": "torrent", "title": title, "seeders": 20,
                    "size": 20_000_000_000, "guid": title,
                    "downloadUrl": "http://prowlarr/dl/" + title}
        rows = [row("Country Love 2025 S17 Complete 2160p WEB-DL HQ H.265-UBWEB"),
                row("Country Love 2019 S17 Complete 2160p WEB-DL")]
        with patch.object(private_trackers, "_private_torrent_indexers",
                          AsyncMock(return_value={45: "M-Team - TP"})), \
                patch.object(private_trackers, "_prowlarr_query",
                             AsyncMock(return_value=rows)), \
                patch.object(usenet, "_expected_info", AsyncMock(
                    return_value=(["Country Love"], 2006))), \
                patch.object(meta, "season_year", AsyncMock(return_value=2025)), \
                patch.object(private_trackers, "MIN_SEEDERS", 1), \
                patch.object(private_trackers.telemetry,
                             "record_private_tracker"):
            found = await private_trackers._search("series", "tt1395015:17:1")
        self.assertEqual(
            ["Country Love 2025 S17 Complete 2160p WEB-DL HQ H.265-UBWEB"],
            [c["title"] for c in found])


if __name__ == "__main__":
    unittest.main()
