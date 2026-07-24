"""The native Prowlarr lane: enablement gating, the load-bearing title/episode
filter on Prowlarr results, cache-check-before-resolve (never adding an uncached
torrent), and the search→resolve orchestration that emits playable streams.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app import prowlarr


def _rel(title, info_hash="", protocol="torrent", seeders=10, size=10**9):
    return {"title": title, "infoHash": info_hash, "protocol": protocol,
            "seeders": seeders, "size": size, "indexer": "1337x"}


class EnabledTests(unittest.TestCase):
    def _enable(self, **over):
        base = dict(_SOURCE_ON=True, _URL="http://pr:9696", _API_KEY="K")
        base.update(over)
        return patch.multiple(prowlarr, **base)

    def test_off_when_source_flag_unset(self):
        with self._enable(_SOURCE_ON=False), \
             patch.object(prowlarr, "_store_base", lambda: "http://st:8080"), \
             patch.object(prowlarr, "_stores", lambda: [("torbox", "T")]):
            self.assertFalse(prowlarr.enabled())

    def test_off_without_credentials_or_store(self):
        with self._enable(_URL=""), \
             patch.object(prowlarr, "_store_base", lambda: "http://st:8080"), \
             patch.object(prowlarr, "_stores", lambda: [("torbox", "T")]):
            self.assertFalse(prowlarr.enabled())
        with self._enable(), \
             patch.object(prowlarr, "_store_base", lambda: ""), \
             patch.object(prowlarr, "_stores", lambda: [("torbox", "T")]):
            self.assertFalse(prowlarr.enabled())
        with self._enable(), \
             patch.object(prowlarr, "_store_base", lambda: "http://st:8080"), \
             patch.object(prowlarr, "_stores", lambda: []):
            self.assertFalse(prowlarr.enabled())

    def test_on_with_any_single_debrid(self):
        # One debrid — any provider — is enough; no specific one is required.
        for provider in ("torbox", "realdebrid", "alldebrid"):
            with self._enable(), \
                 patch.object(prowlarr, "_store_base", lambda: "http://st:8080"), \
                 patch.object(prowlarr, "_stores", lambda p=provider: [(p, "T")]):
                self.assertTrue(prowlarr.enabled(), provider)


class StoresTests(unittest.TestCase):
    def test_collects_all_configured_debrids_deduped(self):
        from app import debrid
        fast = debrid.build_comet("https://c",
                                  [("torbox", "TB"), ("realdebrid", "RD")])
        st = debrid.build_stremthru("https://s",
                                    [("realdebrid", "RD"), ("alldebrid", "AD")])
        env = {"FAST_BASE_URL": fast, "STREMTHRU_BASE_URL": st}
        with patch.dict("os.environ", env, clear=False):
            got = prowlarr._stores()
        # torbox + realdebrid from Comet, alldebrid added from StremThru; the
        # duplicate realdebrid appears once. No ordering-as-primary meaning.
        self.assertEqual({"torbox", "realdebrid", "alldebrid"},
                         {name for name, _ in got})
        self.assertEqual(len(got), len({name for name, _ in got}))


class CandidateFilterTests(unittest.TestCase):
    def test_keeps_only_title_matched_torrents_with_hashes(self):
        results = [
            _rel("The Matrix 1999 1080p BluRay x264", "a" * 40),
            _rel("The Matrix Reloaded 2003 1080p", "b" * 40),   # wrong title
            _rel("The Matrix 1999 2160p", "", ),                 # no hash
            _rel("The Matrix 1999 720p", "c" * 40, protocol="usenet"),  # nzb
        ]
        got = prowlarr._candidates(results, ["The Matrix"], None)
        self.assertEqual(["a" * 40], [c["hash"] for c in got])

    def test_series_requires_episode_match(self):
        results = [
            _rel("Show Name S01E02 1080p WEB", "d" * 40),
            _rel("Show Name S01E03 1080p WEB", "e" * 40),
            _rel("Show Name S02E02 1080p WEB", "f" * 40),
        ]
        got = prowlarr._candidates(results, ["Show Name"], (1, 2))
        self.assertEqual(["d" * 40], [c["hash"] for c in got])

    def test_dedupes_hash_keeping_more_seeders_and_ranks(self):
        results = [
            _rel("The Matrix 1999 1080p", "a" * 40, seeders=5, size=10),
            _rel("The Matrix 1999 1080p PROPER", "a" * 40, seeders=99, size=10),
            _rel("The Matrix 1999 2160p", "b" * 40, seeders=50, size=99),
        ]
        got = prowlarr._candidates(results, ["The Matrix"], None)
        self.assertEqual(2, len(got))
        # The "a" hash is deduped to its 99-seeder copy, which then out-ranks
        # the 50-seeder "b" copy.
        self.assertEqual("a" * 40, got[0]["hash"])
        self.assertEqual(99, got[0]["seeders"])

    def test_min_seeders_filter(self):
        with patch.object(prowlarr, "MIN_SEEDERS", 5):
            got = prowlarr._candidates(
                [_rel("The Matrix 1999", "a" * 40, seeders=2)],
                ["The Matrix"], None)
            self.assertEqual([], got)

    def test_same_name_series_with_explicit_wrong_year_is_rejected(self):
        results = [
            _rel("Shared Show 2026 S01E01 1080p", "a" * 40),
            _rel("Shared Show 2005 S01E01 1080p", "b" * 40),
            _rel("Shared Show S01E01 1080p", "c" * 40),
        ]
        got = prowlarr._candidates(
            results, ["Shared Show"], (1, 1), year=2005)
        self.assertEqual({"b" * 40, "c" * 40}, {row["hash"] for row in got})


class PickFileTests(unittest.TestCase):
    def test_largest_video_for_movie(self):
        files = [{"name": "sample.mkv", "size": 5, "link": "l1"},
                 {"name": "movie.mkv", "size": 5000, "link": "l2"},
                 {"name": "readme.txt", "size": 1, "link": "l3"}]
        self.assertEqual("l2", prowlarr._pick_file(files, None)["link"])

    def test_episode_file_for_series(self):
        files = [{"name": "Show.S01E01.mkv", "size": 900, "link": "l1"},
                 {"name": "Show.S01E02.mkv", "size": 800, "link": "l2"}]
        self.assertEqual("l2", prowlarr._pick_file(files, (1, 2))["link"])

    def test_series_pack_without_matching_episode_is_skipped(self):
        files = [{"name": "Show.S01E05.mkv", "size": 900, "link": "l1"}]
        self.assertIsNone(prowlarr._pick_file(files, (1, 2)))


class StreamsTests(unittest.IsolatedAsyncioTestCase):
    async def test_searches_native_alias_when_english_title_has_no_result(self):
        seen = []
        native = _rel("外来媳妇本地郎.S01E01.1080p.WEB-DL", "c" * 40)

        async def fake_search(query):
            seen.append(query)
            return [native] if query.startswith("外来媳妇本地郎") else []

        async def fake_resolve(cand, store, token, se):
            return {"name": "Prowlarr", "url": "http://cdn/native",
                    "behaviorHints": {}}

        with patch.object(prowlarr, "enabled", lambda: True), \
             patch.object(prowlarr, "_stores", lambda: [("torbox", "T")]), \
             patch.object(prowlarr.usenet, "_expected_info",
                          side_effect=_returns(
                              (["Kang's Family", "外来媳妇本地郎"], 2000))), \
             patch.object(prowlarr, "_search", side_effect=fake_search), \
             patch.object(prowlarr, "_cached_hashes",
                          side_effect=_returns({"c" * 40})), \
             patch.object(prowlarr, "_resolve", side_effect=fake_resolve):
            out = await prowlarr.streams("series", "tt7803586:1:1")
        self.assertEqual(["Kang's Family S01E01", "外来媳妇本地郎 S01E01"],
                         seen)
        self.assertEqual(["http://cdn/native"], [stream["url"] for stream in out])

    async def test_resolves_only_cached_and_emits_streams(self):
        results = [_rel("The Matrix 1999 1080p", "a" * 40),
                   _rel("The Matrix 1999 2160p", "b" * 40)]
        resolved = []

        async def fake_resolve(cand, store, token, se):
            resolved.append(cand["hash"])
            return {"name": "Prowlarr", "url": f"http://cdn/{cand['hash']}",
                    "behaviorHints": {}}

        with patch.object(prowlarr, "enabled", lambda: True), \
             patch.object(prowlarr, "_stores", lambda: [("torbox", "T")]), \
             patch.object(prowlarr.usenet, "_expected_info",
                          side_effect=_fake_expected), \
             patch.object(prowlarr, "_search", side_effect=_returns(results)), \
             patch.object(prowlarr, "_cached_hashes",
                          side_effect=_returns({"a" * 40})), \
             patch.object(prowlarr, "_resolve", side_effect=fake_resolve):
            out = await prowlarr.streams("movie", "tt0133093")
        # Only the cached hash was resolved; the uncached one never touched.
        self.assertEqual(["a" * 40], resolved)
        self.assertEqual(["http://cdn/" + "a" * 40], [s["url"] for s in out])

    async def test_resolves_via_whichever_debrid_has_it(self):
        # The hash is cached only on the SECOND-listed debrid — it must still
        # resolve, through that debrid. No provider is assumed primary.
        results = [_rel("The Matrix 1999 1080p", "a" * 40)]
        used = {}

        async def fake_resolve(cand, store, token, se):
            used["store"] = store
            return {"name": "P", "url": "u", "behaviorHints": {}}

        async def fake_check(hashes, name, token):
            return {"a" * 40} if name == "realdebrid" else set()

        with patch.object(prowlarr, "enabled", lambda: True), \
             patch.object(prowlarr, "_stores",
                          lambda: [("torbox", "T1"), ("realdebrid", "T2")]), \
             patch.object(prowlarr.usenet, "_expected_info",
                          side_effect=_fake_expected), \
             patch.object(prowlarr, "_search", side_effect=_returns(results)), \
             patch.object(prowlarr, "_cached_hashes", side_effect=fake_check), \
             patch.object(prowlarr, "_resolve", side_effect=fake_resolve):
            out = await prowlarr.streams("movie", "tt0133093")
        self.assertEqual(1, len(out))
        self.assertEqual("realdebrid", used["store"])

    async def test_emits_a_copy_per_debrid_for_failover(self):
        # Cached on BOTH debrids → emit both byte-identical copies so the proxy
        # has a twin to fail over to. Redundancy is used, not discarded.
        results = [_rel("The Matrix 1999 1080p", "a" * 40)]
        calls = []

        async def fake_resolve(cand, store, token, se):
            calls.append(store)
            return {"name": f"Prowlarr [{store}]", "url": f"u/{store}",
                    "behaviorHints": {}}

        async def fake_check(hashes, name, token):
            return {"a" * 40}                     # cached on every store

        with patch.object(prowlarr, "enabled", lambda: True), \
             patch.object(prowlarr, "_stores",
                          lambda: [("torbox", "T1"), ("realdebrid", "T2")]), \
             patch.object(prowlarr.usenet, "_expected_info",
                          side_effect=_fake_expected), \
             patch.object(prowlarr, "_search", side_effect=_returns(results)), \
             patch.object(prowlarr, "_cached_hashes", side_effect=fake_check), \
             patch.object(prowlarr, "_resolve", side_effect=fake_resolve):
            out = await prowlarr.streams("movie", "tt0133093")
        self.assertEqual(2, len(out))                     # one copy per debrid
        self.assertEqual({"torbox", "realdebrid"}, set(calls))
        self.assertEqual(2, len({s["url"] for s in out}))  # distinct debrid urls

    async def test_no_matches_returns_empty_without_resolving(self):
        with patch.object(prowlarr, "enabled", lambda: True), \
             patch.object(prowlarr, "_stores", lambda: [("torbox", "T")]), \
             patch.object(prowlarr.usenet, "_expected_info",
                          side_effect=_fake_expected), \
             patch.object(prowlarr, "_search",
                          side_effect=_returns([_rel("Other Film", "z" * 40)])), \
             patch.object(prowlarr, "_cached_hashes",
                          side_effect=_boom("should not cache-check")):
            out = await prowlarr.streams("movie", "tt0133093")
        self.assertEqual([], out)

    async def test_disabled_short_circuits(self):
        with patch.object(prowlarr, "enabled", lambda: False):
            self.assertEqual([], await prowlarr.streams("movie", "tt0133093"))


class _FakeResp:
    def __init__(self, data): self._data = data
    def raise_for_status(self): pass
    def json(self): return self._data


class TwinIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_copies_share_signature_but_differ_by_debrid_tag(self):
        from app import telemetry
        cand = {"hash": "a" * 40, "title": "The Matrix 1999",
                "size": 0, "indexer": "YTS"}
        add = _FakeResp({"data": {"files": [
            {"name": "The.Matrix.1999.1080p.BluRay.mkv", "size": 3000,
             "link": "L"}]}})
        gen = _FakeResp({"data": {"link": "http://cdn/x"}})

        async def fake_post(url, headers=None, json=None, timeout=None):
            return gen if url.endswith("/generate") else add

        with patch.object(prowlarr._client, "post", side_effect=fake_post):
            tb = await prowlarr._resolve(cand, "torbox", "T", None)
            rd = await prowlarr._resolve(cand, "realdebrid", "T", None)
        token = telemetry.request_ctx.set({"media_id": "tt0133093"})
        try:
            # Distinct debrid tags → the twin detector treats them as the same
            # file on different nodes; identical signatures → the proxy may
            # splice/fail over between them.
            self.assertEqual("TB", telemetry.debrid_tag(tb["name"]).rstrip("+"))
            self.assertEqual("RD", telemetry.debrid_tag(rd["name"]).rstrip("+"))
            self.assertNotEqual(telemetry.debrid_tag(tb["name"]),
                                telemetry.debrid_tag(rd["name"]))
            self.assertTrue(telemetry.signature(tb))
            self.assertEqual(telemetry.signature(tb), telemetry.signature(rd))
        finally:
            telemetry.request_ctx.reset(token)


async def _fake_expected(media, media_id):
    return ["The Matrix"], 1999


def _returns(value):
    async def _f(*a, **k):
        return value
    return _f


def _boom(msg):
    async def _f(*a, **k):
        raise AssertionError(msg)
    return _f


if __name__ == "__main__":
    unittest.main()
