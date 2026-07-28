"""Answering a failing-source warning: dismiss, block, or clear.

The warning is only worth having if it can be ended. These cover the three
decisions, the fact that a stale failure stops shouting on its own, and that
a blocked source actually stops being picked rather than merely being hidden.
"""

import os
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("ADDON_SECRET", "test-secret")


def _probe(src, ok, age_h, reason=""):
    return {"kind": "probe", "src": src, "ok": ok,
            "ts": time.time() - age_h * 3600, "reason": reason,
            "id": "tt1", "res": 1080}


class _StoreCase(unittest.TestCase):
    """Each case gets its own store file — these tests write real decisions."""

    def setUp(self):
        from app import source_health
        self.source_health = source_health
        self._tmp = tempfile.TemporaryDirectory(prefix="sp-srchealth-")
        self._patch = mock.patch.multiple(
            source_health,
            _FILE=os.path.join(self._tmp.name, "source_health.json"),
            _store={"dismissed": {}, "blocked": {}})
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()


class DecisionTests(_StoreCase):
    def test_dismissing_silences_the_warning_but_keeps_using_the_source(self):
        self.source_health.dismiss("VidLink")

        self.assertEqual("dismissed", self.source_health.state("VidLink"))
        self.assertTrue(self.source_health.is_dismissed("VidLink"))
        self.assertFalse(self.source_health.is_blocked("VidLink"))

    def test_blocking_supersedes_a_dismissal_rather_than_stacking(self):
        self.source_health.dismiss("VidLink")
        self.source_health.block("VidLink")

        self.assertEqual("blocked", self.source_health.state("VidLink"))
        self.assertFalse(self.source_health.is_dismissed("VidLink"))

    def test_clearing_returns_a_source_to_being_judged_on_its_own_evidence(self):
        self.source_health.block("VidLink")
        self.source_health.clear("VidLink")

        self.assertEqual("", self.source_health.state("VidLink"))
        self.assertFalse(self.source_health.is_blocked("VidLink"))

    def test_a_decision_survives_a_restart(self):
        self.source_health.block("VidLink")
        self.source_health._store = {"dismissed": {}, "blocked": {}}
        self.source_health._load()

        self.assertTrue(self.source_health.is_blocked("VidLink"))

    def test_an_unreadable_store_does_not_stop_the_service_booting(self):
        with open(self.source_health._FILE, "w") as fh:
            fh.write("{ not json")

        self.source_health._load()          # must not raise

        self.assertEqual("", self.source_health.state("VidLink"))

    def test_an_empty_name_is_not_a_decision(self):
        self.source_health.block("   ")

        self.assertEqual([], self.source_health.blocked_names())


class HeroTests(_StoreCase):
    def _broken(self, recs):
        from app import home_ui
        with mock.patch.object(home_ui.usenet_health, "indexer_listing",
                               return_value=[]):
            return home_ui._broken_services(recs)

    def test_a_source_failing_everything_right_now_is_raised(self):
        recs = [_probe("VidLink", False, 1, "HTTP 429") for _ in range(10)]

        self.assertEqual(["VidLink"], self._broken(recs))

    def test_a_source_that_stopped_failing_days_ago_stops_shouting(self):
        # The VidLink case: removed from the config, but its records sit in
        # telemetry for weeks. A dead source has no successes to dilute its
        # 100% failure rate, so without a recency bound it warns forever.
        recs = [_probe("VidLink", False, 24 * 6, "HTTP 429") for _ in range(10)]

        self.assertEqual([], self._broken(recs))

    def test_a_handful_of_failures_is_not_yet_a_dead_source(self):
        recs = [_probe("Flaky", False, 1) for _ in range(3)]

        self.assertEqual([], self._broken(recs))

    def test_a_source_that_still_works_sometimes_is_not_broken(self):
        recs = ([_probe("Mixed", False, 1) for _ in range(9)]
                + [_probe("Mixed", True, 1)])

        self.assertEqual([], self._broken(recs))

    def test_a_dismissed_source_never_raises_the_warning_again(self):
        recs = [_probe("VidLink", False, 1) for _ in range(10)]
        self.source_health.dismiss("VidLink")

        self.assertEqual([], self._broken(recs))

    def test_a_blocked_source_never_raises_the_warning_either(self):
        recs = [_probe("VidLink", False, 1) for _ in range(10)]
        self.source_health.block("VidLink")

        self.assertEqual([], self._broken(recs))

    def test_the_warning_links_each_name_to_its_own_page(self):
        from app import home_ui
        recs = [_probe("VidLink", False, 1) for _ in range(10)]
        with (
            mock.patch.object(home_ui.config, "restart_pending",
                              return_value=False),
            mock.patch.object(home_ui.proxy, "active_stream_details",
                              return_value=[]),
            mock.patch.object(home_ui.usenet_health, "indexer_listing",
                              return_value=[]),
        ):
            hero = home_ui._hero(recs, [], {})

        self.assertIn("/health/source/VidLink", hero)
        self.assertNotIn("href='/health/sources'", hero)


class PickerEnforcementTests(_StoreCase):
    """Blocking has to remove candidates, not just hide a warning."""

    def _stream(self, source):
        return {"name": "The Thing 1080p WEB-DL",
                "title": f"Source: {source}\n2.1 GB",
                "url": "https://example.invalid/stream.mkv"}

    def test_a_blocked_source_stops_being_a_usable_candidate(self):
        from app import picker, telemetry
        stream = self._stream("VidLink")
        # The name enforced against is the one the warning showed.
        self.assertEqual("VidLink",
                         telemetry.source_of(picker._stream_text(stream)))
        profile = picker.PROFILES["full"]
        self.assertTrue(picker._usable(stream, profile, 7200))

        self.source_health.block("VidLink")

        self.assertFalse(picker._usable(stream, profile, 7200))

    def test_blocking_one_source_leaves_every_other_candidate_alone(self):
        from app import picker
        other = self._stream("StremThru")

        self.source_health.block("VidLink")

        self.assertTrue(picker._usable(other, picker.PROFILES["full"], 7200))

    def test_clearing_a_block_puts_the_source_back_in_play(self):
        from app import picker
        stream = self._stream("VidLink")
        self.source_health.block("VidLink")

        self.source_health.clear("VidLink")

        self.assertTrue(picker._usable(stream, picker.PROFILES["full"], 7200))


if __name__ == "__main__":
    unittest.main()
