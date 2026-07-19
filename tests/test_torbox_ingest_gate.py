"""TorBox ingestion-slot gate (TORBOX_MAX_DOWNLOADS).

A TorBox plan allows only N concurrent downloads *into* the account
(subscription-dependent). Streaming already-cached content is not
slot-limited, but probing a link NOT marked cached can make the debrid start
a real download that occupies a slot — so those probes are capped at the
configured limit, and a slot frees the instant body bytes prove the file was
cached all along (so mislabeled-but-cached links never serialize).
"""

import asyncio
import os
import unittest

os.environ.setdefault("ADDON_SECRET", "test-secret")

from app import probe


class IngestGateMappingTests(unittest.TestCase):
    def _gate(self, name):
        async def run():
            return probe.ingest_gate({"name": name, "url": "http://x/f.mkv"})
        return asyncio.run(run())

    def test_unmarked_torbox_is_gated(self):
        self.assertIsNotNone(self._gate("[TB] Comet 4k"))

    def test_cached_markers_are_not_gated(self):
        self.assertIsNone(self._gate("[TB+] AIOStreams 4k"))
        self.assertIsNone(self._gate("[TB⚡] Comet 4k"))

    def test_other_debrids_and_plain_streams_are_not_gated(self):
        self.assertIsNone(self._gate("[RD] Comet 1080p"))
        self.assertIsNone(self._gate("Some.Release.1080p.WEB-DL"))

    def test_all_torbox_probes_share_one_semaphore_per_loop(self):
        async def run():
            return (probe.ingest_gate({"name": "[TB] a"}),
                    probe.ingest_gate({"name": "[TB] b"}))
        a, b = asyncio.run(run())
        self.assertIs(a, b)

    def test_limit_matches_the_configured_plan(self):
        async def run():
            return probe.ingest_gate({"name": "[TB] a"})._value
        self.assertEqual(probe.TORBOX_MAX_DOWNLOADS, asyncio.run(run()))


class IngestGateConcurrencyTests(unittest.TestCase):
    """Drive probe_race with a fake _probe_url that tracks peak concurrency.
    Invariants, not exact dispatch counts (the race scheduler batches vary)."""

    def setUp(self):
        self._probe_url = probe._probe_url
        self._record = probe._record
        probe._record = lambda *a, **k: None

    def tearDown(self):
        probe._probe_url = self._probe_url
        probe._record = self._record

    def _peak_concurrency(self, n, first_byte_arrives):
        state = {"now": 0, "peak": 0}

        async def fake(url, required, ttfb_max, t0, hops, media=None,
                       headers=None, expect_secs=None, on_body=None):
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
            if first_byte_arrives and on_body is not None:
                on_body()
            await asyncio.sleep(0.02)
            state["now"] -= 1
            return probe.ProbeResult(True, ttfb=0.1, speed_bps=1e9)

        probe._probe_url = fake

        async def run():
            cands = [{"name": f"[TB] r{i}", "url": f"http://x/{i}"}
                     for i in range(n)]
            await probe.probe_race(cands, lambda s: None, ttfb_max=5,
                                   want=n, concurrency=n)
        asyncio.run(run())
        return state["peak"]

    def test_silent_tb_probes_never_exceed_the_plan_limit(self):
        # No body bytes ever arrive (the genuinely-uncached shape): in-flight
        # unmarked-TB probes must stay within TORBOX_MAX_DOWNLOADS.
        self.assertLessEqual(self._peak_concurrency(8, first_byte_arrives=False),
                             probe.TORBOX_MAX_DOWNLOADS)

    def test_slot_frees_at_first_byte_so_cached_links_run_wide(self):
        # Bytes arrive immediately (the mislabeled-but-cached shape, ~96% of
        # unmarked-TB links live): the gate must not serialize the wave.
        self.assertGreater(self._peak_concurrency(8, first_byte_arrives=True),
                           probe.TORBOX_MAX_DOWNLOADS)


if __name__ == "__main__":
    unittest.main()
