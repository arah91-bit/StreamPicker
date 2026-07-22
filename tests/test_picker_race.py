import asyncio
import time
import unittest
from unittest.mock import patch

from app import picker, probe, sources


def _stream(name, url):
    return {"name": f"1080p {name}", "url": url,
            "behaviorHints": {"filename": f"Example.Movie.2024.1080p-{name}.mkv"}}


def _sd_stream(name, url):
    return {"name": f"DVD {name}", "url": url,
            "behaviorHints": {"filename": f"Example.Movie.480p.DVDRip-{name}.mkv"}}


class FastRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_verified_fallback_exposes_no_unverified_candidates(self):
        candidate = _stream("UNVERIFIED", "https://unknown.example/video")
        background = {}

        async def finish(*_args, **_kwargs):
            return None

        with (patch.object(picker, "_background", background),
              patch.object(picker, "_finish_in_background", side_effect=finish)):
            streams = picker._fast_checking_notice(
                "full:movie:tt1234567", "movie", "tt1234567",
                picker.PROFILES["full"], 7_200, [candidate])
            await asyncio.gather(*background.values())

        self.assertEqual(1, len(streams))
        self.assertEqual("checking", streams[0][picker._NOTICE_STATE_KEY])
        self.assertNotEqual(candidate["url"], streams[0]["url"])

    async def test_fast_metadata_stage_leaves_time_for_byte_probe(self):
        race_started = asyncio.Event()

        async def no_cache(*_args, **_kwargs):
            return None

        async def identity(*_args, **_kwargs):
            await asyncio.sleep(0.01)

        async def slow_metadata(*_args, **_kwargs):
            await asyncio.sleep(5)

        async def race(*_args, **_kwargs):
            race_started.set()
            return [], []

        started = time.monotonic()
        with (patch.object(picker, "_cached_pick", side_effect=no_cache),
              patch.object(picker.sources, "search_all", return_value=[]),
              patch.object(picker, "_resolve_identity_profile",
                           side_effect=identity),
              patch.object(picker, "_resolve_accept_langs",
                           side_effect=slow_metadata),
              patch.object(picker, "_runtime_seconds",
                           side_effect=slow_metadata),
              patch.object(picker, "_race_fast", side_effect=race),
              patch.object(picker, "_fast_checking_notice", return_value=[]),
              patch.object(picker, "FAST_METADATA_BUDGET", 0.06),
              patch.object(picker, "TOTAL_DEADLINE", 0.25),
              patch.object(picker, "FAST_RACE_DEADLINE", 0.25)):
            await picker._pick_online("movie", "tt1234567")

        self.assertTrue(race_started.is_set())
        self.assertLess(time.monotonic() - started, 0.2)

    async def test_ready_high_quality_library_is_probed_then_immediate_stop(self):
        library_stream = _stream("LIBRARY", "https://library.example/video")

        async def slow_get(source, media, media_id, wait):
            await asyncio.sleep(0.30)
            return []

        async def library_result():
            await asyncio.sleep(0.01)
            return [library_stream]

        async def verify(candidates, *args, **kwargs):
            stream = candidates[0]
            return [(stream, probe.ProbeResult(
                True, ttfb=0.01, speed_bps=50_000_000))]

        started = time.monotonic()
        task = asyncio.create_task(library_result())
        with (patch("app.sources.has", return_value=True),
              patch("app.sources.get", side_effect=slow_get),
              patch("app.sources.peek", return_value=[]),
              patch("app.picker.nzb_lane.in_progress", return_value=False),
              patch("app.probe.probe_race", side_effect=verify),
              patch("app.reputation.blocked", return_value=False)):
            verified, _ = await picker._race_fast(
                "movie", "tt1234567", picker.PROFILES["full"], 7200,
                lambda _s: 1_000_000, started, lib_task=task)

        self.assertIsNotNone(verified[0][1])
        self.assertEqual(library_stream["url"], verified[0][0]["url"])
        self.assertLess(time.monotonic() - started, 0.15)

    async def test_later_good_source_is_not_hidden_by_earlier_hanging_probe(self):
        bad = _stream("BAD", "https://bad.example/video")
        good = _stream("GOOD", "https://good.example/video")

        async def fake_get(source, media, media_id, wait):
            if source == sources.FAST:
                return [bad]
            if source == sources.STREMTHRU:
                await asyncio.sleep(0.05)
                return [good]
            return []

        async def fake_probe_race(candidates, need_bps_of, ttfb_max, want,
                                  concurrency=8, deadline=None,
                                  expect_secs=None, outcomes=None, **_kwargs):
            stream = candidates[0]
            if stream["url"] == bad["url"]:
                await asyncio.sleep(0.30)
                if outcomes is not None:
                    outcomes.append((stream, probe.ProbeResult(
                        False, reason="HTTP 404")))
                return []
            await asyncio.sleep(0.01)
            return [(stream, probe.ProbeResult(True, ttfb=0.1,
                                               speed_bps=20_000_000))]

        started = time.monotonic()
        with (patch("app.sources.has",
                    side_effect=lambda s: s in (sources.FAST, sources.STREMTHRU)),
              patch("app.sources.get", side_effect=fake_get),
              patch("app.sources.peek", return_value=[]),
              patch("app.picker.nzb_lane.in_progress", return_value=False),
              patch("app.probe.probe_race", side_effect=fake_probe_race),
              patch("app.reputation.blocked", return_value=False)):
            verified, _ = await picker._race_fast(
                "movie", "tt1234567", picker.PROFILES["full"], 7200,
                lambda _s: 1_000_000, started)

        self.assertEqual(good["url"], verified[0][0]["url"])
        self.assertLess(time.monotonic() - started, 0.25)

    async def test_systemic_outcome_benches_only_the_failed_host(self):
        first = _stream("FIRST", "https://bad.example/first")
        same_host = _stream("SECOND", "https://bad.example/second")
        good = _stream("GOOD", "https://good.example/video")
        called = []

        async def fake_probe_race(candidates, *_args, outcomes=None, **_kwargs):
            stream = candidates[0]
            called.append(stream["url"])
            self.assertIsNotNone(outcomes)
            if stream["url"] == first["url"]:
                outcomes.append((stream, probe.ProbeResult(
                    False, reason="ConnectTimeout")))
                return []
            return [(stream, probe.ProbeResult(
                True, ttfb=0.01, speed_bps=20_000_000))]

        started = time.monotonic()
        with (patch("app.picker.sources.search_all", return_value=[sources.FAST]),
              patch("app.sources.get", return_value=[first, same_host, good]),
              patch("app.sources.peek", return_value=[]),
              patch("app.picker.nzb_lane.in_progress", return_value=False),
              patch("app.probe.probe_race", side_effect=fake_probe_race),
              patch("app.reputation.blocked", return_value=False),
              patch.object(picker, "PROBE_BATCH", 1),
              patch.object(picker, "PROBE_HOST_BENCH", 1),
              patch.object(picker, "_enough", side_effect=bool)):
            verified, _ = await picker._race_fast(
                "movie", "tt1234567", picker.PROFILES["full"], 7200,
                lambda _s: 1_000_000, started)

        self.assertEqual("bad.example", picker._probe_host(first))
        self.assertEqual("", picker._probe_host({
            **first, "_nzb_release_key": "nzb:direct",
        }))
        self.assertEqual([first["url"], good["url"]], called)
        self.assertEqual(good["url"], verified[0][0]["url"])


    async def test_grace_timer_returns_best_verified_and_stops_chasing_hd(self):
        # Once a stream that definitely plays is in hand, the race gives itself
        # only FAST_VERIFIED_GRACE more seconds for something better, then
        # answers — it must not sit on an unproven "HD" label whose probe never
        # completes (exactly the junk-HLS case that held Magic School Bus 48s).
        good = _sd_stream("GOOD", "https://good.example/video")   # verifies fast
        fakehd = _stream("FAKEHD", "https://junk.example/hls")    # 1080p, hangs

        async def fake_probe_race(candidates, *_a, outcomes=None, **_k):
            stream = candidates[0]
            if stream["url"] == fakehd["url"]:
                await asyncio.sleep(5)                # never verifies in time
                return []
            await asyncio.sleep(0.02)
            return [(stream, probe.ProbeResult(True, ttfb=0.1,
                                               speed_bps=6_000_000))]

        started = time.monotonic()
        with (patch("app.picker.sources.search_all", return_value=[sources.FAST]),
              patch("app.sources.get", return_value=[fakehd, good]),
              patch("app.sources.peek", return_value=[]),
              patch("app.picker.nzb_lane.in_progress", return_value=False),
              patch("app.probe.probe_race", side_effect=fake_probe_race),
              patch("app.reputation.blocked", return_value=False),
              patch.object(picker, "FAST_VERIFIED_GRACE", 0.3)):
            verified, _ = await picker._race_fast(
                "movie", "tt1234567", picker.PROFILES["full"], 7200,
                lambda _s: 1_000_000, started)

        elapsed = time.monotonic() - started
        self.assertEqual(good["url"], verified[0][0]["url"])
        self.assertGreaterEqual(elapsed, 0.3)     # waited the grace window
        self.assertLess(elapsed, 2.0)             # but not the hanging HD probe

    async def test_speed_first_probes_fast_verifier_before_nzb_mount(self):
        # Before anything has verified, the opening probe order puts a
        # fast-to-verify direct link ahead of a same-quality NZB that needs a
        # mount assembled — so a floor result lands sooner and starts the grace
        # timer. The NZB is listed first in the (quality-tied, stable) pool.
        nzb = _stream("NZB", "https://nzbdav.example/video")
        nzb["_nzb_release_key"] = "nzb:example"
        http = _stream("HTTP", "https://cdn.example/video")
        called: list[str] = []

        async def fake_probe_race(candidates, *_a, outcomes=None, **_k):
            called.append(candidates[0]["url"])
            return [(candidates[0], probe.ProbeResult(True, ttfb=0.01,
                                                      speed_bps=20_000_000))]

        started = time.monotonic()
        with (patch("app.picker.sources.search_all", return_value=[sources.FAST]),
              patch("app.sources.get", return_value=[nzb, http]),
              patch("app.sources.peek", return_value=[]),
              patch("app.picker.nzb_lane.in_progress", return_value=False),
              patch("app.probe.probe_race", side_effect=fake_probe_race),
              patch("app.reputation.blocked", return_value=False),
              patch.object(picker, "PROBE_BATCH", 1),
              patch.object(picker, "FAST_SPEED_FIRST", True)):
            await picker._race_fast(
                "movie", "tt1234567", picker.PROFILES["full"], 7200,
                lambda _s: 1_000_000, started)

        self.assertEqual(http["url"], called[0])

    async def test_speed_first_off_keeps_best_quality_probe_order(self):
        # With the flag off, opening order is strict quality-first (stable), so
        # the NZB listed first is probed first even though it verifies slower.
        nzb = _stream("NZB", "https://nzbdav.example/video")
        nzb["_nzb_release_key"] = "nzb:example"
        http = _stream("HTTP", "https://cdn.example/video")
        called: list[str] = []

        async def fake_probe_race(candidates, *_a, outcomes=None, **_k):
            called.append(candidates[0]["url"])
            return [(candidates[0], probe.ProbeResult(True, ttfb=0.01,
                                                      speed_bps=20_000_000))]

        started = time.monotonic()
        with (patch("app.picker.sources.search_all", return_value=[sources.FAST]),
              patch("app.sources.get", return_value=[nzb, http]),
              patch("app.sources.peek", return_value=[]),
              patch("app.picker.nzb_lane.in_progress", return_value=False),
              patch("app.probe.probe_race", side_effect=fake_probe_race),
              patch("app.reputation.blocked", return_value=False),
              patch.object(picker, "PROBE_BATCH", 1),
              patch.object(picker, "FAST_SPEED_FIRST", False)):
            await picker._race_fast(
                "movie", "tt1234567", picker.PROFILES["full"], 7200,
                lambda _s: 1_000_000, started)

        self.assertEqual(nzb["url"], called[0])

    async def test_fast_race_gives_nzb_the_usenet_ttfb_budget(self):
        # A fresh nzbdav mount needs 20-35s for its first byte; the race must
        # probe direct-NZB with USENET_TTFB_MAX, not the 12s debrid budget that
        # would fail a healthy cold mount. Other candidates keep PROBE_TTFB_MAX.
        nzb = _stream("NZB", "https://nzbdav.example/video")
        nzb["_nzb_release_key"] = "nzb:example"
        http = _stream("HTTP", "https://cdn.example/video")
        seen: dict[str, float] = {}

        async def fake_probe_race(candidates, need, ttfb, *_a, **_k):
            seen[candidates[0]["url"]] = ttfb
            return [(candidates[0], probe.ProbeResult(True, ttfb=0.01,
                                                      speed_bps=20_000_000))]

        started = time.monotonic()
        with (patch("app.picker.sources.search_all", return_value=[sources.FAST]),
              patch("app.sources.get", return_value=[nzb, http]),
              patch("app.sources.peek", return_value=[]),
              patch("app.picker.nzb_lane.in_progress", return_value=False),
              patch("app.probe.probe_race", side_effect=fake_probe_race),
              patch("app.reputation.blocked", return_value=False),
              patch.object(picker, "PROBE_BATCH", 3),
              patch.object(picker, "FAST_SPEED_FIRST", False)):
            await picker._race_fast(
                "movie", "tt1234567", picker.PROFILES["full"], 7200,
                lambda _s: 1_000_000, started)

        self.assertEqual(picker.USENET_TTFB_MAX, seen[nzb["url"]])
        self.assertEqual(picker.PROBE_TTFB_MAX, seen[http["url"]])

    async def test_verified_hd_returns_immediately_without_waiting_grace(self):
        # A verified 1080p trips _enough, so the race returns at once even though
        # the grace window is long.
        hd = _stream("HD", "https://good.example/hd")            # 1080p

        async def fake_probe_race(candidates, *_a, outcomes=None, **_k):
            await asyncio.sleep(0.02)
            return [(candidates[0], probe.ProbeResult(True, ttfb=0.1,
                                                      speed_bps=20_000_000))]

        started = time.monotonic()
        with (patch("app.picker.sources.search_all", return_value=[sources.FAST]),
              patch("app.sources.get", return_value=[hd]),
              patch("app.sources.peek", return_value=[]),
              patch("app.picker.nzb_lane.in_progress", return_value=False),
              patch("app.probe.probe_race", side_effect=fake_probe_race),
              patch("app.reputation.blocked", return_value=False),
              patch.object(picker, "FAST_VERIFIED_GRACE", 5)):
            verified, _ = await picker._race_fast(
                "movie", "tt1234567", picker.PROFILES["full"], 7200,
                lambda _s: 1_000_000, started)

        self.assertEqual(hd["url"], verified[0][0]["url"])
        self.assertLess(time.monotonic() - started, 1.0)   # did not wait 5s


class ProbeRequirementTests(unittest.TestCase):
    def test_known_bitrate_uses_relative_headroom_without_unknown_floor(self):
        self.assertEqual(1_500_000, probe._required_bps(1_000_000))

    def test_unknown_bitrate_uses_fixed_safety_floor(self):
        self.assertEqual(probe.MIN_SPEED_BPS, probe._required_bps(None))


if __name__ == "__main__":
    unittest.main()
