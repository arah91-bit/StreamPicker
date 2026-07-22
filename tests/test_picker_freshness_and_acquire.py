"""Fresh-result and acquisition single-flight regressions."""

import asyncio
import time
import unittest
from unittest import mock

from app import acquire, candidate_health, picker, probe, telemetry


def _verified(url: str = "https://stream.invalid/movie") -> dict:
    raw = {
        "name": "1080p Stream",
        "url": url,
        "behaviorHints": {"filename": "Movie.2024.1080p.WEB-DL-GROUP.mkv"},
    }
    picker._annotate_quality([raw], 7200)
    return picker._mark(
        raw, 1, probe.ProbeResult(True, ttfb=0.1, speed_bps=20_000_000))


class CacheFreshnessTests(unittest.TestCase):
    def test_current_block_is_applied_to_an_existing_cache_entry(self):
        stream = _verified()
        key = "full:movie:tt1234567"
        sig = telemetry.signature(stream)
        with mock.patch.object(picker, "_cache", {
                key: (time.monotonic(), [stream])}), \
                mock.patch("app.picker.reputation.blocked",
                           side_effect=lambda candidate: candidate == sig), \
                mock.patch("app.picker.reputation.cooled", return_value=False):
            self.assertIsNone(picker._cached_candidate(key))

    def test_result_url_ttl_is_shorter_than_catalog_ttl(self):
        stream = _verified()
        key = "full:movie:tt1234567"
        with mock.patch.object(picker, "_cache", {
                key: (time.monotonic() - picker.RESULT_CACHE_TTL - 1, [stream])}):
            self.assertIsNone(picker._cached_candidate(key))


class StaleWhileRevalidateTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_proof_is_live_rechecked_before_being_served(self):
        key = "full:movie:tt1234567"
        first = _verified("https://stream.invalid/old-leader")
        second_raw = {
            "name": "1080p Stream 2",
            "url": "https://stream.invalid/old-second",
            "behaviorHints": {
                "filename": "Movie.2024.1080p.WEB-DL-OTHER.mkv",
            },
        }
        picker._annotate_quality([second_raw], 7_200)
        second = picker._mark(
            second_raw, 2,
            probe.ProbeResult(True, ttfb=0.2, speed_bps=18_000_000))
        expired = (time.monotonic() - picker.RESULT_CACHE_TTL - 1,
                   [first, second])

        async def passing(candidates, *args, **kwargs):
            return [(candidates[0], probe.ProbeResult(
                True, ttfb=0.1, speed_bps=20_000_000))]

        with mock.patch.multiple(
                picker, _cache={key: expired}, _stale_cache={}, _background={},
                _resolve_identity_profile=mock.AsyncMock(),
                _resolve_accept_langs=mock.AsyncMock(),
                _runtime_seconds=mock.AsyncMock(return_value=7_200),
                _publish_fast_verified=mock.Mock(),
                _schedule_cache_refresh=mock.Mock()), \
                mock.patch.object(picker.sources, "invalidate") as invalidated, \
                mock.patch.object(picker.probe, "probe_race", new=passing), \
                mock.patch.object(candidate_health, "should_skip", return_value=False):
            streams = await picker._cached_pick(
                key, "movie", "tt1234567", picker.PROFILES["full"])

        self.assertIsNotNone(streams)
        self.assertTrue(picker._is_ranked(streams[0]))
        # The second stream used to be verified too, but its proof expired and it
        # did not pass this recheck, so its old URL is not returned at all.
        self.assertEqual(1, len(streams))
        invalidated.assert_called_once_with("movie", "tt1234567")

    async def test_failed_stale_link_is_kept_as_history_not_current_proof(self):
        key = "full:movie:tt1234567"
        stream = _verified("https://stream.invalid/dead")
        expired = (time.monotonic() - picker.RESULT_CACHE_TTL - 1, [stream])

        with mock.patch.multiple(
                picker, _cache={key: expired}, _stale_cache={}, _background={},
                _resolve_identity_profile=mock.AsyncMock(),
                _resolve_accept_langs=mock.AsyncMock(),
                _runtime_seconds=mock.AsyncMock(return_value=7_200)), \
                mock.patch.object(picker.sources, "invalidate") as invalidated, \
                mock.patch.object(
                    picker.probe, "probe_race", new=mock.AsyncMock(return_value=[])), \
                mock.patch.object(candidate_health, "should_skip", return_value=False):
            streams = await picker._cached_pick(
                key, "movie", "tt1234567", picker.PROFILES["full"])

            self.assertIsNone(streams)
            self.assertNotIn(key, picker._cache)
            self.assertIn(key, picker._stale_cache)
        invalidated.assert_called_once_with("movie", "tt1234567")

class AcquisitionSingleFlightTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_requesters_join_one_real_operation(self):
        calls = 0

        async def perform(media, media_id, key):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            return True

        with mock.patch.object(acquire, "_inflight", {}), \
                mock.patch.object(acquire, "_requested", {}), \
                mock.patch("app.acquire.enabled_for", return_value=True), \
                mock.patch("app.acquire._perform", side_effect=perform):
            first, second = await asyncio.gather(
                acquire.request("movie", "tt1234567"),
                acquire.request("movie", "tt1234567"))

        self.assertTrue(first and second)
        self.assertEqual(1, calls)


class StaleLibraryAcquisitionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.media_id = "tt1234567"
        self.cache_key = f"slow:full:movie:{self.media_id}"
        self.stale = {
            "name": "Library 1080p",
            "url": "https://library.invalid/stale",
            "behaviorHints": {
                "filename": "Movie.2024.1080p.WEB-DL-LIBRARY.mkv",
            },
        }

    async def test_foreground_stale_library_hit_does_not_suppress_acquisition(self):
        acquire_request = mock.AsyncMock(return_value=True)
        failed_probe = mock.AsyncMock(return_value=[])
        with mock.patch.multiple(
                picker,
                _cache={},
                _notice_until={},
                _background={self.cache_key: object()},
                _cached_pick=mock.AsyncMock(return_value=None),
                _gather_extras=mock.AsyncMock(return_value=[]),
                _runtime_seconds=mock.AsyncMock(return_value=7_200),
                _resolve_accept_langs=mock.AsyncMock(),
                _eligible_library=mock.Mock(return_value=[self.stale]),
                _merge_rank=mock.Mock(return_value=([], None)),
                _take_fast_verified=mock.Mock(return_value=[]),
                _probe_bounded=failed_probe,
                _refine_video_bitrate=mock.AsyncMock(),
                _publish_fast_verified=mock.Mock(),
                _release_expected=mock.AsyncMock(return_value=True)), \
                mock.patch.multiple(
                    picker.library,
                    enabled=mock.Mock(return_value=True),
                    streams=mock.AsyncMock(return_value=[self.stale])), \
                mock.patch.multiple(
                    picker.sources,
                    search_all=mock.Mock(return_value=[]),
                    get=mock.AsyncMock(return_value=[])), \
                mock.patch.object(
                    picker.nzb_lane, "in_progress", return_value=False), \
                mock.patch.multiple(
                    picker.acquire,
                    enabled_for=mock.Mock(return_value=True),
                    request=acquire_request):
            streams = await picker.pick_slow("movie", self.media_id, "full")

        self.assertEqual([self.stale], failed_probe.await_args.args[0])
        acquire_request.assert_awaited_once_with("movie", self.media_id)
        self.assertEqual("added", streams[0][picker._NOTICE_STATE_KEY])

    async def test_background_stale_library_hit_does_not_suppress_acquisition(self):
        acquire_request = mock.AsyncMock(return_value=True)
        with mock.patch.multiple(
                picker,
                _notice_until={},
                _background={self.cache_key: object()},
                _gather_extras=mock.AsyncMock(return_value=[]),
                _merge_rank=mock.Mock(return_value=([], None)),
                _take_fast_verified=mock.Mock(return_value=[]),
                _eligible_library=mock.Mock(return_value=[self.stale]),
                _probe_bounded=mock.AsyncMock(return_value=[]),
                _refine_video_bitrate=mock.AsyncMock(),
                _release_expected=mock.AsyncMock(return_value=True)), \
                mock.patch.object(
                    picker.sources, "get", new=mock.AsyncMock(return_value=[])), \
                mock.patch.object(
                    picker.nzb_lane, "wait_complete",
                    new=mock.AsyncMock(return_value=None)), \
                mock.patch.multiple(
                    picker.library,
                    enabled=mock.Mock(return_value=True),
                    streams=mock.AsyncMock(return_value=[self.stale])), \
                mock.patch.multiple(
                    picker.acquire,
                    enabled_for=mock.Mock(return_value=True),
                    request=acquire_request):
            await picker._finish_slow(
                self.cache_key, "movie", self.media_id,
                picker.PROFILES["full"], 7_200,
            )

        acquire_request.assert_awaited_once_with("movie", self.media_id)


if __name__ == "__main__":
    unittest.main()
