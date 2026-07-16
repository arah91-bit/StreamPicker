import unittest
from unittest import mock

import httpx

from app import usenet
from app.usenet_health import classify_reason


def _release(*, link: str = "https://indexer.example/get/one") -> dict:
    return {
        "release_key": "nzb:" + "a" * 64,
        "title": "Example.Movie.2024.4K.WEB-DL-GROUP",
        "size": 18_000_000_000,
        "offers": [{"indexer": "ExampleIndexer", "link": link}],
    }


class UsenetClassificationTests(unittest.TestCase):
    def test_bare_4k_is_the_top_resolution_tier(self) -> None:
        self.assertEqual(3, usenet._quality(_release())[1])

    def test_not_media_is_a_decisive_health_failure(self) -> None:
        self.assertEqual("hard", classify_reason("not-media"))
        self.assertEqual("hard", classify_reason("not media"))

    def test_missing_mount_content_has_reachable_distinct_classes(self) -> None:
        wrong_episode = [("/content/job/Example.S01E03.mkv", 4_000_000_000)]
        non_video = [("/content/job/readme.txt", 100)]

        self.assertEqual(
            "wrong-episode",
            usenet._missing_content_reason(wrong_episode, (1, 2), True),
        )
        self.assertEqual(
            "not-video",
            usenet._missing_content_reason(non_video, None, True),
        )
        self.assertEqual(
            "never-appeared",
            usenet._missing_content_reason([], None, False),
        )


class FailureTelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_dav_timeout_is_structured_and_deduped_per_mount(self) -> None:
        release = _release()
        timeout = httpx.ReadTimeout(
            "credentialed URL must not be retained",
            request=httpx.Request("PROPFIND", "https://user:secret@dav.example/x"),
        )
        seen: set[str] = set()
        with mock.patch.object(
                usenet._client, "request",
                new=mock.AsyncMock(side_effect=timeout)), \
                mock.patch.object(
                    usenet.telemetry, "record_usenet_failure") as record:
            self.assertIsNone(await usenet._dav_list("/content/job", release, seen))
            self.assertIsNone(await usenet._dav_list("/content/job", release, seen))

        record.assert_called_once()
        fields = record.call_args.kwargs
        self.assertEqual("nzbdav-dav", fields["stage"])
        self.assertEqual("timeout", fields["reason"])
        self.assertIn("ReadTimeout", fields["detail"])
        self.assertIn("credentialed URL must not be retained", fields["detail"])
        self.assertNotIn("secret", str(fields))

    async def test_fetch_http_failure_retains_status_without_url(self) -> None:
        link = "https://indexer.example/get/one?apikey=do-not-store"
        release = _release(link=link)
        response = httpx.Response(
            403, request=httpx.Request("GET", link), content=b"forbidden")
        with mock.patch.object(
                usenet, "_dav_list", new=mock.AsyncMock(return_value=None)), \
                mock.patch.object(
                    usenet, "_history_failure", new=mock.AsyncMock(return_value=None)), \
                mock.patch.object(
                    usenet._client, "get", new=mock.AsyncMock(return_value=response)), \
                mock.patch.object(
                    usenet.usenet_health, "indexer_score", return_value=0.5), \
                mock.patch.object(
                    usenet.usenet_health, "record_fetch") as record_fetch, \
                mock.patch.object(
                    usenet.telemetry, "record_usenet_failure") as record:
            self.assertIsNone(await usenet._mount(release, "movies"))

        record_fetch.assert_called_once_with("ExampleIndexer", False)
        record.assert_called_once()
        fields = record.call_args.kwargs
        self.assertEqual("nzb-fetch", fields["stage"])
        self.assertEqual("http-403", fields["reason"])
        self.assertIn("HTTPStatusError HTTP 403", fields["detail"])
        self.assertIn("body=forbidden", fields["detail"])
        self.assertNotIn("do-not-store", str(fields))

    async def test_repeated_put_status_is_one_stable_failure_sample(self) -> None:
        release = _release()
        nzb = httpx.Response(
            200, request=httpx.Request("GET", release["offers"][0]["link"]),
            content=b"<nzb></nzb>")
        unavailable = httpx.Response(503)
        with mock.patch.object(
                usenet, "_dav_list", new=mock.AsyncMock(return_value=None)), \
                mock.patch.object(
                    usenet, "_history_failure", new=mock.AsyncMock(return_value=None)), \
                mock.patch.object(
                    usenet._client, "get", new=mock.AsyncMock(return_value=nzb)), \
                mock.patch.object(
                    usenet._client, "put",
                    new=mock.AsyncMock(side_effect=[unavailable, unavailable])), \
                mock.patch.object(
                    usenet.asyncio, "sleep", new=mock.AsyncMock()), \
                mock.patch.object(
                    usenet.usenet_health, "indexer_score", return_value=0.5), \
                mock.patch.object(
                    usenet.usenet_health, "record_fetch"), \
                mock.patch.object(
                    usenet.telemetry, "record_usenet_failure") as record:
            self.assertIsNone(await usenet._mount(release, "movies"))

        record.assert_called_once()
        fields = record.call_args.kwargs
        self.assertEqual("nzbdav-put", fields["stage"])
        self.assertEqual("http-503", fields["reason"])
        self.assertIn("HTTP 503", fields["detail"])

    async def test_mounted_non_video_content_records_a_hard_failure(self) -> None:
        # The junk verdict requires a layout that held still for three
        # observations — a settled mount full of non-video really is junk.
        release = _release()
        entries = [("/content/movies/job/readme.txt", 100)]
        with mock.patch.object(
                usenet, "_dav_list", new=mock.AsyncMock(return_value=entries)), \
                mock.patch.object(usenet, "MOUNT_WAIT", 0.2), \
                mock.patch.object(
                    usenet.asyncio, "sleep", new=mock.AsyncMock()), \
                mock.patch.object(
                    usenet, "_history_failure",
                    new=mock.AsyncMock(return_value=None)), \
                mock.patch.object(
                    usenet.usenet_health, "record_failure", return_value=True), \
                mock.patch.object(
                    usenet.telemetry, "record_usenet_failure") as record:
            self.assertIsNone(await usenet._mount(release, "movies"))

        record.assert_called_once()
        fields = record.call_args.kwargs
        self.assertEqual("nzbdav-content", fields["stage"])
        self.assertEqual("hard", fields["decision"])
        self.assertEqual("not-video", fields["reason"])

    async def test_video_nested_in_release_folder_is_found(self) -> None:
        # Some imports materialize the release's own folder inside the job
        # dir; the mkv one level down must be found (live incident: a
        # playable bundle was struck not-video twice for this).
        release = _release()

        async def dav(path, release=None, seen=None):
            # Real nzbdav shape: collection hrefs carry NO trailing slash
            # and the inner folder is a dotted release name; the listing
            # also echoes the requested dir itself.
            if path.endswith("Example.Movie.2024.4K.WEB-DL-GROUP"):
                return [(path, 0),
                        (f"{path}/Example.Movie.2024.4K.WEB-DL-GROUP.mkv",
                         5_000_000_000)]
            return [(path, 0), (f"{path}/Example.Movie.2024.4K.WEB-DL-GROUP", 0)]

        with mock.patch.object(usenet, "_dav_list", new=dav), \
                mock.patch.object(usenet, "MOUNT_WAIT", 0):
            stream = await usenet._mount(release, "movies")

        self.assertIsNotNone(stream)
        self.assertTrue(stream["url"].endswith(
            "Example.Movie.2024.4K.WEB-DL-GROUP.mkv"))

    async def test_dav_tree_descent_is_bounded(self) -> None:
        calls = []

        async def dav(path, release=None, seen=None):
            calls.append(path)
            return [(f"{path}/deeper/", 0)]

        with mock.patch.object(usenet, "_dav_list", new=dav):
            out = await usenet._dav_tree("/content/movies/job")

        # top-level + at most two nested levels; endless chains stop.
        self.assertLessEqual(len(calls), 4)
        self.assertTrue(any(h.endswith("/deeper/") for h, _ in out))

    async def test_deadline_expiry_mid_import_never_strikes_hard(self) -> None:
        # A directory still materializing when MOUNT_WAIT expires is "import
        # still running", not junk — a playable release earned a 24h
        # not-video strike this way (PAW Patrol incident, 2026-07-15).  The
        # changing shape means stability is never reached.
        release = _release()
        state = {"n": 0}

        async def dav(path, release=None, seen=None):
            # A listing that grows on every observation: the import is
            # materializing files, so the shape never holds still.
            state["n"] += 1
            return [(f"/content/movies/job/part{i}.nfo", 100 + i)
                    for i in range(state["n"])]

        with mock.patch.object(usenet, "_dav_list", new=dav), \
                mock.patch.object(usenet, "MOUNT_WAIT", 0.05), \
                mock.patch.object(
                    usenet.asyncio, "sleep", new=mock.AsyncMock()), \
                mock.patch.object(
                    usenet, "_history_failure",
                    new=mock.AsyncMock(return_value=None)), \
                mock.patch.object(
                    usenet.usenet_health, "record_failure", return_value=True), \
                mock.patch.object(
                    usenet.telemetry, "record_usenet_failure") as record:
            self.assertIsNone(await usenet._mount(release, "movies"))

        record.assert_called_once()
        fields = record.call_args.kwargs
        self.assertEqual("transient", fields["decision"])
        self.assertEqual("never-appeared", fields["reason"])

    async def test_never_appeared_records_a_transient_mount_timeout(self) -> None:
        release = _release()
        nzb = httpx.Response(
            200, request=httpx.Request("GET", release["offers"][0]["link"]),
            content=b"<nzb></nzb>")
        accepted = mock.Mock(return_value=True)
        with mock.patch.object(
                usenet, "_dav_list", new=mock.AsyncMock(return_value=None)), \
                mock.patch.object(
                    usenet, "_history_failure", new=mock.AsyncMock(return_value=None)), \
                mock.patch.object(
                    usenet._client, "get", new=mock.AsyncMock(return_value=nzb)), \
                mock.patch.object(
                    usenet._client, "put",
                    new=mock.AsyncMock(return_value=httpx.Response(201))), \
                mock.patch.object(usenet, "MOUNT_WAIT", 0), \
                mock.patch.object(
                    usenet.usenet_health, "indexer_score", return_value=0.5), \
                mock.patch.object(usenet.usenet_health, "record_fetch"), \
                mock.patch.object(
                    usenet.usenet_health, "record_failure", accepted), \
                mock.patch.object(
                    usenet.telemetry, "record_usenet_failure") as record:
            self.assertIsNone(await usenet._mount(release, "movies"))

        self.assertEqual("never-appeared", accepted.call_args.args[3])
        record.assert_called_once()
        fields = record.call_args.kwargs
        self.assertEqual("nzbdav-mount", fields["stage"])
        self.assertEqual("transient", fields["decision"])
        self.assertEqual("never-appeared", fields["reason"])


if __name__ == "__main__":
    unittest.main()
