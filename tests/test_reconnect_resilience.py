"""Reconnect budgets: how much failure a stream absorbs before it gives up.

Field-review case (2026-10-15). Three paths shared one weakness:

  * the buffered producer failed the whole entry on the first reconnect that
    did not answer — reproduced: a single connect blip ended a buffer at 50 %
    while the next request would have been served — and the rest of the film
    then degraded to a cold upstream fetch per range;
  * the direct paths counted reconnects per *stream*, never reset, so the
    fourth blip anywhere in a two-hour film ended it;
  * every retry fired back to back, so one hiccup could spend the budget.
"""

import asyncio
import os
import re
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app import proxy

TOTAL = 256 * 1024
HALF = TOTAL // 2
DATA = bytes(range(256)) * (TOTAL // 256)
DATA = b"\x1aE\xdf\xa3" + DATA[4:]


class _Resp:
    """A 206 from `start` that streams DATA, dropping after `fail_after` bytes."""

    def __init__(self, start: int, *, fail_after: int | None = None,
                 end: int | None = None, chunk: int = 4096):
        self.status_code = 206
        last = TOTAL - 1 if end is None else end
        self.headers = {"content-range": f"bytes {start}-{last}/{TOTAL}"}
        self._start, self._end = start, last + 1
        self._fail, self._chunk = fail_after, chunk
        self.closed = False

    async def aiter_raw(self):
        sent = 0
        for i in range(self._start, self._end, self._chunk):
            if self._fail is not None and sent >= self._fail:
                raise httpx.ReadError("connection reset")
            piece = DATA[i:min(i + self._chunk, self._end)]
            sent += len(piece)
            yield piece

    async def aclose(self):
        self.closed = True


def _cand(**kw):
    return dict({"u": "https://cdn.example/f.mkv", "sig": "file:" + "a" * 64,
                 "lbl": "1080p X", "dbr": "TB+", "need": 0, "rh": {},
                 "cid": "url:x"}, **kw)


class _Upstream:
    """A fake `_send` that honours the requested Range. Each call takes the
    next step of `plan`: "refuse" raises ConnectError, an int is a response
    that drops after that many bytes, None is a response that runs to the end."""

    def __init__(self, plan):
        self.plan, self.ranges = list(plan), []

    async def __call__(self, _cand, range_header):
        self.ranges.append(range_header)
        step = self.plan.pop(0) if self.plan else None
        if step == "refuse":
            raise httpx.ConnectError("refused")
        m = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header or "bytes=0-")
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else None
        return _Resp(start, fail_after=step, end=end)


def _quick_backoff():
    """Keep the spacing in the code path without making the suite wait."""
    return patch("app.proxy._reconnect_delay", MagicMock(return_value=0.0))


async def _drain(agen, limit: float = 10.0) -> bytes:
    async def collect():
        return b"".join([c async for c in agen])
    return await asyncio.wait_for(collect(), limit)


class _EntryCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        for p in (patch("app.proxy.reputation", MagicMock()),
                  patch("app.proxy.telemetry.record_buffer"),
                  patch("app.proxy.telemetry.record_play"),
                  patch("app.proxy.usenet_health")):
            p.start()
            self.addCleanup(p.stop)

    def entry(self, cand=None):
        cand = cand or _cand()
        e = proxy._Entry("url:x", os.path.join(self.dir.name, "f.bin"), [cand],
                         cand, "video/x-matroska", TOTAL, "fast", "tt1")
        e.consumers = 1
        proxy._entries[e.sig] = e
        self.addCleanup(proxy._entries.pop, e.sig, None)
        return e


class ProducerResumeTests(_EntryCase):
    async def test_one_refused_reconnect_no_longer_fails_the_buffer(self):
        first = _Resp(0, fail_after=HALF)
        up = _Upstream(["refuse", None])
        e = self.entry()
        with patch("app.proxy._send", new=up):
            await proxy._produce(e, "tok", first, first.aiter_raw(), [])

        self.assertTrue(e.complete)
        self.assertFalse(e.failed)
        self.assertEqual(2, len(up.ranges), "refused once, then resumed")
        with open(e.path, "rb") as f:
            self.assertEqual(DATA, f.read())

    async def test_a_source_that_never_returns_fails_within_the_budget(self):
        first = _Resp(0, fail_after=HALF)
        send = AsyncMock(side_effect=httpx.ConnectError("gone"))
        e = self.entry()
        t0 = time.monotonic()
        with patch("app.proxy._send", send), \
             patch("app.proxy.BUFFER_RESUME_BUDGET", 1.0):
            await proxy._produce(e, "tok", first, first.aiter_raw(), [])

        self.assertTrue(e.failed)
        self.assertGreater(send.await_count, 1, "budget means more than one try")
        self.assertLess(time.monotonic() - t0, 5.0)


