"""Contract tests for persistent direct-Usenet health and learning.

These tests deliberately use only the Python standard library.  They exercise
the public API rather than SQLite internals so the store remains free to change
its schema and maintenance strategy.
"""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path

from app import telemetry, usenet_health
from app.usenet_health import HealthStore, classify_reason, release_key


class FakeClock:
    """Mutable wall clock supporting both callable and ``.time()`` styles."""

    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _field(status, name: str):
    """Read the small release-status value object without dict-locking it."""

    if isinstance(status, Mapping):
        return status[name]
    return getattr(status, name)


class ReleaseKeyTests(unittest.TestCase):
    def test_release_key_is_normalized_and_stable(self) -> None:
        size = 4_000_000_000
        dotted = release_key("Show.Name.S01E01.1080p.WEB-DL-GROUP", size)
        spaced = release_key("show name s01e01 1080P web dl group", size)

        self.assertTrue(dotted)
        self.assertEqual(dotted, spaced)
        self.assertEqual(dotted,
                         release_key("Show.Name.S01E01.1080p.WEB-DL-GROUP",
                                     size))

    def test_release_key_distinguishes_release_and_material_size_changes(self) -> None:
        one = release_key("Show.Name.S01E01.1080p.WEB-DL-GROUP", 4_000_000_000)
        next_ep = release_key("Show.Name.S01E02.1080p.WEB-DL-GROUP",
                              4_000_000_000)
        different_file = release_key("Show.Name.S01E01.1080p.WEB-DL-GROUP",
                                     9_000_000_000)

        self.assertNotEqual(one, next_ep)
        self.assertNotEqual(one, different_file)

    def test_scoped_release_key_isolates_same_name_titles_and_episodes(self) -> None:
        title = "The.Office.S01E01.1080p.WEB-DL-GROUP"
        size = 4_000_000_000
        uk = release_key(title, size, "series", "tt0290978:1:1")
        us = release_key(title, size, "series", "tt0386676:1:1")
        next_episode = release_key(title, size, "series", "tt0290978:1:2")
        movie = release_key(title, size, "movie", "tt0290978")

        self.assertEqual(4, len({uk, us, next_episode, movie}))
        self.assertEqual(
            uk, release_key(title, size, "tv", "tt0290978:01:001"))
        self.assertEqual(
            uk, release_key(title, size, "series", "tt290978:1:1"))
        # Legacy two-argument identity remains stable for existing databases.
        self.assertEqual(release_key(title, size), release_key(title, size))

    def test_show_scope_is_distinct_from_season_and_episode(self) -> None:
        show = usenet_health._content_scope("series", "tt0290978")
        season = usenet_health._content_scope("series", "tt0290978:1")
        episode = usenet_health._content_scope("series", "tt0290978:1:2")
        self.assertEqual("series:tt290978", show)
        self.assertEqual(3, len({show, season, episode}))

    def test_probe_reason_classification_separates_decisive_and_transient(self) -> None:
        for reason in ("missing articles", "MISSING ARTICLES", "HTTP 404",
                       "not-video", "not-media", "wrong-title", "wrong-year",
                       "wrong-imdb", "wrong-season"):
            with self.subTest(reason=reason):
                self.assertEqual("hard", classify_reason(reason))

        for reason in ("ReadTimeout: ", "connect-fail", "HTTP 502",
                       "throughput far below need"):
            with self.subTest(reason=reason):
                self.assertEqual("transient", classify_reason(reason))


