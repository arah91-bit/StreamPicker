"""Mid-stream stalls: what the proxy does when a source goes quiet, and the
record it leaves of every wait it imposed on a player.

A reported failure mode: "stalls mid-episode" with nothing in
any log. Two gaps made that possible. A stalled HTTP source was waited out on
the client's 60 s read timeout — longer than any player's buffer — before a
reconnect was even tried; and no code path recorded the player waiting on us,
so a stall the server caused was indistinguishable from one the network or
the player caused.
"""

import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import proxy


class _Resp:
    """A 206 that streams `body` in small chunks, optionally going silent for
    `stall_after` bytes' worth and then hanging."""

    def __init__(self, body: bytes, start: int, total: int, *,
                 stall_after: int | None = None, chunk: int = 64,
                 delay: float = 0.0):
        self.status_code = 206
        self.headers = {"content-range": f"bytes {start}-{total - 1}/{total}"}
        self._body, self._chunk = body, chunk
        self._stall_after, self._delay = stall_after, delay
        self.closed = False

    async def aiter_raw(self):
        for i in range(0, len(self._body), self._chunk):
            if self._stall_after is not None and i >= self._stall_after:
                await asyncio.sleep(3600)          # a CDN that stops talking
            if self._delay:
                await asyncio.sleep(self._delay)
            yield self._body[i:i + self._chunk]

    async def aclose(self):
        self.closed = True


async def _drain(agen, limit: float = 5.0) -> bytes:
    """Collect a body, failing (not hanging) if a stall is never recovered."""
    async def collect():
        return b"".join([c async for c in agen])
    return await asyncio.wait_for(collect(), limit)


def _cand(sig="file:" + "a" * 64, **kw):
    return dict({"u": "https://cdn.example/f.mkv", "sig": sig, "lbl": "1080p X",
                 "dbr": "TB+", "need": 0, "rh": {}}, **kw)


class StallLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_silent_http_source_is_declared_stalled(self):
        async def silent():
            await asyncio.sleep(3600)
            yield b"never"

        with patch.object(proxy, "STREAM_STALL_SECS", 0.05):
            with self.assertRaises(proxy._SourceStalled):
                await proxy._next_chunk(silent(), _cand())

    async def test_usenet_is_left_to_its_own_pauses(self):
        """nzbdav pauses legitimately, and reconnecting there re-runs a costly
        seek, so a usenet source is never cut off by the stall limit."""
        self.assertIsNone(proxy._stall_limit(_cand(sig="nzb:" + "b" * 64)))

        async def slow():
            await asyncio.sleep(0.15)
            yield b"late but fine"

        with patch.object(proxy, "STREAM_STALL_SECS", 0.05):
            got = await proxy._next_chunk(slow(), _cand(sig="nzb:" + "b" * 64))
        self.assertEqual(b"late but fine", got)

    def test_zero_disables(self):
        with patch.object(proxy, "STREAM_STALL_SECS", 0):
            self.assertIsNone(proxy._stall_limit(_cand()))


class DirectStallTests(unittest.IsolatedAsyncioTestCase):
    """Pass-through playback (PROXY_BUFFER=0, or a read past the write head)."""

    TOTAL = 4096

    def setUp(self):
        self.data = bytes((i * 13 + 5) % 251 for i in range(self.TOTAL))
        for name, val in (("STREAM_STALL_SECS", 0.1), ("_STARVE_LOG_SECS", 0.05)):
            p = patch.object(proxy, name, val)
            p.start()
            self.addCleanup(p.stop)

    async def test_a_stalled_source_is_reconnected_and_the_wait_recorded(self):
        first = _Resp(self.data, 0, self.TOTAL, stall_after=1024)
        resumed = _Resp(self.data[1024:], 1024, self.TOTAL)
        plays, events = [], []
        entry = {"total": self.TOTAL, "picker": "fast", "id": "tt1"}

        with (patch("app.proxy._send", AsyncMock(return_value=resumed)) as send,
              patch("app.proxy.telemetry.record_play",
                    side_effect=lambda *a, **k: plays.append(k)),
              patch("app.proxy.telemetry.record_buffer",
                    side_effect=lambda ev, **k: events.append((ev, k))),
              patch("app.proxy.reputation", MagicMock())):
            body = await _drain(proxy._body(
                entry, 0, _cand(), first, first.aiter_raw(), [], 0, None, 0.1,
                "tok", {}))

        self.assertEqual(self.data, body, "the player must get every byte, once")
        self.assertEqual("bytes=1024-", send.await_args.args[1])
        self.assertTrue(first.closed)
        self.assertEqual(1, plays[-1]["reconnects"])
        self.assertEqual(1, plays[-1]["waits"]["waits"])
        starve = [k for ev, k in events if ev == "starve"]
        self.assertEqual(1, len(starve))
        self.assertEqual(1024, starve[0]["offset"])

    async def test_a_seek_past_the_buffer_reconnects_too(self):
        """_serve_direct.gen is what serves a read past the write head; it
        must not fall back to the client's 60 s read timeout either."""
        first = _Resp(self.data[512:], 512, self.TOTAL, stall_after=512)
        resumed = _Resp(self.data[1024:], 1024, self.TOTAL)
        request = MagicMock()
        request.headers = {"range": "bytes=512-"}
        events = []

        with (patch("app.proxy._send",
                    AsyncMock(side_effect=[first, resumed])) as send,
              patch("app.proxy.telemetry.record_buffer",
                    side_effect=lambda ev, **k: events.append((ev, k))),
              patch("app.proxy.reputation", MagicMock())):
            resp = await proxy._serve_direct(
                {"picker": "fast", "id": "tt1"}, [_cand()], request, "tok",
                source=_cand(), expected_total=self.TOTAL)
            body = await _drain(resp.body_iterator)

        self.assertEqual(self.data[512:], body)
        self.assertEqual("bytes=1024-4095", send.await_args.args[1])
        self.assertIn("starve", [ev for ev, _ in events])

    async def test_a_steady_source_records_no_waits(self):
        first = _Resp(self.data, 0, self.TOTAL)
        plays, events = [], []
        with (patch("app.proxy._send", AsyncMock()) as send,
              patch("app.proxy.telemetry.record_play",
                    side_effect=lambda *a, **k: plays.append(k)),
              patch("app.proxy.telemetry.record_buffer",
                    side_effect=lambda ev, **k: events.append(ev)),
              patch("app.proxy.reputation", MagicMock())):
            body = await _drain(proxy._body(
                {"total": self.TOTAL}, 0, _cand(), first, first.aiter_raw(),
                [], 0, None, 0.1, "tok", {}))
        self.assertEqual(self.data, body)
        send.assert_not_awaited()
        self.assertEqual(0, plays[-1]["waits"]["waits"])
        self.assertNotIn("starve", events)


class BufferedWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_reader_at_the_write_head_logs_its_wait(self):
        """With the read-ahead buffer on, the player only ever waits when it
        has caught the producer — which is exactly the case worth a record."""
        with tempfile.TemporaryDirectory() as td, \
                patch.object(proxy, "_STARVE_LOG_SECS", 0.05):
            path = os.path.join(td, "buf.bin")
            with open(path, "wb") as f:
                f.write(b"a" * 100)
            e = proxy._Entry("url:w", path, [], _cand(), "video/x-matroska",
                             200, "fast", "tt1")
            e.avail = 100
            plays, events = [], []

            async def producer():
                await asyncio.sleep(0.2)
                with open(path, "ab") as f:
                    f.write(b"b" * 100)
                async with e.cond:
                    e.avail = 200
                    e.complete = True
                    e.cond.notify_all()

            with (patch("app.proxy.telemetry.record_play",
                        side_effect=lambda *a, **k: plays.append(k)),
                  patch("app.proxy.telemetry.record_buffer",
                        side_effect=lambda ev, **k: events.append((ev, k))),
                  patch("app.proxy._note_consumer_close"),
                  patch("app.proxy._arm_reject_timer")):
                task = asyncio.create_task(producer())
                body = await _drain(proxy._consume(e, 0, None, "tok"))
                await task

        self.assertEqual(b"a" * 100 + b"b" * 100, body)
        starve = [k for ev, k in events if ev == "starve"]
        self.assertEqual(1, len(starve))
        self.assertEqual(100, starve[0]["offset"])
        self.assertIn("buffered", starve[0]["reason"])
        self.assertEqual(1, plays[-1]["waits"]["waits"])


class UnknownSizeStartGateTests(unittest.IsolatedAsyncioTestCase):
    """A candidate no addon gave a size for had need=0, so the start gate's
    rate check was skipped — how a 1.1 MB/s copy of a ~5 MB/s REMUX started
    during playback. The upstream's Content-Range supplies the size at play time."""

    def _resp(self, total, delay):
        body = b"\x1aE\xdf\xa3" + b"\0" * (3 * 1024 * 1024)
        return _Resp(body, 0, total, chunk=256 * 1024, delay=delay)

    async def _select(self, cand, resp):
        rep = MagicMock()
        rep.blocked.return_value = False
        rep.cooled.return_value = False
        with (patch("app.proxy._send", AsyncMock(return_value=resp)),
              patch("app.proxy.reputation", rep),
              patch("app.proxy.telemetry.record_buffer"),
              patch("app.proxy.usenet_health"),
              patch.object(proxy, "NZB_START_RETRY_SECS", 0)):
            return await proxy._select_start([cand], "bytes=0-", 0, None, "tok")

    async def test_a_size_less_source_too_slow_for_its_file_is_refused(self):
        cand = _cand(need=0, rt=100.0)             # 1 GB / 100 s = 10 MB/s
        got = await self._select(cand, self._resp(1_000_000_000, delay=0.05))
        self.assertIsNone(got, "≈5 MB/s cannot carry a 10 MB/s file")
        self.assertAlmostEqual(10_000_000, cand["need"], delta=1)

    async def test_a_size_less_source_fast_enough_still_starts(self):
        cand = _cand(need=0, rt=10_000.0)          # 0.1 MB/s file
        got = await self._select(cand, self._resp(1_000_000_000, delay=0.05))
        self.assertIsNotNone(got)


