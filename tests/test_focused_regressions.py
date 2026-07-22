"""Focused regressions for picker and direct-Usenet edge cases.

The async tests replace every network operation with deterministic in-process
fakes.  No fixture in this module contacts an indexer, nzbdav, or Jellyfin.
"""

from __future__ import annotations

import asyncio
import os
import time
import unittest
from unittest import mock

from app import content_identity, picker, probe, telemetry, usenet


def _stream(filename: str, url: str = "https://stream.invalid/video") -> dict:
    return {
        "name": "Stream",
        "url": url,
        "behaviorHints": {"filename": filename},
    }


def _verified_stream(filename: str,
                     url: str = "https://stream.invalid/video") -> dict:
    stream = _stream(filename, url)
    picker._annotate_quality([stream], 7_200)
    return picker._mark(
        stream, 1,
        probe.ProbeResult(True, ttfb=0.1, speed_bps=20_000_000))


class AudioLanguageRegressionTests(unittest.TestCase):
    def test_measured_track_language_overrides_misleading_filename(self) -> None:
        stream = _stream("Movie.2024.English.1080p.WEB-DL-GROUP.mkv")
        stream["_audio_langs"] = ["de"]
        token = picker._accept_langs.set(frozenset({"en"}))
        known = picker._original_lang_known.set(True)
        try:
            self.assertEqual(({"de"}, False), picker._audio_langs(stream))
            self.assertEqual(0, picker._audio_ok(stream))
        finally:
            picker._original_lang_known.reset(known)
            picker._accept_langs.reset(token)

    def test_web_dl_does_not_mean_multi_audio(self) -> None:
        stream = _stream("Movie.Name.2024.1080p.WEB-DL.DDP5.1-GROUP.mkv")

        languages, multi = picker._audio_langs(stream)

        self.assertFalse(multi)
        self.assertEqual(set(), languages)

    def test_direct_nzb_label_supplies_language_when_filename_is_obfuscated(self) -> None:
        stream = _stream("a1b2c3d4.mkv")
        stream.update({
            "name": "NZB",
            "description": "Source: example-indexer",
            "_nzb_label": "Movie.Name.2024.GERMAN.1080p.WEB-DL-GROUP",
        })

        languages, multi = picker._audio_langs(stream)
        token = picker._accept_langs.set(frozenset({"en"}))
        try:
            with mock.patch.object(picker, "AUDIO_GATE", True):
                acceptable = picker._audio_ok(stream)
        finally:
            picker._accept_langs.reset(token)

        self.assertEqual({"de"}, languages)
        self.assertFalse(multi)
        self.assertEqual(0, acceptable)

    def test_multi_subtitles_do_not_bypass_wrong_audio_gate(self) -> None:
        stream = _stream("Movie.Name.2024.1080p.WEB-DL-GROUP.mkv")
        stream["description"] = "🎙️ German\n💬 Multi Subs"
        token = picker._accept_langs.set(frozenset({"en"}))
        try:
            languages, multi = picker._audio_langs(stream)
            acceptable = picker._audio_ok(stream)
        finally:
            picker._accept_langs.reset(token)
        self.assertEqual({"de"}, languages)
        self.assertFalse(multi)
        self.assertEqual(0, acceptable)

    def test_german_only_library_results_are_filtered_from_both_picker_tiers(self) -> None:
        german = _stream(
            "Movie.Name.2024.German.1080p.BluRay-GROUP.mkv",
            "https://library.invalid/german",
        )
        german["name"] = "📚 Library 1080P"
        english = _stream(
            "Movie.Name.2024.English.1080p.BluRay-GROUP.mkv",
            "https://library.invalid/english",
        )
        english["name"] = "📚 Library 1080P"

        token = picker._accept_langs.set(frozenset({"en"}))
        try:
            with mock.patch.object(picker, "AUDIO_GATE", True):
                eligible = picker._eligible_library(
                    [german, english], picker.PROFILES["full"], 7_200)
                fast = picker._prepend_library(eligible, [])
                slow = picker._as_verified(eligible)
        finally:
            picker._accept_langs.reset(token)

        self.assertEqual(["https://library.invalid/english"],
                         [s["url"] for s in fast])
        self.assertEqual(["https://library.invalid/english"],
                         [s["url"] for s, _ in slow])

    def test_false_spellings_disable_picker_boolean_knobs(self) -> None:
        for value in ("0", "false", "False", "no", "OFF", ""):
            with self.subTest(value=value), \
                    mock.patch.dict(os.environ, {"AUDIO_GATE": value}):
                self.assertFalse(picker._env_bool("AUDIO_GATE"))
        for value in ("1", "true", "TRUE", "yes", "on"):
            with self.subTest(value=value), \
                    mock.patch.dict(os.environ, {"AUDIO_GATE": value}):
                self.assertTrue(picker._env_bool("AUDIO_GATE"))


