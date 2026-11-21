import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

from app import content_identity, picker, probe, sources


def _stream(name, url):
    return {"name": f"1080p {name}", "url": url,
            "behaviorHints": {"filename": f"Example.Movie.2024.1080p-{name}.mkv"}}


def _sd_stream(name, url):
    return {"name": f"DVD {name}", "url": url,
            "behaviorHints": {"filename": f"Example.Movie.480p.DVDRip-{name}.mkv"}}


class FastRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_verified_fallback_exposes_nothing_at_all(self):
        """Nuvio auto-plays the first thing it gets, so with nothing verified
        the answer is no streams — not an unverified candidate, and not a
        placeholder that would be played instead of the film."""
        candidate = _stream("UNVERIFIED", "https://unknown.example/video")
        background = {}

        async def finish(*_args, **_kwargs):
            return None

        with (patch.object(picker, "_background", background),
              patch.object(picker, "_finish_in_background", side_effect=finish),
              patch("app.picker.private_trackers.enabled", return_value=False)):
            streams = await picker._fast_no_verified(
                "full:movie:tt1234567", "movie", "tt1234567",
                picker.PROFILES["full"], 7_200, [candidate])
            self.assertIn("full:movie:tt1234567", background,
                          "the finisher must keep verifying for the retry")
            await asyncio.gather(*background.values())

        self.assertEqual([], streams)

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
              patch.object(picker, "_fast_no_verified",
                           new=AsyncMock(return_value=[])),
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

    async def test_a_lane_still_searching_can_still_beat_an_early_sd(self):
        """Give slower lanes time to improve on a sub-1080p candidate."""
        sd = _sd_stream("EARLY", "https://fast.example/sd")
        hd = _stream("LATER", "https://slow.example/hd")

        async def get(source, *_a, **_k):
            if source == sources.EASYNEWS:
                return [sd]
            await asyncio.sleep(0.3)               # the debrid lane, a beat behind
            return [hd]

        async def fake_probe_race(candidates, *_a, outcomes=None, **_k):
            return [(candidates[0], probe.ProbeResult(True, ttfb=0.05,
                                                      speed_bps=20_000_000))]

        started = time.monotonic()
        with (patch("app.picker.sources.search_all",
                    return_value=[sources.EASYNEWS, sources.FAST]),
              patch("app.sources.get", side_effect=get),
              patch("app.sources.peek", return_value=[]),
              patch("app.picker.nzb_lane.in_progress", return_value=False),
              patch("app.probe.probe_race", side_effect=fake_probe_race),
              patch("app.reputation.blocked", return_value=False),
              patch.object(picker, "FAST_VERIFIED_GRACE", 5)):
            verified, _ = await picker._race_fast(
                "movie", "tt1234567", picker.PROFILES["full"], 7200,
                lambda _s: 1_000_000, started)

        self.assertEqual(hd["url"], verified[0][0]["url"])
        self.assertLess(time.monotonic() - started, 2.0,
                        "the 1080p ends the race the moment it verifies")

    async def test_lone_low_res_source_is_served_without_burning_the_grace(self):
        """Plenty of older titles only ever have a 720p or an SD copy. That copy
        is the answer, not a consolation prize — with nothing better left to
        probe there is no judgment call to make, so sitting out the grace timer
        would show the viewer an empty screen for no gain."""
        sd = _stream("SD", "https://good.example/sd")
        sd["name"] = "Old Show 480p"
        sd["_effres"] = 480

        async def fake_probe_race(candidates, *_a, outcomes=None, **_k):
            await asyncio.sleep(0.02)
            return [(candidates[0], probe.ProbeResult(True, ttfb=0.1,
                                                      speed_bps=20_000_000))]

        started = time.monotonic()
        with (patch("app.picker.sources.search_all", return_value=[sources.FAST]),
              patch("app.sources.get", return_value=[sd]),
              patch("app.sources.peek", return_value=[]),
              patch("app.picker.nzb_lane.in_progress", return_value=False),
              patch("app.probe.probe_race", side_effect=fake_probe_race),
              patch("app.reputation.blocked", return_value=False),
              patch.object(picker, "FAST_VERIFIED_GRACE", 5)):
            verified, _ = await picker._race_fast(
                "movie", "tt1234567", picker.PROFILES["full"], 7200,
                lambda _s: 1_000_000, started)

        self.assertEqual(sd["url"], verified[0][0]["url"])
        # _enough() is False for 480p, so the old rule would have waited 5s.
        self.assertLess(time.monotonic() - started, 1.0)


