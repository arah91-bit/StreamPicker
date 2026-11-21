"""Private-tracker playback starts while the torrent downloads.

Field case (2026-10): every private play started only once the file had
finished — Phineas and Ferb Star Wars played 1 s after its 1.2 GB completed,
61 s after the click. qBittorrent was downloading sequentially all along; it
just names an incomplete file `X.mkv.!qB` ("Append .!qB extension to incomplete
files"), and the lane only ever looked for `X.mkv`.
"""

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx
from starlette.requests import Request

os.environ.setdefault("ADDON_SECRET", "test-secret")

from app import private_trackers as pt


def _request(range_header=""):
    headers = [(b"range", range_header.encode())] if range_header else []
    return Request({"type": "http", "method": "GET", "path": "/private/x",
                    "headers": headers, "query_string": b"",
                    "server": ("test", 80), "client": ("127.0.0.1", 1),
                    "scheme": "http"})


def _json(data):
    return httpx.Response(200, json=data,
                          request=httpx.Request("GET", "http://qbit"))


class IncompleteNameTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        p = mock.patch.object(pt, "DOWNLOAD_ROOT", self.root)
        p.start()
        self.addCleanup(p.stop)

    def test_a_downloading_file_is_found_under_its_incomplete_name(self):
        (self.root / "Movie.2014.mkv.!qB").write_bytes(b"x" * 10)
        path = pt._safe_local_path("Movie.2014.mkv", "Movie.2014.mkv")
        self.assertEqual("Movie.2014.mkv", path.name)
        self.assertEqual("Movie.2014.mkv.!qB", pt._live_path(path).name)

    def test_the_final_name_wins_once_complete(self):
        (self.root / "Movie.2014.mkv").write_bytes(b"x")
        path = pt._safe_local_path("Movie.2014.mkv", "Movie.2014.mkv")
        self.assertEqual(path, pt._live_path(path))

    def test_pack_members_resolve_inside_the_release_folder(self):
        pack = self.root / "Show.S01"
        pack.mkdir()
        (pack / "Show.S01E02.mkv.!qB").write_bytes(b"x")
        path = pt._safe_local_path("Show.S01", "Show.S01/Show.S01E02.mkv")
        self.assertIsNotNone(pt._live_path(path))

    def test_traversal_is_still_refused(self):
        with self.assertRaises(RuntimeError):
            pt._safe_local_path("r", "../../etc/passwd")


class RenameMidStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_reader_follows_the_rename_when_the_file_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            final = Path(tmp) / "Movie.mkv"
            partial = Path(tmp) / "Movie.mkv.!qB"
            data = bytes(range(256)) * (32 * 1024)        # 8 MiB, two chunks
            partial.write_bytes(data)
            got = []

            async def read_all():
                async for chunk in pt._body(final, "h", 0, 1 << 20, 0,
                                            len(data) - 1, complete=False):
                    got.append(chunk)
                    if len(got) == 1:
                        partial.rename(final)            # qBittorrent finishes

            with mock.patch.object(pt, "_wait_available", new=mock.AsyncMock()):
                await asyncio.wait_for(read_all(), 5)
        self.assertEqual(data, b"".join(got))


class HeadroomTests(unittest.TestCase):
    SIZE = 1_800_000_000          # 1.8 GB
    RUNTIME = 1_800.0             # 30 min → 1 MB/s

    def test_a_fast_swarm_needs_only_the_floor(self):
        need = pt._headroom_bytes(self.SIZE, self.RUNTIME, rate=20e6)
        self.assertEqual(int(1e6 * pt.START_MIN_SECS), need)

    def test_a_slow_swarm_needs_the_shortfall_up_front(self):
        # 0.5 MB/s against a 1 MB/s file: discounted to 0.4, so 60 % of the
        # file must be there before playback can never catch the download.
        need = pt._headroom_bytes(self.SIZE, self.RUNTIME, rate=0.5e6)
        self.assertAlmostEqual(0.6 * self.SIZE, need, delta=self.SIZE * 0.01)

    def test_unknown_runtime_holds_nothing(self):
        self.assertEqual(0, pt._headroom_bytes(self.SIZE, 0, rate=0))


