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


class ReleasePolicyTests(unittest.TestCase):
    def test_foreign_series_searches_canonical_and_native_titles(self):
        queries = private_trackers._query_strings(
            "series", ["Kang's Family", "外来媳妇本地郎"], 2000, (1, 1))
        self.assertIn("Kang's Family S01E01", queries)
        self.assertIn("外来媳妇本地郎 S01E01", queries)
        self.assertIn("外来媳妇本地郎 S01", queries)
        self.assertIn("外来媳妇本地郎", queries)

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

    async def test_all_files_stay_selected_and_clicked_file_gets_priority(self):
        files = [{"index": 0}, {"index": 1}, {"index": 2}]
        response = httpx.Response(200, request=httpx.Request("POST", "http://q"))
        with mock.patch.object(private_trackers, "_qapi",
                               new=mock.AsyncMock(return_value=response)) as api:
            await private_trackers._prioritize_all("abc", files, 1)
        calls = api.await_args_list
        self.assertEqual("0|1|2", calls[0].kwargs["data"]["id"])
        self.assertEqual("1", calls[0].kwargs["data"]["priority"])
        self.assertEqual("1", calls[1].kwargs["data"]["id"])
        self.assertEqual("7", calls[1].kwargs["data"]["priority"])
        self.assertEqual("/torrents/setShareLimits", calls[2].args[1])
        self.assertEqual("-1", calls[2].kwargs["data"]["ratioLimit"])
        self.assertEqual("-1", calls[2].kwargs["data"]["seedingTimeLimit"])

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
