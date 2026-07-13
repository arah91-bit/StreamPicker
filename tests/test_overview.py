"""Overview page: real numbers from telemetry records, and a graceful empty
state. The point of the page is that nothing is invented, so the tests pin the
arithmetic (GB, hours, usenet strike rate) to the records fed in."""

import os
import time
import unittest

os.environ.setdefault("ADDON_SECRET", "test-secret")

from app import overview


def _play(mb, secs, **kw):
    r = {"kind": "play", "ts": time.time(), "mb": mb, "secs": secs,
         "id": kw.get("id", "tt1"), "res": kw.get("res", 1080),
         "debrid": kw.get("debrid", "TB+"), "cached": kw.get("cached", True),
         "hdr": kw.get("hdr", "sdr"), "codec": kw.get("codec", "hevc"),
         "switched": kw.get("switched", False),
         "reconnects": kw.get("reconnects", 0),
         "mbps": kw.get("mbps", mb / secs if secs else 0)}
    return r


class OverviewTests(unittest.TestCase):
    def test_empty_state(self):
        page = overview.render([], "test-secret")
        self.assertIn("Nothing streamed", page)
        self.assertNotIn("undefined", page)

    def test_totals_are_real(self):
        recs = [_play(5000, 3600, id="tt1"), _play(3000, 1800, id="tt2")]
        page = overview.render(recs, "test-secret")
        self.assertIn("8.0", page)          # 8000 MB → 8.0 GB
        self.assertIn("GB", page)
        self.assertIn("1.5", page)          # 5400 s → 1.5 hours
        self.assertIn("2</b> titles", page)

    def test_terabyte_scaling(self):
        recs = [_play(1_500_000, 3600)]     # 1500 GB → 1.50 TB
        page = overview.render(recs, "test-secret")
        self.assertIn("1.50", page)
        self.assertIn("TB", page)

    def test_failover_saves_counted(self):
        recs = [_play(100, 60, switched=True), _play(100, 60, switched=False),
                _play(100, 60, switched=True)]
        page = overview.render(recs, "test-secret")
        self.assertIn("failover saves", page)

    def test_usenet_report_card(self):
        recs = [
            {"kind": "probe", "lane": "nzb", "ok": True,
             "fetch_indexer": "nzbgeek", "ts": time.time()},
            {"kind": "probe", "lane": "nzb", "ok": False,
             "fetch_indexer": "nzbgeek", "ts": time.time()},
            {"kind": "nzb_failure", "reason": "missing-articles",
             "ts": time.time()},
            {"kind": "nzb_failure", "reason": "wrong-episode",
             "ts": time.time()},
        ]
        page = overview.render(recs, "test-secret")
        self.assertIn("Usenet strike rate", page)
        self.assertIn("50%", page)                       # 1 of 2 worked
        self.assertIn("nzbgeek", page)
        self.assertIn("Missing articles", page)          # friendly reason
        self.assertIn("Wrong episode returned", page)

    def test_superlatives_use_names_from_served(self):
        recs = [
            {"kind": "served", "id": "tt9", "label": "🎬 Big Movie 2024 2160p"},
            _play(9000, 7200, id="tt9"),
            _play(100, 60, id="tt1"),
        ]
        page = overview.render(recs, "test-secret")
        self.assertIn("Biggest single stream", page)
        self.assertIn("Big Movie 2024", page)            # name, not tt9
        self.assertNotIn("🎬", page)                     # leading emoji stripped

    def test_no_crash_on_partial_records(self):
        # records missing optional fields must not raise
        recs = [{"kind": "play"}, {"kind": "probe", "lane": "nzb"},
                {"kind": "nzb_failure"}, {"kind": "buffer", "event": "twin"}]
        page = overview.render(recs, "test-secret")
        self.assertIn("<html", page)


if __name__ == "__main__":
    unittest.main()
