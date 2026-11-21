"""Dashboard telemetry reads must never stall playback, and must read only
what they render.

Field-review case (2026-10-15): telemetry.load() parsed ~230k JSON lines on
the event loop — 2.5-3.8 s measured against the live store — on every Home,
Health and Private Trackers view. Home refreshes itself every 30 s while a
stream plays and the Private Trackers page polls every 10 s, so leaving that
page open froze every stream in the process roughly a third of the time.
72 % of those lines were `identity` diagnostics no dashboard reads.
"""

import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from app import telemetry


class _StoreCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.db = os.path.join(self.dir.name, "telemetry.sqlite3")
        for p in (patch.object(telemetry, "_DIR", self.dir.name),
                  patch.object(telemetry, "_DB_PATH", self.db),
                  patch.object(telemetry, "_LEGACY_PATH",
                               os.path.join(self.dir.name, "probes.jsonl")),
                  patch.object(telemetry, "_conn", None),
                  patch.object(telemetry, "ENABLED", True),
                  patch.object(telemetry, "_load_cache", {}),
                  patch.object(telemetry, "_load_inflight", {})):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._close)

    def _close(self):
        if telemetry._conn is not None:
            telemetry._conn.close()
            telemetry._conn = None

    def write(self, *recs):
        for r in recs:
            telemetry._append({"ts": time.time(), **r})


class StoreTests(_StoreCase):
    def test_kinds_are_answered_by_the_index_not_by_parsing(self):
        self.write({"kind": "probe", "ok": True},
                   {"kind": "private_tracker", "event": "added"},
                   {"kind": "play", "served": 1})
        real = json.loads
        parsed = []

        def counting(text, *a, **k):
            parsed.append(text)
            return real(text, *a, **k)

        with patch("app.telemetry.json.loads", side_effect=counting):
            got = telemetry.load(kinds=("private_tracker",))

        self.assertEqual(["added"], [r["event"] for r in got])
        self.assertEqual(1, len(parsed), "other kinds must never be parsed")

    def test_no_kinds_keeps_everything_in_order(self):
        self.write({"kind": "probe", "n": 1}, {"kind": "play", "n": 2})
        self.assertEqual([1, 2], [r["n"] for r in telemetry.load()])

    def test_limit_keeps_the_newest_chronologically(self):
        self.write(*({"kind": "probe", "n": i} for i in range(10)))
        self.assertEqual([7, 8, 9], [r["n"] for r in telemetry.load(limit=3)])

    def test_since_cuts_by_the_records_own_time(self):
        now = time.time()
        telemetry._append({"ts": now - 7200, "kind": "probe", "n": "old"})
        telemetry._append({"ts": now, "kind": "probe", "n": "new"})
        self.assertEqual(["new"], [r["n"] for r in
                                   telemetry.load(since=now - 3600)])

    def test_a_missing_store_reads_empty(self):
        self.assertEqual([], telemetry.load())

    def test_identity_diagnostics_have_their_own_budget(self):
        with patch.object(telemetry, "_IDENTITY_MAX_ROWS", 5), \
                patch.object(telemetry, "_PRUNE_EVERY", 1):
            self.write(*({"kind": "identity", "n": i} for i in range(20)))
            self.write({"kind": "probe", "n": "kept"})
        identity = telemetry.load(kinds=("identity",))
        self.assertLessEqual(len(identity), 6)
        self.assertEqual(19, identity[-1]["n"], "the newest are kept")
        self.assertEqual(["kept"], [r["n"] for r in
                                    telemetry.load(kinds=("probe",))])

    def test_the_byte_budget_drops_the_oldest(self):
        with patch.object(telemetry, "_MAX_BYTES", 64 * 1024), \
                patch.object(telemetry, "_PRUNE_EVERY", 50), \
                patch.object(telemetry, "_PRUNE_CHUNK", 200):
            self.write(*({"kind": "probe", "n": i, "pad": "x" * 400}
                         for i in range(2000)))
        got = telemetry.load(kinds=("probe",))
        self.assertLess(len(got), 2000)
        self.assertEqual(1999, got[-1]["n"])

    def test_prune_keeps_newest_event_even_when_one_row_exceeds_budget(self):
        with patch.object(telemetry, "_MAX_BYTES", 1), \
                patch.object(telemetry, "_PRUNE_EVERY", 1):
            self.write({"kind": "probe", "ts": 10, "n": 1},
                       {"kind": "probe", "ts": 10, "n": 2},
                       {"kind": "probe", "ts": 9, "n": 3})
        self.assertEqual([2], [r["n"] for r in telemetry.load()])

    def test_dashboard_default_leaves_identity_out(self):
        self.write({"kind": "identity"}, {"kind": "probe"})
        self.assertNotIn("identity", telemetry.DASHBOARD_KINDS)