class LateIdentityMetadataTests(unittest.IsolatedAsyncioTestCase):
    """Dark S01E01: metadata missed the fast picker's 4 s budget, the lookup
    was cancelled with it, and the request ran on the fail-closed profile —
    dozens of good copies probed fine and none was allowed to lead, for 70+ s.
    The lookup now survives the budget and the race adopts it on arrival."""

    async def test_a_late_profile_verifies_what_already_played(self):
        release = "DARK.S01E01.Secrets.2160p.NF.WEBRip.DDP5.1.x264-NTb.mkv"
        good = {"name": "[TB⚡] Comet 2160p", "title": release,
                "url": "https://dark.example/1",
                "behaviorHints": {"filename": release}}
        real = content_identity.IdentityProfile(
            media="series", imdb_id="tt5753856", aliases=("Dark",),
            years=frozenset({2017}), season=1, episode=1)

        async def slow_metadata(media, media_id):
            await asyncio.sleep(0.3)
            return real

        async def fake_probe_race(candidates, *_a, outcomes=None, **_k):
            return [(candidates[0], probe.ProbeResult(
                True, ttfb=0.01, speed_bps=20_000_000))]

        with (patch.object(picker, "_profile_tasks", {}),
              patch("app.picker.meta.identity_profile", side_effect=slow_metadata),
              patch("app.picker.sources.search_all",
                    return_value=[sources.FAST]),
              patch("app.sources.get", return_value=[dict(good)]),
              patch("app.sources.peek", return_value=[]),
              patch("app.picker.nzb_lane.in_progress", return_value=False),
              patch("app.probe.probe_race", side_effect=fake_probe_race),
              patch("app.reputation.blocked", return_value=False),
              patch("app.picker._set_anime_expectation", new=AsyncMock())):
            started = time.monotonic()
            await picker._resolve_identity_profile(
                "series", "tt5753856:1:1", timeout=0.05)
            self.assertEqual((), picker._identity_profile_ctx.get().aliases,
                             "the budget lapsed: fail-closed stand-in first")
            verified, _ = await picker._race_fast(
                "series", "tt5753856:1:1", picker.PROFILES["full"], 3200,
                lambda _s: 1_000_000, started)

        self.assertEqual(good["url"], verified[0][0]["url"])
        self.assertLess(time.monotonic() - started, 5)


