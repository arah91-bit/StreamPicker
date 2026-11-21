import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from unittest import mock

import httpx
from starlette.requests import Request

os.environ.setdefault("ADDON_SECRET", "test-secret")

from app import config, picker, private_trackers, private_ui, proxy


def _request(method="GET", range_header=""):
    headers = []
    if range_header:
        headers.append((b"range", range_header.encode()))
    return Request({"type": "http", "method": method, "path": "/private/x",
                    "headers": headers, "query_string": b"",
                    "server": ("test", 80), "client": ("127.0.0.1", 1),
                    "scheme": "http"})


class ThinResultPolicyTests(unittest.TestCase):
    """When a public search comes back thin, private trackers get a look.

    The threshold used to be zero and hard-coded: private was reachable only
    once the ordinary path found nothing at all.
    """

    def setUp(self):
        self.enabled = mock.patch.object(private_trackers, "enabled",
                                         return_value=True)
        self.enabled.start()
        self.addCleanup(self.enabled.stop)

    def _with_threshold(self, value):
        return mock.patch.object(private_trackers, "MIN_PUBLIC_SOURCES", value)

    def test_zero_keeps_private_a_last_resort(self):
        with self._with_threshold(0):
            self.assertFalse(private_trackers.should_search(0))
            self.assertFalse(private_trackers.should_search(9))

    def test_a_threshold_fires_only_on_thin_results(self):
        with self._with_threshold(5):
            self.assertTrue(private_trackers.should_search(4))
            self.assertFalse(private_trackers.should_search(5))
            self.assertFalse(private_trackers.should_search(12))

    def test_minus_one_always_searches(self):
        with self._with_threshold(-1):
            self.assertTrue(private_trackers.should_search(0))
            self.assertTrue(private_trackers.should_search(500))

    def test_unconfigured_trackers_are_never_searched(self):
        with self._with_threshold(-1), \
                mock.patch.object(private_trackers, "enabled",
                                  return_value=False):
            self.assertFalse(private_trackers.should_search(0))

    def test_thin_results_are_the_shipped_default(self):
        """Someone who turns the lane on and touches nothing else gets the
        second look on thin results, not last-resort-only."""
        self.assertEqual("5", config.default("PRIVATE_TRACKER_MIN_SOURCES"))

    def test_always_on_sentinel_is_a_saveable_value(self):
        """-1 is documented as 'always'. The generic floor for an advanced
        number is zero, which would have rejected the one value the knob
        exists to offer."""
        old = os.environ.get("CONFIG_FILE")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["CONFIG_FILE"] = os.path.join(tmp, "config.json")
                config.save({"PRIVATE_TRACKER_MIN_SOURCES": "-1"})
                self.assertEqual(
                    "-1", config.pending("PRIVATE_TRACKER_MIN_SOURCES"))
                config.save({"PRIVATE_TRACKER_MIN_SOURCES": "0"})
                self.assertEqual(
                    "0", config.pending("PRIVATE_TRACKER_MIN_SOURCES"))
                with self.assertRaisesRegex(ValueError, "at least"):
                    config.save({"PRIVATE_TRACKER_MIN_SOURCES": "-2"})
                with self.assertRaisesRegex(ValueError, "whole number"):
                    config.save({"PRIVATE_TRACKER_MIN_SOURCES": "2.5"})
        finally:
            if old is None:
                os.environ.pop("CONFIG_FILE", None)
            else:
                os.environ["CONFIG_FILE"] = old

    def test_the_trigger_sits_beside_the_master_switch(self):
        """One page, one place: the on/off and the 'how thin' number are the
        first two controls, and the page's save endpoint accepts the number."""
        page = private_ui.render({"events": {}})
        head = page.split("Progressive mode streams", 1)[0]
        self.assertIn("PRIVATE_TRACKERS_ENABLED", head)
        self.assertIn("id='private_min_sources'", head)
        self.assertIn("min='-1'", head)
        self.assertIn("PRIVATE_TRACKER_MIN_SOURCES", private_ui.KEYS)