class ReviveTests(_EntryCase):
    def failed_entry(self):
        e = self.entry()
        with open(e.path, "wb") as f:
            f.write(DATA[:HALF])
        e.avail, e.failed = HALF, True
        return e

    async def test_a_failed_entry_resumes_at_its_write_head(self):
        e = self.failed_entry()
        send = AsyncMock(return_value=_Resp(HALF))
        with patch("app.proxy._send", send):
            self.assertTrue(await proxy._revive(e, "tok"))
            await asyncio.wait_for(e.producer, 5)

        self.assertEqual(f"bytes={HALF}-", send.await_args.args[1])
        self.assertTrue(e.complete)
        with open(e.path, "rb") as f:
            self.assertEqual(DATA, f.read(), "resume must append, not truncate")

    async def test_a_reader_at_the_edge_of_a_failed_entry_is_not_cut_off(self):
        """The player's own response continues across the revive."""
        e = self.failed_entry()
        e.consumers = 0
        with patch("app.proxy._send", AsyncMock(return_value=_Resp(HALF))):
            body = await _drain(proxy._consume(e, 0, None, "tok"))
        self.assertEqual(DATA, body)

    async def test_an_entry_retired_meanwhile_is_left_alone(self):
        e = self.failed_entry()

        async def send(*_a, **_k):
            proxy._entries.pop(e.sig, None)        # an opening request won
            return _Resp(HALF)

        with patch("app.proxy._send", side_effect=send):
            self.assertFalse(await proxy._revive(e, "tok"))
        self.assertIsNone(e.producer)
        self.assertTrue(e.failed)


class DirectBudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for p in (patch("app.proxy.reputation", MagicMock()),
                  patch("app.proxy.telemetry.record_buffer"),
                  patch("app.proxy.telemetry.record_play"),
                  patch("app.proxy.usenet_health"),
                  patch("app.proxy._RECONNECT_RESET_BYTES", 16 * 1024)):
            p.start()
            self.addCleanup(p.stop)

    async def test_four_spread_out_blips_do_not_end_a_direct_stream(self):
        """RECONNECT_MAX bounds consecutive failures, not failures per film."""
        step = TOTAL // 5
        first = _Resp(0, fail_after=step)
        up = _Upstream([step, step, step, None])
        with patch("app.proxy._send", new=up), _quick_backoff():
            body = await _drain(proxy._body(
                {"total": TOTAL}, 0, _cand(), first, first.aiter_raw(), [], 0,
                None, 0.1, "tok", {}))
        self.assertEqual(DATA, body)
        self.assertEqual(4, len(up.ranges))

    async def test_a_refused_reconnect_is_retried_not_fatal(self):
        first = _Resp(0, fail_after=HALF)
        up = _Upstream(["refuse", None])
        with patch("app.proxy._send", new=up), \
             _quick_backoff() as delay:
            body = await _drain(proxy._body(
                {"total": TOTAL}, 0, _cand(), first, first.aiter_raw(), [], 0,
                None, 0.1, "tok", {}))
        self.assertEqual(DATA, body)
        self.assertEqual([1, 2], [c.args[0] for c in delay.call_args_list],
                         "each attempt is spaced by its own backoff step")

    async def test_seek_path_retries_the_reconnect_not_the_closed_response(self):
        request = MagicMock()
        request.headers = {"range": f"bytes={HALF // 2}-"}
        start = HALF // 2
        up = _Upstream([HALF // 2, "refuse", None])
        with patch("app.proxy._send", new=up), _quick_backoff():
            resp = await proxy._serve_direct(
                {"picker": "fast", "id": "tt1"}, [_cand()], request, "tok",
                source=_cand(), expected_total=TOTAL)
            body = await _drain(resp.body_iterator)
        self.assertEqual(DATA[start:], body)
        self.assertEqual(3, len(up.ranges))
        self.assertEqual(up.ranges[1], up.ranges[2],
                         "the refused reconnect is retried at the same byte")


class NearHeadTests(_EntryCase):
    async def test_a_reader_reopening_at_the_write_head_waits_for_the_fill(self):
        """Not a second upstream connection beside the producer."""
        e = self.entry()
        e.consumers = 0
        with open(e.path, "wb") as f:
            f.write(DATA[:HALF])
        e.avail = HALF
        gate = asyncio.Event()

        async def produce():
            await gate.wait()
            with open(e.path, "r+b") as f:
                f.seek(HALF)
                f.write(DATA[HALF:])
            async with e.cond:
                e.avail, e.complete = TOTAL, True
                e.cond.notify_all()

        e.producer = asyncio.create_task(produce())
        request = MagicMock()
        request.headers = {"range": f"bytes={HALF}-"}
        session = {"bufcid": e.sig, "picker": "fast", "id": "tt1"}
        with patch("app.proxy._serve_direct", AsyncMock()) as direct:
            resp = await proxy._serve_buffered(
                "tok", session, [_cand()], HALF, None, True, request)
            gate.set()
            body = await _drain(resp.body_iterator)
        direct.assert_not_awaited()
        self.assertEqual(DATA[HALF:], body)


if __name__ == "__main__":
    unittest.main()
