"""The seam between stream-picker and the vendored Daily Picks package."""

import os

os.environ.setdefault("ADDON_SECRET", "test-secret")

import unittest

from fastapi.testclient import TestClient

from app import main


class RouteShadowingTests(unittest.TestCase):
    """`/{secret}/manifest.json` matches ANY single segment, so a literal-prefix
    recs route registered after it would be silently swallowed."""

    @classmethod
    def setUpClass(cls):
        cls._ctx = TestClient(main.app, client=("127.0.0.1", 50000))
        cls.client = cls._ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._ctx.__exit__(None, None, None)

    def test_literal_recs_paths_are_not_captured_as_an_addon_secret(self):
        # "kids" must reach the kids catalog pack, not bind to {secret} and 404.
        response = self.client.get("/kids/manifest.json")
        self.assertEqual(200, response.status_code)
        self.assertIn("catalogs", response.json())

    def test_the_shared_stream_addon_still_answers(self):
        response = self.client.get("/test-secret/manifest.json")
        self.assertEqual(200, response.status_code)
        self.assertEqual(["stream"],
                         [r["name"] if isinstance(r, dict) else r
                          for r in response.json()["resources"]])

    def test_an_unknown_segment_is_still_a_404(self):
        self.assertEqual(
            404, self.client.get("/wrong-secret/manifest.json").status_code)
        self.assertEqual(
            404,
            self.client.get("/wrong-secret/stream/movie/tt1.json").status_code)

    def test_setup_onboarding_stays_reachable(self):
        # Remote viewers connect Trakt here; it must not sit behind the
        # LAN-only admin gate.
        response = self.client.get("/setup/not-the-secret")
        self.assertEqual(404, response.status_code)


if __name__ == "__main__":
    unittest.main()


class BackgroundTaskTests(unittest.IsolatedAsyncioTestCase):
    """app/recs' own @app.on_event("startup") never fires in the merged app —
    only its routes were copied across. Anything added there is silently dead
    unless recs_mount.startup() starts it too, which is exactly how the Asian
    Dramas builder came up with an empty state the first time."""

    async def test_every_recs_background_runner_is_actually_started(self):
        import asyncio
        import inspect

        from app import recs_mount
        from app.recs import main as recs_main

        # The runners app/recs/main.py schedules in its own startup handler.
        source = inspect.getsource(recs_main.startup)
        declared = {name for name in
                    ("scheduler", "kids_catalogs", "profile_streaming", "dramas",
                     "watching",
                     "playhistory")
                    if f"{name}.run()" in source}
        self.assertTrue(declared, "could not read recs startup handler")

        started = set()

        def spy(name):
            async def _run():
                started.add(name)
                await asyncio.sleep(0)
            return _run

        from unittest.mock import AsyncMock, patch
        from app.recs import (dramas, kids_catalogs, playhistory,
                              profile_streaming, scheduler, watching)
        with (
            patch.object(recs_mount, "__name__", recs_mount.__name__),
            patch.object(scheduler, "run", spy("scheduler")),
            patch.object(kids_catalogs, "run", spy("kids_catalogs")),
            patch.object(profile_streaming, "run", spy("profile_streaming")),
            patch.object(dramas, "run", spy("dramas")),
            patch.object(watching, "run", spy("watching")),
            patch.object(playhistory, "run", spy("playhistory")),
            patch("app.recs.db.init", AsyncMock()),
        ):
            tasks = await recs_mount.startup()
            await asyncio.gather(*tasks)

        self.assertEqual(declared, started,
                         "recs_mount.startup() is out of step with "
                         "app/recs/main.py's startup handler")


