"""TorBox auto-cache lane (app.tbcache, TB_AUTO_CACHE).

Fires only when a pick's outcome is weak: nothing verified, or the best
verified stream sits a resolution tier below what an uncached TorBox torrent
offers. Restraint is the point — per-title cooldown with tried-release
memory, global slot pacing against TORBOX_MAX_DOWNLOADS, and only [TB⬇️]
entries (never RD, never already-cached) are ever triggered.
"""

import asyncio
import base64
import json
import os
import tempfile
import time
import unittest

os.environ.setdefault("ADDON_SECRET", "test-secret")

from app import probe, tbcache


def _b64cfg(services=({"service": "torbox", "apiKey": "k"},)):
    cfg = {"maxSize": 0, "cachedOnly": True, "debridServices": list(services)}
    return base64.b64encode(json.dumps(cfg).encode()).decode()


def _cand(name="[TB⬇️] Comet 2160p", size=20e9, filename="X.2160p.mkv"):
    return {"name": name, "url": "http://comet/playback/1",
            "description": f"📄 {filename}",
            "behaviorHints": {"filename": filename, "videoSize": size}}


class UncachedBaseTests(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.get("FAST_BASE_URL")
        tbcache._base_cache = None

    def tearDown(self):
        if self._env is None:
            os.environ.pop("FAST_BASE_URL", None)
        else:
            os.environ["FAST_BASE_URL"] = self._env
        tbcache._base_cache = None

    def test_mints_cachedonly_false_variant_in_comet_b64_dialect(self):
        os.environ["FAST_BASE_URL"] = f"https://comet.example/{_b64cfg()}"
        got = tbcache._uncached_base()
        self.assertIsNotNone(got)
        prefix, b64 = got.rsplit("/", 1)
        self.assertEqual("https://comet.example", prefix)
        # Comet decodes standard base64 and needs padding intact.
        self.assertEqual(0, len(b64) % 4)
        cfg = json.loads(base64.b64decode(b64))
        self.assertIs(False, cfg["cachedOnly"])
        self.assertEqual("torbox", cfg["debridServices"][0]["service"])

    def test_requires_a_torbox_store_in_the_config(self):
        os.environ["FAST_BASE_URL"] = (
            f"https://comet.example/"
            f"{_b64cfg(services=({'service': 'realdebrid', 'apiKey': 'k'},))}")
        self.assertIsNone(tbcache._uncached_base())

    def test_non_comet_or_garbage_config_disables_the_lane(self):
        os.environ["FAST_BASE_URL"] = "https://comet.example/not-base64!!"
        self.assertIsNone(tbcache._uncached_base())
        os.environ.pop("FAST_BASE_URL", None)
        tbcache._base_cache = None
        self.assertIsNone(tbcache._uncached_base())


class CandidateFilterTests(unittest.TestCase):
    def test_only_uncached_torbox_entries_qualify(self):
        self.assertTrue(tbcache._is_uncached_tb(_cand("[TB⬇️] Comet 2160p")))
        self.assertFalse(tbcache._is_uncached_tb(_cand("[TB⚡] Comet 2160p")))
        self.assertFalse(tbcache._is_uncached_tb(_cand("[TB+] AIO 2160p")))
        self.assertFalse(tbcache._is_uncached_tb(_cand("[RD⬇️] Comet 2160p")))
        self.assertFalse(tbcache._is_uncached_tb(_cand("Free.Addon.2160p")))


class MaybeCacheTests(unittest.TestCase):
    """Decision flow with the search and trigger stubbed out."""

    RUNTIME = 7200.0

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._env = os.environ.get("FAST_BASE_URL")
        self._orig = (tbcache.ENABLED, tbcache._FILE, tbcache._state,
                      tbcache._uncached_candidates, tbcache._trigger,
                      tbcache._base_cache, dict(tbcache._checked))
        os.environ["FAST_BASE_URL"] = f"http://comet/{_b64cfg()}"
        tbcache.ENABLED = True
        tbcache._FILE = self._tmp.name
        tbcache._state = None
        tbcache._base_cache = None
        tbcache._checked.clear()
        self.triggered = []

        async def fake_trigger(s, media_id):
            self.triggered.append(s)
        tbcache._trigger = fake_trigger
        self.cands = []

        async def fake_search(media, media_id):
            return list(self.cands)
        tbcache._uncached_candidates = fake_search

    def tearDown(self):
        if self._env is None:
            os.environ.pop("FAST_BASE_URL", None)
        else:
            os.environ["FAST_BASE_URL"] = self._env
        (tbcache.ENABLED, tbcache._FILE, tbcache._state,
         tbcache._uncached_candidates, tbcache._trigger,
         tbcache._base_cache, checked) = self._orig
        tbcache._checked.clear()
        tbcache._checked.update(checked)
        os.unlink(self._tmp.name)

    def _run(self, best_res, media_id="tt1:1:2"):
        asyncio.run(tbcache.maybe_cache("series", media_id, best_res,
                                        self.RUNTIME))

    def test_nothing_verified_triggers_the_best_release(self):
        self.cands = [_cand("[TB⬇️] Comet 1080p", 4e9, "A.1080p.mkv"),
                      _cand("[TB⬇️] Comet 2160p", 20e9, "B.2160p.mkv")]
        self._run(best_res=0)
        self.assertEqual(1, len(self.triggered))
        self.assertEqual("B.2160p.mkv",
                         self.triggered[0]["behaviorHints"]["filename"])

    def test_fires_only_above_the_verified_resolution_tier(self):
        self.cands = [_cand("[TB⬇️] Comet 2160p", 20e9, "B.2160p.mkv")]
        self._run(best_res=2160, media_id="tt1:1:3")
        self.assertEqual([], self.triggered)      # equal tier: no upgrade
        tbcache._checked.clear()
        self._run(best_res=1080, media_id="tt1:1:4")
        self.assertEqual(1, len(self.triggered))  # 2160 beats verified 1080

    def test_title_cooldown_blocks_and_retry_skips_tried_release(self):
        self.cands = [_cand("[TB⬇️] Comet 2160p", 20e9, "B.2160p.mkv"),
                      _cand("[TB⬇️] Comet 2160p", 18e9, "C.2160p.mkv")]
        self._run(best_res=0)
        tbcache._checked.clear()
        self._run(best_res=0)
        self.assertEqual(1, len(self.triggered))  # cooldown holds
        # cooldown expired: the already-tried release is skipped
        key = "series:tt1:1:2"
        tbcache._state["titles"][key]["ts"] -= tbcache.TITLE_COOLDOWN + 1
        tbcache._state["fired"] = []
        tbcache._checked.clear()
        self._run(best_res=0)
        self.assertEqual(2, len(self.triggered))
        self.assertEqual("C.2160p.mkv",
                         self.triggered[1]["behaviorHints"]["filename"])

    def test_slot_pacing_assumes_plan_limit_is_busy(self):
        tbcache._load()["fired"] = [time.time()] * probe.TORBOX_MAX_DOWNLOADS
        self.cands = [_cand()]
        self._run(best_res=0)
        self.assertEqual([], self.triggered)

    def test_state_survives_reload(self):
        self.cands = [_cand()]
        self._run(best_res=0)
        tbcache._state = None                      # simulate restart
        self.assertIn("series:tt1:1:2", tbcache._load()["titles"])


if __name__ == "__main__":
    unittest.main()