class NothingVerifiedAnswerTests(unittest.IsolatedAsyncioTestCase):
    """What the fast lane says when nothing verified in its window. Nuvio
    auto-plays the first thing either addon returns, so the only placeholder
    allowed is a settled "nothing found" — everything else is silence until a
    real stream exists, or the private trackers as the last option."""

    async def _answer(self, pool, *, nzb=False, srcs=False, priv_running=False,
                      priv_enabled=False, priv_outcome=None, priv_rows=()):
        async def finish(*_a, **_k):
            return None

        async def last_resort(*_a, **_k):
            return list(priv_rows)

        async def found(*_a, **_k):
            return []

        with (patch.object(picker, "_background", {}),
              patch.object(picker, "_cache", {}),
              patch("app.picker._finish_in_background", side_effect=finish),
              patch("app.picker.nzb_lane.in_progress", return_value=nzb),
              patch("app.picker.sources.in_progress", return_value=srcs),
              patch("app.picker.private_trackers.search_in_progress",
                    return_value=priv_running),
              patch("app.picker.private_trackers.enabled",
                    return_value=priv_enabled),
              patch("app.picker.private_trackers.candidates", side_effect=found),
              patch("app.picker.private_trackers.search_outcome",
                    lambda *a, **k: dict(priv_outcome or {})),
              patch("app.picker._private_last_resort", side_effect=last_resort)):
            return await picker._fast_no_verified(
                "k", "movie", "tt1", picker.PROFILES["full"], 7200, pool)

    async def test_says_nothing_found_when_every_lane_is_empty_and_idle(self):
        rows = await self._answer([])
        self.assertEqual("not_found", rows[0][picker._NOTICE_STATE_KEY])

    async def test_silent_while_candidates_await_verification(self):
        self.assertEqual([], await self._answer([_stream("HD", "https://x/1")]))

    async def test_silent_while_a_usenet_mount_is_still_running(self):
        self.assertEqual([], await self._answer([], nzb=True))

    async def test_silent_while_a_source_search_is_still_running(self):
        self.assertEqual([], await self._answer([], srcs=True))

    async def test_silent_while_private_trackers_are_searching(self):
        self.assertEqual([], await self._answer(
            [], priv_enabled=True, priv_running=True))

    async def test_empty_public_result_consults_private_before_giving_up(self):
        self.assertEqual([], await self._answer([], priv_enabled=True))

    async def test_private_empty_is_a_settled_nothing_found(self):
        rows = await self._answer([], priv_enabled=True,
                                  priv_outcome={"state": "empty"})
        self.assertEqual("not_found", rows[0][picker._NOTICE_STATE_KEY])

    async def test_a_failed_private_search_is_not_an_answer(self):
        self.assertEqual([], await self._answer(
            [], priv_enabled=True, priv_outcome={"state": "failed"}))

    async def test_private_releases_are_the_last_available_option(self):
        row = {"name": "🔒 X · 1080p · Click to Download & Stream",
               "url": "https://addon/private/t", "_private_tracker": True,
               "_private_action": True}
        rows = await self._answer([], priv_enabled=True,
                                  priv_outcome={"state": "ok", "count": 1},
                                  priv_rows=[row])
        self.assertEqual([row], rows)

    async def test_private_rows_never_jump_live_public_candidates(self):
        row = {"name": "🔒", "url": "https://addon/private/t",
               "_private_tracker": True}
        self.assertEqual([], await self._answer(
            [_stream("HD", "https://x/1")], priv_enabled=True,
            priv_outcome={"state": "ok"}, priv_rows=[row]))


class DisplayGateTests(unittest.TestCase):
    """The single rule at the boundary: what may reach a player that
    auto-plays whatever comes first."""

    def _gate(self, streams, pending=False):
        with patch.object(picker, "_work_pending", return_value=pending):
            return picker._display_gate("k", "movie", "tt1", streams)

    def test_transient_placeholders_never_leave_the_picker(self):
        for kind in ("checking", "private_failed"):
            with self.subTest(kind=kind):
                self.assertEqual([], self._gate([picker._notice_stream(kind)]))

    def test_settled_outcomes_are_reported(self):
        for kind in ("not_found", "theatrical", "private_empty"):
            with self.subTest(kind=kind):
                rows = self._gate([picker._notice_stream(kind)])
                self.assertEqual(kind, rows[0][picker._NOTICE_STATE_KEY])

    def test_unverified_candidates_are_never_a_leader(self):
        self.assertEqual([], self._gate([_stream("U", "https://x/u")]))


class SufficiencyRuleTests(unittest.TestCase):
    """The rule the race stops on: hold something that plays, and keep waiting
    only while something better is genuinely still coming."""

    def test_enough_bar_itself_is_unchanged(self):
        hd = _stream("HD", "https://x/hd")
        hd["_effres"] = 1080
        self.assertTrue(picker._enough([(hd, None)]))
        sd = _stream("SD", "https://x/sd")
        sd["_effres"] = 480
        self.assertFalse(picker._enough([(sd, None)]))

    def test_four_k_grace_never_exceeds_the_general_grace(self):
        # A 4K hold is meant to be the *shorter* of the two waits: it defers a
        # result we are otherwise happy with, where the general grace defers one
        # we are not.
        self.assertLessEqual(picker.FAST_4K_GRACE, picker.FAST_VERIFIED_GRACE)


class ProbeRequirementTests(unittest.TestCase):
    def test_known_bitrate_uses_relative_headroom_without_unknown_floor(self):
        self.assertEqual(1_500_000, probe._required_bps(1_000_000))

    def test_unknown_bitrate_uses_fixed_safety_floor(self):
        self.assertEqual(probe.MIN_SPEED_BPS, probe._required_bps(None))


if __name__ == "__main__":
    unittest.main()