class StreamLaneTests(unittest.IsolatedAsyncioTestCase):
    """Fast and slow cannot share one manifest — a Stremio manifest declares a
    single stream endpoint, and the slow picker waits for every source before
    answering, so merging them would make every request as slow as the slow
    lane. They are separate per-viewer installs, and every one of those URLs
    still carries the token, so all of them attribute to the same person."""

    async def asyncSetUp(self):
        from unittest.mock import AsyncMock, patch
        self._p = patch.object(
            main.recs_mount, "viewer_for",
            AsyncMock(return_value={"token": "vt", "name": "Viewer"}))
        self._p.start()
        self.addCleanup(self._p.stop)
        self._ctx = TestClient(main.app, client=("127.0.0.1", 50000))
        self.client = self._ctx.__enter__()
        self.addCleanup(lambda: self._ctx.__exit__(None, None, None))

    async def _manifest(self, path):
        from unittest.mock import AsyncMock, patch
        with patch.object(
                main.recs_mount, "personal_manifest",
                AsyncMock(side_effect=lambda t, lane="": {"lane": lane})):
            return self.client.get(path).json()

    async def test_each_lane_asks_for_its_own_variant(self):
        for path, lane in (("/vt/manifest.json", ""),
                           ("/vt/slow/manifest.json", "slow")):
            self.assertEqual(lane, (await self._manifest(path))["lane"], path)

    async def test_mobile_is_not_a_per_viewer_lane(self):
        """Dropped on purpose — MAX_BITRATE_MBPS in Settings covers the same
        need without two more installs per person. The shared Auto Stream
        (Mobile) addon is unaffected, so these 404 for a token rather than
        falling back to it."""
        for path in ("/vt/mobile/manifest.json",
                     "/vt/slow/mobile/manifest.json",
                     "/vt/mobile/stream/movie/tt1.json"):
            self.assertEqual(404, self.client.get(path).status_code, path)

    async def test_only_the_primary_lane_carries_catalogs(self):
        """Repeating 300-odd rows on every lane would duplicate the whole home
        screen once per installed add-on."""
        from unittest.mock import AsyncMock, patch
        base = {"id": "org.x.u1", "name": "Daily Picks", "catalogs": [{"id": "c"}],
                "resources": ["catalog"], "types": [], "version": "1"}
        with patch("app.recs.main.manifest", AsyncMock(return_value=base)):
            primary = await main.recs_mount.personal_manifest("vt", "")
            slow = await main.recs_mount.personal_manifest("vt", "slow")

        self.assertEqual(["catalog", "stream"], primary["resources"])
        self.assertEqual([{"id": "c"}], primary["catalogs"])
        self.assertEqual(["stream"], slow["resources"])
        self.assertEqual([], slow["catalogs"])

    async def test_lanes_get_distinct_addon_ids(self):
        """Stremio keys installs by manifest id; a shared id means only the
        first one sticks."""
        from unittest.mock import AsyncMock, patch
        base = {"id": "org.x.u1", "name": "Daily Picks", "catalogs": [],
                "resources": [], "types": [], "version": "1"}
        ids = set()
        with patch("app.recs.main.manifest", AsyncMock(return_value=base)):
            for lane in main.recs_mount.STREAM_LANES:
                ids.add((await main.recs_mount.personal_manifest("vt", lane))["id"])
        self.assertEqual(len(main.recs_mount.STREAM_LANES), len(ids))


class ManifestTypeOrderTests(unittest.IsolatedAsyncioTestCase):
    """The combined type is declared first because it is the addon's primary
    content type. This is presentation-neutral: it was briefly believed to
    control home-row position, and that was disproved on a real client."""

    async def test_the_combined_type_is_declared_first(self):
        from unittest.mock import AsyncMock, patch

        from app.recs import config, main as recs_main
        with (
            patch.object(recs_main.db, "get_user",
                         AsyncMock(return_value={"token": "t"})),
            patch.object(recs_main.db, "get_catalog_defs",
                         AsyncMock(return_value=[])),
        ):
            manifest = await recs_main.manifest("t")

        self.assertEqual(config.COMBINED_TYPE, manifest["types"][0])
        self.assertIn("movie", manifest["types"])
        self.assertIn("series", manifest["types"])
        self.assertEqual(len(set(manifest["types"])), len(manifest["types"]))


class PlayAttributionTests(unittest.TestCase):
    """A buffered proxy entry is keyed by cache id and shared between viewers,
    so the viewer recorded on it is whoever opened the file first. Watch
    history must be attributed from the per-viewer session token instead, or
    the second person to play a cached file is credited to the first."""

    def test_buffered_playback_attributes_to_the_session_not_the_buffer(self):
        import inspect

        from app import proxy

        source = inspect.getsource(proxy)
        at = source.index('telemetry.record_play({"picker": e.picker')
        # Window the lines just before the call too: that is where the session
        # lookup lives.
        call = source[max(0, at - 400):at + 400]
        self.assertIn("_lookup(token)", call,
                      "buffered play must resolve the viewer from the session")
        self.assertIn('"viewer_key"', call)
        self.assertNotIn('e.viewer', call,
                         "must not read the viewer off the shared buffer entry")
