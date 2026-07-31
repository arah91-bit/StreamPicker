"""The Easynews lane: enablement (credentials are the switch), the load-bearing
title/episode/sample filter on a raw filename index, credential-safe URL
construction, and the rows it emits staying untrusted.

The gates here are doing more work than their equivalents on the other lanes.
Easynews searches posted *filenames*, not a release index, so a query for one
show genuinely returns a different show that shares a word, and samples
outrank the real file because they carry the same release name.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from app import easynews, sources


def _row(name, *, ext=".mkv", size=8 * 10**9, runtime=7200, height=2160,
         subject="", **over):
    row = {"0": "a1b2c3", "10": name, "11": ext, "2": ext, "type": "VIDEO",
           "rawSize": size, "size": size, "runtime": runtime,
           "height": height, "yres": height, "fullres": "3840 x 2160",
           "vcodec": "HEVC", "acodec": "EAC3", "sig": "SIG",
           "6": subject or name, "passwd": False, "virus": False}
    row.update(over)
    return row


_PAYLOAD = {"downURL": "https://members.easynews.com/dl", "dlFarm": "auto",
            "dlPort": 443, "sid": "SID"}


class EnabledTests(unittest.TestCase):
    def test_credentials_are_the_switch(self):
        with patch.multiple(easynews, _USER="u", _PASS="p", _SOURCE_ON=True):
            self.assertTrue(easynews.enabled())

    def test_off_without_credentials(self):
        with patch.multiple(easynews, _USER="", _PASS="p", _SOURCE_ON=True):
            self.assertFalse(easynews.enabled())
        with patch.multiple(easynews, _USER="u", _PASS="", _SOURCE_ON=True):
            self.assertFalse(easynews.enabled())

    def test_explicit_off_keeps_credentials_but_disables(self):
        with patch.multiple(easynews, _USER="u", _PASS="p", _SOURCE_ON=False):
            self.assertFalse(easynews.enabled())
            self.assertTrue(easynews.configured())


class TitleGateTests(unittest.TestCase):
    """A raw filename search returns other shows that merely share a word."""

    def _cands(self, rows, titles=("The Bear",), se=(3, 5), year=None,
               runtime=None):
        return easynews._candidates(rows, list(titles), se, year, runtime)

    def test_rejects_a_different_show_sharing_a_word(self):
        # Both of these really are in Easynews' top results for "The Bear S03E05".
        rows = [_row("The.Island.With.Bear.Grylls.S03E05.HDTV.x264-C4TV"),
                _row("The.Yogi.Bear.Show.S03E05.Yogis.Pest.Guest.1080p.HMAX")]
        self.assertEqual([], self._cands(rows))

    def test_keeps_the_requested_show(self):
        rows = [_row("The.Bear.S03E05.Children.2160p.HULU.WEB-DL.H.265-playWEB")]
        self.assertEqual(1, len(self._cands(rows)))

    def test_rejects_the_wrong_episode(self):
        rows = [_row("The.Bear.S03E06.2160p.HULU.WEB-DL.H.265-playWEB")]
        self.assertEqual([], self._cands(rows))

    def test_rejects_a_contradictory_year_for_a_movie(self):
        rows = [_row("Dune.Part.Two.1984.2160p.UHD.BluRay.x265")]
        self.assertEqual([], easynews._candidates(
            rows, ["Dune Part Two"], None, 2024, None))


class SampleGateTests(unittest.TestCase):
    """Samples share the release name, so relevance sorting ranks them first."""

    def _cands(self, rows, runtime=None):
        return easynews._candidates(rows, ["Dune Part Two"], None, 2024, runtime)

    def test_rejects_a_named_sample(self):
        rows = [_row("dune.part.two.2024.dv.2160p.web.h265-ethel.sample",
                     size=192 * 10**6)]
        self.assertEqual([], self._cands(rows))

    def test_rejects_a_sample_named_only_in_the_subject(self):
        rows = [_row("dune.part.two.2024.2160p.web.h265-ethel",
                     size=8 * 10**9,
                     subject='"dune.part.two.2024.sample.rar" - yEnc')]
        self.assertEqual([], self._cands(rows))

    def test_rejects_an_unlabelled_clip_by_declared_runtime(self):
        # No "sample" token anywhere and a plausible size — only the runtime
        # gives it away, which is why the name check alone is not enough.
        rows = [_row("dune.part.two.2024.2160p.uhd.bluray.x265-strikes",
                     size=300 * 10**6, runtime=120)]
        self.assertEqual([], self._cands(rows, runtime=9960))

    def test_rejects_anything_under_the_size_floor(self):
        rows = [_row("dune.part.two.2024.2160p.web.h265", size=10 * 10**6)]
        self.assertEqual([], self._cands(rows))

    def test_keeps_the_real_file(self):
        rows = [_row("dune.part.two.2024.2160p.uhd.bluray.x265-strikes",
                     size=60 * 10**9, runtime=9960)]
        self.assertEqual(1, len(self._cands(rows, runtime=9960)))


class CandidateHygieneTests(unittest.TestCase):
    def _cands(self, rows):
        return easynews._candidates(rows, ["Dune Part Two"], None, 2024, None)

    def test_rejects_password_protected_and_flagged_rows(self):
        base = "dune.part.two.2024.2160p.web.h265"
        self.assertEqual([], self._cands([_row(base, passwd=True)]))
        self.assertEqual([], self._cands([_row(base, virus=True)]))

    def test_rejects_non_video_extensions(self):
        rows = [_row("dune.part.two.2024.2160p.web.h265", ext=".rar")]
        self.assertEqual([], self._cands(rows))

    def test_collapses_reposts_keeping_the_largest_copy(self):
        name = "dune.part.two.2024.2160p.uhd.bluray.x265-strikes"
        got = self._cands([_row(name, size=20 * 10**9),
                           _row(name, size=60 * 10**9)])
        self.assertEqual(1, len(got))
        self.assertEqual(60 * 10**9, easynews._size_of(got[0]))

    def test_ranks_by_resolution_then_size(self):
        got = self._cands([
            _row("dune.part.two.2024.1080p.web.h264", height=1080, size=9 * 10**9),
            _row("dune.part.two.2024.2160p.web.h265", height=2160, size=8 * 10**9)])
        self.assertEqual(2160, easynews._height_of(got[0]))


class DownloadUrlTests(unittest.TestCase):
    """Credentials ride in the userinfo so proxy._must_wrap never serves the row
    raw. A header would be dropped past WRAP_MAX and handed to the player."""

    def _url(self, user="b146418ca260d636@eweka.nl", password="pw:/x"):
        with patch.multiple(easynews, _USER=user, _PASS=password):
            return easynews._download_url(_PAYLOAD, _row("Some.Movie.2024.2160p"))

    def test_credentials_are_percent_encoded_into_the_userinfo(self):
        url = self._url()
        netloc = urlsplit(url).netloc
        self.assertIn("@", netloc)
        # The '@' inside the username must not create a second userinfo break.
        self.assertEqual("members.easynews.com", netloc.rsplit("@", 1)[1])
        self.assertIn("b146418ca260d636%40eweka.nl", netloc)
        self.assertNotIn("pw:/x", netloc)          # ':' and '/' escaped

    def test_proxy_must_wrap_recognises_the_credential(self):
        from app import proxy
        self.assertTrue(proxy._must_wrap(self._url()))

    def test_carries_farm_port_and_signature(self):
        url = self._url()
        self.assertIn("/auto/443/SIG/", url)
        self.assertTrue(url.endswith("/Some.Movie.2024.2160p.mkv"))

    def test_falls_back_to_farm_port_without_a_signature(self):
        with patch.multiple(easynews, _USER="u", _PASS="p"):
            url = easynews._download_url(
                _PAYLOAD, _row("Some.Movie.2024.2160p", sig=""))
        self.assertIn("/auto/443/a1b2c3.mkv/", url)

    def test_returns_blank_on_a_junk_envelope(self):
        with patch.multiple(easynews, _USER="u", _PASS="p"):
            self.assertEqual("", easynews._download_url(
                {"downURL": ""}, _row("Some.Movie.2024.2160p")))


class RowTests(unittest.TestCase):
    def _built(self):
        with patch.multiple(easynews, _USER="u", _PASS="p"):
            return easynews._row(_PAYLOAD, _row("Some.Movie.2024.2160p"))

    def test_carries_filename_and_exact_size_for_the_picker(self):
        row = self._built()
        hints = row["behaviorHints"]
        self.assertEqual("Some.Movie.2024.2160p.mkv", hints["filename"])
        self.assertEqual(8 * 10**9, hints["videoSize"])

    def test_never_uses_proxy_headers_for_credentials(self):
        # The non-HLS branch of proxy.wrap does not strip proxyHeaders, so a
        # credential there would reach the player on any row past WRAP_MAX.
        self.assertNotIn("proxyHeaders", self._built()["behaviorHints"])


class TrustTests(unittest.TestCase):
    """Easynews rows are validated exactly like any other HTTPS stream."""

    def test_rows_are_not_stamped_with_nzb_trust(self):
        row = {"url": "https://x/y.mkv", "_source_key": sources.EASYNEWS}
        self.assertFalse(sources.trusted_nzb(row))

    def test_picker_files_them_in_the_https_probe_lane(self):
        from app import picker
        self.assertEqual("https", picker._lane_of(
            {"_source_key": sources.EASYNEWS, "name": "Easynews\nfile.mkv"}))


class ConnectionCapTests(unittest.IsolatedAsyncioTestCase):
    """An Easynews account caps concurrent transfers, low. Measured live: 4 is
    fine, 6 pushes the worst TTFB to 5.9s, 8+ kills connections mid-stream with
    RemoteProtocolError. A title yields ~12 candidates, so an ungated probe wave
    starves playback's own producer and tail warm — which is what a "stuck on
    the splash screen" start actually was."""

    async def test_easynews_probes_are_gated(self):
        from app import probe
        gate = probe.ingest_gate({"_source_key": "easynews", "name": "Easynews\nx"})
        self.assertIsNotNone(gate)
        self.assertEqual(probe.EASYNEWS_MAX_PROBES, gate._value)

    async def test_gate_is_shared_across_easynews_streams(self):
        from app import probe
        a = probe.ingest_gate({"_source_key": "easynews", "name": "Easynews\na"})
        b = probe.ingest_gate({"_source_key": "easynews", "name": "Easynews\nb"})
        self.assertIs(a, b)          # one budget for the account, not per file

    async def test_other_sources_are_not_gated_by_it(self):
        from app import probe
        self.assertIsNone(probe.ingest_gate(
            {"_source_key": "nzb", "name": "NZB thing.mkv"}))

    async def test_cached_debrid_still_ungated(self):
        from app import probe
        self.assertIsNone(probe.ingest_gate({"name": "[TB+] cached.mkv"}))


