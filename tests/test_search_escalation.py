"""Every search lane asks its likeliest query first and cycles through the rest
of the title's spellings and aliases before giving up.

The case that exposed the gap: a Vietnamese title. Trackers and indexers
mostly store release names without diacritics, so "Tiem Banh Hoang Tu Be" is
the likeliest hit; the title as written, "Tiệm Bánh Hoàng Tử Bé", is still
worth one more pass before a lane reports nothing.
"""

import contextlib
import os
import unittest
from unittest import mock
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ADDON_SECRET", "test-secret")

from app import content_identity, easynews, private_trackers, textfold, usenet  # noqa: E402

VI = "Tiệm Bánh Hoàng Tử Bé"
VI_FOLDED = "Tiem Banh Hoang Tu Be"


class SpellingTests(unittest.TestCase):
    def test_folded_spelling_leads_and_the_written_one_follows(self):
        self.assertEqual([VI_FOLDED, VI], usenet._spellings(VI))
        self.assertEqual(["The First Frost"],
                         usenet._spellings("The First Frost"))
        self.assertEqual(["乡村爱情"], usenet._spellings("乡村爱情"))

    def test_native_titles_are_not_indexer_queries(self):
        self.assertEqual(["Love Stories in the Countryside", "Country Love"],
                         usenet._latin_titles(
                             ["Love Stories in the Countryside", "乡村爱情",
                              "Country Love", "Country.Love"]))


class UndecomposedLetterTests(unittest.TestCase):
    """Letters NFKD leaves alone (Vietnamese Đ, Polish ł, Nordic ø, ...) once
    made a correctly named release fail its own title."""

    CASES = [("Đường Về Nhà", "Duong.Ve.Nha.S01E01.1080p.WEB-DL", "Duong Ve Nha"),
             ("Ślepnąc od świateł", "Slepnac.od.swiatel.S01E01.1080p.WEB-DL",
              "Slepnac od swiatel"),
             ("Skam Øst", "Skam.Ost.S01E01.1080p.WEB-DL", "Skam Ost"),
             ("Die Straße", "Die.Strasse.S01E01.1080p.WEB-DL", "Die Strasse"),
             ("Kıraç", "Kirac.S01E01.1080p.WEB-DL", "Kirac"),
             ("Þrot", "Throt.S01E01.1080p.WEB-DL", "Throt")]

    def test_search_spelling_is_plain_ascii(self):
        for title, _release, spelled in self.CASES:
            self.assertEqual(spelled, usenet._spellings(title)[0], title)

    def test_title_gate_and_identity_accept_the_ascii_release(self):
        for title, release, _spelled in self.CASES:
            self.assertTrue(usenet._release_title_match(release, title), title)
            profile = content_identity.IdentityProfile(
                media="series", imdb_id="tt1", aliases=(title,),
                years=frozenset(), season=1, episode=1)
            self.assertNotEqual(content_identity.CONTRADICTION,
                                content_identity.classify(profile, release),
                                title)

    def test_fold_keeps_non_latin_scripts(self):
        self.assertEqual("乡村爱情", textfold.fold("乡村爱情"))
        self.assertEqual("amelie", textfold.fold("Amélie"))


