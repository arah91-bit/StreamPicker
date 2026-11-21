"""sources.get() must not swallow its caller's cancellation.

Field review (2026-10-15): get() caught CancelledError alongside TimeoutError
and returned normally, so a cancelled waiter looked like a finished one — a
timeout or shutdown above it never took effect.
"""

import asyncio
import unittest
from unittest.mock import patch

from app import sources


class GetCancellationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for p in (patch.dict(sources._BASES, {"x:test": "https://addon.invalid"}),
                  patch.object(sources, "EXTRAS", ["x:test"]),
                  patch.object(sources, "_raw", {}),
                  patch.object(sources, "_inflight", {})):
            p.start()
            self.addCleanup(p.stop)

    def _slow_search(self, done: asyncio.Event):
        async def run(key, base, media, media_id, timeout):
            try:
                await asyncio.sleep(3600)
            finally:
                done.set()
        return patch.object(sources, "_run", side_effect=run)

    async def test_a_cancelled_caller_is_cancelled(self):
        done = asyncio.Event()
        with self._slow_search(done):
            waiter = asyncio.create_task(
                sources.get("x:test", "movie", "tt1", wait=30))
            await asyncio.sleep(0.05)
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            # ...while the shared search itself carries on for other joiners.
            self.assertFalse(done.is_set())
            for t in list(sources._inflight.values()):
                t.cancel()

    async def test_the_shared_search_cancelled_under_us_is_an_answer(self):
        done = asyncio.Event()
        with self._slow_search(done):
            waiter = asyncio.create_task(
                sources.get("x:test", "movie", "tt1", wait=30))
            await asyncio.sleep(0.05)
            for t in list(sources._inflight.values()):
                t.cancel()
            self.assertEqual([], await waiter)

    async def test_a_timeout_is_still_an_answer(self):
        done = asyncio.Event()
        with self._slow_search(done):
            self.assertEqual([], await sources.get("x:test", "movie", "tt1",
                                                   wait=0.1))
            for t in list(sources._inflight.values()):
                t.cancel()


if __name__ == "__main__":
    unittest.main()
