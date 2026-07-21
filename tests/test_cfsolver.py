"""FlareSolverr integration (app.cfsolver): header merging, challenge
detection, per-host clearance caching, and background solve scheduling.

Every test replaces the network hop to FlareSolverr with an in-process fake;
nothing here contacts a real solver.
"""

from __future__ import annotations

import asyncio
import time
import unittest
from unittest import mock

from app import cfsolver


class HeaderMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        cfsolver.reset()

    def test_no_clearance_returns_base_unchanged(self) -> None:
        base = {"Referer": "https://x.invalid/"}
        self.assertEqual(base, cfsolver.merge_headers(
            "https://torrentio.invalid/resolve", base))

    def test_clearance_useragent_wins_and_cookie_merges(self) -> None:
        cfsolver._clearance["torrentio.invalid"] = (
            time.monotonic() + 300,
            {"Cookie": "cf_clearance=TOKEN", "User-Agent": "SolverUA"})
        merged = cfsolver.merge_headers(
            "https://torrentio.invalid/resolve",
            {"User-Agent": "Stremio", "Cookie": "sid=1", "Referer": "r"})
        # Clearance UA must win (the cookie is bound to it)...
        self.assertEqual("SolverUA", merged["User-Agent"])
        # ...the declared referer survives...
        self.assertEqual("r", merged["Referer"])
        # ...and the existing cookie is kept alongside the clearance cookie.
        self.assertIn("sid=1", merged["Cookie"])
        self.assertIn("cf_clearance=TOKEN", merged["Cookie"])

    def test_expired_clearance_is_dropped(self) -> None:
        cfsolver._clearance["torrentio.invalid"] = (
            time.monotonic() - 1, {"Cookie": "cf_clearance=OLD"})
        merged = cfsolver.merge_headers(
            "https://torrentio.invalid/resolve", {"User-Agent": "Stremio"})
        self.assertEqual({"User-Agent": "Stremio"}, merged)
        self.assertNotIn("torrentio.invalid", cfsolver._clearance)


class ChallengeDetectionTests(unittest.TestCase):
    def test_plain_403_without_cloudflare_is_not_a_challenge(self) -> None:
        self.assertFalse(cfsolver.looks_challenged(
            403, {"server": "nginx"}, b"forbidden"))

    def test_cloudflare_server_403_is_a_challenge(self) -> None:
        self.assertTrue(cfsolver.looks_challenged(
            403, {"server": "cloudflare"}, None))

    def test_cf_mitigated_header_is_a_challenge(self) -> None:
        self.assertTrue(cfsolver.looks_challenged(
            403, {"cf-mitigated": "challenge"}, None))

    def test_block_page_body_is_a_challenge(self) -> None:
        self.assertTrue(cfsolver.looks_challenged(
            503, {"server": "x"}, b"<title>Just a moment...</title>"))

    def test_2xx_is_never_a_challenge(self) -> None:
        self.assertFalse(cfsolver.looks_challenged(
            200, {"server": "cloudflare"}, None))


class SolveSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        cfsolver.reset()

    async def test_note_challenge_solves_and_caches_clearance(self) -> None:
        solved = {
            "status": "ok",
            "solution": {
                "userAgent": "SolverUA",
                "cookies": [{"name": "cf_clearance", "value": "TOK"},
                            {"name": "other", "value": "z"}]}}
        with mock.patch.object(cfsolver, "_flaresolverr_get",
                               new=mock.AsyncMock(return_value=solved)):
            cfsolver.note_challenge("https://torrentio.invalid/resolve/abc")
            # background task scheduled — let it run
            for _ in range(50):
                if "torrentio.invalid" in cfsolver._clearance:
                    break
                await asyncio.sleep(0.01)
        merged = cfsolver.merge_headers(
            "https://torrentio.invalid/other", {"User-Agent": "Stremio"})
        self.assertEqual("SolverUA", merged["User-Agent"])
        self.assertIn("cf_clearance=TOK", merged["Cookie"])

    async def test_solve_without_cf_clearance_is_not_cached(self) -> None:
        solved = {"status": "ok",
                  "solution": {"userAgent": "UA",
                               "cookies": [{"name": "foo", "value": "bar"}]}}
        with mock.patch.object(cfsolver, "_flaresolverr_get",
                               new=mock.AsyncMock(return_value=solved)):
            cfsolver.note_challenge("https://x.invalid/y")
            await asyncio.sleep(0.05)
        self.assertNotIn("x.invalid", cfsolver._clearance)

    async def test_note_challenge_is_deduped_per_host(self) -> None:
        calls = 0

        async def slow_solve(origin):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)
            return {"status": "ok", "solution": {
                "userAgent": "UA",
                "cookies": [{"name": "cf_clearance", "value": "T"}]}}

        with mock.patch.object(cfsolver, "_flaresolverr_get", new=slow_solve):
            cfsolver.note_challenge("https://h.invalid/a")
            cfsolver.note_challenge("https://h.invalid/b")
            cfsolver.note_challenge("https://h.invalid/c")
            await asyncio.sleep(0.1)
        self.assertEqual(1, calls)

    async def test_disabled_solver_is_a_noop(self) -> None:
        called = mock.AsyncMock()
        with mock.patch.dict("os.environ", {"CF_SOLVER": "0"}), \
                mock.patch.object(cfsolver, "_flaresolverr_get", new=called):
            cfsolver.note_challenge("https://h.invalid/a")
            await asyncio.sleep(0.03)
        called.assert_not_awaited()

    async def test_allowlist_skips_unlisted_hosts(self) -> None:
        called = mock.AsyncMock(return_value={"status": "error"})
        with mock.patch.dict("os.environ",
                             {"CF_SOLVER_HOSTS": "torrentio.invalid"}), \
                mock.patch.object(cfsolver, "_flaresolverr_get", new=called):
            cfsolver.note_challenge("https://other.invalid/a")
            await asyncio.sleep(0.03)
        called.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