class MigrationTests(_StoreCase):
    def test_the_jsonl_ledger_is_imported_oldest_first_and_set_aside(self):
        legacy = telemetry._LEGACY_PATH
        with open(legacy + ".1", "w") as f:              # the older segment
            f.write(json.dumps({"ts": 1.0, "kind": "probe", "n": "a"}) + "\n")
        with open(legacy, "w") as f:
            f.write(json.dumps({"ts": 2.0, "kind": "play", "n": "b"}) + "\n")
            f.write("not json at all\n")
            f.write(json.dumps({"ts": 3.0, "kind": "probe", "n": "c"}) + "\n")

        self.assertEqual(3, telemetry.migrate_legacy())

        self.assertEqual(["a", "b", "c"], [r["n"] for r in telemetry.load()])
        self.assertFalse(os.path.exists(legacy))
        self.assertTrue(os.path.exists(legacy + ".migrated"))
        self.assertTrue(os.path.exists(legacy + ".1.migrated"))
        self.assertEqual(0, telemetry.migrate_legacy(), "runs once")

    async def test_startup_imports_off_the_loop(self):
        with open(telemetry._LEGACY_PATH, "w") as f:
            f.write(json.dumps({"ts": 1.0, "kind": "probe", "n": 1}) + "\n")
        with patch.object(telemetry, "_migration", None):
            await telemetry.startup()
            await telemetry._migration
        self.assertEqual([1], [r["n"] for r in telemetry.load()])


class LoadAsyncTests(_StoreCase):
    async def test_the_parse_runs_off_the_event_loop(self):
        def slow_load(*_a, **_k):
            time.sleep(0.4)                  # a blocking parse, like the real one
            return []

        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        t = asyncio.create_task(ticker())
        with patch.object(telemetry, "load", side_effect=slow_load):
            await telemetry.load_async()
        t.cancel()
        self.assertGreater(ticks, 5, "the loop kept serving while it parsed")

    async def test_concurrent_callers_share_one_parse(self):
        calls = 0

        def slow_load(*_a, **_k):
            nonlocal calls
            calls += 1
            time.sleep(0.1)
            return [{"kind": "probe"}]

        with patch.object(telemetry, "load", side_effect=slow_load):
            a, b, c = await asyncio.gather(*(telemetry.load_async()
                                             for _ in range(3)))
        self.assertEqual(1, calls)
        self.assertIs(a, b)
        self.assertIs(b, c)

    async def test_an_expensive_parse_is_reused_a_cheap_one_is_not(self):
        self.write({"kind": "probe", "n": 1})
        with patch.object(telemetry, "_LOAD_REUSE_COST", 10.0):
            first = await telemetry.load_async()
            self.write({"kind": "probe", "n": 2})
            second = await telemetry.load_async()
        self.assertEqual(1, len(first))
        self.assertEqual(2, len(second), "a cheap parse is always fresh")

        with patch.object(telemetry, "_LOAD_REUSE_COST", 0.0):
            third = await telemetry.load_async()
            self.write({"kind": "probe", "n": 3})
            fourth = await telemetry.load_async()
        self.assertIs(third, fourth, "an expensive parse is reused")

    async def test_reuse_expires(self):
        self.write({"kind": "probe", "n": 1})
        with patch.object(telemetry, "_LOAD_REUSE_COST", 0.0), \
             patch.object(telemetry, "LOAD_TTL", 0.0):
            first = await telemetry.load_async()
            self.write({"kind": "probe", "n": 2})
            second = await telemetry.load_async()
        self.assertEqual(2, len(second))
        self.assertIsNot(first, second)


if __name__ == "__main__":
    unittest.main()
