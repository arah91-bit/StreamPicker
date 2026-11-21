"""Event-loop stalls are measured, attributed, and shown.

Field review (2026-10-15): a dashboard view froze every stream for ~3 s and
nothing recorded it — the waits it caused were logged as `starve` events that
blamed the source. One loop serves every stream; its lag is worth a gauge.
"""

import asyncio
import time
import unittest
from unittest.mock import patch

from app import home_ui, loopwatch


class _Reset(unittest.TestCase):
    def setUp(self):
        for p in (patch.object(loopwatch, "_requests",
                               loopwatch.deque(maxlen=64)),
                  patch.object(loopwatch, "_stalls",
                               loopwatch.deque(maxlen=50))):
            p.start()
            self.addCleanup(p.stop)


class ObserveTests(_Reset):
    def test_a_short_wobble_is_not_a_stall(self):
        with patch("app.loopwatch.telemetry.record_loop_lag") as rec:
            loopwatch._observe(0.01, time.monotonic())
        rec.assert_not_called()
        self.assertIsNone(loopwatch.worst_recent())

    def test_a_stall_names_the_requests_that_began_before_it(self):
        loopwatch.note_request("GET", "/health/sources")
        with patch("app.loopwatch.telemetry.record_loop_lag") as rec, \
                self.assertLogs("stream-picker", "WARNING") as logs:
            loopwatch._observe(2.5, time.monotonic())
        rec.assert_called_once_with(2.5, ["GET /health/sources"])
        self.assertIn("event loop stalled 2500 ms", logs.output[0])
        worst = loopwatch.worst_recent()
        self.assertEqual(2.5, worst["lag"])
        self.assertEqual(["GET /health/sources"], worst["suspects"])

    def test_old_stalls_age_out(self):
        loopwatch._stalls.append((time.time() - 3600, 4.0, []))
        self.assertIsNone(loopwatch.worst_recent())


class RunTests(_Reset, unittest.IsolatedAsyncioTestCase):
    async def test_a_blocking_call_on_the_loop_is_caught(self):
        with patch.object(loopwatch, "INTERVAL", 0.05), \
                patch.object(loopwatch, "THRESHOLD", 0.15), \
                patch("app.loopwatch.telemetry.record_loop_lag") as rec, \
                self.assertLogs("stream-picker", "WARNING"):
            task = asyncio.create_task(loopwatch.run())
            await asyncio.sleep(0.1)
            time.sleep(0.4)                  # the kind of thing that froze it
            await asyncio.sleep(0.1)
            task.cancel()
        self.assertEqual(1, rec.call_count)
        self.assertGreater(rec.call_args.args[0], 0.3)


class HomeTests(_Reset):
    def test_home_says_the_server_stalled(self):
        loopwatch._stalls.append((time.time(), 3.2, ["GET /health/sources"]))
        with patch.object(home_ui.config, "restart_pending", return_value=False), \
                patch.object(home_ui.proxy, "active_stream_details",
                             return_value=[]), \
                patch.object(home_ui.usenet_health, "transport_stalled",
                             return_value={}):
            html = home_ui._hero([], [], {})
        self.assertIn("The server stalled", html)
        self.assertIn("3.2 s", html)
        self.assertIn("GET /health/sources", html)


class LogMaskTests(unittest.TestCase):
    def test_viewer_tokens_never_reach_the_log(self):
        from app import main
        token = "AbCdEfGhIjKlMnOpQrStUv"
        for path in (f"/{token}/manifest.json",
                     f"/{token}/catalog/movie/nr-top.json",
                     f"/{token}/stream/series/tt1:1:1.json",
                     f"/{token}/taste/api/deck"):
            with self.subTest(path=path):
                self.assertNotIn(token, main._masked_path(path))
        for path in ("/private-trackers/setup", "/health/sources",
                     "/kids/manifest.json"):
            with self.subTest(path=path):
                self.assertEqual(path, main._masked_path(path))


if __name__ == "__main__":
    unittest.main()