class PickerTrustMarkerRegressionTests(unittest.TestCase):
    def test_rank_looking_upstream_name_cannot_overtake_library(self) -> None:
        spoof = _stream("Movie.2024.2160p.WEB-DL-EVIL.mkv",
                        "https://stream.invalid/spoof")
        spoof["name"] = "EVIL 1 · 2160p WEB-DL"
        library = _stream("Movie.2024.1080p.WEB-DL-LIBRARY.mkv",
                          "https://library.invalid/good")
        library["name"] = "Library 1080p"
        picker._annotate_quality([spoof, library], 7_200)

        streams = picker._prepend_library([library], [spoof])

        self.assertFalse(picker._is_ranked(spoof))
        self.assertEqual(library["url"], streams[0]["url"])
        self.assertTrue(picker._is_ranked(streams[0]))
        self.assertEqual(spoof["url"], streams[1]["url"])

    def test_upstream_private_marker_is_scrubbed_from_leftovers(self) -> None:
        spoof = _stream("Movie.2024.2160p.WEB-DL-EVIL.mkv")
        spoof[picker._VERIFIED_STATE_KEY] = picker._VERIFIED_SENTINEL

        streams = picker._assemble([], [spoof], None)

        self.assertFalse(picker._is_ranked(streams[0]))
        self.assertNotIn(picker._VERIFIED_STATE_KEY, streams[0])

    def test_internal_marker_never_leaks_at_http_boundary(self) -> None:
        stream = _stream("Movie.2024.1080p.WEB-DL-GOOD.mkv")
        marked = picker._mark(
            stream, 1, probe.ProbeResult(
                True, ttfb=0.1, speed_bps=20_000_000))

        self.assertTrue(picker._is_ranked(marked))
        self.assertNotIn(picker._VERIFIED_STATE_KEY,
                         picker.clean_output([marked])[0])