class QualityPickerReachTests(unittest.IsolatedAsyncioTestCase):
    """The fast race reaches every lane through sources.search_all(), but the
    slow picker gathers its sources by name — so the lane has to be folded into
    _gather_extras or the quality picker is the one surface that cannot see it.
    Easynews carries full 4K remuxes, which is exactly what that picker wants."""

    async def test_gather_extras_includes_the_easynews_lane(self):
        from app import picker
        asked: list[str] = []

        async def fake_get(src, media, media_id, wait=0.0):
            asked.append(src)
            return [{"url": f"https://x/{src}.mkv"}]

        with patch.object(sources, "has", lambda s: s == sources.EASYNEWS), \
             patch.object(sources, "EXTRAS", []), \
             patch.object(sources, "get", fake_get):
            got = await picker._gather_extras("movie", "tt1", wait=1.0)
        self.assertEqual([sources.EASYNEWS], asked)
        self.assertEqual(1, len(got))


class ScrapersPanelTests(unittest.TestCase):
    """Credentials switch the lane on; switching it off must stick without
    discarding them."""

    def test_a_saved_login_enables_the_engine(self):
        from app import scrapers
        got = scrapers.current("", "", "", "", "", "", "", True)
        self.assertIn("easynews", {r["id"] for r in got})

    def test_explicit_off_wins_over_stored_credentials(self):
        from app import scrapers
        got = scrapers.current("", "", "", "", "", "", "0", True)
        self.assertNotIn("easynews", {r["id"] for r in got})

    def test_no_login_means_off(self):
        from app import scrapers
        got = scrapers.current("", "", "", "", "", "", "1", False)
        self.assertNotIn("easynews", {r["id"] for r in got})

    def test_enabled_even_when_an_older_scrapers_list_predates_it(self):
        from app import scrapers
        got = scrapers.current("", "", "", "", '[{"id":"comet"}]', "", "", True)
        self.assertEqual({"comet", "easynews"}, {r["id"] for r in got})


if __name__ == "__main__":
    unittest.main()
