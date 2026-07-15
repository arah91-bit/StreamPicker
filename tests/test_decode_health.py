"""Learned decode compatibility: rejections teach, plays exonerate, and the
picker demotes what the household's players provably can't open."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("ADDON_SECRET", "test-secret")

from app import decode_health, picker, probe


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(decode_health._store)
        decode_health._store.clear()
        self._save = decode_health._save
        decode_health._save = lambda: None      # no disk writes from tests

    def tearDown(self):
        decode_health._save = self._save
        decode_health._store.clear()
        decode_health._store.update(self._saved)

    def test_two_rejections_with_no_plays_make_a_codec_bad(self):
        decode_health.record_reject(["flac"], "h264", label="Bad Remux")
        self.assertEqual(frozenset(), decode_health.bad_keys())
        decode_health.record_reject(["flac"], "h264")
        self.assertIn("a:flac", decode_health.bad_keys())
        # the video codec was struck too, but H.264 plays constantly…
        self.assertIn("v:h264", decode_health.bad_keys())
        decode_health.record_play([], "h264")
        # …so one successful play exonerates it.
        self.assertNotIn("v:h264", decode_health.bad_keys())
        self.assertIn("a:flac", decode_health.bad_keys())

    def test_ambiguous_multi_audio_blame_self_corrects(self):
        # A rejected file carries [flac, ac3]: both struck. Another file with
        # ac3 plays fine → only flac stays suspect.
        decode_health.record_reject(["flac", "ac3"], "h264")
        decode_health.record_reject(["flac", "ac3"], "h264")
        self.assertIn("a:ac3", decode_health.bad_keys())
        decode_health.record_play(["ac3"], "h264")
        self.assertNotIn("a:ac3", decode_health.bad_keys())
        self.assertIn("a:flac", decode_health.bad_keys())

    def test_suspect_prefers_sniffed_codecs_over_name(self):
        decode_health.record_reject(["flac"], "")
        decode_health.record_reject(["flac"], "")
        # sniffed: file is actually AAC, whatever the name says
        self.assertFalse(decode_health.suspect(
            "Movie.2024.FLAC.1080p", acodecs=["aac"]))
        # sniffed FLAC is authoritative
        self.assertTrue(decode_health.suspect("Movie 1080p", acodecs=["flac"]))
        # no sniff: only an explicit name declaration counts
        self.assertTrue(decode_health.suspect("Movie.2024.FLAC.1080p"))
        self.assertFalse(decode_health.suspect("Movie.2024.DTS.1080p"))

    def test_empty_store_is_free(self):
        self.assertFalse(decode_health.suspect("Movie.2024.FLAC.1080p"))


class RankingTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(decode_health._store)
        decode_health._store.clear()
        self._save = decode_health._save
        decode_health._save = lambda: None
        decode_health.record_reject(["flac"], "")
        decode_health.record_reject(["flac"], "")

    def tearDown(self):
        decode_health._save = self._save
        decode_health._store.clear()
        decode_health._store.update(self._saved)

    def test_undecodable_4k_sorts_below_clean_1080p(self):
        bad_4k = {"name": "Remux 4K", "url": "u1",
                  "behaviorHints": {"videoSize": 57_000_000_000},
                  "_acodecs": ["flac"]}
        clean_1080 = {"name": "WEB-DL 1080p", "url": "u2",
                      "behaviorHints": {"videoSize": 8_000_000_000}}
        picker._annotate_quality([bad_4k, clean_1080], 7200)
        ranked = sorted([bad_4k, clean_1080], key=picker._quality_key,
                        reverse=True)
        self.assertEqual("u2", ranked[0]["url"])

    def test_name_declared_flac_is_demoted_without_a_sniff(self):
        bad = {"name": "Remux 4K FLAC", "url": "u1",
               "behaviorHints": {"videoSize": 57_000_000_000}}
        clean = {"name": "WEB-DL 1080p", "url": "u2",
                 "behaviorHints": {"videoSize": 8_000_000_000}}
        picker._annotate_quality([bad, clean], 7200)
        ranked = sorted([bad, clean], key=picker._quality_key, reverse=True)
        self.assertEqual("u2", ranked[0]["url"])

    def test_sniffed_codecs_flow_from_probe_result_to_ranking(self):
        s = {"name": "Remux 4K", "url": "u1",
             "behaviorHints": {"videoSize": 57_000_000_000}}
        r = probe.ProbeResult(True, ttfb=0.2, speed_bps=30_000_000,
                              acodecs=("flac", "ac3"), vcodec="h264")
        picker._apply_probe_quality(s, r, 7200)
        self.assertEqual(["flac", "ac3"], s["_acodecs"])
        self.assertEqual(0, picker._decode_ok(s))
        clean = picker._strip_internal(s)
        self.assertNotIn("_acodecs", clean)
        self.assertNotIn("_vcodec_real", clean)

    def test_probe_speed_still_beats_decode_suspect_nothing_else(self):
        # An undecodable release is demoted, never dropped: alone, it serves.
        bad = {"name": "Remux 4K FLAC", "url": "u1",
               "behaviorHints": {"videoSize": 57_000_000_000}}
        self.assertTrue(picker._usable(
            bad, picker.PROFILES["full"], 7200))


class CooldownTests(unittest.TestCase):
    def test_custom_duration_and_expiry_semantics(self):
        from app import reputation
        with patch.object(reputation, "_save_cooldowns", lambda: None):
            reputation.cooldown("sig-short", 0.0)     # expires immediately
            reputation.cooldown("sig-long", 3600.0)
            self.assertFalse(reputation.cooled("sig-short"))
            self.assertTrue(reputation.cooled("sig-long"))
            reputation._cooldowns.pop("sig-short", None)
            reputation._cooldowns.pop("sig-long", None)

    def test_cooldowns_survive_a_reload(self):
        import tempfile
        from app import reputation
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "cooldowns.json")
            with patch.object(reputation, "_CD_FILE", path):
                reputation.cooldown("sig-persist", 3600.0)
                reputation._cooldowns.clear()
                self.assertFalse(reputation.cooled("sig-persist"))
                reputation._load_cooldowns()
                self.assertTrue(reputation.cooled("sig-persist"))
                reputation._cooldowns.pop("sig-persist", None)


if __name__ == "__main__":
    unittest.main()