class ReleasePolicyTests(unittest.TestCase):
    def test_foreign_series_searches_canonical_and_native_titles(self):
        first, later = private_trackers._query_tiers(
            "series", ["Kang's Family", "外来媳妇本地郎"], 2000, (1, 1))
        queries = first + later
        # Folded first (release names drop the apostrophe), as written after.
        self.assertIn("Kangs Family S01E01", first)
        self.assertIn("Kang's Family S01E01", later)
        self.assertIn("外来媳妇本地郎 S01E01", queries)
        self.assertIn("外来媳妇本地郎 S01", queries)
        self.assertIn("外来媳妇本地郎", queries)

    def test_regional_aliases_query_only_the_episode_and_season(self):
        queries, _later = private_trackers._query_tiers(
            "series", ["Love Stories in the Countryside", "乡村爱情",
                       "Rural Love Story"], 2006, (1, 1))
        self.assertIn("乡村爱情", queries)
        self.assertIn("Rural Love Story S01E01", queries)
        self.assertIn("Rural Love Story S01", queries)
        self.assertNotIn("Rural Love Story", queries)
        self.assertNotIn("Rural Love Story complete series", queries)
        self.assertEqual(10, len(queries))

    def test_later_season_episode_is_not_a_season_one_pack(self):
        # The season parser once backtracked "S18E13" to season 1, filing a
        # long-running show's recent episodes as packs for its first season.
        for title in ("Love Stories in the Countryside S18E13 1080p WEB-DL",
                      "Country Love 2026 S18E12-E13 2160p WEB-DL",
                      "Show.S12E03.1080p.WEB-DL"):
            self.assertEqual(
                "", private_trackers.classify_release(title, 1, 1), title)
        self.assertEqual("season", private_trackers.classify_release(
            "Rural Love Story 2006 S01 1080p WEB-DL", 1, 1))
        self.assertEqual("episode", private_trackers.classify_release(
            "Show.S12E03.1080p.WEB-DL", 12, 3))

    def test_malformed_upstream_counts_become_unknown(self):
        self.assertEqual(0, private_trackers._nonnegative_int(".2.91 GB"))
        self.assertEqual(0, private_trackers._nonnegative_int(None))
        self.assertEqual(12, private_trackers._nonnegative_int("12"))

    def test_exact_single_season_packs_are_allowed(self):
        for title in ("Show.S01.Complete.1080p.BluRay",
                      "Show Season 1 2160p WEB-DL"):
            self.assertEqual("season",
                             private_trackers.classify_release(title, 1, 4))

    def test_individual_episode_is_allowed(self):
        self.assertEqual(
            "episode",
            private_trackers.classify_release(
                "Show.S01E04.1080p.WEB-DL-GROUP", 1, 4))

    def test_whole_series_and_multi_season_packs_are_low_priority(self):
        for title in ("Show Complete Series 1080p",
                      "Show S01-S05 Complete BluRay",
                      "Show Seasons 1-5 WEB-DL",
                      "Show S01 S02 1080p"):
            self.assertEqual("series",
                             private_trackers.classify_release(title, 1, 4), title)

    def test_wrong_episode_or_season_is_rejected(self):
        self.assertEqual("", private_trackers.classify_release(
            "Show.S01E03.1080p.WEB-DL", 1, 4))
        self.assertEqual("", private_trackers.classify_release(
            "Show.S02.Complete.1080p.WEB-DL", 1, 4))

    def test_individual_episode_is_preferred_over_season_pack(self):
        pack = {"kind": "season", "title": "Show S01 720p WEB-DL",
                "seeders": 1, "size": 1}
        episode = {"kind": "episode", "title": "Show S01E04 2160p REMUX",
                   "seeders": 100, "size": 100}
        self.assertGreater(private_trackers._quality_key(episode),
                           private_trackers._quality_key(pack))

    def test_series_pack_ranks_below_individual_episode(self):
        series = {"kind": "series", "title": "Show Complete Series 2160p REMUX",
                  "seeders": 100, "size": 100}
        episode = {"kind": "episode", "title": "Show S01E04 720p WEB-DL",
                   "seeders": 1, "size": 1}
        self.assertGreater(private_trackers._quality_key(episode),
                           private_trackers._quality_key(series))

    def test_release_preference_can_put_season_pack_first(self):
        pack = {"kind": "season", "title": "Show S01 720p WEB-DL",
                "seeders": 1, "size": 1}
        episode = {"kind": "episode", "title": "Show S01E04 2160p REMUX",
                   "seeders": 100, "size": 100}
        with mock.patch.object(
                private_trackers, "RELEASE_RANK",
                {"season": 3, "episode": 2, "series": 1}):
            self.assertGreater(private_trackers._quality_key(pack),
                               private_trackers._quality_key(episode))

    def test_indexer_score_defaults_to_neutral(self):
        self.assertEqual(50, private_trackers._indexer_score("Anything"))
        with mock.patch.object(private_trackers, "INDEXER_SCORES",
                               {"favtracker": 3}):
            self.assertEqual(3, private_trackers._indexer_score("FavTracker"))
            self.assertEqual(3, private_trackers._indexer_score("favtracker"))
            self.assertEqual(50, private_trackers._indexer_score("Unlisted"))

    def test_preferred_tracker_outranks_neutral_within_release_kind(self):
        favorite = {"kind": "episode", "title": "Show S01E04 720p WEB-DL",
                    "seeders": 1, "size": 1, "indexer": "FavTracker"}
        other = {"kind": "episode", "title": "Show S01E04 2160p REMUX",
                 "seeders": 100, "size": 100, "indexer": "OtherTracker"}
        with mock.patch.object(private_trackers, "INDEXER_SCORES",
                               {"favtracker": 1, "othertracker": 50}):
            self.assertGreater(private_trackers._quality_key(favorite),
                               private_trackers._quality_key(other))

    def test_neutral_scores_preserve_pure_quality_order(self):
        low = {"kind": "episode", "title": "Show S01E04 720p WEB-DL",
               "seeders": 1, "size": 1, "indexer": "A"}
        high = {"kind": "episode", "title": "Show S01E04 2160p REMUX",
                "seeders": 1, "size": 1, "indexer": "B"}
        self.assertGreater(private_trackers._quality_key(high),
                           private_trackers._quality_key(low))

    def test_private_result_names_its_exact_tracker(self):
        rows = private_trackers.fallback_streams("series", "tt1:1:4", [{
            "media": "series", "media_id": "tt1:1:4",
            "title": "Show.S01E04.1080p.WEB-DL", "kind": "episode",
            "size": 2_000_000_000, "seeders": 14,
            "indexer": "TorrentLeech", "indexer_id": 7,
            "download_url": "http://prowlarr/7/download", "guid": "g",
            "season": 1, "episode": 4,
        }])
        self.assertEqual(1, len(rows))
        self.assertIn("TorrentLeech", rows[0]["name"])
        self.assertIn("1080p", rows[0]["name"])
        self.assertIn("Private tracker: TorrentLeech", rows[0]["title"])
        self.assertIn("Private tracker: TorrentLeech",
                      rows[0]["description"])

    def test_indexer_name_removes_line_breaks(self):
        self.assertEqual("Tracker Name",
                         private_trackers._indexer_name(" Tracker\nName "))

    def test_indexer_provenance_tag_round_trips(self):
        tag = private_trackers._indexer_tag("BeyondHD")
        self.assertEqual("BeyondHD", private_trackers._indexer_from_tags(
            f"unrelated,{tag}"))


def _cand(title, **over):
    row = {"media": "series", "media_id": "tt1:1:4", "title": title,
           "kind": "episode", "size": 2_000_000_000, "seeders": 14,
           "indexer": "TorrentLeech", "indexer_id": 7,
           "download_url": "http://prowlarr/7/download", "guid": title,
           "season": 1, "episode": 4}
    row.update(over)
    return row


