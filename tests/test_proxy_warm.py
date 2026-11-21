"""Warming the top pick before Play (POST /proxy/<token>/warm).

The NuvioTV fork's details page used to "warm the index" with a HEAD — which
only probes upstream headers, so no producer and no tail warm ever started —
and the obvious repair, a 1-byte GET, is worse: a short open at byte 0
followed by silence is exactly what the player-rejection detector scores as
"the player can't open this file" (CLAUDE.md failure mode 6), so it would cool
the top pick and could strike its codecs.

A warm therefore has its own request and its own contract: the normal start
gate chooses, the producer warms the tail and reads a little ahead, and
nothing about it counts as playback or as a verdict on any release.
"""

import asyncio
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import httpx
from fastapi.testclient import TestClient

from app import main, proxy

KB = 1024
TOTAL = 2 * 1024 * KB
TAIL = 64 * KB
AHEAD = 512 * KB
CHUNK = 16 * KB


def _data(total=TOTAL) -> bytes:
    body = bytearray(bytes(range(256)) * (total // 256))
    body[:4] = b"\x1aE\xdf\xa3"                  # Matroska, so the gate sniffs media
    return bytes(body)


def _cand(name="a", host="cdn.example", sig=None):
    return {"u": f"https://{host}/{name}.mkv", "sig": sig or "file:" + name * 64,
            "lbl": f"1080p {name}", "dbr": "TB+", "need": 0, "rh": {},
            "auto": True}


class _Resp:
    def __init__(self, up, start, end):
        self.up, self.start, self.end = up, start, end
        self.status_code = 206
        self.headers = {"content-range": f"bytes {start}-{end}/{len(up.data)}",
                        "content-type": "video/x-matroska"}
        self.closed = False

    async def aiter_raw(self):
        for i in range(self.start, self.end + 1, CHUNK):
            if (self.start == 0 and self.up.drop_at is not None
                    and i >= self.up.drop_at):
                self.up.drop_at = None           # one drop, then a clean resume
                raise httpx.ReadError("connection reset")
            await asyncio.sleep(self.up.delay)
            yield self.up.data[i:min(i + CHUNK, self.end + 1)]

    async def aclose(self):
        self.closed = True


class _Upstream:
    """One file served over honest range requests, recording every request."""

    def __init__(self, data=None, *, delay=0.0, drop_at=None):
        self.data = data or _data()
        self.delay, self.drop_at = delay, drop_at
        self.sent: list[str] = []
        self.resps: list[_Resp] = []
        self.gate: asyncio.Event | None = None   # hold opening requests here

    async def send(self, cand, rh):
        self.sent.append(rh)
        if self.gate is not None and rh == "bytes=0-":
            await self.gate.wait()
        lo, _, hi = rh[len("bytes="):].partition("-")
        resp = _Resp(self, int(lo), int(hi) if hi else len(self.data) - 1)
        self.resps.append(resp)
        return resp

    def all_closed(self) -> bool:
        return bool(self.resps) and all(r.closed for r in self.resps)


class _Dead(_Upstream):
    """Connects, replies 206, and sends nothing — the corpse a real open would
    strike as `dead` on the first offence."""

    async def send(self, cand, rh):
        self.sent.append(rh)
        resp = _Resp(self, 0, len(self.data) - 1)
        resp.aiter_raw = self._nothing
        return resp

    @staticmethod
    async def _nothing():
        return
        yield


async def _until(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


class _WarmCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        rep = MagicMock()
        rep.blocked.return_value = False
        rep.cooled.return_value = False
        self.rep, self.health = rep, MagicMock()
        self.record_buffer, self.record_play = MagicMock(), MagicMock()
        for p in (patch.object(proxy, "_sessions", {}),
                  patch.object(proxy, "_entries", {}),
                  patch.object(proxy, "_start_locks", {}),
                  patch.object(proxy, "_warming", set()),
                  patch.object(proxy, "_warmed_at", {}),
                  patch.object(proxy, "_persist"),
                  patch.object(proxy, "_free_bytes", return_value=None),
                  patch.object(proxy, "BUFFER_DIR", self.td.name),
                  patch.object(proxy, "PROXY_BUFFER", True),
                  patch.object(proxy, "WARM", True),
                  patch.object(proxy, "BUFFER_TAIL_BYTES", TAIL),
                  patch.object(proxy, "EVAL_BYTES", 64 * KB),
                  patch.object(proxy, "WARM_AHEAD_BYTES", AHEAD),
                  patch.object(proxy, "TAIL_WARM_HEADSTART", 1.0),
                  patch.object(proxy, "TAIL_WARM_HEADSTART_RATIONED", 1.0),
                  patch.object(proxy, "NZB_START_RETRY_SECS", 0),
                  patch.object(proxy, "START_TTFB", 0.3),
                  patch("app.proxy.reputation", rep),
                  patch("app.proxy.usenet_health", self.health),
                  patch("app.proxy.telemetry.record_buffer", self.record_buffer),
                  patch("app.proxy.telemetry.record_play", self.record_play)):
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        tasks = [e.producer for e in proxy._entries.values() if e.producer]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def session(self, *cands, token="tok", hls=False):
        cands = list(cands) or [_cand()]
        s = {"cands": cands, "pool": cands, "id": "tt1:1:1", "picker": "fast",
             "pin": None, "total": None, "auto": True, "viewer": "",
             "viewer_key": ""}
        if hls:
            s["hls"] = True
        proxy._sessions[token] = (time.time(), s)
        return s

    def upstream(self, up):
        p = patch("app.proxy._send", up.send)
        p.start()
        self.addCleanup(p.stop)
        return up

    def entry(self, session):
        return proxy._entries.get(session.get("bufcid") or "")

    async def warmed(self, session, token="tok"):
        """Warm and wait until it has let go of its source."""
        self.assertEqual((202, {"state": "warming"}), proxy.warm(token))
        await _until(lambda: (self.entry(session) is not None
                              and self.entry(session).tail is not None
                              and self.upstream_.all_closed()))
        return self.entry(session)

    def assert_no_verdicts(self):
        self.rep.observe.assert_not_called()
        self.rep.cooldown.assert_not_called()
        self.health.record_failure.assert_not_called()
        self.health.record_success.assert_not_called()


class WarmStartsTheFillTests(_WarmCase):
    async def test_a_warm_starts_a_producer_and_warms_the_tail(self):
        self.upstream_ = self.upstream(_Upstream())
        s = self.session()
        e = await self.warmed(s)

        self.assertTrue(e.warm)
        self.assertEqual(0, e.consumers)
        self.assertEqual(self.upstream_.data[-TAIL:], e.tail)
        self.assertEqual(TOTAL - TAIL, e.tail_off)
        self.assertIn(f"bytes={TOTAL - TAIL}-{TOTAL - 1}", self.upstream_.sent)
        # A short read-ahead, not the film: held at its allowance with the
        # source released, and not growing.
        self.assertGreaterEqual(e.avail, AHEAD)
        self.assertLess(e.avail, TOTAL)
        held = e.avail
        await asyncio.sleep(0.2)
        self.assertEqual(held, e.avail)
        self.assertIsNotNone(e.producer)         # parked, ready for a reader
        # Not playback: nothing playing, nothing recorded as a play.
        self.assertFalse(proxy._playing(e, time.time()))
        self.record_play.assert_not_called()

    async def test_on_a_rationed_host_the_grace_stops_it_before_its_allowance(self):
        self.upstream_ = self.upstream(_Upstream(_data(8 * 1024 * KB),
                                                 delay=0.02))
        s = self.session(_cand(host="members.easynews.com"))
        with (patch.object(proxy, "BUFFER_IDLE_GRACE_RATIONED", 0.3),
              patch.object(proxy, "WARM_AHEAD_BYTES", 64 * 1024 * KB)):
            t0 = time.monotonic()
            e = await self.warmed(s)
            took = time.monotonic() - t0
            held = e.avail
            await asyncio.sleep(0.2)
        self.assertLess(took, 1.5)
        self.assertEqual(held, e.avail)
        self.assertLess(e.avail, 2 * 1024 * KB)  # far short of the allowance

    async def test_a_head_after_a_warm_answers_without_going_upstream(self):
        self.upstream_ = self.upstream(_Upstream())
        s = self.session()
        await self.warmed(s)
        before = len(self.upstream_.sent)
        resp = await proxy._head(s)
        self.assertEqual(str(TOTAL), resp.headers["content-length"])
        self.assertEqual(before, len(self.upstream_.sent))


class WarmJudgesNothingTests(_WarmCase):
    async def test_a_warm_that_finds_nothing_strikes_nothing(self):
        self.upstream_ = self.upstream(_Dead())
        s = self.session(_cand("a"), _cand("b", sig="nzb:" + "b" * 64))
        self.assertEqual(202, proxy.warm("tok")[0])
        await _until(lambda: "tok" not in proxy._warming)

        self.assertIsNone(self.entry(s))
        self.assertEqual(2, len(self.upstream_.sent))     # both really tried
        self.assert_no_verdicts()
        errors = [c.kwargs["reason"] for c in self.record_buffer.call_args_list
                  if c.args and c.args[0] == "start_error" and c.kwargs.get("sig")]
        self.assertTrue(errors)
        self.assertTrue(all(r.startswith("warm:") for r in errors), errors)

    async def test_a_warm_never_looks_like_a_rejected_play(self):
        self.upstream_ = self.upstream(_Upstream())
        s = self.session()
        with (patch.object(proxy, "_note_consumer_close") as closes,
              patch.object(proxy, "_arm_reject_timer") as timer,
              patch.object(proxy, "_spawn_learn") as learn,
              patch.object(proxy, "_schedule_prefetch") as prefetch):
            e = await self.warmed(s)
            await asyncio.sleep(0.1)
        closes.assert_not_called()
        timer.assert_not_called()
        learn.assert_not_called()
        prefetch.assert_not_called()
        self.assertEqual({}, e.playfail)
        self.assertNotIn("pos", s)               # no read head recorded either
        self.record_play.assert_not_called()
        self.assert_no_verdicts()

    async def test_a_drop_during_a_warm_is_not_a_strike(self):
        self.upstream_ = self.upstream(_Upstream(drop_at=128 * KB))
        s = self.session()
        e = await self.warmed(s)
        self.assertGreaterEqual(e.avail, AHEAD)  # resumed past the drop
        self.assertTrue(any(r != "bytes=0-" and r.endswith("-")
                            for r in self.upstream_.sent))
        self.assert_no_verdicts()


class PlayAfterWarmTests(_WarmCase):
    async def test_play_reuses_the_warm_and_picks_up_at_its_write_head(self):
        self.upstream_ = self.upstream(_Upstream())
        s = self.session()
        e = await self.warmed(s)
        held = e.avail

        resp = await proxy._serve_buffered("tok", s, s["cands"], 0, None,
                                           False, None)
        body = b"".join([c async for c in resp.body_iterator])

        self.assertEqual(self.upstream_.data, body)
        self.assertFalse(e.warm)
        self.assertEqual(1, self.upstream_.sent.count("bytes=0-"))  # chosen once
        self.assertIn(f"bytes={held}-", self.upstream_.sent)
        self.assertIs(e, self.entry(s))
        self.assertTrue(proxy._playing(e, time.time()))   # now it is
        self.record_play.assert_called_once()
        self.rep.observe.assert_not_called()


class RepeatedWarmTests(_WarmCase):
    async def test_repeating_a_warm_on_a_warmed_token_does_no_work(self):
        self.upstream_ = self.upstream(_Upstream())
        s = self.session()
        e = await self.warmed(s)
        sent = len(self.upstream_.sent)
        for _ in range(5):
            status, body = proxy.warm("tok")
            self.assertEqual(200, status)
            self.assertEqual("filling", body["state"])
            self.assertEqual(e.avail, body["avail"])
        self.assertEqual(sent, len(self.upstream_.sent))
        self.assertEqual(set(), proxy._warming)

    async def test_a_warm_already_choosing_is_joined_not_repeated(self):
        self.upstream_ = self.upstream(_Upstream())
        self.upstream_.gate = asyncio.Event()
        s = self.session()
        self.assertEqual(202, proxy.warm("tok")[0])
        await _until(lambda: self.upstream_.sent)
        for _ in range(3):
            self.assertEqual((202, {"state": "warming"}), proxy.warm("tok"))
        self.upstream_.gate.set()
        await _until(lambda: self.entry(s) is not None)
        self.assertEqual(1, self.upstream_.sent.count("bytes=0-"))

    async def test_a_warm_that_found_nothing_is_rate_limited(self):
        self.upstream_ = self.upstream(_Dead())
        self.session()
        self.assertEqual(202, proxy.warm("tok")[0])
        await _until(lambda: "tok" not in proxy._warming)
        tried = len(self.upstream_.sent)

        status, body = proxy.warm("tok")
        self.assertEqual(429, status)
        self.assertLessEqual(body["retry_after"], proxy._WARM_MIN_INTERVAL)
        self.assertEqual(tried, len(self.upstream_.sent))
        with patch.object(proxy, "_WARM_MIN_INTERVAL", 0.0):
            self.assertEqual(202, proxy.warm("tok")[0])
            await _until(lambda: "tok" not in proxy._warming)

    async def test_concurrent_warms_across_tokens_are_capped(self):
        self.upstream_ = self.upstream(_Upstream())
        self.upstream_.gate = asyncio.Event()
        for i in range(proxy._WARM_MAX_INFLIGHT + 1):
            self.session(_cand(chr(ord("a") + i)), token=f"tok{i}")
        codes = [proxy.warm(f"tok{i}")[0]
                 for i in range(proxy._WARM_MAX_INFLIGHT + 1)]
        self.assertEqual([202] * proxy._WARM_MAX_INFLIGHT + [429], codes)
        self.upstream_.gate.set()
        await _until(lambda: not proxy._warming)

    async def test_nothing_to_warm(self):
        self.assertEqual(410, proxy.warm("nope")[0])
        self.session(hls=True)
        self.assertEqual(204, proxy.warm("tok")[0])
        self.session()
        with patch.object(proxy, "WARM", False):
            self.assertEqual(204, proxy.warm("tok")[0])
        self.assertEqual(set(), proxy._warming)


class WarmRouteTests(unittest.TestCase):
    def setUp(self):
        for p in (patch.object(proxy, "_sessions", {}),
                  patch.object(proxy, "_entries", {}),
                  patch.object(proxy, "_warming", set()),
                  patch.object(proxy, "_warmed_at", {}),
                  patch.object(proxy, "_persist")):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(main.app)

    def _session(self, **extra):
        proxy._sessions["tok"] = (time.time(), {
            "cands": [_cand()], "pool": [_cand()], "id": "tt1", "picker": "fast",
            "pin": None, "total": None, "auto": True, **extra})

    def test_post_only_and_no_secret_needed(self):
        self.assertEqual(410, self.client.post("/proxy/nope/warm").status_code)
        self.assertEqual(405, self.client.get("/proxy/nope/warm").status_code)

    def test_nothing_to_warm_is_204(self):
        self._session(hls=True)
        r = self.client.post("/proxy/tok/warm")
        self.assertEqual(204, r.status_code)
        self.assertEqual("no-store", r.headers["cache-control"])

    def test_rate_limit_says_when_to_come_back(self):
        self._session()
        proxy._warmed_at["tok"] = time.monotonic()
        with patch.object(proxy, "PROXY_BUFFER", True):
            r = self.client.post("/proxy/tok/warm")
        self.assertEqual(429, r.status_code)
        self.assertEqual("rate-limited", r.json()["state"])
        self.assertLessEqual(int(r.headers["retry-after"]),
                             proxy._WARM_MIN_INTERVAL)


if __name__ == "__main__":
    unittest.main()
