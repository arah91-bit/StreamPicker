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
        page = overview.render([])
        self.assertIn("Nothing streamed", page)
        self.assertNotIn("undefined", page)

    def test_totals_are_real(self):
        recs = [_play(5000, 3600, id="tt1"), _play(3000, 1800, id="tt2")]
        page = overview.render(recs)
        self.assertIn("8.0", page)          # 8000 MB → 8.0 GB
        self.assertIn("GB", page)
        self.assertIn("1.5", page)          # 5400 s → 1.5 hours
        self.assertIn("2</b> titles", page)

    def test_terabyte_scaling(self):
        recs = [_play(1_500_000, 3600)]     # 1500 GB → 1.50 TB
        page = overview.render(recs)
        self.assertIn("1.50", page)
        self.assertIn("TB", page)

    def test_failover_saves_counted(self):
        recs = [_play(100, 60, switched=True), _play(100, 60, switched=False),
                _play(100, 60, switched=True)]
        page = overview.render(recs)
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
        page = overview.render(recs)
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
        page = overview.render(recs)
        self.assertIn("Biggest single stream", page)
        self.assertIn("Big Movie 2024", page)            # name, not tt9
        self.assertNotIn("🎬", page)                     # leading emoji stripped

    def test_no_crash_on_partial_records(self):
        # records missing optional fields must not raise
        recs = [{"kind": "play"}, {"kind": "probe", "lane": "nzb"},
                {"kind": "nzb_failure"}, {"kind": "buffer", "event": "twin"}]
        page = overview.render(recs)
        self.assertIn("<html", page)


def _live(**kw):
    s = {"media_id": "tt1", "label": "Some.Release.2024.mkv", "debrid": "TB+",
         "res": 1080, "node": "", "avail": 0, "total": None, "consumers": 1,
         "picker": "slow"}
    s.update(kw)
    return s


class NowPlayingTests(unittest.TestCase):
    """The Now Playing section reads proxy.active_stream_details() live; these
    tests stub that call so they need no event loop or real buffer entries."""

    def setUp(self):
        self._orig = overview.proxy.active_stream_details

    def tearDown(self):
        overview.proxy.active_stream_details = self._orig

    def _playing(self, entries):
        overview.proxy.active_stream_details = lambda: entries

    def test_hidden_when_nothing_is_playing(self):
        self._playing([])
        page = overview.render([])
        self.assertNotIn("Now Playing", page)
        self.assertNotIn('http-equiv="refresh"', page)   # no pointless reloads

    def test_card_resolves_title_and_shows_progress(self):
        self._playing([_live(media_id="tt9", res=2160,
                             node="nexus-190.example.com",
                             avail=2 * 1024 ** 3, total=8 * 1024 ** 3)])
        recs = [{"kind": "served", "id": "tt9",
                 "label": "🎬 Big Movie 2024 2160p", "ts": time.time()}]
        page = overview.render(recs)
        self.assertIn("Now Playing", page)
        self.assertIn("Big Movie 2024", page)            # name, not tt9
        self.assertIn("2160p</span>", page)              # resolution badge
        self.assertIn("TB+", page)
        self.assertIn("nexus-190", page)                 # node shortened...
        self.assertNotIn("nexus-190.example.com", page)  # ...to first segment
        self.assertIn("2.00 / 8.00 GB buffered", page)
        self.assertIn("25%", page)
        self.assertIn('http-equiv="refresh"', page)      # live → auto-refresh

    def test_unknown_total_shows_buffering_and_viewer_count(self):
        self._playing([_live(avail=123, total=None, consumers=2)])
        page = overview.render([])
        self.assertIn("buffering…", page)
        self.assertIn("2 viewers", page)

    def test_labels_are_escaped_once(self):
        self._playing([_live(label='<img src=x onerror=alert(1)> & Co')])
        page = overview.render([])
        self.assertNotIn("<img src=x", page)             # no raw HTML injection
        self.assertIn("&amp; Co", page)                  # escaped exactly once
        self.assertNotIn("&amp;amp;", page)              # not double-escaped


class AddonLinksTests(unittest.TestCase):
    def test_links_render_with_copy_buttons(self):
        addons = [("Auto Stream",
                   "https://addon.example/sec/manifest.json"),
                  ("Auto Stream (Best Quality)",
                   "https://addon.example/sec/slow/manifest.json")]
        page = overview.render([], addons=addons)
        self.assertIn("Addon install links", page)
        for _, url in addons:
            self.assertIn(url, page)
        self.assertIn("copybtn", page)
        # LAN dashboards are plain http (non-secure context): the clipboard
        # API is unavailable there, so the fallback must ship too.
        self.assertIn("navigator.clipboard", page)
        self.assertIn("execCommand", page)

    def test_no_panel_or_script_without_links(self):
        page = overview.render([])
        self.assertNotIn("Addon install links", page)
        self.assertNotIn("navigator.clipboard", page)


if __name__ == "__main__":
    unittest.main()