class SlowSwitchBackoffTests(unittest.IsolatedAsyncioTestCase):
    """A REMUX feeding 1.1 MB/s against a ~5 MB/s need. Every slow
    window the producer 'switched' — but there was nothing to switch to, so
    it re-opened the same copy every 10 s for minutes, paying the CDN's
    first-byte cost each time."""

    async def test_nothing_better_backs_off_and_cools_the_copy(self):
        source = _cand(need=10_000_000)
        twin = _cand(sig=source["sig"], u="https://other.example/f.mkv", dbr="RD+")
        total = 1 << 30
        slow = _Resp(b"x" * 200_000, 0, total, chunk=1024, delay=0.005)
        rep = MagicMock()

        async def resume(e, tried, prefer_new=False):
            again = _Resp(b"x" * 200_000, e.avail, total, chunk=1024, delay=0.005)
            return source, again, again.aiter_raw()

        with tempfile.TemporaryDirectory() as td, \
                patch.object(proxy, "BUFFER_SLOW_WINDOW", 0.05), \
                patch.object(proxy, "BUFFER_TAIL_BYTES", 0), \
                patch.object(proxy, "STREAM_STALL_SECS", 0), \
                patch.object(proxy, "_connect_resume",
                             new=AsyncMock(side_effect=resume)) as cr, \
                patch("app.proxy.telemetry.record_buffer"), \
                patch("app.proxy.reputation", rep):
            e = proxy._Entry("url:slow", os.path.join(td, "s.bin"),
                             [source, twin], source, "video/x-matroska",
                             total, "fast", "tt1")
            e.consumers = 1                      # a reader sits at the write head
            task = asyncio.create_task(
                proxy._produce(e, "tok", slow, slow.aiter_raw(), []))
            await asyncio.sleep(0.8)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(1, cr.await_count,
                         "with nothing better to go to, one try is enough")
        self.assertEqual("mid-stream-slow", rep.observe.call_args.args[2])
        rep.cooldown.assert_called_once_with(source["sig"])


if __name__ == "__main__":
    unittest.main()


class SideReadBoundTests(unittest.IsolatedAsyncioTestCase):
    """Reads that are not the player's own byte stream are bounded too.

    Field review (2026-10-15): the splice verification window, the tail warm
    and the HEAD probe all read with no timeout of their own, so a silent
    source held each of them for the client's 60 s read timeout — a resume a
    player was waiting on, one of Easynews' two connections, a player's open.
    """

    TOTAL = 4096

    def _entry(self, td):
        path = os.path.join(td, "buf.bin")
        with open(path, "wb") as f:
            f.write(bytes(self.TOTAL))
        e = proxy._Entry("url:x", path, [_cand()], _cand(), "video/x-matroska",
                         self.TOTAL, "fast", "tt1")
        e.avail = self.TOTAL // 2
        return e

    async def test_a_silent_splice_candidate_is_refused_in_time(self):
        silent = _Resp(bytes(self.TOTAL), 0, self.TOTAL, stall_after=0)
        silent.headers = {"content-range":
                          f"bytes 0-{self.TOTAL - 1}/{self.TOTAL}"}
        with tempfile.TemporaryDirectory() as td, \
                patch.object(proxy, "_SIDE_READ_SECS", 0.2), \
                patch.object(proxy, "SPLICE_VERIFY_BYTES", self.TOTAL // 2), \
                patch("app.proxy._send", AsyncMock(return_value=silent)):
            e = self._entry(td)
            got = await asyncio.wait_for(
                proxy._open_verified(e, _cand(u="https://other/f.mkv"),
                                     e.avail), 3)
        self.assertIsNone(got)
        self.assertTrue(silent.closed)

    async def test_a_silent_tail_warm_gives_up_and_says_so(self):
        silent = _Resp(bytes(1024), self.TOTAL - 1024, self.TOTAL,
                       stall_after=0)
        with tempfile.TemporaryDirectory() as td, \
                patch.object(proxy, "_SIDE_READ_SECS", 0.2), \
                patch.object(proxy, "BUFFER_TAIL_BYTES", 1024), \
                patch("app.proxy._send", AsyncMock(return_value=silent)), \
                self.assertLogs("stream-picker", "INFO") as logs:
            e = self._entry(td)
            await asyncio.wait_for(proxy._fetch_tail(e), 3)
        self.assertIsNone(e.tail)
        self.assertTrue(any("went silent" in m for m in logs.output))

    async def test_head_does_not_wait_out_a_silent_candidate(self):
        async def hang(*_a, **_k):
            await asyncio.sleep(3600)

        with patch.object(proxy, "START_TTFB", 0.2), \
                patch("app.proxy._send", side_effect=hang), \
                patch("app.proxy.reputation", MagicMock(**{
                    "blocked.return_value": False,
                    "cooled.return_value": False})):
            resp = await asyncio.wait_for(
                proxy._head({"cands": [_cand()]}), 3)
        self.assertEqual(502, resp.status_code)

    def test_library_playback_has_a_read_timeout(self):
        from app import library
        self.assertIsNotNone(library._client.timeout.read)
