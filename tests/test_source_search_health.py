"""A source lane that fails every search must show up on Home.

Field case (2026-10-15): StremThru answered HTTP 500 to every request for
three days — 87 of 87 — and nothing in the dashboard said so. A failed search
returns no rows, so there was nothing to probe, and Home's "failing every
time" check only ever looked at probes.
"""

import time
import unittest
from unittest.mock import patch

import httpx

from app import home_ui, source_ui, sources


class _Resp:
    def raise_for_status(self):
        raise httpx.HTTPStatusError(
            "boom", request=httpx.Request("GET", "http://stremthru:8080/x"),
            response=httpx.Response(500))


class RecordTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_failed_search_leaves_a_record(self):
        async def get(*_a, **_k):
            return _Resp()

        key = ("stremthru", "movie", "tt1")
        with patch.object(sources._client, "get", side_effect=get), \
                patch.object(sources, "_raw", {}), \
                patch.object(sources, "_outcomes", {}), \
                patch("app.sources.telemetry.record_source_search") as rec:
            await sources._run(key, "http://stremthru:8080/x", "movie", "tt1", 5)
        source, label, state, detail, _secs, count = rec.call_args.args
        self.assertEqual(("stremthru", "StremThru", "failed", 0),
                         (source, label, state, count))
        self.assertIn("500", detail)

    def test_labels(self):
        self.assertEqual("Comet", sources.label(sources.FAST))
        self.assertEqual("x:gone", sources.label("x:gone"))


def _search(label, state, ago=60):
    return {"kind": "source_search", "label": label, "source": label.lower(),
            "state": state, "detail": "HTTPStatusError HTTP 500",
            "ts": time.time() - ago}


class HomeTests(unittest.TestCase):
    def broken(self, recs):
        with patch.object(home_ui.usenet_health, "indexer_listing",
                          return_value=[]), \
                patch.object(home_ui.source_health, "state", return_value=""):
            return home_ui._broken_services(recs)

    def test_a_lane_failing_every_search_is_down(self):
        recs = [_search("StremThru", "failed") for _ in range(6)]
        self.assertEqual(["StremThru"], self.broken(recs))

    def test_one_success_means_it_is_not(self):
        recs = [_search("StremThru", "failed") for _ in range(6)]
        recs.append(_search("StremThru", "ok"))
        self.assertEqual([], self.broken(recs))

    def test_a_few_failures_are_not_enough_and_old_ones_do_not_count(self):
        self.assertEqual([], self.broken([_search("Comet", "failed")] * 3))
        stale = [_search("Comet", "failed", ago=4 * 86400) for _ in range(9)]
        self.assertEqual([], self.broken(stale))


class SourcePageTests(unittest.TestCase):
    def test_the_lane_page_shows_its_failed_searches(self):
        recs = [_search("StremThru", "failed") for _ in range(4)]
        with patch.object(source_ui.usenet_health, "indexer_listing",
                          return_value=[]):
            s = source_ui.summarize("StremThru", recs)
        self.assertEqual((4, 4), (s["searches"], s["searches_failed"]))
        self.assertEqual("search: HTTPStatusError HTTP 500", s["reasons"][0][0])


if __name__ == "__main__":
    unittest.main()


class TorzPaddingTests(unittest.TestCase):
    """A StremThru URL saved unpadded by an older build is repaired on load."""

    def test_an_unpadded_segment_gets_its_padding_back(self):
        import base64, json
        cfg = {"indexers": None, "cached": True,
               "stores": [{"c": "tb", "t": "a" * 36}, {"c": "rd", "t": "b" * 52}]}
        seg = base64.urlsafe_b64encode(
            json.dumps(cfg, separators=(",", ":")).encode()).decode()
        unpadded = seg.rstrip("=")
        self.assertNotEqual(0, len(unpadded) % 4, "fixture must need padding")
        fixed = sources._torz_padded(f"http://stremthru:8080/stremio/torz/{unpadded}")
        got = fixed.rsplit("/", 1)[1]
        self.assertEqual(seg, got)
        self.assertEqual(cfg, json.loads(base64.urlsafe_b64decode(got)))

    def test_only_a_short_torz_segment_changes(self):
        cases = (
            ("http://st/stremio/torz/eyJhIjoxfQ==", "http://st/stremio/torz/eyJhIjoxfQ=="),
            ("http://st/stremio/torz/eyJhIjoxMn0", "http://st/stremio/torz/eyJhIjoxMn0="),
            ("https://comet.example/abc", "https://comet.example/abc"),
            ("http://st/stremio/torz/not+base64/x", "http://st/stremio/torz/not+base64/x"),
            ("", ""),
        )
        for url, expected in cases:
            with self.subTest(url=url):
                self.assertEqual(expected, sources._torz_padded(url))

    def test_a_trailing_path_survives(self):
        self.assertEqual(
            "http://st/stremio/torz/eyJhIjoxMn0=/manifest.json",
            sources._torz_padded("http://st/stremio/torz/eyJhIjoxMn0/manifest.json"))