class RationedNetworkTests(unittest.IsolatedAsyncioTestCase):
    """The AvistaZ network shares one API quota; see _RATIONED_IMPLEMENTATIONS."""

    # AvistaZ 5, CinemaZ 16, PrivateHD 35 take IMDb ids; AnimeZ 28 does not
    # and carries anime only; M-Team 45 is an ordinary tracker.
    T = private_trackers._IndexerTraits
    TRAITS = {5: T(True, True, True), 16: T(True, True, True),
              35: T(True, True, True), 28: T(True, anime_only=True),
              45: T()}

    def setUp(self):
        private_trackers._query_cache.clear()
        private_trackers._rationed_next = 0.0
        self._traits = mock.patch.dict(private_trackers._indexer_traits,
                                       self.TRAITS, clear=True)
        self._traits.start()
        # A contended asyncio.Lock binds to its event loop; each test runs in
        # its own loop, so each gets its own lock.
        self._lock = mock.patch.object(private_trackers, "_rationed_lock",
                                       asyncio.Lock())
        self._lock.start()

    def tearDown(self):
        self._lock.stop()
        self._traits.stop()
        private_trackers._query_cache.clear()
        private_trackers._rationed_next = 0.0

    def test_rationed_requests_are_one_id_search_and_two_titles(self):
        requests, _later = private_trackers._rationed_tiers(
            "series", "tt1395015:17:1",
            ["Love Stories in the Countryside", "乡村爱情", "Country Love",
             "Rural Love Story"], (17, 1), {5, 16, 35, 28})
        self.assertEqual([
            ("tvsearch", "{ImdbId:tt1395015}{Season:17}", {5, 16, 35}),
            ("search", "Love Stories in the Countryside", {5, 16, 35, 28}),
            ("search", "Country Love", {5, 16, 35, 28}),
        ], requests)
        movie = private_trackers._rationed_tiers(
            "movie", "tt6751668", ["Parasite"], None, {5, 28})[0]
        self.assertEqual([("movie", "{ImdbId:tt6751668}", {5}),
                          ("search", "Parasite", {5, 28})], movie)
        # Live action: the anime-only site gets nothing.
        live = private_trackers._rationed_tiers(
            "movie", "tt6751668", ["Parasite"], None, {5, 28}, False)[0]
        self.assertEqual([("movie", "{ImdbId:tt6751668}", {5}),
                          ("search", "Parasite", {5})], live)

    async def test_search_sends_the_network_only_its_short_list(self):
        calls = []
        pack = {"indexerId": 5, "indexer": "AvistaZ", "protocol": "torrent",
                "title": "The First Frost S01 (2025) 1080p NF WEB-DL AAC2.0 "
                         "H.265-teamACH",
                "seeders": 22, "size": 30_000_000_000, "guid": "az-1",
                "downloadUrl": "http://prowlarr/dl/az-1"}

        async def query(q, ids, kind="search"):
            calls.append((kind, q, frozenset(ids)))
            return [pack] if 5 in ids else []

        with mock.patch.object(
                private_trackers, "_private_torrent_indexers",
                new=mock.AsyncMock(return_value={
                    5: "AvistaZ", 16: "CinemaZ", 35: "PrivateHD",
                    28: "AnimeZ", 45: "M-Team - TP"})), \
                mock.patch.object(private_trackers, "_prowlarr_query",
                                  new=query), \
                mock.patch.object(private_trackers.usenet, "_expected_info",
                                  new=mock.AsyncMock(return_value=(
                                      ["The First Frost", "难哄"], 2025))), \
                mock.patch.object(private_trackers, "MIN_SEEDERS", 1), \
                mock.patch.object(private_trackers, "RATIONED_SPACING", 0), \
                mock.patch.object(private_trackers.meta, "is_animation",
                                  new=mock.AsyncMock(return_value=False)), \
                mock.patch.object(private_trackers.telemetry,
                                  "record_private_tracker"):
            found = await private_trackers._search("series", "tt31887832:1:1")
        self.assertFalse(any(28 in ids for _, _, ids in calls))
        rationed = [c for c in calls if c[2] & {5, 16, 35, 28}]
        self.assertEqual([("tvsearch", "{ImdbId:tt31887832}{Season:1}"),
                          ("search", "The First Frost")],
                         [(k, q) for k, q, _ in rationed])
        self.assertTrue(all(ids <= {5, 16, 35, 28} for _, _, ids in rationed))
        # The ordinary fan-out still covers every other tracker.
        self.assertIn(("search", "难哄 S01E01", frozenset({45})), calls)
        self.assertEqual(["season"], [c["kind"] for c in found])

    async def test_rationed_queries_are_spaced_and_cache_hits_skip_the_queue(self):
        query = mock.AsyncMock(return_value=[{"title": "x"}])
        sleep = mock.AsyncMock()
        with mock.patch.object(private_trackers, "_prowlarr_query", new=query), \
                mock.patch.object(private_trackers.asyncio, "sleep", new=sleep):
            await private_trackers._rationed_rows(
                [("search", "A", {5}), ("search", "B", {5})])
            self.assertEqual(1, sleep.await_count)
            self.assertAlmostEqual(private_trackers.RATIONED_SPACING,
                                   sleep.await_args.args[0], delta=0.5)
            private_trackers._query_cache[("search", "C", frozenset({5}))] = (
                private_trackers.time.monotonic() + 60, [{"title": "c"}])
            rows = await private_trackers._rationed_rows(
                [("search", "C", {5})])
        self.assertEqual([{"title": "c"}], rows)
        self.assertEqual(2, query.await_count)       # C came from the cache
        self.assertEqual(1, sleep.await_count)

    async def test_rationed_rows_survive_one_failed_query(self):
        query = mock.AsyncMock(side_effect=[httpx.ReadTimeout("slow"),
                                            [{"title": "ok"}]])
        with mock.patch.object(private_trackers, "_prowlarr_query", new=query), \
                mock.patch.object(private_trackers.asyncio, "sleep",
                                  new=mock.AsyncMock()):
            rows = await private_trackers._rationed_rows(
                [("search", "A", {5}), ("search", "B", {5})])
        self.assertEqual([{"title": "ok"}], rows)

    async def test_concurrent_lookups_share_one_query_without_waiting_twice(self):
        # E01 and E02 of one season ask the same season-scoped query at once.
        calls = 0

        async def get(*args, **kwargs):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)
            return httpx.Response(200, json=[{"title": "pack"}],
                                  request=httpx.Request("GET", "http://p"))

        request = [("tvsearch", "{ImdbId:tt1}{Season:1}", {5})]
        with mock.patch.object(private_trackers._client, "get", new=get), \
                mock.patch.object(private_trackers, "RATIONED_SPACING", 5.0):
            started = private_trackers.time.monotonic()
            first, second = await asyncio.gather(
                private_trackers._rationed_rows(request),
                private_trackers._rationed_rows(request))
            elapsed = private_trackers.time.monotonic() - started
        self.assertEqual(1, calls)
        self.assertEqual(first, second)
        self.assertLess(elapsed, 1.0)        # the second never sat out 5 s

    async def test_a_queued_lookup_gives_up_at_its_deadline(self):
        query = mock.AsyncMock(return_value=[{"title": "x"}])
        with mock.patch.object(private_trackers, "_prowlarr_query", new=query):
            await private_trackers._rationed_lock.acquire()
            try:
                started = private_trackers.time.monotonic()
                rows = await private_trackers._rationed_rows(
                    [("search", "A", {5})], budget=0.2)
                elapsed = private_trackers.time.monotonic() - started
            finally:
                private_trackers._rationed_lock.release()
        self.assertEqual([], rows)
        self.assertLess(elapsed, 1.0)
        query.assert_not_awaited()

    async def test_a_hung_query_returns_what_was_collected(self):
        async def query(q, ids, kind="search"):
            if q == "B":
                await asyncio.Event().wait()     # never answers
            return [{"title": q}]

        with mock.patch.object(private_trackers, "_prowlarr_query", new=query), \
                mock.patch.object(private_trackers, "RATIONED_SPACING", 0):
            started = private_trackers.time.monotonic()
            rows = await private_trackers._rationed_rows(
                [("search", "A", {5}), ("search", "B", {5}),
                 ("search", "C", {5})], budget=0.3)
            elapsed = private_trackers.time.monotonic() - started
        self.assertEqual([{"title": "A"}], rows)
        self.assertLess(elapsed, 1.0)

    async def test_failed_network_keeps_other_trackers_results(self):
        row = {"indexerId": 45, "indexer": "M-Team - TP", "protocol": "torrent",
               "title": "The First Frost 2025 S01 2160p WEB-DL H.265 AAC-HHWEB",
               "seeders": 103, "size": 60_000_000_000, "guid": "mt-1",
               "downloadUrl": "http://prowlarr/dl/mt-1"}

        async def query(q, ids, kind="search"):
            if ids & {5, 16, 35, 28}:
                raise httpx.HTTPStatusError(
                    "429", request=httpx.Request("GET", "http://p"),
                    response=httpx.Response(429))
            return [row]

        with mock.patch.object(
                private_trackers, "_private_torrent_indexers",
                new=mock.AsyncMock(return_value={
                    5: "AvistaZ", 28: "AnimeZ", 45: "M-Team - TP"})), \
                mock.patch.object(private_trackers, "_prowlarr_query",
                                  new=query), \
                mock.patch.object(private_trackers.usenet, "_expected_info",
                                  new=mock.AsyncMock(return_value=(
                                      ["The First Frost"], 2025))), \
                mock.patch.object(private_trackers.meta, "is_animation",
                                  new=mock.AsyncMock(return_value=None)), \
                mock.patch.object(private_trackers, "MIN_SEEDERS", 1), \
                mock.patch.object(private_trackers, "RATIONED_SPACING", 0), \
                mock.patch.object(private_trackers.telemetry,
                                  "record_private_tracker"):
            found = await private_trackers._search("series", "tt31887832:1:1")
        self.assertEqual([row["title"]], [c["title"] for c in found])
        self.assertEqual("ok", private_trackers.search_outcome(
            "series", "tt31887832:1:1")["state"])

    async def test_query_cache_reuses_answers_and_distrusts_empties(self):
        responses = [httpx.Response(200, json=[{"title": "hit"}],
                                    request=httpx.Request("GET", "http://p")),
                     httpx.Response(200, json=[],
                                    request=httpx.Request("GET", "http://p"))]
        get = mock.AsyncMock(side_effect=responses)
        with mock.patch.object(private_trackers._client, "get", new=get):
            first = await private_trackers._prowlarr_query("Show S01", {45})
            again = await private_trackers._prowlarr_query("Show S01", {45})
            await private_trackers._prowlarr_query("Show S02", {45})
        self.assertEqual(first, again)
        self.assertEqual(2, get.await_count)
        full, _ = private_trackers._query_cache[
            ("search", "Show S01", frozenset({45}))]
        empty, _ = private_trackers._query_cache[
            ("search", "Show S02", frozenset({45}))]
        self.assertLess(empty, full)
        self.assertLessEqual(empty - private_trackers.time.monotonic(),
                             private_trackers._EMPTY_QUERY_TTL)

    async def test_indexer_catalogue_records_the_network_and_id_support(self):
        catalogue = [
            {"id": 5, "name": "AvistaZ", "implementation": "AvistaZ",
             "protocol": "torrent", "privacy": "private", "enable": True,
             "capabilities": {"tvSearchParams": ["q", "season", "imdbId"],
                              "movieSearchParams": ["q", "imdbId"]}},
            {"id": 28, "name": "AnimeZ", "implementation": "AnimeZ",
             "protocol": "torrent", "privacy": "private", "enable": True,
             "capabilities": {"tvSearchParams": ["q", "season", "ep"]}},
            {"id": 45, "name": "M-Team - TP", "implementation": "MTeamTp",
             "protocol": "torrent", "privacy": "private", "enable": True,
             "capabilities": {"tvSearchParams": ["q", "imdbId"]}},
        ]
        get = mock.AsyncMock(return_value=httpx.Response(
            200, json=catalogue, request=httpx.Request("GET", "http://p")))
        with mock.patch.object(private_trackers._client, "get", new=get), \
                mock.patch.object(private_trackers, "_private_indexers", None):
            await private_trackers._private_torrent_indexers()
        T = private_trackers._IndexerTraits
        self.assertEqual(T(True, True, True, False),
                         private_trackers._indexer_traits[5])
        self.assertEqual(T(True, False, False, True),
                         private_trackers._indexer_traits[28])
        self.assertEqual(T(False, True, False, False),
                         private_trackers._indexer_traits[45])


