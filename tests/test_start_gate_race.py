"""The opening request's time budget, and backups started beside a slow leader.

Field review (2026-10-15): the start gate tried candidates strictly one at a
time with no overall bound — connect (10 s) + first byte (8.5 s) + evaluation
each — so four bad candidates cost over a minute, long after the TV player
(~15 s) had gone, still opening connections and handing out strikes.
"""

import asyncio
import time
import unittest
from unittest.mock import MagicMock, patch

from app import proxy

MKV = b"\x1aE\xdf\xa3" + b"\0" * (3 * 1024 * 1024)


class _Up:
    """An upstream that answers after `first_after` s, or never (`hang`)."""

    def __init__(self, first_after: float = 0.0, *, hang: bool = False):
        self.status_code, self.headers = 200, {}
        self._after, self._hang = first_after, hang
        self.closed = False

    async def aclose(self):
        self.closed = True

    def aiter_raw(self):
        async def gen():
            if self._hang:
                await asyncio.sleep(3600)
            await asyncio.sleep(self._after)
            yield MKV
        return gen()


def _cand(name: str, **kw) -> dict:
    return dict({"sig": f"file:{name:0<64}"[:69], "lbl": name, "dbr": "",
                 "u": f"https://{name}.example/f.mkv"}, **kw)


class _GateCase(unittest.TestCase):
    def run_gate(self, cands, ups: dict, **knobs):
        rep = MagicMock()
        rep.blocked.return_value = False
        rep.cooled.return_value = False
        started: dict[str, float] = {}
        t0 = time.monotonic()

        async def send(c, _range):
            started[c["lbl"]] = time.monotonic() - t0
            return ups[c["lbl"]]

        patches = [patch("app.proxy.reputation", rep),
                   patch("app.proxy.telemetry.record_buffer"),
                   patch("app.proxy.usenet_health"),
                   patch("app.proxy._send", side_effect=send),
                   patch.object(proxy, "NZB_START_RETRY_SECS", 0)]
        patches += [patch.object(proxy, k, v) for k, v in knobs.items()]
        for p in patches:
            p.start()
        try:
            got = asyncio.run(proxy._select_start(cands, None, 0, None, "tok"))
        finally:
            for p in reversed(patches):
                p.stop()
        return got, time.monotonic() - t0, started, rep


class RankingTests(_GateCase):
    def test_a_faster_backup_does_not_beat_the_chosen_release(self):
        """The viewer picked candidate 0; starting #1 early only hides its
        latency in case #0 fails — it must not win on speed."""
        ups = {"leader": _Up(0.6), "backup": _Up(0.0)}
        got, _, started, _ = self.run_gate(
            [_cand("leader"), _cand("backup")], ups,
            START_STAGGER=0.1, START_BUDGET=5.0)
        self.assertEqual("leader", got[1]["lbl"])
        self.assertIn("backup", started, "the backup was started early")
        self.assertTrue(ups["backup"].closed, "the held backup is released")

    def test_a_failing_leader_hands_over_without_paying_twice(self):
        ups = {"leader": _Up(hang=True), "backup": _Up(0.3)}
        got, elapsed, started, rep = self.run_gate(
            [_cand("leader"), _cand("backup")], ups,
            START_STAGGER=0.1, START_TTFB=0.5, START_BUDGET=5.0)
        self.assertEqual("backup", got[1]["lbl"])
        # Sequential would be ~1.0 s (leader dies) + 0.3 s (backup's start).
        self.assertLess(elapsed, 1.2)
        self.assertLess(started["backup"], 0.5)
        self.assertEqual("dead", rep.observe.call_args.args[2])

    def test_rationed_nzb_and_uncached_backups_wait_their_turn(self):
        for backup in (_cand("nzbcopy", sig="nzb:" + "b" * 64),
                       _cand("easy", u="https://members.easynews.com/f.mkv"),
                       _cand("uncached", dbr="TB")):
            with self.subTest(backup=backup["lbl"]):
                ups = {"leader": _Up(hang=True), backup["lbl"]: _Up(0.0)}
                got, _, started, _ = self.run_gate(
                    [_cand("leader"), backup], ups,
                    START_STAGGER=0.05, START_TTFB=0.15, START_BUDGET=5.0)
                self.assertEqual(backup["lbl"], got[1]["lbl"])
                self.assertGreater(started[backup["lbl"]], 0.15,
                                   "started only after the leader failed")


class BudgetTests(_GateCase):
    def test_the_whole_decision_is_bounded(self):
        names = ("a", "b", "c", "d")
        ups = {n: _Up(hang=True) for n in names}
        got, elapsed, started, rep = self.run_gate(
            [_cand(n) for n in names], ups,
            START_STAGGER=0.0, START_TTFB=30.0, START_BUDGET=0.4)
        self.assertIsNone(got)
        self.assertLess(elapsed, 1.2)
        rep.observe.assert_not_called()       # out of time is nobody's fault
        self.assertTrue(ups["a"].closed)
        self.assertEqual(["a"], list(started), "no one else was tried")

    def test_when_time_runs_out_a_passing_backup_is_served(self):
        ups = {"leader": _Up(hang=True), "backup": _Up(0.0)}
        got, elapsed, _, _ = self.run_gate(
            [_cand("leader"), _cand("backup")], ups,
            START_STAGGER=0.05, START_TTFB=30.0, START_BUDGET=0.4)
        self.assertEqual("backup", got[1]["lbl"])
        self.assertLess(elapsed, 1.2)
        self.assertTrue(ups["leader"].closed)

    def test_zero_restores_the_unbounded_sequential_gate(self):
        ups = {"leader": _Up(hang=True), "backup": _Up(0.0)}
        got, _, started, _ = self.run_gate(
            [_cand("leader"), _cand("backup")], ups,
            START_STAGGER=0.0, START_TTFB=0.3, START_BUDGET=0.0)
        self.assertEqual("backup", got[1]["lbl"])
        self.assertGreater(started["backup"], 0.3)


if __name__ == "__main__":
    unittest.main()