class HealthStoreTests(unittest.TestCase):
    MAX_BYTES = 2 * 1024 * 1024

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "usenet-health.sqlite3")
        self.clock = FakeClock()
        self.store = HealthStore(self.path, max_bytes=self.MAX_BYTES,
                                 clock=self.clock)
        self.key = release_key("Example.Show.S01E01.2160p.WEB-DL-GROUP",
                               18_000_000_000)

    def tearDown(self) -> None:
        if self.store is not None:
            self.store.close()
        self.tmp.cleanup()

    def _probe(self, *, key: str | None = None, ok: bool, reason: str = "",
               attempt_id: str, indexers: list[str] | None = None) -> None:
        self.store.record_probe(
            key or self.key,
            "Example.Show.S01E01.2160p.WEB-DL-GROUP",
            indexers or ["NZBgeek"],
            ok,
            reason,
            attempt_id,
        )

    def _advance_past_retry(self, key: str | None = None) -> None:
        status = self.store.release_status(key or self.key)
        retry_at = float(_field(status, "retry_at"))
        self.clock.now = max(self.clock.now, retry_at + 0.001)

    def test_state_and_learned_indexer_stats_survive_reopen(self) -> None:
        self.store.record_search("NZBgeek", True, results=23, latency=0.4)
        self.store.record_fetch("NZBgeek", True)
        self._probe(ok=False, reason="missing articles", attempt_id="try-1")

        before_status = self.store.release_status(self.key)
        before_score = self.store.indexer_score("NZBgeek")
        before_samples = self.store.indexer_samples("NZBgeek")
        self.assertTrue(self.store.should_skip(self.key))
        self.store.close()
        self.store = HealthStore(self.path, max_bytes=self.MAX_BYTES,
                                 clock=self.clock)

        after_status = self.store.release_status(self.key)
        self.assertEqual(_field(before_status, "hard_failures"),
                         _field(after_status, "hard_failures"))
        self.assertEqual(_field(before_status, "blocked"),
                         _field(after_status, "blocked"))
        self.assertEqual(before_samples,
                         self.store.indexer_samples("NZBgeek"))
        self.assertAlmostEqual(before_score,
                               self.store.indexer_score("NZBgeek"))
        self.assertTrue(self.store.should_skip(self.key))

    def test_duplicate_attempt_is_idempotent(self) -> None:
        self._probe(ok=False, reason="missing articles", attempt_id="same")
        first_status = self.store.release_status(self.key)
        first_samples = self.store.indexer_samples("NZBgeek")

        # Idempotency must survive a restart; retries and sibling pickers can
        # replay an already-persisted attempt after the process is rebuilt.
        self.store.close()
        self.store = HealthStore(self.path, max_bytes=self.MAX_BYTES,
                                 clock=self.clock)
        self._probe(ok=False, reason="missing articles", attempt_id="same")
        second_status = self.store.release_status(self.key)

        self.assertEqual(1, _field(second_status, "hard_failures"))
        self.assertEqual(_field(first_status, "hard_failures"),
                         _field(second_status, "hard_failures"))
        self.assertEqual(first_samples,
                         self.store.indexer_samples("NZBgeek"))

    def test_old_attempt_remains_idempotent_after_many_newer_attempts(self) -> None:
        self._probe(ok=False, reason="missing articles", attempt_id="oldest")
        for i in range(12):
            self._probe(ok=False, reason="HTTP 502",
                        attempt_id=f"newer-{i}")
        before = self.store.release_status(self.key)
        before_samples = self.store.indexer_samples("NZBgeek")

        # The former eight-marker cap evicted "oldest" here and replaying it
        # produced another indexer sample (and potentially another hard strike).
        self._probe(ok=False, reason="missing articles", attempt_id="oldest")

        self.assertEqual(before, self.store.release_status(self.key))
        self.assertEqual(before_samples,
                         self.store.indexer_samples("NZBgeek"))

    def test_transient_failure_cannot_shorten_hard_retry(self) -> None:
        self._probe(ok=False, reason="missing articles", attempt_id="hard")
        hard_retry = _field(self.store.release_status(self.key), "retry_at")
        self.clock.advance(60)

        self._probe(ok=False, reason="HTTP 502", attempt_id="transient")

        self.assertGreaterEqual(
            _field(self.store.release_status(self.key), "retry_at"), hard_retry)

    def test_first_hard_failure_cools_down_second_separated_failure_blocks(self) -> None:
        self._probe(ok=False, reason="missing articles", attempt_id="try-1")
        first = self.store.release_status(self.key)

        self.assertEqual(1, _field(first, "hard_failures"))
        self.assertFalse(_field(first, "blocked"))
        self.assertTrue(self.store.should_skip(self.key))

        self._advance_past_retry()
        self.assertFalse(self.store.should_skip(self.key))
        self._probe(ok=False, reason="HTTP 404", attempt_id="try-2")
        second = self.store.release_status(self.key)

        self.assertEqual(2, _field(second, "hard_failures"))
        self.assertTrue(_field(second, "blocked"))
        self.assertTrue(self.store.should_skip(self.key))

        # Passing the ordinary retry/cooldown point must not silently turn a
        # two-strike hard block back into a normal retry.
        self._advance_past_retry()
        self.assertTrue(self.store.should_skip(self.key))

    def test_transient_failures_retry_but_never_form_a_hard_block(self) -> None:
        reasons = ("ReadTimeout: ", "HTTP 502", "connect-fail")
        for i, reason in enumerate(reasons):
            self._probe(ok=False, reason=reason, attempt_id=f"transient-{i}")
            status = self.store.release_status(self.key)
            self.assertEqual(0, _field(status, "hard_failures"))
            self.assertFalse(_field(status, "blocked"))
            self.assertTrue(self.store.should_skip(self.key))
            self._advance_past_retry()
            self.assertFalse(self.store.should_skip(self.key))

    def test_verified_success_rehabilitates_a_hard_block(self) -> None:
        self._probe(ok=False, reason="missing articles", attempt_id="hard-1")
        self._advance_past_retry()
        self._probe(ok=False, reason="not-video", attempt_id="hard-2")
        self.assertTrue(_field(self.store.release_status(self.key), "blocked"))

        self._probe(ok=True, attempt_id="verified-good")
        status = self.store.release_status(self.key)

        self.assertFalse(_field(status, "blocked"))
        self.assertEqual(0, _field(status, "hard_failures"))
        self.assertFalse(self.store.should_skip(self.key))

    def test_smoothed_score_prefers_proven_reliability_over_one_lucky_sample(self) -> None:
        cold_score = self.store.indexer_score("unseen")
        self.assertEqual(cold_score, self.store.indexer_score("also-unseen"))

        # Eight of ten verified probes is meaningful evidence.  A raw success
        # percentage would incorrectly put the one-for-one indexer first; a
        # smoothed/confidence-aware score should not.
        for i in range(10):
            key = release_key(f"Proven.Show.S01E{i + 1:02}.1080p-GROUP",
                              4_000_000_000 + i)
            self.store.record_probe(
                key, f"proven-{i}", ["proven"], i < 8,
                "" if i < 8 else "missing articles", f"proven-{i}")

        lucky_key = release_key("Lucky.Show.S01E01.1080p-GROUP", 4_000_000_000)
        self.store.record_probe(lucky_key, "lucky", ["lucky"], True, "",
                                "lucky-1")

        self.assertEqual(10, self.store.indexer_samples("proven"))
        self.assertEqual(1, self.store.indexer_samples("lucky"))
        self.assertGreater(self.store.indexer_score("proven"),
                           self.store.indexer_score("lucky"))
        ordered = sorted(("lucky", "proven"),
                         key=self.store.indexer_score, reverse=True)
        self.assertEqual(["proven", "lucky"], ordered)

    def test_fetch_score_isolates_broken_download_endpoints(self) -> None:
        # An indexer whose downloads always 403 (seen live: 0 ok / 80 fail)
        # must rank last for NZB fetching even if its releases play fine.
        for i in range(30):
            self.store.record_fetch("deadfetch", False)
            self.store.record_fetch("goodfetch", True)
        self.assertLess(self.store.fetch_score("deadfetch"), 0.1)
        self.assertGreater(self.store.fetch_score("goodfetch"), 0.8)
        self.assertFalse(self.store.fetch_allowed("deadfetch"))
        self.assertTrue(self.store.fetch_allowed("goodfetch"))
        self.store.clear_fetch_health("deadfetch")
        self.assertTrue(self.store.fetch_allowed("deadfetch"))
        self.assertEqual(0.5, self.store.fetch_score("deadfetch"))
        # cold start stays neutral so new indexers aren't punished
        self.assertEqual(0.5, self.store.fetch_score("brand-new"))
        self.assertTrue(self.store.fetch_allowed("brand-new"))

    def test_search_and_fetch_evidence_contributes_to_indexer_learning(self) -> None:
        for _ in range(5):
            self.store.record_search("responsive", True, results=20, latency=0.3)
            self.store.record_fetch("responsive", True)
            self.store.record_search("broken", False, results=0, latency=8.0)
            self.store.record_fetch("broken", False)

        self.assertGreater(self.store.indexer_samples("responsive"), 0)
        self.assertGreater(self.store.indexer_samples("broken"), 0)
        self.assertGreater(self.store.indexer_score("responsive"),
                           self.store.indexer_score("broken"))

    def test_store_file_honors_configured_bound_after_compaction(self) -> None:
        self.store.close()
        self.store = None
        max_bytes = 256 * 1024
        bounded = HealthStore(self.path, max_bytes=max_bytes, clock=self.clock)
        newest_key = ""
        try:
            for i in range(1_500):
                newest_key = release_key(
                    f"Archive.Show.S{i // 100 + 1:02}E{i % 100 + 1:02}.1080p-"
                    f"{'X' * 100}.{i}", 3_000_000_000 + i)
                bounded.record_probe(newest_key, f"release-{'x' * 160}-{i}",
                                     ["archive"], True, "", f"archive-{i}")
        finally:
            bounded.close()

        total = sum(
            p.stat().st_size
            for p in (Path(self.path), Path(self.path + "-wal"),
                      Path(self.path + "-shm"))
            if p.exists()
        )
        # SQLite allocates in pages, so allow a small number of pages beyond
        # the logical budget while still proving growth is actually bounded.
        self.assertLessEqual(total, max_bytes + 16 * 1024)

        self.store = HealthStore(self.path, max_bytes=max_bytes,
                                 clock=self.clock)
        self.assertIsNotNone(self.store.release_status(newest_key))