class VideoTrackerFilterTests(unittest.IsolatedAsyncioTestCase):
    """Games, music and book trackers are never searched, and a lookup only
    goes to trackers that can carry that kind of release."""

    CATALOGUE = [
        ("M-Team - TP", 45, [{"id": 2000}, {"id": 5000}]),
        ("GazelleGames", 23, [{"id": 4000}, {"id": 1000}]),
        ("Redacted", 10, [{"id": 3000}, {"id": 7000}]),
        ("GreatPosterWall", 25, [{"id": 2000, "subCategories": [{"id": 2040}]}]),
        ("Nebulance", 20, [{"id": 100000, "subCategories": [{"id": 5040}]}]),
        ("Unlisted", 99, []),
    ]

    def setUp(self):
        private_trackers._query_cache.clear()
        private_trackers._search_outcomes.clear()
        self._traits = mock.patch.dict(private_trackers._indexer_traits, {},
                                       clear=True)
        self._traits.start()

    def tearDown(self):
        self._traits.stop()

    async def _load_catalogue(self):
        rows = [{"id": i, "name": name, "implementation": name,
                 "protocol": "torrent", "privacy": "private", "enable": True,
                 "capabilities": {"categories": cats}}
                for name, i, cats in self.CATALOGUE]
        get = mock.AsyncMock(return_value=httpx.Response(
            200, json=rows, request=httpx.Request("GET", "http://p")))
        with mock.patch.object(private_trackers._client, "get", new=get), \
                mock.patch.object(private_trackers, "_private_indexers", None):
            return await private_trackers._private_torrent_indexers()

    async def test_categories_decide_movies_and_tv(self):
        await self._load_catalogue()
        traits = private_trackers._indexer_traits
        self.assertEqual((True, True), (traits[45].movies, traits[45].tv))
        self.assertEqual((False, False), (traits[23].movies, traits[23].tv))
        self.assertEqual((False, False), (traits[10].movies, traits[10].tv))
        self.assertEqual((True, False), (traits[25].movies, traits[25].tv))
        self.assertEqual((False, True), (traits[20].movies, traits[20].tv))
        # No category data: kept, a catalogue gap must not hide a source.
        self.assertEqual((True, True), (traits[99].movies, traits[99].tv))

    async def test_each_lookup_asks_only_trackers_that_can_have_it(self):
        indexers = await self._load_catalogue()
        asked: dict[str, set] = {}

        async def query(q, ids, kind="search"):
            asked.setdefault(media, set()).update(ids)
            return []

        for media, media_id, titles in (
                ("series", "tt1:1:1", ["Some Show"]),
                ("movie", "tt2", ["Some Film"])):
            with mock.patch.object(private_trackers, "_private_torrent_indexers",
                                   new=mock.AsyncMock(return_value=indexers)), \
                    mock.patch.object(private_trackers, "_prowlarr_query",
                                      new=query), \
                    mock.patch.object(private_trackers.usenet, "_expected_info",
                                      new=mock.AsyncMock(
                                          return_value=(titles, 2020))), \
                    mock.patch.object(private_trackers.usenet, "_accepted_years",
                                      new=mock.AsyncMock(
                                          return_value=frozenset({2020}))), \
                    mock.patch.object(private_trackers.telemetry,
                                      "record_private_tracker"):
                await private_trackers._search(media, media_id)
        self.assertEqual({45, 20, 99}, asked["series"])
        self.assertEqual({45, 25, 99}, asked["movie"])


class AlreadyDownloadedOrderingTests(unittest.TestCase):
    """A private search returns ~20 near-identical rows. The one already on
    disk plays now, costs no ratio and no download slot — so it leads, and says
    so, instead of being left for the user to spot by luck."""

    def _names(self, rows):
        return [r["name"] for r in rows]

    def test_downloaded_result_is_promoted_to_the_top(self):
        found = [_cand("Show.S01E04.720p.WEB-DL"),
                 _cand("Show.S01E04.2160p.REMUX"),
                 _cand("Show.S01E04.1080p.WEB-DL")]
        have = {private_trackers._release_fingerprint("Show.S01E04.1080p.WEB-DL"):
                {"complete": True, "progress": 1.0}}
        rows = private_trackers.fallback_streams("series", "tt1:1:4", found, have)
        self.assertIn("Already Downloaded", rows[0]["name"])
        self.assertIn("1080p", rows[0]["name"])

    def test_partially_downloaded_ranks_between_complete_and_new(self):
        found = [_cand("Show.S01E04.720p.WEB-DL"),
                 _cand("Show.S01E04.2160p.REMUX"),
                 _cand("Show.S01E04.1080p.WEB-DL")]
        have = {
            private_trackers._release_fingerprint("Show.S01E04.1080p.WEB-DL"):
                {"complete": False, "progress": 0.42},
            private_trackers._release_fingerprint("Show.S01E04.2160p.REMUX"):
                {"complete": True, "progress": 1.0},
        }
        rows = private_trackers.fallback_streams("series", "tt1:1:4", found, have)
        self.assertIn("Already Downloaded", rows[0]["name"])
        self.assertIn("Already Downloading 42%", rows[1]["name"])
        self.assertIn("Click to Download", rows[2]["name"])

    def test_quality_order_survives_inside_each_group(self):
        # Nothing downloaded: the search's own ranking must be untouched.
        found = [_cand("Show.S01E04.2160p.REMUX"),
                 _cand("Show.S01E04.1080p.WEB-DL"),
                 _cand("Show.S01E04.720p.WEB-DL")]
        rows = private_trackers.fallback_streams("series", "tt1:1:4", found, {})
        self.assertEqual(["2160p", "1080p", "720p"],
                         [n.split(" · ")[1] for n in self._names(rows)])

    def test_completed_row_is_not_an_action_row(self):
        found = [_cand("Show.S01E04.1080p.WEB-DL")]
        have = {private_trackers._release_fingerprint("Show.S01E04.1080p.WEB-DL"):
                {"complete": True, "progress": 1.0}}
        rows = private_trackers.fallback_streams("series", "tt1:1:4", found, have)
        self.assertFalse(rows[0]["_private_action"])
        # …while one still downloading remains an action row.
        have2 = {private_trackers._release_fingerprint("Show.S01E04.1080p.WEB-DL"):
                 {"complete": False, "progress": 0.5}}
        rows2 = private_trackers.fallback_streams("series", "tt1:1:4", found, have2)
        self.assertTrue(rows2[0]["_private_action"])

    def test_behaves_exactly_as_before_without_an_index(self):
        found = [_cand("Show.S01E04.1080p.WEB-DL")]
        rows = private_trackers.fallback_streams("series", "tt1:1:4", found)
        self.assertIn("Click to Download & Stream", rows[0]["name"])
        self.assertTrue(rows[0]["_private_action"])

    def test_fingerprint_ignores_punctuation_but_not_different_encodes(self):
        fp = private_trackers._release_fingerprint
        # Tracker title vs the client's torrent name for the same release.
        self.assertEqual(fp("Show.S01E04.1080p.WEB-DL-GRP"),
                         fp("Show S01E04 1080p WEB DL GRP"))
        # Two genuinely different encodes must not collide — promising a
        # download we do not have is the one failure that matters here.
        self.assertNotEqual(fp("Show.S01E04.1080p.WEB-DL-GRP"),
                            fp("Show.S01E04.2160p.WEB-DL-GRP"))


