"""Persistent probe-memory safety and cache-metric regressions."""

import json
import unittest
from unittest import mock

from app import candidate_health, probe, sources, telemetry


def _stream(url: str) -> dict:
    return {
        "name": "Movie 1080p WEB-DL",
        "url": url,
        "behaviorHints": {
            "filename": "Movie.2024.1080p.WEB-DL-GROUP.mkv",
        },
    }


class CandidateHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ctx = telemetry.request_ctx.set({
            "media": "movie", "media_id": "tt1234567", "picker": "fast",
        })
        candidate_health.reset_for_tests()

    def tearDown(self) -> None:
        candidate_health.reset_for_tests()
        telemetry.request_ctx.reset(self.ctx)

    def test_dead_signed_url_does_not_suppress_fresh_url_for_same_release(self):
        old = _stream("https://debrid.invalid/file?token=old-secret")
        fresh = _stream("https://debrid.invalid/file?token=fresh-secret")

        with mock.patch.object(candidate_health, "_save"):
            candidate_health.record_probe(
                old, probe.ProbeResult(False, reason="HTTP 403"))

        self.assertTrue(candidate_health.should_skip(old))
        self.assertFalse(candidate_health.should_skip(fresh))
        persisted = json.dumps(candidate_health._store)
        self.assertNotIn("old-secret", persisted)
        self.assertNotIn(old["url"], persisted)

    def test_success_clears_link_cooldown_and_retains_quality_hint(self):
        stream = _stream("https://debrid.invalid/file?token=one")
        with mock.patch.object(candidate_health, "_save"):
            candidate_health.record_probe(
                stream, probe.ProbeResult(False, reason="ConnectTimeout"))
            candidate_health.record_probe(
                stream, probe.ProbeResult(
                    True, ttfb=0.2, speed_bps=20_000_000,
                    media_bps=12_000_000, media_height=2160,
                    media_codecs="hvc1.2.4.L153.B0"))

        self.assertFalse(candidate_health.should_skip(stream))
        self.assertEqual(2, candidate_health.prior_success(stream))
        hint = candidate_health.quality_hint(stream)
        self.assertEqual(12_000_000, hint["media_bps"])
        self.assertEqual(2160, hint["media_height"])

    def test_transient_failure_self_heals_after_short_retry(self):
        stream = _stream("https://debrid.invalid/file?token=one")
        with mock.patch.object(candidate_health, "_save"), \
                mock.patch("app.candidate_health.time.time", return_value=1_000):
            candidate_health.record_probe(
                stream, probe.ProbeResult(False, reason="ConnectTimeout"))
        with mock.patch(
                "app.candidate_health.time.time",
                return_value=1_000 + candidate_health.TRANSIENT_RETRY + 1):
            self.assertFalse(candidate_health.should_skip(stream))

    def test_verified_season_pack_seeds_sibling_episodes_without_urls(self):
        pack = {
            "url": "https://user:secret@nzbdav.invalid/content/member.mkv",
            "_nzb_pack": True,
            "_nzb_pack_scope": "series:tt1234567:1",
            "_nzb_release_key": "nzb:" + "a" * 64,
            "_nzb_pack_legacy_key": "nzb:" + "b" * 64,
            "_nzb_pack_title": "Example.Show.S01.COMPLETE.1080p.WEB-DL",
            "_nzb_pack_size": 40_000_000_000,
            "_nzb_pack_titles": ["Example Show"],
            "_nzb_pack_year": 2024,
        }
        with mock.patch.object(candidate_health, "_save"):
            candidate_health.remember_verified_pack(pack)

        self.assertEqual(1, len(candidate_health.pack_seeds(
            "tt1234567:1:2")))
        self.assertEqual(1, len(candidate_health.pack_seeds(
            "tt1234567:1:9")))
        self.assertEqual([], candidate_health.pack_seeds(
            "tt1234567:2:1"))
        persisted = json.dumps(candidate_health._store)
        self.assertNotIn("secret", persisted)
        self.assertNotIn(pack["url"], persisted)


class CacheMetricTests(unittest.TestCase):
    def test_cache_aggregate_reports_prewarm_and_revalidation_effectiveness(self):
        rows = [
            {"kind": "cache", "event": "prewarm_intent"},
            {"kind": "cache", "event": "prewarm_ready", "seconds": 2.0},
            {"kind": "cache", "event": "prewarm_ready", "seconds": 8.0},
            {"kind": "cache", "event": "prewarm_cache_hit"},
            {"kind": "cache", "event": "stale_revalidate_ok"},
            {"kind": "cache", "event": "stale_revalidate_fail"},
            {"kind": "cache", "event": "probe_avoided", "count": 3},
            {"kind": "cache", "event": "pack_member_verified"},
            {"kind": "cache", "event": "pack_member_reused"},
            {"kind": "cache", "event": "transport_ok_identity_rejected"},
        ]

        metrics = telemetry.aggregate_cache(rows)

        self.assertEqual(2, metrics["prewarm_ready"])
        self.assertEqual(5.0, metrics["prewarm_seconds_med"])
        self.assertEqual(8.0, metrics["prewarm_seconds_p90"])
        self.assertEqual(50.0, metrics["stale_success_pct"])
        self.assertEqual(3, metrics["probes_avoided"])
        self.assertEqual(1, metrics["pack_members_verified"])
        self.assertEqual(1, metrics["pack_members_reused"])
        self.assertEqual(1, metrics["identity_rejected"])


class SourceUrlCacheTests(unittest.TestCase):
    def test_title_refresh_drops_only_its_completed_url_lists(self):
        target = (sources.FAST, "series", "tt1234567:1:2")
        sibling = (sources.FAST, "series", "tt1234567:1:3")
        raw = {
            target: (1.0, [{"url": "https://old.invalid/signed"}]),
            sibling: (2.0, [{"url": "https://other.invalid/current"}]),
        }
        with mock.patch.object(sources, "_raw", raw):
            removed = sources.invalidate("series", "tt1234567:1:2")

            self.assertEqual(1, removed)
            self.assertNotIn(target, sources._raw)
            self.assertIn(sibling, sources._raw)


if __name__ == "__main__":
    unittest.main()