class MetadataSafetyRegressionTests(unittest.TestCase):
    def test_low_information_streams_do_not_share_a_blockable_signature(self) -> None:
        first = telemetry.signature({
            "url": "https://user:secret@dav.example/content/one/file",
        })
        second = telemetry.signature({
            "url": "https://user:secret@dav.example/content/two/file",
        })

        self.assertNotEqual("gr0s0", first)
        self.assertNotEqual("gr0s0", second)
        # Returning an empty signature is safe because the reputation layer
        # ignores it; a non-empty fallback must be stream-specific.
        self.assertTrue(not first or not second or first != second)
        self.assertNotIn("secret", first)
        self.assertNotIn("secret", second)

    def test_clean_output_strips_all_private_nzb_annotations(self) -> None:
        from app import picker

        stream = {
            "name": "NZB stream",
            "url": "https://dav.example/content/file.mkv",
            "_nzb_release_key": "private-release-key",
            "_nzb_indexer": "private-indexer",
            "_nzb_indexers": ["private-indexer", "private-mirror"],
            "_qbps": 10,
        }

        cleaned = picker.clean_output([stream])[0]

        self.assertFalse(any(k.startswith("_nzb_") for k in cleaned))
        self.assertNotIn("_qbps", cleaned)
        self.assertIn("_nzb_release_key", stream)  # input/cache is not mutated


if __name__ == "__main__":
    unittest.main()
