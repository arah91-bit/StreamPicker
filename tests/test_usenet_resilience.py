"""Crash/cancellation/upstream-shape regressions for the direct Usenet lane."""

import asyncio
import unittest
from unittest import mock

import httpx

from app import sources, usenet


def _release() -> dict:
    return {
        "release_key": "nzb:" + "b" * 64,
        "title": "Example.Movie.2024.1080p.WEB-DL-GROUP",
        "size": 8_000_000_000,
        "offers": [{"indexer": "Example", "link": "https://idx/get/1"}],
    }


class NewznabParserTests(unittest.TestCase):
    def test_namespaced_whitespace_feed_is_parsed(self) -> None:
        body = """\n
          <rss xmlns:n="http://www.newznab.com/DTD/2010/feeds/attributes/">
            <channel><item><title> Example.Movie.2024.1080p </title>
              <link> https://idx/get/1 </link>
              <enclosure url="https://idx/get/2" length=" 12345 "/>
              <n:attr name="size" value=" 67890 "/>
            </item></channel>
          </rss>\n"""
        rows, issue = usenet._parse_items_diagnostic(body)

        self.assertIsNone(issue)
        self.assertEqual("Example.Movie.2024.1080p", rows[0]["title"])
        self.assertEqual("https://idx/get/2", rows[0]["link"])
        self.assertEqual(67890, rows[0]["size"])

    def test_newznab_identity_attributes_are_parsed_privately(self) -> None:
        body = """<rss xmlns:n="http://www.newznab.com/DTD/2010/feeds/attributes/">
          <channel><item><title>Example.Show.S01E02.1080p</title>
          <link>https://idx/get/1</link>
          <n:attr name="imdb" value="tt1234567"/>
          <n:attr name="season" value="01"/>
          <n:attr name="episode" value="2"/>
          <n:attr name="category" value="5040"/>
          </item></channel></rss>"""

        rows, issue = usenet._parse_items_diagnostic(body)

        self.assertIsNone(issue)
        self.assertEqual(
            {"imdb": ["tt1234567"], "season": ["01"], "episode": ["2"]},
            rows[0]["_newznab_identity_attrs"],
        )
        self.assertNotIn("category", rows[0]["_newznab_identity_attrs"])

    def test_http_200_newznab_error_is_not_successful_empty(self) -> None:
        body = '<error code="100" description="Incorrect API key" />'
        rows, issue = usenet._parse_items_diagnostic(body)

        self.assertEqual([], rows)
        self.assertEqual("newznab-error", issue[0])
        self.assertIn("code=100", issue[1])

    def test_malformed_feed_retains_parser_position(self) -> None:
        rows, issue = usenet._parse_items_diagnostic("<rss><item></rss>")

        self.assertEqual([], rows)
        self.assertEqual("invalid-xml", issue[0])
        self.assertIn("line=", issue[1])
        self.assertIn("column=", issue[1])

    def test_nzb_download_error_retains_newznab_code(self) -> None:
        issue = usenet._nzb_payload_issue(
            b'<error code="300" description="NZB not found" />')

        self.assertEqual("newznab-error", issue[0])
        self.assertIn("code=300", issue[1])


class DetachedLaneTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        for task in list(usenet._mount_jobs.values()):
            task.cancel()
        if usenet._mount_jobs:
            await asyncio.gather(*usenet._mount_jobs.values(),
                                 return_exceptions=True)
        usenet._mount_jobs.clear()
        usenet._mount_events.clear()
        usenet._mount_outputs.clear()
        usenet._mount_outcomes.clear()

    async def asyncTearDown(self) -> None:
        for task in list(usenet._mount_jobs.values()):
            task.cancel()
        if usenet._mount_jobs:
            await asyncio.gather(*usenet._mount_jobs.values(),
                                 return_exceptions=True)
        usenet._mount_jobs.clear()
        usenet._mount_events.clear()
        usenet._mount_outputs.clear()
        usenet._mount_outcomes.clear()

    async def test_cancelling_first_caller_does_not_cancel_lane_owner(self) -> None:
        gate = asyncio.Event()

        async def delayed_empty(*_args):
            await gate.wait()
            return []

        key = ("movie", "tt0000001")
        with mock.patch.object(usenet, "enabled", return_value=True), \
                mock.patch.object(usenet, "search", side_effect=delayed_empty), \
                mock.patch.object(usenet, "MOUNT_EARLY_WAIT", 10):
            caller = asyncio.create_task(usenet.streams(*key))
            await asyncio.sleep(0)
            owner = usenet._mount_jobs[key]
            caller.cancel()
            await asyncio.gather(caller, return_exceptions=True)

            self.assertFalse(owner.done())
            self.assertTrue(usenet.in_progress(*key))
            gate.set()
            await asyncio.wait_for(owner, 1)

        self.assertFalse(usenet.in_progress(*key))
        self.assertEqual("empty", usenet.outcome(*key)["state"])

    async def test_source_http_timeout_does_not_own_internal_lane(self) -> None:
        expected = [{"url": "https://dav/content/video.mkv"}]

        async def progressive(*_args):
            await asyncio.sleep(0.02)
            return expected

        with mock.patch.object(usenet, "streams", side_effect=progressive), \
                mock.patch.object(usenet, "outcome", return_value={"state": "ok"}):
            got = await sources._run(
                (sources.NZB, "movie", "tt1"), "internal", "movie", "tt1",
                0.001)

        self.assertEqual(expected[0]["url"], got[0]["url"])
        self.assertEqual("nzb", got[0]["_source_key"])
        self.assertEqual("ok", sources.outcome(sources.NZB, "movie", "tt1")["state"])

    async def test_put_slot_bounds_the_write_but_not_the_poll_wait(self) -> None:
        # The import slot is now held only around the NZB fetch+PUT write, not
        # the read-only poll that waits out nzbdav's import — so a batch of
        # mounts throttles the writes to the slot count while every mount still
        # proceeds to (and returns from) its own unbounded poll phase.
        active = 0
        maximum = 0

        async def measured_put(*_args, **_kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
            return True, "Example"

        video = (("/content/movies/job/v.mkv", 5), "strong",
                 ["basename-title"], "")
        with mock.patch.object(usenet, "_put_slots", asyncio.Semaphore(2)), \
                mock.patch.object(usenet, "HEAD_WARM", False), \
                mock.patch.object(usenet, "_dav_tree",
                                  new=mock.AsyncMock(return_value=None)), \
                mock.patch.object(usenet, "_dav_list",
                                  new=mock.AsyncMock(return_value=None)), \
                mock.patch.object(usenet, "_related_attempts",
                                  new=mock.AsyncMock(return_value=([], []))), \
                mock.patch.object(usenet, "_history_failure",
                                  new=mock.AsyncMock(return_value=None)), \
                mock.patch.object(usenet, "_fetch_and_submit",
                                  side_effect=measured_put), \
                mock.patch.object(usenet, "_pick_video_identity",
                                  return_value=video):
            mounted = await asyncio.gather(*(
                usenet._mount_limited(_release(), "movies") for _ in range(7)))

        self.assertEqual(2, maximum)                 # writes bounded by the slot
        self.assertEqual(7, sum(m is not None for m in mounted))  # all proceeded


class NzbdavAttemptTests(unittest.IsolatedAsyncioTestCase):
    async def test_mount_preserves_exact_dav_video_size(self) -> None:
        release = _release()
        exact_size = 8_004_321_987
        entries = [("/content/movies/example/video.mkv", exact_size)]

        with mock.patch.object(
                usenet, "_dav_list",
                new=mock.AsyncMock(return_value=entries)):
            stream = await usenet._mount(release, "movies")

        self.assertIsNotNone(stream)
        self.assertEqual(
            exact_size, stream["behaviorHints"]["videoSize"],
            "slow range verification must receive DAV's exact byte total",
        )

    async def test_mount_exports_private_identity_confidence(self) -> None:
        release = _release()
        release["_nzb_expected"] = {
            "media": "movie", "media_id": "tt1234567",
            "titles": ["Example Movie"], "year": 2024,
        }
        entries = [("/content/movies/job/Example.Movie.2024.1080p.mkv", 123)]
        with mock.patch.object(
                usenet, "_dav_list", new=mock.AsyncMock(return_value=entries)):
            stream = await usenet._mount(release, "movies")

        self.assertEqual("strong", stream["_nzb_identity_confidence"])
        self.assertEqual(["basename-title", "basename-year"],
                         stream["_nzb_identity_evidence"])

    async def test_scoped_job_reuses_a_legacy_mount_after_basename_validation(self) -> None:
        release = _release()
        release["release_key"] = "nzb:" + "c" * 64
        release["legacy_release_key"] = "nzb:" + "d" * 64
        release["_nzb_expected"] = {
            "media": "movie", "media_id": "tt1234567",
            "titles": ["Example Movie"], "year": 2024,
        }
        entries = [("/content/movies/legacy/Example.Movie.2024.mkv", 123)]
        listing = mock.AsyncMock(side_effect=[None, entries])
        with mock.patch.object(usenet, "_dav_list", new=listing):
            stream = await usenet._mount(release, "movies")

        self.assertTrue(stream["_nzb_mount_reused"])
        self.assertEqual("strong", stream["_nzb_identity_confidence"])
        self.assertIn("-cccccccccccccccc", listing.await_args_list[0].args[0])
        self.assertIn("-dddddddd", listing.await_args_list[1].args[0])

    async def test_old_history_row_causes_attempt_specific_retry_name(self) -> None:
        release = _release()
        nzb = httpx.Response(
            200, request=httpx.Request("GET", release["offers"][0]["link"]),
            content=b"<nzb></nzb>")
        put = mock.AsyncMock(return_value=httpx.Response(201))
        old = ("hard", "missing-articles", "old-nzo", "missing article")
        with mock.patch.object(usenet, "_dav_list", new=mock.AsyncMock(return_value=None)), \
                mock.patch.object(usenet, "_history_failure",
                                  new=mock.AsyncMock(return_value=old)), \
                mock.patch.object(usenet._client, "get",
                                  new=mock.AsyncMock(return_value=nzb)), \
                mock.patch.object(usenet._client, "put", new=put), \
                mock.patch.object(usenet, "MOUNT_WAIT", 0), \
                mock.patch.object(usenet.usenet_health, "indexer_score",
                                  return_value=0.5), \
                mock.patch.object(usenet.usenet_health, "record_fetch"), \
                mock.patch.object(usenet.usenet_health, "record_failure",
                                  return_value=False), \
                mock.patch.object(usenet.telemetry, "record_usenet_failure"):
            self.assertIsNone(await usenet._mount(release, "movies"))

        submitted_url = put.call_args.args[0]
        base_job = usenet._slug(release["title"]) + "-" + release["release_key"][-8:]
        self.assertIn(base_job + "-a", submitted_url)

    async def test_queue_and_history_poll_is_shared(self) -> None:
        queue = httpx.Response(
            200, request=httpx.Request("GET", "https://dav/api"),
            json={"queue": {"slots": []}})
        history = httpx.Response(
            200, request=httpx.Request("GET", "https://dav/api"),
            json={"history": {"slots": []}})
        usenet._api_snapshot_cache = (0.0, [], [], [])
        get = mock.AsyncMock(side_effect=[queue, history])
        with mock.patch.object(usenet, "NZBDAV_API_KEY", "configured"), \
                mock.patch.object(usenet._client, "get", new=get):
            await asyncio.gather(usenet._history_failure("one"),
                                 usenet._history_failure("two"))

        self.assertEqual(2, get.await_count)

    async def test_queued_attempt_is_joined_without_duplicate_put(self) -> None:
        release = _release()
        queued_job = usenet._slug(release["title"]) + "-queued"
        mounted = [(f"/content/movies/{queued_job}/video.mkv", 123)]
        listing = mock.AsyncMock(side_effect=[None, mounted])
        submit = mock.AsyncMock()
        with mock.patch.object(usenet, "_dav_list", new=listing), \
                mock.patch.object(usenet, "_related_attempts",
                                  new=mock.AsyncMock(return_value=([queued_job], []))), \
                mock.patch.object(usenet, "_fetch_and_submit", new=submit), \
                mock.patch.object(usenet.asyncio, "sleep", new=mock.AsyncMock()):
            stream = await usenet._mount(release, "movies")

        self.assertIsNotNone(stream)
        submit.assert_not_awaited()
        self.assertIn(queued_job, stream["url"])

    async def test_completed_unique_attempt_is_reused_after_cache_expiry(self) -> None:
        release = _release()
        prior_job = usenet._slug(release["title"]) + "-aprior"
        mounted = [(f"/content/movies/{prior_job}/video.mkv", 123)]
        submit = mock.AsyncMock()
        with mock.patch.object(
                usenet, "_dav_list",
                new=mock.AsyncMock(side_effect=[None, mounted])), \
                mock.patch.object(usenet, "_related_attempts",
                                  new=mock.AsyncMock(return_value=([], [prior_job]))), \
                mock.patch.object(usenet, "_fetch_and_submit", new=submit):
            stream = await usenet._mount(release, "movies")

        self.assertIsNotNone(stream)
        self.assertTrue(stream["_nzb_mount_reused"])
        submit.assert_not_awaited()

    async def test_dav_parser_accepts_namespace_prefix_variants(self) -> None:
        body = b"""<d:multistatus xmlns:d="DAV:">
          <d:response><d:href> /content/a/video.mkv </d:href>
          <d:propstat><d:prop><d:getcontentlength> 42 </d:getcontentlength>
          </d:prop></d:propstat></d:response></d:multistatus>"""
        response = httpx.Response(207, content=body)
        with mock.patch.object(
                usenet._client, "request",
                new=mock.AsyncMock(return_value=response)):
            rows = await usenet._dav_list("/content/a")

        self.assertEqual([("/content/a/video.mkv", 42)], rows)


class PolicyDiagnosticTests(unittest.TestCase):
    def test_import_diagnostic_is_written_when_strike_is_idempotent(self) -> None:
        with mock.patch.object(usenet.usenet_health, "record_failure",
                               return_value=False), \
                mock.patch.object(usenet.telemetry,
                                  "record_usenet_failure") as record:
            usenet._record_import_failure(
                _release(), "hard", "missing-articles", "same-nzo",
                "health check failed: missing article")

        record.assert_called_once()
        self.assertEqual("missing-articles", record.call_args.kwargs["reason"])

    def test_disc_size_tokens_are_rejected_before_import(self) -> None:
        for title in ("Movie.2024.BD50.1080p", "Movie.2024.BD100.UHD",
                      "Movie.2024.BluRay.ISO"):
            with self.subTest(title=title):
                self.assertFalse(usenet._mountable_release(title))


def _titled_release() -> dict:
    release = _release()
    release["_nzb_expected"] = {
        "media": "movie", "media_id": "tt1234567",
        "titles": ["Example Movie"], "year": 2024,
    }
    return release


class MountSpeedupTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_mount_head_warms_reused_does_not(self) -> None:
        # A just-materialized mount primes its opening bytes; a reused (already
        # assembled) mount does not spend an NNTP read re-warming.
        entries = [("/content/movies/example/video.mkv", 5_000)]
        warmed: list[str] = []

        with mock.patch.object(usenet, "_fire_head_warm",
                               side_effect=lambda url: warmed.append(url)), \
                mock.patch.object(usenet, "_dav_tree",
                                  new=mock.AsyncMock(return_value=entries)):
            await usenet._mount(_titled_release(), "movies")   # reused
        self.assertEqual([], warmed)

        with mock.patch.object(usenet, "_fire_head_warm",
                               side_effect=lambda url: warmed.append(url)), \
                mock.patch.object(usenet, "_dav_tree",
                                  new=mock.AsyncMock(side_effect=[None, entries])), \
                mock.patch.object(usenet, "_dav_list",
                                  new=mock.AsyncMock(return_value=None)), \
                mock.patch.object(usenet, "_related_attempts",
                                  new=mock.AsyncMock(return_value=([], []))), \
                mock.patch.object(usenet, "_history_failure",
                                  new=mock.AsyncMock(return_value=None)), \
                mock.patch.object(usenet, "_fetch_and_submit",
                                  new=mock.AsyncMock(return_value=(True, "Example"))):
            await usenet._mount(_titled_release(), "movies")   # fresh
        self.assertEqual(1, len(warmed))

    async def test_head_warm_disabled_schedules_nothing(self) -> None:
        before = set(usenet._warm_tasks)
        with mock.patch.object(usenet, "HEAD_WARM", False):
            usenet._fire_head_warm("https://nzbdav.example/v.mkv")
        self.assertEqual(before, set(usenet._warm_tasks))

    async def test_obfuscated_single_file_mount_is_strong_when_title_scoped(
            self) -> None:
        # The RoboGobo case: an obfuscated filename in a release that matched
        # the requested title now mounts as strong identity instead of dropping.
        entries = [("/content/movies/job/akvzvczPaEkR8tzNBlBM5NZwzhU.mkv", 9_000)]
        with mock.patch.object(usenet, "_dav_tree",
                               new=mock.AsyncMock(return_value=entries)):
            stream = await usenet._mount(_titled_release(), "movies")

        self.assertIsNotNone(stream)
        self.assertEqual("strong", stream["_nzb_identity_confidence"])
        self.assertIn("release-title-scoped", stream["_nzb_identity_evidence"])

    async def test_prefetch_scope_caps_lane_selection(self) -> None:
        captured: dict[str, int] = {}

        def fake_select(releases, limit, media):
            captured["limit"] = limit
            return []

        out: list[dict] = []
        event = asyncio.Event()
        with usenet.prefetch_scope(), \
                mock.patch.object(usenet, "search",
                                  new=mock.AsyncMock(return_value=[_release()])), \
                mock.patch.object(usenet, "_select_releases",
                                  side_effect=fake_select):
            await usenet._run_lane("movie", "tt1234567", out, event)

        self.assertEqual(usenet.PREFETCH_MOUNT_MAX, captured["limit"])
        self.assertTrue(event.is_set())

    async def test_full_lane_uses_mount_max_without_prefetch_scope(self) -> None:
        captured: dict[str, int] = {}

        def fake_select(releases, limit, media):
            captured["limit"] = limit
            return []

        with mock.patch.object(usenet, "search",
                               new=mock.AsyncMock(return_value=[_release()])), \
                mock.patch.object(usenet, "_select_releases",
                                  side_effect=fake_select):
            await usenet._run_lane("movie", "tt1234567", [], asyncio.Event())

        self.assertEqual(usenet.MOUNT_MAX, captured["limit"])

    async def test_verified_pack_resolves_sibling_before_fresh_search_finishes(
            self) -> None:
        search_done = False
        mount_observations: list[bool] = []
        key = "nzb:" + "c" * 64
        seed = {
            "scope": "series:tt1234567:1", "release_key": key,
            "legacy_release_key": "nzb:" + "d" * 64,
            "title": "Example.Show.S01.COMPLETE.1080p.WEB-DL",
            "size": 40_000_000_000, "titles": ["Example Show"],
            "year": 2024,
        }

        async def delayed_search(*_args):
            nonlocal search_done
            await asyncio.sleep(0.04)
            search_done = True
            return []

        async def reused_mount(release, _cat, _delay, episode):
            mount_observations.append(search_done)
            self.assertEqual((1, 3), episode)
            self.assertEqual([], release["offers"])
            return {
                "name": "NZB", "url": "https://nzbdav.invalid/e3",
                "behaviorHints": {
                    "filename": "Example.Show.S01E03.1080p.WEB-DL.mkv"},
                "_nzb_release_key": key, "_nzb_pack": True,
            }

        out: list[dict] = []
        event = asyncio.Event()
        with mock.patch.object(
                usenet.candidate_health, "pack_seeds", return_value=[seed]), \
                mock.patch.object(usenet.usenet_health, "should_skip",
                                  return_value=False), \
                mock.patch.object(usenet.usenet_health, "status",
                                  return_value={"successes": 1}), \
                mock.patch.object(usenet, "search", new=delayed_search), \
                mock.patch.object(usenet, "_mount_limited", new=reused_mount):
            await usenet._run_lane(
                "series", "tt1234567:1:3", out, event)

        self.assertEqual([False], mount_observations)
        self.assertEqual(["https://nzbdav.invalid/e3"],
                         [stream["url"] for stream in out])
        self.assertTrue(event.is_set())

    async def test_prefetch_disabled_starts_no_new_lane(self) -> None:
        with mock.patch.object(usenet, "PREFETCH_MOUNT", False), \
                mock.patch.object(usenet, "enabled", return_value=True), \
                mock.patch.object(usenet, "_start_lane") as start, \
                usenet.prefetch_scope():
            out = await usenet.streams("movie", "tt-never-watched")

        self.assertEqual([], out)
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
