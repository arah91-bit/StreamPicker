"""Behavioural tests for the shark theme's Feeding Frenzy mini-game.

The game ships as a JS blob inside ``uitheme.SKIN_JS``, so asserting on the
source would only ever prove that a string is present. Instead we run the
real script in Node against a DOM stub and a virtual clock
(``tests/shark_harness.mjs``) and assert on what a player would actually
experience: it gets harder, it can end, and your best run is kept.

Node is a dev-machine dependency only — the deployed image is Python — so
these skip cleanly where it is absent.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HARNESS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "shark_harness.mjs")


def _node() -> str | None:
    node = shutil.which("node") or shutil.which("nodejs")
    if node:
        return node
    # nvm installs land outside PATH for non-login shells
    root = os.path.expanduser("~/.nvm/versions/node")
    if os.path.isdir(root):
        for ver in sorted(os.listdir(root), reverse=True):
            cand = os.path.join(root, ver, "bin", "node")
            if os.access(cand, os.X_OK):
                return cand
    return None


@unittest.skipUnless(_node(), "node is needed to run the game harness")
class SharkGameTest(unittest.TestCase):
    """One Node run drives every scenario; each test reads one section."""

    report: dict = {}

    @classmethod
    def setUpClass(cls):
        from app import uitheme
        js = re.sub(r"^<script>", "", uitheme.SKIN_JS)
        js = re.sub(r"</script>$", "", js)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "skin.js")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(js)
            proc = subprocess.run([_node(), HARNESS, path],
                                  capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            raise AssertionError(
                f"game harness crashed (rc={proc.returncode}):\n{proc.stderr}")
        cls.report = json.loads(proc.stdout)

    def test_a_good_run_is_not_cut_short_by_a_clock(self):
        """There is no round timer any more: the only thing that ends a run
        is running out of teeth. A competent player must still be playing
        well past any plausible fixed round length."""
        feed = self.report["feed"]
        self.assertTrue(self.report["startedRunning"], "poking the shark starts a round")
        self.assertTrue(feed["running"],
                        f"round ended on its own after {feed['elapsedSeconds']}s")
        self.assertGreaterEqual(feed["elapsedSeconds"], 90)

    def test_the_round_gets_harder_the_longer_you_last(self):
        """Levels climb with fish eaten and never go backwards, and each
        level puts more in the water — the ramp *is* the ending condition,
        so a flat curve is a game that never ends."""
        feed = self.report["feed"]
        levels = [m["level"] for m in feed["marks"]]
        self.assertEqual(levels, sorted(levels), f"level went backwards: {levels}")
        self.assertGreater(levels[-1], levels[0])
        scores = [m["score"] for m in feed["marks"]]
        self.assertEqual(scores, sorted(scores), f"score went backwards: {scores}")

        windows = self.report["ramp"]["windows"]
        first, last = windows[0]["perSecond"], windows[-1]["perSecond"]
        self.assertGreater(last, first * 1.4,
                           f"spawn pressure barely moved: {windows}")

    def test_a_run_actually_ends_when_the_teeth_run_out(self):
        """Three teeth, and junk costs one. Losing the last must stop the
        round cleanly — no leftover prey drifting over the settings page."""
        lose = self.report["lose"]
        self.assertEqual(3, lose["teeth0"])
        self.assertFalse(lose["running"], "eating junk forever never ended the round")
        self.assertEqual(0, lose["preyLeft"], "prey left on screen after game over")

    def test_your_best_run_is_remembered(self):
        """The high score is the only thing that persists, so it has to
        survive the ways a round actually ends — including quitting, which
        used to throw the score away."""
        lose, quit_, keep = (self.report["lose"], self.report["quit"],
                             self.report["keepsBest"])
        self.assertGreaterEqual(lose["best"], lose["scored"])
        self.assertEqual(quit_["score"], quit_["best"],
                         "quitting mid-round dropped the score")
        self.assertEqual(keep["banked"], keep["after"],
                         "a worse second run overwrote the best")

    def test_a_hidden_tab_pauses_instead_of_filling_the_sea(self):
        """requestAnimationFrame stops in a background tab but setInterval
        does not, so without an explicit pause the shark freezes while prey
        keep spawning — you come back to a wall of fish."""
        p = self.report["pause"]
        self.assertGreater(p["beforeHide"], 0, "nothing was in the water to begin with")
        self.assertEqual(0, p["clearedOnHide"])
        self.assertEqual(0, p["spawnedWhileHidden"], "kept spawning in a hidden tab")
        self.assertTrue(p["pausedFlag"], "HUD did not show the paused state")
        self.assertGreater(p["spawnedAfterShow"], 0, "never resumed")
        self.assertFalse(p["resumedFlag"])
        self.assertTrue(p["stillRunning"])

    def test_wasd_never_eats_a_keystroke_meant_for_a_form(self):
        """The game runs on top of a live settings page and steers with
        WASD, which are also just letters."""
        keys = self.report["keys"]
        self.assertEqual(0, keys["preventedInFields"],
                         "swallowed typing aimed at an input")
        self.assertEqual(1, keys["preventedOnPage"],
                         "steering keys stopped working")


if __name__ == "__main__":
    unittest.main()