class PrivateTierTests(unittest.TestCase):
    def test_accented_title_is_searched_folded_first_then_as_written(self):
        first, later = private_trackers._query_tiers(
            "series", [VI], 2013, (1, 1))
        self.assertEqual([f"{VI_FOLDED} S01E01", f"{VI_FOLDED} S01",
                          VI_FOLDED, f"{VI_FOLDED} complete series"], first)
        self.assertEqual([f"{VI} S01E01", f"{VI} S01", VI,
                          f"{VI} complete series"], later)

    def test_second_tier_holds_every_remaining_alias_and_form(self):
        titles = ["Love Stories in the Countryside", "乡村爱情", "Country Love",
                  "Rural Love Story", "乡村爱情故事", "Xiang Cun Ai Qing"]
        first, later = private_trackers._query_tiers(
            "series", titles, 2006, (1, 1))
        self.assertEqual(12, len(first))
        self.assertNotIn("Rural Love Story", first)
        for query in ("Rural Love Story", "Country Love complete series",
                      "Xiang Cun Ai Qing S01E01", "乡村爱情故事 S01"):
            self.assertIn(query, later)
        self.assertFalse(set(first) & set(later))
        self.assertLessEqual(len(later), private_trackers._MAX_FALLBACK_QUERIES)

    def test_movie_second_tier_drops_the_year_token(self):
        first, later = private_trackers._query_tiers(
            "movie", ["Amélie"], 2001, None)
        self.assertEqual(["Amelie 2001"], first)
        self.assertEqual(["Amélie 2001", "Amelie"], later)

    def test_plain_title_with_no_aliases_has_one_tier(self):
        self.assertEqual(1, len(private_trackers._query_tiers(
            "series", ["The Bear"], 2022, (3, 5))))

    def test_rationed_second_tier_is_written_spelling_and_capped(self):
        T = private_trackers._IndexerTraits
        with patch.dict(private_trackers._indexer_traits,
                        {5: T(True, True, True)}, clear=True):
            tiers = private_trackers._rationed_tiers(
                "series", "tt12116838:1:1", [VI], (1, 1), {5})
            many = private_trackers._rationed_tiers(
                "series", "tt1:1:1", ["A One", "B Two", "C Three", "D Four",
                                      "E Five", "F Six"], (1, 1), {5})
        self.assertEqual([("tvsearch", "{ImdbId:tt12116838}{Season:1}", {5}),
                          ("search", VI_FOLDED, {5})], tiers[0])
        self.assertEqual([("search", VI, {5})], tiers[1])
        self.assertEqual(3, len(many[0]))
        self.assertEqual(3, len(many[1]))


def _row(title, indexer_id=45):
    return {"indexerId": indexer_id, "indexer": "M-Team - TP",
            "protocol": "torrent", "title": title, "seeders": 20,
            "size": 20_000_000_000, "guid": title,
            "downloadUrl": "http://prowlarr/dl/" + title}


class PrivateEscalationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        private_trackers._search_outcomes.clear()

    @contextlib.contextmanager
    def _lane(self, query):
        with mock.patch.object(
                private_trackers, "_private_torrent_indexers",
                new=AsyncMock(return_value={45: "M-Team - TP"})), \
                mock.patch.dict(private_trackers._indexer_traits, {},
                                clear=True), \
                mock.patch.object(private_trackers, "_prowlarr_query",
                                  new=query), \
                mock.patch.object(usenet, "_expected_info",
                                  new=AsyncMock(return_value=([VI], 2013))), \
                mock.patch.object(usenet, "_accepted_years",
                                  new=AsyncMock(return_value=frozenset({2013}))), \
                mock.patch.object(private_trackers, "MIN_SEEDERS", 1), \
                mock.patch.object(private_trackers.telemetry,
                                  "record_private_tracker"):
            yield

    async def test_written_spelling_is_tried_when_folded_finds_nothing(self):
        asked = []

        async def query(q, ids, kind="search"):
            asked.append(q)
            return [_row(f"{VI} S01 1080p WEB-DL")] if q == VI else []

        with self._lane(query):
            found = await private_trackers._search("series", "tt12116838:1:1")
        self.assertEqual([f"{VI} S01 1080p WEB-DL"], [c["title"] for c in found])
        self.assertLess(asked.index(f"{VI_FOLDED} S01E01"), asked.index(VI))

    async def test_a_hit_in_the_first_tier_stops_there(self):
        asked = []

        async def query(q, ids, kind="search"):
            asked.append(q)
            return [_row(f"{VI_FOLDED} S01E01 1080p WEB-DL")]

        with self._lane(query):
            found = await private_trackers._search("series", "tt12116838:1:1")
        self.assertEqual(1, len(found))
        self.assertFalse(any("ệ" in q for q in asked))

    async def test_a_failed_second_tier_is_a_failure_not_an_empty_answer(self):
        async def query(q, ids, kind="search"):
            if "ệ" in q:
                raise RuntimeError("prowlarr went away")
            return []

        with self._lane(query):
            found = await private_trackers._search("series", "tt12116838:1:1")
        self.assertEqual([], found)
        self.assertEqual("failed", private_trackers.search_outcome(
            "series", "tt12116838:1:1")["state"])


