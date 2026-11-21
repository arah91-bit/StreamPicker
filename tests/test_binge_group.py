"""bingeGroups follow the pick, not the upstream torrent.

Nuvio (and Stremio) auto-play the next episode's stream that carries the
bingeGroup of the one just played, ahead of list order. Passed through, an
upstream group could override the rank of the next episode.
"""
import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("ADDON_SECRET", "test-secret")

from app import main, picker  # noqa: E402

PACK = "torz:0123456789abcdef0123456789abcdef01234567"


def _row(name, group=None):
    bh = {"filename": f"Example.Show.S01E18.{name}.mkv"}
    if group:
        bh["bingeGroup"] = group
    return {"name": name, "url": f"https://x.example/{name}", "behaviorHints": bh}


class BingeGroupTests(unittest.TestCase):
    def test_the_leader_carries_the_pickers_group_and_no_one_else_does(self):
        rows = [picker._mark(_row("NF", "comet|torbox|3f54"), 1, None),
                picker._mark(_row("HQ", PACK), 2, None),
                _row("raw", PACK)]
        out = picker.binge_groups(rows, "fast")
        self.assertEqual("stream-picker|fast", out[0]["behaviorHints"]["bingeGroup"])
        self.assertNotIn("bingeGroup", out[1]["behaviorHints"])
        self.assertNotIn("bingeGroup", out[2]["behaviorHints"])
        self.assertEqual(rows[0]["behaviorHints"]["filename"],
                         out[0]["behaviorHints"]["filename"])

    def test_cached_rows_are_not_mutated(self):
        rows = [picker._mark(_row("NF", "comet|torbox|3f54"), 1, None),
                _row("HQ", PACK)]
        picker.binge_groups(rows, "fast")
        self.assertEqual("comet|torbox|3f54", rows[0]["behaviorHints"]["bingeGroup"])
        self.assertEqual(PACK, rows[1]["behaviorHints"]["bingeGroup"])

    def test_an_unverified_head_gets_no_group(self):
        out = picker.binge_groups([_row("notice", PACK), _row("HQ", PACK)], "fast")
        self.assertTrue(all("bingeGroup" not in s["behaviorHints"] for s in out))

    def test_each_addon_keeps_its_own_group(self):
        lead = picker._mark(_row("NF"), 1, None)
        self.assertEqual("stream-picker|slow/mob", picker.binge_groups(
            [lead], "slow/mob")[0]["behaviorHints"]["bingeGroup"])

    def test_the_same_group_names_every_episodes_leader(self):
        e18 = picker._mark(_row("NF"), 1, None)
        e19 = picker._mark(_row("YK", "torz:e984"), 1, None)
        self.assertEqual(
            picker.binge_groups([e18], "fast")[0]["behaviorHints"]["bingeGroup"],
            picker.binge_groups([e19], "fast")[0]["behaviorHints"]["bingeGroup"])


class StreamRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_addon_answer_carries_only_the_pickers_group(self):
        rows = [picker._mark(_row("NF", "comet|torbox|3f54"), 1, None),
                picker._mark(_row("HQ", PACK), 2, None)]

        async def fake_pick(*_a, **_k):
            return rows

        with (patch.object(main.picker, "pick", fake_pick),
              patch.object(main.proxy, "wrap", lambda s, *_a, **_k: list(s)),
              patch.object(main.telemetry, "record_served", lambda *_a: None)):
            resp = await main._streams("series", "tt37884717:1:18", "full")
        streams = json.loads(resp.body)["streams"]
        self.assertEqual(["stream-picker|fast", None],
                         [s["behaviorHints"].get("bingeGroup") for s in streams])


if __name__ == "__main__":
    unittest.main()