class TorrentMetainfoTests(unittest.TestCase):
    def test_info_hash_is_over_raw_info_dictionary(self):
        info = b"d4:name4:test6:lengthi123ee"
        torrent = b"d8:announce14:https://t.test4:info" + info + b"e"
        self.assertEqual(hashlib.sha1(info).hexdigest(),
                         private_trackers.torrent_info_hash(torrent))

    def test_missing_info_dictionary_is_rejected(self):
        with self.assertRaises(ValueError):
            private_trackers.torrent_info_hash(b"d4:name4:teste")

    def test_browser_facing_prowlarr_host_is_rewritten_internal(self):
        with mock.patch.object(private_trackers, "PROWLARR_URL",
                               "http://prowlarr:9696"):
            url = private_trackers._prowlarr_download_url(
                "http://127.0.0.1:9696/7/download?token=opaque")
        self.assertEqual("http://prowlarr:9696/7/download?token=opaque", url)


class ActivationBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        private_trackers._tokens.clear()
        private_trackers._token_locks.clear()
        private_trackers._rqbit_vpn_checked_at = 0.0

    async def test_head_never_activates_torrent(self):
        private_trackers._tokens["cap"] = (
            __import__("time").time(), {"mode": "candidate"})
        with mock.patch.object(
                private_trackers, "_activate_candidate",
                new=mock.AsyncMock()) as activate:
            response = await private_trackers.serve("cap", _request("HEAD"))
        self.assertEqual(204, response.status_code)
        activate.assert_not_awaited()

    async def test_pack_is_staged_so_clicked_episode_downloads_first(self):
        # Everything but the clicked file parks at 0: a sequential torrent
        # picks the lowest-index *wanted* piece, so leaving the rest at 1 would
        # start the pack at file 0 instead of the episode being watched.
        files = [{"index": 0}, {"index": 1}, {"index": 2}]
        response = httpx.Response(200, request=httpx.Request("POST", "http://q"))
        with mock.patch.object(
                private_trackers, "_qapi",
                new=mock.AsyncMock(return_value=response)) as api, \
                mock.patch.object(private_trackers,
                                  "_watch_staged_release") as watch:
            await private_trackers._prioritize_all("abc", files, 1)
        calls = api.await_args_list
        self.assertEqual("0|2", calls[0].kwargs["data"]["id"])
        self.assertEqual("0", calls[0].kwargs["data"]["priority"])
        self.assertEqual("1", calls[1].kwargs["data"]["id"])
        self.assertEqual("7", calls[1].kwargs["data"]["priority"])
        self.assertEqual("/torrents/setShareLimits", calls[2].args[1])
        self.assertEqual("-1", calls[2].kwargs["data"]["ratioLimit"])
        self.assertEqual("-1", calls[2].kwargs["data"]["seedingTimeLimit"])
        watch.assert_called_once_with("abc", 1)

    async def test_finished_episode_leaves_rest_of_pack_enabled(self):
        # A later open of the same torrent must not undo a promotion that
        # already happened, or the pack would stop completing.
        files = [{"index": 0}, {"index": 1, "progress": 1.0}, {"index": 2}]
        response = httpx.Response(200, request=httpx.Request("POST", "http://q"))
        with mock.patch.object(
                private_trackers, "_qapi",
                new=mock.AsyncMock(return_value=response)) as api, \
                mock.patch.object(private_trackers,
                                  "_watch_staged_release") as watch:
            await private_trackers._prioritize_all("abc", files, 1)
        self.assertEqual("1", api.await_args_list[0].kwargs["data"]["priority"])
        watch.assert_not_called()

    async def test_partial_release_mode_never_promotes_the_rest(self):
        files = [{"index": 0}, {"index": 1}, {"index": 2}]
        response = httpx.Response(200, request=httpx.Request("POST", "http://q"))
        with mock.patch.object(private_trackers, "WHOLE_TORRENT", False), \
                mock.patch.object(
                    private_trackers, "_qapi",
                    new=mock.AsyncMock(return_value=response)) as api, \
                mock.patch.object(private_trackers,
                                  "_watch_staged_release") as watch:
            await private_trackers._prioritize_all("abc", files, 1)
        self.assertEqual("0", api.await_args_list[0].kwargs["data"]["priority"])
        watch.assert_not_called()

    async def test_staged_pack_resumes_once_the_episode_is_whole(self):
        partial = [{"index": 0}, {"index": 1, "progress": 0.4}, {"index": 2}]
        done = [{"index": 0}, {"index": 1, "progress": 1.0}, {"index": 2}]
        response = httpx.Response(200, request=httpx.Request("POST", "http://q"))
        with mock.patch.object(
                private_trackers, "_files",
                new=mock.AsyncMock(side_effect=[partial, done])), \
                mock.patch.object(
                    private_trackers, "_qapi",
                    new=mock.AsyncMock(return_value=response)) as api, \
                mock.patch("app.private_trackers.asyncio.sleep",
                           new=mock.AsyncMock()):
            private_trackers._watch_staged_release("abc", 1)
            await private_trackers._promote_tasks["abc"]
        call = api.await_args_list[-1]
        self.assertEqual("/torrents/filePrio", call.args[1])
        self.assertEqual("0|2", call.kwargs["data"]["id"])
        self.assertEqual("1", call.kwargs["data"]["priority"])

    async def test_active_download_count_ignores_completed_seeds(self):
        response = httpx.Response(
            200, json=[{"progress": 0.2}, {"progress": 1.0},
                       {"progress": 0.999}],
            request=httpx.Request("GET", "http://q"))
        with mock.patch.object(private_trackers, "_qapi",
                               new=mock.AsyncMock(return_value=response)):
            self.assertEqual(2, await private_trackers._active_download_count())

    async def test_wait_for_files_retries_registration_404(self):
        missing = httpx.HTTPStatusError(
            "not registered",
            request=httpx.Request("GET", "http://q/torrents/files"),
            response=httpx.Response(404))
        files = [{"index": 0, "name": "Movie.2160p.mkv", "size": 100}]
        with mock.patch.object(
                private_trackers, "_files",
                new=mock.AsyncMock(side_effect=[missing, files])) as query, \
                mock.patch("app.private_trackers.asyncio.sleep",
                           new=mock.AsyncMock()):
            self.assertEqual(files, await private_trackers._wait_for_files("abc"))
        self.assertEqual(2, query.await_count)

    async def test_activation_reuses_hash_after_downstream_retry(self):
        payload = {
            "media": "movie", "media_id": "tt1", "kind": "movie",
            "download_url": "http://prowlarr/download", "title": "Movie",
        }
        torrent = b"torrent bytes"
        files = [{"index": 0, "name": "Movie.2160p.mkv", "size": 100}]
        ok = httpx.Response(200, text="Ok.",
                            request=httpx.Request("POST", "http://q/add"))
        with mock.patch.object(private_trackers, "_torrent_bytes",
                               new=mock.AsyncMock(return_value=torrent)) as fetch, \
                mock.patch.object(private_trackers, "torrent_info_hash",
                                  return_value="abc"), \
                mock.patch.object(private_trackers, "_ensure_category",
                                  new=mock.AsyncMock()), \
                mock.patch.object(private_trackers, "_torrent_info",
                                  new=mock.AsyncMock(side_effect=[None, {}])), \
                mock.patch.object(private_trackers, "_active_download_count",
                                  new=mock.AsyncMock(return_value=0)), \
                mock.patch.object(private_trackers, "_qapi",
                                  new=mock.AsyncMock(return_value=ok)) as qapi, \
                mock.patch.object(private_trackers, "_wait_for_files",
                                  new=mock.AsyncMock(side_effect=[
                                      RuntimeError("registration gap"), files])), \
                mock.patch.object(private_trackers, "_prioritize_all",
                                  new=mock.AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, "registration gap"):
                await private_trackers._activate_candidate(payload)
            result = await private_trackers._activate_candidate(payload)
        self.assertEqual("existing", result["mode"])
        self.assertEqual(1, fetch.await_count)
        add = next(call for call in qapi.await_args_list
                   if call.args[1] == "/torrents/add")
        self.assertIn("stream-picker-indexer=Private tracker",
                      add.kwargs["data"]["tags"])

    async def test_rqbit_activation_focuses_selected_file_and_stops_qbit(self):
        payload = {
            "media": "series", "media_id": "tt1:1:2", "kind": "season",
            "season": 1, "episode": 2, "indexer": "BeyondHD",
        }
        details = {
            "info_hash": "abc",
            "files": [
                {"name": "Show.S01E01.mkv", "length": 100},
                {"name": "Show.S01E02.mkv", "length": 200},
            ],
        }
        with mock.patch.object(private_trackers, "_rqbit_list_metainfo",
                               new=mock.AsyncMock(return_value=details)), \
                mock.patch.object(private_trackers, "_torrent_info",
                                  new=mock.AsyncMock(return_value={
                                      "state": "downloading"})), \
                mock.patch.object(private_trackers, "_stop_qbit",
                                  new=mock.AsyncMock()) as stop, \
                mock.patch.object(private_trackers, "_add_qbit_stopped",
                                  new=mock.AsyncMock()) as register, \
                mock.patch.object(private_trackers, "_rqbit_add",
                                  new=mock.AsyncMock(return_value=details)) as add, \
                mock.patch.object(private_trackers, "_watch_rqbit_completion") as watch, \
                mock.patch.object(private_trackers, "_notify_picker"):
            result = await private_trackers._activate_rqbit_candidate(
                payload, b"torrent", "abc")
        stop.assert_awaited_once_with("abc")
        register.assert_not_awaited()
        add.assert_awaited_once_with(b"torrent", 1)
        self.assertEqual("rqbit", result["mode"])
        self.assertEqual(1, result["file_index"])
        self.assertTrue(result["_rqbit_prepared"])
        watch.assert_called_once()

    async def test_rqbit_activation_registers_qbit_stopped_before_download(self):
        payload = {
            "media": "movie", "media_id": "tt1", "kind": "movie",
            "indexer": "BeyondHD",
        }
        details = {
            "info_hash": "abc",
            "files": [{"name": "Movie.mkv", "length": 200}],
        }
        with mock.patch.object(private_trackers, "_rqbit_list_metainfo",
                               new=mock.AsyncMock(return_value=details)), \
                mock.patch.object(private_trackers, "_torrent_info",
                                  new=mock.AsyncMock(return_value=None)), \
                mock.patch.object(private_trackers, "_active_download_count",
                                  new=mock.AsyncMock(return_value=0)), \
                mock.patch.object(private_trackers,
                                  "_rqbit_active_download_count",
                                  new=mock.AsyncMock(return_value=0)), \
                mock.patch.object(private_trackers, "_add_qbit_stopped",
                                  new=mock.AsyncMock()) as register, \
                mock.patch.object(private_trackers, "_rqbit_add",
                                  new=mock.AsyncMock(return_value=details)), \
                mock.patch.object(private_trackers,
                                  "_watch_rqbit_completion"), \
                mock.patch.object(private_trackers, "_notify_picker"):
            await private_trackers._activate_rqbit_candidate(
                payload, b"torrent", "abc")
        register.assert_awaited_once_with("abc", b"torrent", "BeyondHD")

    async def test_qbit_stopped_registration_matches_rqbit_flat_layout(self):
        response = httpx.Response(
            200, text="Ok.",
            request=httpx.Request("POST", "http://qbit/torrents/add"))
        with mock.patch.object(private_trackers, "_ensure_category",
                               new=mock.AsyncMock()), \
                mock.patch.object(private_trackers, "_torrent_info",
                                  new=mock.AsyncMock(return_value=None)), \
                mock.patch.object(private_trackers, "_qapi",
                                  new=mock.AsyncMock(return_value=response)) as api:
            await private_trackers._add_qbit_stopped(
                "abc", b"torrent", "BeyondHD")
        add = next(call for call in api.await_args_list
                   if call.args[1] == "/torrents/add")
        self.assertEqual("NoSubfolder",
                         add.kwargs["data"]["contentLayout"])

    async def test_handoff_rechecks_before_qbittorrent_starts(self):
        files = [{"index": 0, "name": "Movie.mkv", "size": 100}]
        events = []

        def marker(name, result=None):
            async def run(*_args, **_kwargs):
                events.append(name)
                return result
            return run

        with mock.patch.object(private_trackers, "_rqbit_stop",
                               side_effect=marker("rqbit-stop")), \
                mock.patch.object(private_trackers, "_rqbit_details",
                                  new=mock.AsyncMock(return_value={
                                      "files": [{"length": 100}]})), \
                mock.patch.object(private_trackers, "_torrent_info",
                                  new=mock.AsyncMock(return_value={
                                      "state": "stoppedDL"})), \
                mock.patch.object(private_trackers, "_files",
                                  new=mock.AsyncMock(return_value=files)), \
                mock.patch.object(private_trackers, "_stop_qbit",
                                  side_effect=marker("qbit-stop")), \
                mock.patch.object(private_trackers, "_wait_for_files",
                                  side_effect=marker("qbit-files", files)), \
                mock.patch.object(private_trackers, "_prioritize_all",
                                  side_effect=marker("qbit-policy")), \
                mock.patch.object(private_trackers, "_qapi",
                                  side_effect=marker("qbit-recheck")), \
                mock.patch.object(private_trackers, "_wait_qbit_rechecked",
                                  side_effect=marker("qbit-checked", files)), \
                mock.patch.object(private_trackers, "_start_qbit",
                                  side_effect=marker("qbit-start")), \
                mock.patch.object(private_trackers, "_rqbit_forget",
                                  side_effect=marker("rqbit-forget")), \
                mock.patch("app.private_trackers.asyncio.sleep",
                           new=mock.AsyncMock()), \
                mock.patch.object(private_trackers, "_notify_picker"):
            await private_trackers._handoff_rqbit(
                "abc", 0, "tt1", "BeyondHD")
        self.assertLess(events.index("qbit-recheck"), events.index("qbit-start"))
        self.assertLess(events.index("qbit-start"), events.index("rqbit-forget"))

    async def test_rqbit_vpn_gate_accepts_only_running_tunnel(self):
        running = httpx.Response(
            200, json={"status": "running"},
            request=httpx.Request("GET", "http://vpn/v1/vpn/status"))
        stopped = httpx.Response(
            200, json={"status": "stopped"},
            request=httpx.Request("GET", "http://vpn/v1/vpn/status"))
        with mock.patch.object(private_trackers, "RQBIT_VPN_URL",
                               "http://vpn"), \
                mock.patch.object(private_trackers, "RQBIT_VPN_API_KEY",
                                  "control-secret"), \
                mock.patch.object(private_trackers._vpn, "get",
                                  new=mock.AsyncMock(
                                      side_effect=[running, stopped])) as get:
            await private_trackers._ensure_rqbit_vpn(force=True)
            with self.assertRaisesRegex(RuntimeError, "not running"):
                await private_trackers._ensure_rqbit_vpn(force=True)
        self.assertEqual(
            "control-secret",
            get.await_args_list[0].kwargs["headers"]["X-API-Key"])
        self.assertEqual(0.0, private_trackers._rqbit_vpn_checked_at)

    async def test_rqbit_add_checks_vpn_before_submitting_torrent(self):
        with mock.patch.object(
                private_trackers, "_ensure_rqbit_vpn",
                new=mock.AsyncMock(side_effect=RuntimeError("VPN down"))), \
                mock.patch.object(
                    private_trackers, "_rapi",
                    new=mock.AsyncMock()) as api:
            with self.assertRaisesRegex(RuntimeError, "VPN down"):
                await private_trackers._rqbit_add(b"torrent", 0)
        api.assert_not_awaited()

    async def test_rqbit_start_accepts_already_initializing_response(self):
        response = httpx.Response(
            400, text="torrent is already running",
            request=httpx.Request("POST", "http://rqbit/torrents/abc/start"))
        with mock.patch.object(
                private_trackers, "_rapi",
                new=mock.AsyncMock(side_effect=httpx.HTTPStatusError(
                    "HTTP 400", request=response.request,
                    response=response))), \
                mock.patch.object(
                    private_trackers, "_rqbit_stats",
                    new=mock.AsyncMock(return_value={
                        "state": "initializing", "finished": False})):
            await private_trackers._rqbit_start("abc")

    async def test_rqbit_start_rejects_real_http_400(self):
        response = httpx.Response(
            400, text="cannot start torrent",
            request=httpx.Request("POST", "http://rqbit/torrents/abc/start"))
        with mock.patch.object(
                private_trackers, "_rapi",
                new=mock.AsyncMock(side_effect=httpx.HTTPStatusError(
                    "HTTP 400", request=response.request,
                    response=response))), \
                mock.patch.object(
                    private_trackers, "_rqbit_stats",
                    new=mock.AsyncMock(return_value={
                        "state": "error", "finished": False})), \
                self.assertRaises(httpx.HTTPStatusError):
            await private_trackers._rqbit_start("abc")

    async def test_rqbit_stop_accepts_already_paused_response(self):
        response = httpx.Response(
            400, text="torrent is already paused",
            request=httpx.Request("POST", "http://rqbit/torrents/abc/pause"))
        with mock.patch.object(
                private_trackers, "_rapi",
                new=mock.AsyncMock(side_effect=httpx.HTTPStatusError(
                    "HTTP 400", request=response.request,
                    response=response))), \
                mock.patch.object(
                    private_trackers, "_rqbit_stats",
                    new=mock.AsyncMock(return_value={
                        "state": "paused", "finished": True})):
            await private_trackers._rqbit_stop("abc")

    def test_private_capabilities_bypass_ordinary_proxy(self):
        stream = {"name": "🔒 Private Tracker", "url": "https://sp/private/x",
                  "_private_tracker": True}
        self.assertFalse(proxy._proxyable(stream))