class HoldTests(unittest.IsolatedAsyncioTestCase):
    SIZE = 100 * (1 << 20)
    PIECE = 1 << 20

    def _patch(self, *, have_pieces: int, rate: float, runtime=100.0):
        states = [2] * have_pieces + [0] * (100 - have_pieces)
        return (mock.patch.object(pt, "_qapi",
                                  new=mock.AsyncMock(return_value=_json(states))),
                mock.patch.object(pt, "_torrent_info",
                                  new=mock.AsyncMock(return_value={"dlspeed": rate})),
                mock.patch.object(pt, "_title_runtime",
                                  new=mock.AsyncMock(return_value=runtime)),
                mock.patch.object(pt.telemetry, "record_private_tracker"))

    async def _hold(self, payload, **kw):
        patches = self._patch(**kw)
        for p in patches:
            p.start()
        try:
            await pt._hold_for_headroom(payload, "h", 0, self.SIZE, self.PIECE)
        finally:
            for p in reversed(patches):
                p.stop()

    async def test_enough_head_start_returns_at_once(self):
        payload = {"media": "movie", "media_id": "tt1"}
        await self._hold(payload, have_pieces=30, rate=10e6)
        self.assertNotIn("_hold_since", payload)

    async def test_not_enough_answers_retry_within_the_player_timeout(self):
        payload = {"media": "movie", "media_id": "tt1"}
        with mock.patch.object(pt, "START_HOLD_SECS", 0.2):
            t0 = time.monotonic()
            with self.assertRaises(pt.StillBuffering):
                await self._hold(payload, have_pieces=2, rate=0.1e6)
        self.assertLess(time.monotonic() - t0, 3)
        self.assertIn("_hold_since", payload, "a retry must continue the hold")

    async def test_the_total_hold_is_bounded_across_retries(self):
        payload = {"media": "movie", "media_id": "tt1",
                   "_hold_since": time.time() - 200}
        with mock.patch.object(pt, "START_HOLD_TOTAL_SECS", 120):
            await self._hold(payload, have_pieces=2, rate=0.1e6)   # starts anyway
        self.assertNotIn("_hold_since", payload)


class ServeWhileDownloadingTests(unittest.IsolatedAsyncioTestCase):
    """The end-to-end shape of the bug: an incomplete file, a player asking
    for byte 0, and pieces already there."""

    async def test_opening_request_streams_from_the_incomplete_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = os.urandom(3 * (1 << 20))
            (root / "Movie.mkv.!qB").write_bytes(data)
            info = {"name": "Movie.mkv", "hash": "h", "dlspeed": 50e6}
            pick = {"index": 0, "name": "Movie.mkv", "size": len(data),
                    "progress": 0.4}
            pt._tokens["tok"] = (time.time(), {
                "mode": "existing", "hash": "h", "file_index": 0,
                "media": "movie", "media_id": "tt1"})

            async def qapi(method, path, **kw):
                if path == "/torrents/properties":
                    return _json({"piece_size": 1 << 20})
                return _json([2, 2, 2])

            with (mock.patch.object(pt, "DOWNLOAD_ROOT", root),
                  mock.patch.object(pt, "_prepare_existing",
                                    new=mock.AsyncMock(
                                        return_value=(info, [pick], pick))),
                  mock.patch.object(pt, "_qapi", side_effect=qapi),
                  mock.patch.object(pt, "_torrent_info",
                                    new=mock.AsyncMock(return_value=info)),
                  mock.patch.object(pt, "_title_runtime",
                                    new=mock.AsyncMock(return_value=60.0)),
                  mock.patch.object(pt.telemetry, "record_private_tracker")):
                t0 = time.monotonic()
                resp = await asyncio.wait_for(
                    pt.serve("tok", _request("bytes=0-")), 5)
                body = b"".join([c async for c in resp.body_iterator])
            self.assertLess(time.monotonic() - t0, 5)
            self.assertEqual(206, resp.status_code)
            self.assertEqual(data, body)
        pt._tokens.pop("tok", None)


if __name__ == "__main__":
    unittest.main()