class UsenetEscalationTests(unittest.IsolatedAsyncioTestCase):
    def test_final_text_pass_is_written_spellings_then_more_aliases(self):
        self.assertEqual(
            [f"{VI} S01E01", f"{VI} S01", f"{VI} Complete"],
            usenet._text_fallback_queries("series", ["tt1", "1", "1"],
                                          [VI], 2013))
        self.assertEqual(
            ["Rural Love Story S17E01", "Xiang Cun Ai Qing S17E01"],
            usenet._text_fallback_queries(
                "series", ["tt1", "17", "1"],
                ["Love Stories in the Countryside", "Country Love",
                 "Rural Love Story", "Xiang Cun Ai Qing", "Another One"],
                2006))
        self.assertEqual([], usenet._text_fallback_queries(
            "series", ["tt1", "1", "1"], ["The Bear"], 2022))

    async def test_search_reaches_the_written_spelling_last(self):
        asked = []
        release = {"title": "Tiệm.Bánh.Hoàng.Tử.Bé.S01E01.1080p.WEB-DL.x264",
                   "size": 2_000_000_000, "link": "https://indexer/vi",
                   "indexer": "one"}

        async def search_one(name, base, key, params):
            asked.append(params.get("q") or "id")
            return [release] if params.get("q") == f"{VI} S01E01" else []

        with patch.object(usenet, "INDEXERS", [("one", "https://x", "k")]), \
                patch.object(usenet, "_search_one", search_one), \
                patch("app.meta.title_year", AsyncMock(
                    return_value=(VI, VI, 2013))), \
                patch("app.meta.title_aliases", AsyncMock(return_value=())), \
                patch("app.meta.season_year", AsyncMock(return_value=None)), \
                patch("app.usenet_health.should_skip", return_value=False), \
                patch("app.usenet_health.status", return_value={}), \
                patch("app.usenet_health.fetch_allowed", return_value=True), \
                patch("app.usenet_health.indexer_score", return_value=0.5):
            found = await usenet.search("series", "tt12116838:1:1")
        self.assertEqual([release["title"]], [r["title"] for r in found])
        self.assertEqual("id", asked[0])
        self.assertLess(asked.index(f"{VI_FOLDED} S01E01"),
                        asked.index(f"{VI} S01E01"))


class EasynewsEscalationTests(unittest.IsolatedAsyncioTestCase):
    def test_fallback_keeps_the_accents(self):
        self.assertEqual([f"{VI} S01E01"], easynews._fallback_queries(
            "series", "tt12116838:1:1", [VI], 2013))
        self.assertEqual([], easynews._fallback_queries(
            "series", "tt1:1:1", ["The Bear"], 2022))

    async def test_second_pass_only_after_a_first_pass_miss(self):
        payload = {"downURL": "https://members.easynews.com/dl",
                   "dlFarm": "auto", "dlPort": 443, "sid": "SID",
                   "data": [{"0": "a1", "10": "Tiệm.Bánh.Hoàng.Tử.Bé.S01E01."
                             "1080p.WEB-DL.x264", "11": ".mkv", "2": ".mkv",
                             "type": "VIDEO", "rawSize": 2 * 10**9,
                             "size": 2 * 10**9, "runtime": 2700,
                             "height": 1080, "yres": 1080, "sig": "S",
                             "passwd": False, "virus": False}]}
        asked = []

        async def search(q):
            asked.append(q)
            return payload if "ệ" in q else {"data": []}

        with patch.object(easynews, "enabled", return_value=True), \
                patch.multiple(easynews, _USER="u", _PASS="p"), \
                patch.object(easynews, "_search", search), \
                patch.object(usenet, "_expected_info",
                             AsyncMock(return_value=([VI], 2013))), \
                patch.object(usenet, "_accepted_years",
                             AsyncMock(return_value=frozenset({2013}))), \
                patch("app.meta.expected_runtime", AsyncMock(return_value=None)):
            rows = await easynews.streams("series", "tt12116838:1:1")
        self.assertEqual([f"{VI_FOLDED} S01E01", f"{VI} S01E01"], asked)
        self.assertEqual(1, len(rows))


if __name__ == "__main__":
    unittest.main()