class RangeAndPathTests(unittest.TestCase):
    def test_single_and_suffix_ranges(self):
        self.assertEqual((10, 99, 206),
                         private_trackers._parse_range("bytes=10-", 100))
        self.assertEqual((90, 99, 206),
                         private_trackers._parse_range("bytes=-10", 100))
        self.assertEqual((0, 99, 200), private_trackers._parse_range("", 100))

    def test_file_offsets_follow_torrent_order(self):
        rows = [{"index": 0, "size": 100}, {"index": 1, "size": 250}]
        self.assertEqual(100, private_trackers._file_offset(rows, 1))

    def test_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(private_trackers, "DOWNLOAD_ROOT",
                                  __import__("pathlib").Path(tmp)):
            with self.assertRaises(RuntimeError):
                private_trackers._safe_local_path("release", "../../secret")

    def test_rqbit_file_progress_is_normalized_per_file(self):
        rows = private_trackers._rqbit_file_rows({
            "files": [
                {"name": "Show.S01E01.mkv", "length": 100, "included": False},
                {"name": "Show.S01E02.mkv", "length": 200, "included": True},
            ],
        }, {"file_progress": [25, 100]})
        self.assertEqual([0.25, 0.5], [row["progress"] for row in rows])
        self.assertEqual([False, True], [row["included"] for row in rows])