class PrefetchCacheRegressionTests(unittest.IsolatedAsyncioTestCase):
    def test_real_play_refreshes_only_the_selected_picker_cache(self) -> None:
        media_id = "tt9999999:1:1"
        stream = _verified_stream("Show.S01E01.1080p.WEB-DL-GOOD.mkv")
        token = telemetry.request_ctx.set({
            "media": "series", "media_id": media_id, "picker": "fast",
        })
        try:
            sig = telemetry.signature(stream)
            old = time.monotonic() - 100
            key = f"full:series:{media_id}"
            sibling = f"slow:full:series:{media_id}"
            cache = {key: (old, [stream]), sibling: (old, [dict(stream)])}
            with mock.patch.object(picker, "_cache", cache), \
                    mock.patch.object(picker, "_stale_cache", {}), \
                    mock.patch.object(picker.reputation, "blocked", return_value=False), \
                    mock.patch.object(picker.reputation, "cooled", return_value=False):
                picker.note_playback("series", media_id, "fast", sig)

                self.assertGreater(picker._cache[key][0], old)
                self.assertEqual(old, picker._cache[sibling][0])
        finally:
            telemetry.request_ctx.reset(token)

    async def test_transient_notices_never_enter_long_lived_result_caches(self) -> None:
        for i, kind in enumerate(("checking", "theatrical", "added"), 2):
            nxt = f"tt9999999:1:{i}"
            with self.subTest(kind=kind), \
                    mock.patch.object(picker, "_cache", {}), \
                    mock.patch.object(picker, "_stale_cache", {}), \
                    mock.patch.object(picker, "_background", {}), \
                    mock.patch.object(picker, "_prefetching", set()), \
                    mock.patch.object(picker, "_PREFETCH_RETRY_MAX", 0), \
                    mock.patch.object(
                        picker, "_next_episode",
                        new=mock.AsyncMock(return_value=nxt)), \
                    mock.patch.object(
                        picker, "pick",
                        new=mock.AsyncMock(
                            return_value=[picker._notice_stream(kind)])), \
                    mock.patch.object(
                        picker, "pick_slow",
                        new=mock.AsyncMock(
                            return_value=[picker._notice_stream(kind)])):
                await picker.prefetch_next(
                    "series", "tt9999999:1:1", "fast")

                self.assertEqual({}, picker._cache)

    async def test_prefetch_primes_both_picker_caches_viewers_first(self) -> None:
        nxt = "tt9999999:1:2"
        stream = _verified_stream("Show.S01E02.1080p.WEB-DL-GOOD.mkv")
        order: list[str] = []

        async def fast_pick(media, media_id, profile):
            order.append("fast")
            return [dict(stream)]

        async def slow_pick(media, media_id, profile):
            order.append("slow")
            return [dict(stream)]

        with mock.patch.object(picker, "_cache", {}), \
                mock.patch.object(picker, "_stale_cache", {}), \
                mock.patch.object(picker, "_background", {}), \
                mock.patch.object(picker, "_prefetching", set()), \
                mock.patch.object(
                    picker, "_next_episode",
                    new=mock.AsyncMock(return_value=nxt)), \
                mock.patch.object(picker, "pick", new=fast_pick), \
                mock.patch.object(picker, "pick_slow", new=slow_pick):
            await picker.prefetch_next("series", "tt9999999:1:1", "slow")

            self.assertEqual({f"full:series:{nxt}",
                              f"slow:full:series:{nxt}"}, set(picker._cache))
            self.assertEqual(["slow", "fast"], order)

    async def test_transient_empty_prewarm_retries_with_backoff(self) -> None:
        nxt = "tt9999999:1:2"
        verified = _verified_stream("Show.S01E02.1080p.WEB-DL-GOOD.mkv")
        fast_calls = 0
        events: list[str] = []
        cache: dict = {}

        async def fast_pick(media, media_id, profile):
            nonlocal fast_calls
            fast_calls += 1
            if fast_calls == 1:
                return [picker._notice_stream("checking")]
            return [dict(verified)]

        async def slow_pick(media, media_id, profile):
            return [picker._notice_stream("checking")]

        with mock.patch.object(picker, "_cache", cache), \
                mock.patch.object(picker, "_stale_cache", {}), \
                mock.patch.object(picker, "_background", {}), \
                mock.patch.object(picker, "_prefetching", set()), \
                mock.patch.object(picker, "_PREFETCH_RETRY_DELAY", 0), \
                mock.patch.object(
                    picker, "_next_episode",
                    new=mock.AsyncMock(return_value=nxt)), \
                mock.patch.object(picker, "pick", new=fast_pick), \
                mock.patch.object(picker, "pick_slow", new=slow_pick), \
                mock.patch.object(picker.sources, "invalidate") as invalidated, \
                mock.patch.object(
                    picker.telemetry, "record_cache_event",
                    side_effect=lambda event, **kwargs: events.append(event)):
            await picker.prefetch_next("series", "tt9999999:1:1", "fast")

        self.assertEqual(2, fast_calls)
        self.assertIn(f"full:series:{nxt}", cache)
        self.assertIn("prewarm_retry", events)
        self.assertIn("prewarm_ready", events)
        invalidated.assert_called_once_with("series", nxt)

    async def test_prefetch_waits_for_current_episode_work(self) -> None:
        nxt = "tt9999999:1:2"
        current_finisher = asyncio.create_task(asyncio.sleep(0.05))
        finisher_done_at_pick: list[bool] = []

        async def a_pick(media, media_id, profile):
            finisher_done_at_pick.append(current_finisher.done())
            return [dict(_verified_stream(
                "Show.S01E02.1080p.WEB-DL-GOOD.mkv"))]

        with mock.patch.object(picker, "_cache", {}), \
                mock.patch.object(picker, "_stale_cache", {}), \
                mock.patch.object(
                    picker, "_background",
                    {"full:series:tt9999999:1:1": current_finisher}), \
                mock.patch.object(picker, "_prefetching", set()), \
                mock.patch.object(
                    picker, "_next_episode",
                    new=mock.AsyncMock(return_value=nxt)), \
                mock.patch.object(picker, "pick", new=a_pick), \
                mock.patch.object(picker, "pick_slow", new=a_pick):
            await picker.prefetch_next("series", "tt9999999:1:1", "fast")

        self.assertEqual([True, True], finisher_done_at_pick)

    async def test_prefetch_waits_for_shared_source_search_completion(self) -> None:
        nxt = "tt9999999:1:2"
        source_task = asyncio.create_task(asyncio.sleep(0.04))
        search_done_at_pick: list[bool] = []

        async def wait_sources(media, media_id, wait):
            await source_task
            return True

        async def a_pick(media, media_id, profile):
            search_done_at_pick.append(source_task.done())
            return [dict(_verified_stream(
                "Show.S01E02.1080p.WEB-DL-GOOD.mkv"))]

        with mock.patch.object(picker, "_cache", {}), \
                mock.patch.object(picker, "_stale_cache", {}), \
                mock.patch.object(picker, "_background", {}), \
                mock.patch.object(picker, "_prefetching", set()), \
                mock.patch.object(
                    picker, "_next_episode",
                    new=mock.AsyncMock(return_value=nxt)), \
                mock.patch.object(
                    picker.sources, "in_progress",
                    side_effect=lambda media, media_id: not source_task.done()), \
                mock.patch.object(
                    picker.sources, "wait_complete", new=wait_sources), \
                mock.patch.object(picker, "pick", new=a_pick), \
                mock.patch.object(picker, "pick_slow", new=a_pick):
            await picker.prefetch_next("series", "tt9999999:1:1", "fast")

        self.assertEqual([True, True], search_done_at_pick)

    async def test_prefetch_quiesce_wait_is_capped(self) -> None:
        nxt = "tt9999999:1:2"
        never_done: asyncio.Future = asyncio.get_event_loop().create_future()
        picked = mock.AsyncMock(
            return_value=[dict(_verified_stream(
                "Show.S01E02.1080p.WEB-DL-GOOD.mkv"))])
        try:
            with mock.patch.object(picker, "_cache", {}), \
                    mock.patch.object(picker, "_stale_cache", {}), \
                    mock.patch.object(
                        picker, "_background",
                        {"full:series:tt9999999:1:1": never_done}), \
                    mock.patch.object(picker, "_prefetching", set()), \
                    mock.patch.object(picker, "_PREFETCH_QUIESCE_MAX", 0.03), \
                    mock.patch.object(
                        picker, "_next_episode",
                        new=mock.AsyncMock(return_value=nxt)), \
                    mock.patch.object(picker, "pick", new=picked), \
                    mock.patch.object(picker, "pick_slow", new=picked):
                await picker.prefetch_next("series", "tt9999999:1:1", "fast")

            self.assertEqual(2, picked.await_count)
        finally:
            never_done.cancel()

    async def test_refresh_drops_stale_list_and_repicks_current_episode(self) -> None:
        # A prefetched-then-opened (or re-watched) episode can be served off a
        # list whose signed links are hours old. Playing it must re-search: drop
        # every stale entry for the id and re-store a freshly-picked list.
        cur = "tt9999999:1:5"
        fresh = _verified_stream("Show.S01E05.1080p.WEB-DL-FRESH.mkv")
        slow_ran: list[str] = []

        async def fast_pick(media, media_id, profile):
            return [dict(fresh)]

        async def slow_pick(media, media_id, profile):
            slow_ran.append(media_id)
            return [dict(fresh)]

        stale_cache = {f"full:series:{cur}": (0.0, [{"stale": True}]),
                       f"slow:full:series:{cur}": (0.0, [{"stale": True}])}
        with mock.patch.object(picker, "_cache", stale_cache), \
                mock.patch.object(picker, "_stale_cache", {}), \
                mock.patch.object(picker, "_background", {}), \
                mock.patch.object(picker, "_prefetching", set()), \
                mock.patch.object(picker, "pick", new=fast_pick), \
                mock.patch.object(picker, "pick_slow", new=slow_pick):
            await picker.refresh("series", cur, "fast")

            # The viewer's (fast) list was re-searched and re-stored fresh...
            self.assertEqual([dict(fresh)],
                             picker._cache[f"full:series:{cur}"][1])
            # ...the stale sibling (slow) entry was invalidated, not left...
            self.assertNotIn(f"slow:full:series:{cur}", picker._cache)
            # ...and refresh runs only the viewer's picker, never the slow one.
            self.assertEqual([], slow_ran)

    async def test_hls_real_play_fires_next_episode_prefetch(self) -> None:
        from app import hlsproxy, proxy
        entry = {"id": "tt9999999:1:1", "picker": "slow"}
        st = {"flushed": False, "t0": 0.0, "segs": 0, "bytes": 0,
              "last": 0.0, "credited": False}
        with mock.patch.object(proxy, "PREFETCH_NEXT", True), \
                mock.patch.object(
                    proxy, "_fire_prefetch", new=mock.AsyncMock()) as fired:
            for _ in range(3):
                hlsproxy._note_segment("tok", entry, st, 1024)
            await asyncio.sleep(0)
            self.assertTrue(entry.get("nextfetched"))
            fired.assert_awaited_once_with("tt9999999:1:1", "slow", "")

    async def test_playback_trigger_is_single_flight_and_carries_release(self) -> None:
        from app import proxy
        entry = {"id": "tt9999999:1:1", "picker": "fast"}
        with mock.patch.object(proxy, "PREFETCH_NEXT", True), \
                mock.patch.object(
                    proxy, "_fire_prefetch", new=mock.AsyncMock()) as fired:
            proxy._schedule_prefetch(entry, "file:known-release")
            proxy._schedule_prefetch(entry, "file:known-release")
            await asyncio.sleep(0)

        self.assertTrue(entry["nextfetched"])
        fired.assert_awaited_once_with(
            "tt9999999:1:1", "fast", "file:known-release")

    async def test_playback_prioritizes_next_episode_not_current_research(self) -> None:
        from app import proxy
        with mock.patch.object(picker, "note_playback") as noted, \
                mock.patch.object(
                    picker, "prefetch_next", new=mock.AsyncMock()) as next_ep, \
                mock.patch.object(
                    picker, "refresh", new=mock.AsyncMock()) as refreshed:
            await proxy._fire_prefetch(
                "tt9999999:1:1", "fast", "file:known-release")

        noted.assert_called_once_with(
            "series", "tt9999999:1:1", "fast", "file:known-release")
        next_ep.assert_awaited_once_with("series", "tt9999999:1:1", "fast")
        refreshed.assert_not_awaited()


