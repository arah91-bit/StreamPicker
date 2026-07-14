"""Player-rejected detection.

A stream the server verified can still be undecodable for the player (live
case: a 5×FLAC multi-audio fansub remux — the player pulled the header three
times at cache speed, gave up silently, and the viewer got an endless spinner
while H.264+DTS files played fine). The proxy must recognize that consumption
shape, cool the release, and serve a different one on the next open.
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("ADDON_SECRET", "test-secret")

from app import proxy


def _short(state, sig="file:abc", **kw):
    kw.setdefault("served", 40 * 1024 * 1024)   # header-sized grab
    kw.setdefault("dur", 0.7)
    proxy._note_consumer_close(state, sig=sig, label="Bad Remux 1080p",
                               node="node-1", token="tok1", picker="fast",
                               media_id="tt1:1:5", **kw)


class DetectorTests(unittest.TestCase):
    def test_three_false_starts_reject_the_release(self):
        state = {}
        with (patch("app.proxy.reputation.observe") as observe,
              patch("app.proxy.reputation.cooldown") as cooldown,
              patch("app.proxy.telemetry.record_buffer") as record):
            _short(state)
            _short(state)
            self.assertFalse(state.get("rejected"))
            observe.assert_not_called()
            _short(state)
        self.assertTrue(state["rejected"])
        observe.assert_called_once()
        self.assertEqual("player-rejected", observe.call_args.args[2])
        cooldown.assert_called_once_with("file:abc")
        self.assertEqual("player_rejected", record.call_args.args[0])

    def test_sustained_playback_latches_the_release_open(self):
        state = {}
        with (patch("app.proxy.reputation.observe") as observe,
              patch("app.proxy.reputation.cooldown") as cooldown):
            _short(state, dur=1023.7, served=2_000_000_000)   # a real watch
            for _ in range(5):                                # then seek storm
                _short(state)
        self.assertFalse(state.get("rejected"))
        observe.assert_not_called()
        cooldown.assert_not_called()

    def test_big_prebuffer_pull_counts_as_playback(self):
        state = {}
        with patch("app.proxy.reputation.observe") as observe:
            _short(state, dur=3.0, served=300 * 1024 * 1024)
            _short(state)
            _short(state)
            _short(state)
        self.assertFalse(state.get("rejected"))
        observe.assert_not_called()

    def test_fires_only_once(self):
        state = {}
        with (patch("app.proxy.reputation.observe") as observe,
              patch("app.proxy.reputation.cooldown"),
              patch("app.proxy.telemetry.record_buffer")):
            for _ in range(6):
                _short(state)
        observe.assert_called_once()

    def test_knob_zero_disables(self):
        state = {}
        with (patch.object(proxy, "PLAYER_REJECT_STARTS", 0),
              patch("app.proxy.reputation.observe") as observe):
            for _ in range(5):
                _short(state)
        self.assertFalse(state.get("rejected"))
        observe.assert_not_called()

    def test_no_signature_is_a_noop(self):
        state = {}
        with patch("app.proxy.reputation.observe") as observe:
            for _ in range(5):
                _short(state, sig="")
        observe.assert_not_called()


def _entry(sig):
    return proxy._Entry(sig, f"/tmp/{sig}.bin",
                        [{"sig": sig, "lbl": "x", "u": "https://s.example/f"}],
                        {"sig": sig, "lbl": "x", "u": "https://s.example/f"},
                        "video/x-matroska", 1000, "fast", "tt1:1:5")


class RejectedEntryReuseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved = dict(proxy._entries)
        proxy._entries.clear()

    def tearDown(self):
        proxy._entries.clear()
        proxy._entries.update(self._saved)

    async def test_rejected_entry_is_not_reused_for_a_fresh_open(self):
        e = _entry("file:bad")
        e.playfail["rejected"] = True
        proxy._entries["file:bad"] = e

        async def no_start(*a, **k):
            return None

        session = {"bufsig": "file:bad"}
        with patch("app.proxy._select_start", side_effect=no_start):
            got = await proxy._get_or_start_entry(
                "tok1", session, [{"sig": "file:bad", "lbl": "x",
                                   "u": "https://s.example/f"}])
        self.assertIsNone(got)          # fell through to (failed) reselection

    async def test_healthy_entry_is_still_reused(self):
        e = _entry("file:good")
        proxy._entries["file:good"] = e
        session = {"bufsig": "file:good"}
        got = await proxy._get_or_start_entry(
            "tok1", session, [{"sig": "file:good", "lbl": "x",
                               "u": "https://s.example/f"}])
        self.assertIs(e, got)

    async def test_fresh_open_on_rejected_sig_reselects(self):
        e = _entry("file:bad")
        e.playfail["rejected"] = True
        proxy._entries["file:bad"] = e
        session = {"bufsig": "file:bad"}
        calls = []

        async def fake_start(token, sess, cands):
            calls.append(True)
            return None

        async def fake_direct(sess, cands, request, token, source=None,
                              expected_total=None):
            from starlette.responses import Response
            return Response(status_code=204)

        with (patch("app.proxy._get_or_start_entry", side_effect=fake_start),
              patch("app.proxy._serve_direct", side_effect=fake_direct)):
            resp = await proxy._serve_buffered("tok1", session, [], 0, None,
                                               False, request=None)
        self.assertTrue(calls)                       # reselected, not reused
        self.assertNotIn("bufsig", session)          # sticky pick cleared
        self.assertEqual(204, resp.status_code)


if __name__ == "__main__":
    unittest.main()