class PrivateUiTests(unittest.TestCase):
    def test_private_tab_never_renders_saved_secrets(self):
        old = os.environ.get("CONFIG_FILE")
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CONFIG_FILE"] = os.path.join(tmp, "config.json")
            config.save({"PRIVATE_PROWLARR_API_KEY": "private-prowlarr-secret",
                         "PRIVATE_QBITTORRENT_PASSWORD": "private-qbit-secret",
                         "PRIVATE_RQBIT_PASSWORD": "private-rqbit-secret",
                         "PRIVATE_RQBIT_VPN_API_KEY": "private-vpn-secret"})
            page = private_ui.render({"events": {}})
            self.assertNotIn("private-prowlarr-secret", page)
            self.assertNotIn("private-qbit-secret", page)
            self.assertNotIn("private-rqbit-secret", page)
            self.assertNotIn("private-vpn-secret", page)
            self.assertIn("Private Trackers", page)
            self.assertIn("id='private_master'", page)
            self.assertIn("data-key='PRIVATE_TRACKERS_ENABLED'", page)
            self.assertIn("/data/nuviodownloads", page)
            self.assertIn("Minimum seeders (hard eligibility floor)", page)
            self.assertIn("id='PRIVATE_TRACKER_MIN_SEEDERS'", page)
            self.assertIn("min='0' max='10000'", page)
            self.assertIn("id='PRIVATE_TRACKER_MAX_TORRENT_GB'", page)
            self.assertIn("id='PRIVATE_TRACKER_MAX_ACTIVE_DOWNLOADS'", page)
            self.assertIn("id='PRIVATE_TRACKER_RELEASE_ORDER'", page)
            self.assertIn("id='release_policy'", page)
            self.assertIn("draggable='true' data-kind='episode'", page)
            self.assertIn("Your download policy", page)
            self.assertIn("isolated home for local downloads", page)
            self.assertNotIn("manual last resort", page)
            self.assertIn("Default: 20.", page)
            self.assertIn("max='1000'", page)
            self.assertIn("/private-trackers/setup", page)
            self.assertIn("rqbit — progressive streaming", page)
            self.assertIn("id='PRIVATE_RQBIT_OUTPUT_PATH'", page)
            self.assertIn("id='PRIVATE_RQBIT_VPN_URL'", page)
            self.assertIn("id='PRIVATE_RQBIT_VPN_API_KEY'", page)
            self.assertIn("id='PRIVATE_TRACKER_INDEXER_SCORES'", page)
            self.assertIn("Tracker preferences", page)
            self.assertIn("id='indexers'", page)
            self.assertIn("id='sdot'", page)
            setup = private_ui.render_setup()
            self.assertIn("Private tracker progressive setup", setup)
            self.assertIn("all five checks", setup)
            self.assertIn("rqbit-pia.compose.yml", setup)
        if old is None:
            os.environ.pop("CONFIG_FILE", None)
        else:
            os.environ["CONFIG_FILE"] = old


class PrivateSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_private_search_returns_visible_status_row(self):
        with mock.patch.object(picker, "_release_expected",
                               new=mock.AsyncMock(return_value=True)), \
                mock.patch.object(private_trackers, "candidates",
                                  new=mock.AsyncMock(return_value=[])), \
                mock.patch.object(private_trackers, "search_in_progress",
                                  return_value=False), \
                mock.patch.object(private_trackers, "search_outcome",
                                  return_value={"state": "empty"}), \
                mock.patch.object(private_trackers, "enabled",
                                  return_value=True):
            rows = await picker._no_source(
                "slow:full:series:tt1:1:1", "series", "tt1:1:1", [[]])
        self.assertEqual("🔒 No Private Tracker Match", rows[0]["name"])
        self.assertIn("No torrent was added", rows[0]["title"])

    async def test_connection_test_uses_download_root_entered_in_form(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(private_trackers, "PROWLARR_API_KEY", ""), \
                mock.patch.object(private_trackers, "QBIT_PASSWORD", ""):
            result = await private_trackers.test_connections({
                "PRIVATE_PROWLARR_URL": "",
                "PRIVATE_QBITTORRENT_URL": "",
                "PRIVATE_TRACKER_DOWNLOAD_ROOT": tmp,
            })
            self.assertTrue(result["storage"]["ok"])
            self.assertIn("is readable", result["storage"]["detail"])

    def test_private_download_paths_must_be_absolute(self):
        old = os.environ.get("CONFIG_FILE")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["CONFIG_FILE"] = os.path.join(tmp, "config.json")
                with self.assertRaisesRegex(ValueError, "absolute container path"):
                    config.save({"PRIVATE_TRACKER_DOWNLOAD_ROOT": "relative"})
        finally:
            if old is None:
                os.environ.pop("CONFIG_FILE", None)
            else:
                os.environ["CONFIG_FILE"] = old

    def test_private_release_order_is_normalized_and_validated(self):
        old = os.environ.get("CONFIG_FILE")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["CONFIG_FILE"] = os.path.join(tmp, "config.json")
                config.save({
                    "PRIVATE_TRACKER_RELEASE_ORDER": " season, episode "})
                self.assertEqual(
                    "season,episode",
                    config.pending("PRIVATE_TRACKER_RELEASE_ORDER"))
                with self.assertRaisesRegex(ValueError, "one or more unique"):
                    config.save({
                        "PRIVATE_TRACKER_RELEASE_ORDER": "episode,episode"})
                with self.assertRaisesRegex(ValueError, "one or more unique"):
                    config.save({"PRIVATE_TRACKER_RELEASE_ORDER": ""})
        finally:
            if old is None:
                os.environ.pop("CONFIG_FILE", None)
            else:
                os.environ["CONFIG_FILE"] = old

    def test_private_indexer_scores_are_normalized_and_validated(self):
        old = os.environ.get("CONFIG_FILE")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["CONFIG_FILE"] = os.path.join(tmp, "config.json")
                # 50 is the neutral default and is dropped; the rest persist.
                config.save({"PRIVATE_TRACKER_INDEXER_SCORES":
                             '{"Fav": 1, "Meh": 50, "Bad": 100}'})
                self.assertEqual(
                    {"Fav": 1, "Bad": 100},
                    json.loads(config.pending(
                        "PRIVATE_TRACKER_INDEXER_SCORES")))
                # An all-neutral map normalizes to empty (all trackers equal).
                config.save({"PRIVATE_TRACKER_INDEXER_SCORES": '{"X": 50}'})
                self.assertEqual(
                    "", config.pending("PRIVATE_TRACKER_INDEXER_SCORES"))
                with self.assertRaisesRegex(ValueError, "between 1 and 100"):
                    config.save({"PRIVATE_TRACKER_INDEXER_SCORES": '{"X": 0}'})
                with self.assertRaisesRegex(ValueError, "whole numbers"):
                    config.save(
                        {"PRIVATE_TRACKER_INDEXER_SCORES": '{"X": "abc"}'})
                with self.assertRaisesRegex(ValueError, "JSON object"):
                    config.save({"PRIVATE_TRACKER_INDEXER_SCORES": '[1, 2]'})
                with self.assertRaisesRegex(ValueError, "not valid JSON"):
                    config.save({"PRIVATE_TRACKER_INDEXER_SCORES": '{oops'})
        finally:
            if old is None:
                os.environ.pop("CONFIG_FILE", None)
            else:
                os.environ["CONFIG_FILE"] = old

    async def test_release_filed_under_a_regional_alias_is_admitted(self):
        # Real M-Team names for 乡村爱情 S01. None carries an IMDb id, and none
        # uses TMDB's English title for the first season.
        def row(title, seeders=20):
            return {"indexerId": 45, "indexer": "M-Team - TP",
                    "protocol": "torrent", "title": title, "seeders": seeders,
                    "size": 20_000_000_000, "guid": title,
                    "downloadUrl": "http://prowlarr/dl/" + title}
        rows = [row("Rural Love Story S01 2006 Complete 2160p WEB-DL HEVC AAC-DBTV"),
                row("Country Love 2006 S01 Complete 2160p WEB-DL H.265 AAC 2.0-UBWEB"),
                row("Love Stories in the Countryside S18E13 1080p YOUKU WEB-DL"),
                row("Lovecraft Country S01 1080p WEB-DL"),
                row("Rural Love Story 2023 S01 2160p WEB-DL H265 HQ AAC-HHWEB")]
        titles = ["Love Stories in the Countryside", "乡村爱情",
                  "Country Love", "Rural Love Story"]
        with mock.patch.object(
                private_trackers, "_private_torrent_indexers",
                new=mock.AsyncMock(return_value={45: "M-Team - TP"})), \
                mock.patch.object(private_trackers, "_prowlarr_query",
                                  new=mock.AsyncMock(return_value=rows)), \
                mock.patch.object(private_trackers.usenet, "_expected_info",
                                  new=mock.AsyncMock(
                                      return_value=(titles, 2006))), \
                mock.patch.object(private_trackers, "MIN_SEEDERS", 1), \
                mock.patch.object(private_trackers.telemetry,
                                  "record_private_tracker"):
            found = await private_trackers._search("series", "tt1395015:1:1")
        self.assertEqual(
            {"Rural Love Story S01 2006 Complete 2160p WEB-DL HEVC AAC-DBTV",
             "Country Love 2006 S01 Complete 2160p WEB-DL H.265 AAC 2.0-UBWEB"},
            {c["title"] for c in found})

    async def test_indexer_preferences_merge_stored_scores(self):
        with mock.patch.object(
                private_trackers, "_private_torrent_indexers",
                new=mock.AsyncMock(return_value={1: "Alpha", 2: "Beta"})), \
                mock.patch.object(private_trackers, "INDEXER_SCORES",
                                  {"beta": 5}):
            rows = await private_trackers.indexer_preferences()
        # Ordered most-preferred first: Beta (5) before neutral Alpha (50).
        self.assertEqual([("Beta", 5), ("Alpha", 50)],
                         [(r["name"], r["score"]) for r in rows])


if __name__ == "__main__":
    unittest.main()