class FailureDetailTelemetryTests(unittest.TestCase):
    def test_failure_detail_preserves_shape_but_redacts_credentials(self) -> None:
        raw = ("Failed article <abc123@news.example>: "
               "https://user:secret@host/path?apikey=topsecret")
        safe = telemetry.sanitize_failure_detail(raw)
        self.assertIn("Failed article <abc123@news.example>", safe)
        self.assertIn("<userinfo>@host/path", safe)
        self.assertIn("apikey=<redacted>", safe)
        self.assertNotIn("secret", safe.replace("<redacted>", ""))

    def test_failure_samples_group_by_sanitized_message_shape(self) -> None:
        rows = telemetry.aggregate_usenet_failures([
            {"kind": "nzb_failure", "stage": "nzbdav-import",
             "detail_hash": "same", "reason": "missing-articles",
             "decision": "hard", "detail": "article unavailable", "ts": 1},
            {"kind": "nzb_failure", "stage": "nzbdav-import",
             "detail_hash": "same", "reason": "missing-articles",
             "decision": "hard", "detail": "article unavailable", "ts": 2},
        ])
        self.assertEqual(1, len(rows))
        self.assertEqual(2, rows[0]["count"])
        self.assertEqual(2, rows[0]["last_ts"])


class PickerDeadlineRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_bounded_keeps_success_when_peer_hangs_to_deadline(self) -> None:
        good = _stream("Movie.2024.1080p.WEB-DL-GOOD.mkv",
                       "https://stream.invalid/good")
        hung = _stream("Movie.2024.1080p.WEB-DL-HUNG.mkv",
                       "https://stream.invalid/hung")

        passed = [(good, probe.ProbeResult(
            True, ttfb=0.01, speed_bps=20_000_000))]

        async def race_finishing_at_its_deadline(*args, **kwargs):
            # This is probe_race after the good peer completed and the other
            # peer hung until probe_race's own deadline.  It has useful partial
            # output ready to return.
            return passed

        real_wait_for = asyncio.wait_for
        budget = 2.0

        async def boundary_sensitive_wait_for(awaitable, timeout):
            # Deterministically model the outer timer winning when it is set to
            # the exact same boundary as probe_race.  A small grace period (or
            # no redundant outer timer) lets probe_race return its partial list.
            if timeout <= budget + 0.001:
                close = getattr(awaitable, "close", None)
                if close:
                    close()
                raise asyncio.TimeoutError
            return await real_wait_for(awaitable, timeout)

        deadline = time.monotonic() + budget
        with mock.patch.object(probe, "probe_race",
                               new=race_finishing_at_its_deadline), \
                mock.patch.object(picker.asyncio, "wait_for",
                                  new=boundary_sensitive_wait_for):
            result = await picker._probe_bounded(
                [good, hung], runtime=7_200, ttfb_max=5,
                max_probes=2, hard_deadline=deadline)

        self.assertEqual([good], [stream for stream, _ in result])
        self.assertTrue(result[0][1].ok)

    async def test_probe_bounded_returns_after_verified_settling_window(self) -> None:
        good = _stream("Movie.2024.1080p.WEB-DL-GOOD.mkv",
                       "https://stream.invalid/good")
        hung = _stream("Movie.2024.2160p.BluRay.REMUX-HUNG.mkv",
                       "https://nzbdav.invalid/hung")
        hung["_nzb_release_key"] = "nzb:hung"
        cancelled = asyncio.Event()

        async def fake_probe(url, *_args, **_kwargs):
            if url.endswith("/good"):
                await asyncio.sleep(0.01)
                return probe.ProbeResult(
                    True, ttfb=0.01, speed_bps=20_000_000)
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        started = time.monotonic()
        with mock.patch.object(probe, "probe", new=fake_probe), \
                mock.patch.object(probe, "_record"):
            result = await picker._probe_bounded(
                [good, hung], runtime=7_200, ttfb_max=60,
                max_probes=2, hard_deadline=started + 2,
                success_grace=0.03)

        self.assertEqual([good], [stream for stream, _ in result])
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(cancelled.is_set())

    async def test_later_resolved_pack_member_survives_and_wins(self) -> None:
        """The first floor starts the window; a later exact pack member wins."""
        low = _stream("Example.Show.2024.S01E03.720p.WEB-DL-LOW.mkv",
                      "https://stream.invalid/low")
        high = _stream("Example.Show.2024.S01E03.1080p.WEB-DL-HIGH.mkv",
                       "https://stream.invalid/high")
        high["description"] = "Example.Show.S01.COMPLETE.1080p.WEB-DL-PACK"
        raw_pack = {
            "name": "Example.Show.S01.COMPLETE.2160p.WEB-DL-RAW",
            "url": "https://stream.invalid/raw-pack",
            "behaviorHints": {"videoSize": 40_000_000_000},
        }
        wrong = _stream("Example.Show.2024.S01E04.2160p.WEB-DL-WRONG.mkv",
                        "https://stream.invalid/wrong")
        low["behaviorHints"]["videoSize"] = 1_000_000_000
        high["behaviorHints"]["videoSize"] = 4_000_000_000
        wrong["behaviorHints"]["videoSize"] = 8_000_000_000
        profile = content_identity.IdentityProfile(
            "series", "tt1234567", ("Example Show",), frozenset({2024}),
            season=1, episode=3)
        profile_token = picker._identity_profile_ctx.set(profile)
        log_token = picker._identity_logged_ctx.set(set())

        async def fake_probe(url, *_args, **_kwargs):
            await asyncio.sleep(0.01 if url.endswith("/low") else 0.03)
            return probe.ProbeResult(
                True, ttfb=0.01, speed_bps=20_000_000)

        try:
            candidates = [low, high, raw_pack, wrong]
            picker._annotate_identity(candidates)
            picker._annotate_quality(candidates, 2_400)
            rejected_events: list[str] = []
            with mock.patch.object(probe, "probe", new=fake_probe), \
                    mock.patch.object(probe, "_record"), \
                    mock.patch.object(
                        picker.telemetry, "record_cache_event",
                        side_effect=lambda event, **_kwargs:
                        rejected_events.append(event)):
                verified = await picker._probe_bounded(
                    candidates, runtime=2_400, ttfb_max=2,
                    max_probes=4, hard_deadline=time.monotonic() + 2,
                    success_grace=0.1)
        finally:
            picker._identity_logged_ctx.reset(log_token)
            picker._identity_profile_ctx.reset(profile_token)

        self.assertEqual({low["url"], high["url"]},
                         {stream["url"] for stream, _ in verified})
        self.assertEqual(2, rejected_events.count(
            "transport_ok_identity_rejected"))
        assembled = picker._assemble(
            verified, [], None, key=picker._verified_quality_key)
        self.assertEqual(high["url"], assembled[0]["url"])


class FastTierRegressionTests(unittest.TestCase):
    def test_count_tiers_uses_effective_not_advertised_resolution(self) -> None:
        starved_4k = _stream("Movie.2024.2160p.WEB-DL-GROUP.mkv")
        starved_4k["_effres"] = 1080
        starved_1080 = _stream("Movie.2024.1080p.WEB-DL-GROUP.mkv")
        starved_1080["_effres"] = 720

        counts = picker._count_tiers([(starved_4k, None),
                                      (starved_1080, None)])

        self.assertEqual((0, 1), counts)

    def test_unknown_size_high_resolution_gets_conservative_probe_need(self) -> None:
        unknown_4k = _stream("Movie.2024.2160p.WEB-DL-GROUP.mkv")
        self.assertEqual(picker.UNKNOWN_NEED_2160,
                         picker._need_bps_fn(7_200)(unknown_4k))


class SlowDirectUsenetRegressionTests(unittest.TestCase):
    def test_probe_slice_gives_usenet_one_exploratory_place(self) -> None:
        online = [
            _stream(f"Movie.2024.2160p.WEB-DL-ONLINE{i}.mkv",
                    f"https://stream.invalid/online/{i}")
            for i in range(18)
        ]
        direct = [
            _stream(f"Movie.2024.1080p.WEB-DL-NZB{i}.mkv",
                    f"https://nzbdav.invalid/direct/{i}")
            for i in range(3)
        ]
        for i, stream in enumerate(direct):
            stream["_nzb_release_key"] = f"nzb:release-{i}"

        selected = picker._slow_probe_slice(online + direct, max_probes=16)

        # Diversity gets one place, not half the wave: every other probe remains
        # available to the better-ranked online prospects.
        self.assertEqual(16, len(selected))
        self.assertEqual(1, sum(picker._is_direct_nzb(s) for s in selected))
        self.assertEqual(online[:15], selected[:15])
        self.assertEqual(direct[:1], selected[15:])

    def test_verified_direct_result_survives_tail_without_becoming_number_one(self) -> None:
        premium = [
            _stream(f"Movie.2024.2160p.BluRay.REMUX-GROUP{i}.mkv",
                    f"https://stream.invalid/premium/{i}")
            for i in range(18)
        ]
        direct = _stream("Movie.2024.1080p.WEB-DL-NZB.mkv",
                         "https://nzbdav.invalid/direct")
        direct.update({
            "name": "NZB",
            "_nzb_release_key": "nzb:verified-direct",
        })
        picker._annotate_quality(premium + [direct], 7_200)
        result = probe.ProbeResult(
            True, ttfb=0.1, speed_bps=20_000_000)

        streams = picker._assemble(
            [(direct, result)] + [(s, result) for s in premium],
            [], None, key=picker._verified_quality_key)

        self.assertEqual(premium[0]["url"], streams[0]["url"])
        self.assertIn(direct["url"], [s["url"] for s in streams])
        self.assertTrue(all(picker._is_ranked(s) for s in streams))


class DirectUsenetFilterRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_movie_titles_do_not_prefix_match_longer_titles(self) -> None:
        cases = (
            ("it", 2017, "It.2017.1080p.WEB-DL-GROUP",
             "It.Follows.2017.1080p.WEB-DL-WRONG"),
            ("up", 2009, "Up.2009.1080p.BluRay-GROUP",
             "Upgrade.2009.1080p.WEB-DL-WRONG"),
        )
        for expected, year, correct, wrong in cases:
            with self.subTest(title=expected):
                items = [
                    {"title": wrong, "size": 4_000_000_000,
                     "link": "https://indexer.invalid/wrong", "indexer": "idx"},
                    {"title": correct, "size": 4_000_000_000,
                     "link": "https://indexer.invalid/correct", "indexer": "idx"},
                ]
                with mock.patch.object(usenet, "INDEXERS",
                                       [("idx", "https://indexer.invalid/api", "key")]), \
                        mock.patch.object(usenet, "_search_one",
                                          new=mock.AsyncMock(return_value=items)), \
                        mock.patch.object(usenet, "_expected_info",
                                          new=mock.AsyncMock(
                                              return_value=([expected], year))), \
                        mock.patch.object(usenet.usenet_health, "should_skip",
                                          return_value=False), \
                        mock.patch.object(usenet.usenet_health, "status",
                                          return_value={}), \
                        mock.patch.object(usenet.usenet_health, "indexer_score",
                                          return_value=0.5):
                    releases = await usenet.search("movie", "tt0000001")

                self.assertEqual([correct], [r["title"] for r in releases])

    def test_region_suffix_is_not_a_safe_short_title_boundary(self) -> None:
        self.assertFalse(usenet._release_title_match(
            "Ghosts.US.S01E01.1080p.WEB-DL", "Ghosts"))
        self.assertFalse(usenet._release_title_match(
            "The.Office.UK.S01E01.1080p.WEB-DL", "The Office"))

    def test_unplayable_disc_and_bare_dv_formats_are_rejected_before_mount(self) -> None:
        self.assertFalse(usenet._mountable_release(
            "Movie.2024.COMPLETE.UHD.BLURAY-GROUP"))
        self.assertFalse(usenet._mountable_release(
            "Movie.2024.2160p.WEB-DL.DV-GROUP"))
        self.assertTrue(usenet._mountable_release(
            "Movie.2024.2160p.WEB-DL.DV.HDR10-GROUP"))

    def test_nzbdav_history_failures_are_safely_classified(self) -> None:
        self.assertEqual(
            ("hard", "missing-articles"),
            usenet._history_failure_class("Health check failed: missing articles"),
        )
        self.assertEqual(
            ("hard", "missing-articles"),
            usenet._history_failure_class("Could not retrieve article from provider"),
        )
        self.assertEqual(
            ("transient", "transport"),
            usenet._history_failure_class("Article request hit a connection timeout"),
        )
        self.assertEqual(
            ("transient", "transport"),
            usenet._history_failure_class("Could not login: connection limit reached"),
        )
        self.assertEqual(
            ("transient", "transport"),
            usenet._history_failure_class("Unrecognized internal import error"),
        )

    async def test_episode_ranges_and_packs_are_not_exact_episode_matches(self) -> None:
        self.assertTrue(usenet._episode_match(
            "Show.Name.S01E02.1080p.WEB-DL-GROUP", 1, 2))
        self.assertTrue(usenet._episode_match(
            "Show.Name.1x02.1080p.WEB-DL-GROUP", 1, 2))

        rejected = (
            "Show.Name.S01E02-E04.1080p.WEB-DL-GROUP",
            "Show.Name.S01E02E03.1080p.WEB-DL-GROUP",
            "Show.Name.S01E02-04.1080p.WEB-DL-GROUP",
            "Show.Name.S01.COMPLETE.1080p.WEB-DL-GROUP",
        )
        for title in rejected:
            with self.subTest(title=title):
                self.assertFalse(usenet._episode_match(title, 1, 2))

    def test_mount_selection_reserves_a_delivery_sized_high_quality_release(self) -> None:
        def release(key: str, title: str, size_gb: int) -> dict:
            return {
                "release_key": key,
                "title": title,
                "size": size_gb * 1_000_000_000,
                "offers": [{"indexer": "idx", "link": f"https://idx/{key}"}],
            }

        huge = [
            release(f"huge-{i}",
                    f"Movie.2024.2160p.BluRay.REMUX.HEVC-GROUP{i}",
                    120 - i * 5)
            for i in range(7)
        ]
        delivery_4k = release(
            "delivery-4k", "Movie.2024.2160p.WEB-DL.HEVC-GOOD", 18)
        delivery_1080 = release(
            "delivery-1080", "Movie.2024.1080p.WEB-DL.H264-GOOD", 8)

        with mock.patch.object(usenet.usenet_health, "status", return_value={}), \
                mock.patch.object(usenet.usenet_health, "indexer_score",
                                  return_value=0.5), \
                mock.patch.object(usenet.usenet_health, "indexer_samples",
                                  return_value=0):
            ranked = sorted(huge + [delivery_4k, delivery_1080],
                            key=usenet._priority, reverse=True)
            selected = usenet._select_releases(ranked, 6)

        selected_keys = {r["release_key"] for r in selected}
        self.assertEqual(6, len(selected))
        self.assertTrue(
            selected_keys & {"delivery-4k", "delivery-1080"},
            "mount wave contained only huge remuxes",
        )

    def test_speed_slot_mounts_smallest_real_release_first(self) -> None:
        # A usenet-only TV title: five big encodes plus one small 720p. The
        # small one must join the wave AND mount first (index 0), so it becomes
        # the fast picker's floor while the big releases are still downloading.
        def ep(key: str, title: str, size_mb: int) -> dict:
            return {"release_key": key, "title": title,
                    "size": size_mb * 1_000_000,
                    "offers": [{"indexer": "idx", "link": f"https://idx/{key}"}]}

        bigs = [ep(f"big-{i}", f"Show.S01E10.1080p.WEB.H264-GRP{i}", 2600)
                for i in range(5)]
        small = ep("small-720", "Show.S01E10.720p.WEB.H264-TINY", 380)

        with mock.patch.object(usenet.usenet_health, "status", return_value={}), \
                mock.patch.object(usenet.usenet_health, "indexer_score",
                                  return_value=0.5), \
                mock.patch.object(usenet.usenet_health, "indexer_samples",
                                  return_value=0):
            ranked = sorted(bigs + [small], key=usenet._priority, reverse=True)
            selected = usenet._select_releases(ranked, 6, media="tv")

        keys = [r["release_key"] for r in selected]
        self.assertIn("small-720", keys)                 # the floor is reserved
        self.assertEqual("small-720", keys[0])           # ...and mounts first
        self.assertEqual(6, len(selected))               # five big + one small

    def test_speed_slot_yields_to_a_proven_good_reuse(self) -> None:
        # A proven-good release mounts first (instant nzbdav reuse, top quality);
        # the small floor slots in right behind it, not ahead.
        def ep(key: str, title: str, size_mb: int) -> dict:
            return {"release_key": key, "title": title,
                    "size": size_mb * 1_000_000,
                    "offers": [{"indexer": "idx", "link": f"https://idx/{key}"}]}

        proven = ep("proven-1080", "Show.S01E10.1080p.WEB.H264-PROVEN", 2600)
        bigs = [ep(f"big-{i}", f"Show.S01E10.1080p.WEB.H264-GRP{i}", 2700)
                for i in range(4)]
        small = ep("small-720", "Show.S01E10.720p.WEB.H264-TINY", 380)

        def status(key):
            return {"successes": 3} if key == "proven-1080" else {}

        with mock.patch.object(usenet.usenet_health, "status", side_effect=status), \
                mock.patch.object(usenet.usenet_health, "indexer_score",
                                  return_value=0.5), \
                mock.patch.object(usenet.usenet_health, "indexer_samples",
                                  return_value=0):
            ranked = sorted(bigs + [proven, small],
                            key=usenet._priority, reverse=True)
            selected = usenet._select_releases(ranked, 6, media="tv")

        keys = [r["release_key"] for r in selected]
        self.assertEqual("proven-1080", keys[0])         # proven reuse leads
        self.assertEqual("small-720", keys[1])           # floor is right behind


if __name__ == "__main__":
    unittest.main()
