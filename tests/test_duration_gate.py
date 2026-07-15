"""Duration verification: a stream's length must roughly match the title.

Live incident: the #1 result for a series episode was a ~3-minute clip — it
delivered fast, carried no size, and nothing measured its length. The evidence
is free in both probe paths: HLS media playlists declare every segment's
duration, and ffprobe reads a direct file's declared duration from the head
bytes the probe already pulled.
"""

import asyncio
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("ADDON_SECRET", "test-secret")

from app import picker, probe, reputation


class DurationReasonTests(unittest.TestCase):
    def test_a_clip_fails_a_full_episode_passes(self):
        self.assertIn("clip", probe._duration_reason(180, 1440))
        self.assertEqual("", probe._duration_reason(1300, 1440))
        self.assertEqual("", probe._duration_reason(6600, 7200))

    def test_absurdly_long_content_fails(self):
        # a whole movie file listed for a 24-minute episode
        self.assertIn("wrong content", probe._duration_reason(7200, 1440))
        # a long movie against a movie runtime is fine
        self.assertEqual("", probe._duration_reason(11000, 6600))

    def test_no_evidence_or_disabled_never_fails(self):
        self.assertEqual("", probe._duration_reason(0, 1440))
        self.assertEqual("", probe._duration_reason(180, 0))
        with patch.object(probe, "DURATION_MIN_FRAC", 0):
            self.assertEqual("", probe._duration_reason(180, 1440))


class HlsDurationTests(unittest.TestCase):
    VOD = ("#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
           "#EXTINF:6.0,\nseg0.ts\n#EXTINF:6.0,\nseg1.ts\n"
           "#EXTINF:4.5,\nseg2.ts\n#EXT-X-ENDLIST\n")
    LIVE = "#EXTM3U\n#EXTINF:6.0,\nseg0.ts\n#EXTINF:6.0,\nseg1.ts\n"

    def test_vod_playlist_sums_segment_durations(self):
        self.assertAlmostEqual(16.5, probe._hls_duration(self.VOD))

    def test_live_playlist_has_no_duration_evidence(self):
        self.assertEqual(0.0, probe._hls_duration(self.LIVE))


class _FakeResponse:
    def __init__(self, status, headers, chunks, url):
        self.status_code, self.headers, self.url = status, headers, url
        self._chunks = chunks

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c


class _FakeStream:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    def __init__(self, routes):
        self.routes = routes

    def stream(self, method, url, headers=None, timeout=None):
        return _FakeStream(self.routes[url])


class ProbeDurationGateTests(unittest.TestCase):
    def setUp(self):
        self._client = probe._client

    def tearDown(self):
        probe._client = self._client

    def _run(self, coro):
        return asyncio.run(coro)

    def test_short_hls_vod_fails_before_fetching_a_segment(self):
        url = "https://cdn.example/clip.m3u8"
        short = ("#EXTM3U\n" + "".join(
            f"#EXTINF:6.0,\nseg{i}.ts\n" for i in range(30))
            + "#EXT-X-ENDLIST\n")                       # 3 minutes total
        probe._client = _FakeClient({
            url: _FakeResponse(200, {"content-type":
                                     "application/vnd.apple.mpegurl"},
                               [short.encode()], url)})
        r = self._run(probe.probe(url, None, ttfb_max=10, expect_secs=1440))
        self.assertFalse(r.ok)
        self.assertIn("clip", r.reason)
        self.assertAlmostEqual(180.0, r.media_secs)

    def test_full_length_hls_vod_still_verifies(self):
        base = "https://cdn.example/full.m3u8"
        seg = "https://cdn.example/seg0.ts"
        full = ("#EXTM3U\n" + "".join(
            f"#EXTINF:6.0,\nseg{i}.ts\n" for i in range(240))
            + "#EXT-X-ENDLIST\n")                       # 24 minutes
        full = full.replace("seg0.ts", "seg0.ts", 1)
        probe._client = _FakeClient({
            base: _FakeResponse(200, {}, [full.encode()], base),
            seg: _FakeResponse(200, {"content-type": "video/mp2t"},
                               [b"G" + b"\x00" * (2 * 1024 * 1024)], seg)})
        r = self._run(probe.probe(base, None, ttfb_max=10, expect_secs=1440))
        self.assertTrue(r.ok, r.reason)
        self.assertAlmostEqual(1440.0, r.media_secs)

    def test_direct_file_duration_from_sniff_gates_too(self):
        url = "https://cdn.example/clip.mp4"
        probe._client = _FakeClient({
            url: _FakeResponse(200, {"content-type": "video/mp4"},
                               [b"\x00\x00\x00 ftypisom"
                                + b"\x00" * (4 * 1024 * 1024)], url)})

        async def fake_codecs(target, timeout=5.0):
            return ["aac"], "h264", 185.0

        with (patch.object(probe, "CODEC_SNIFF", True),
              patch("app.probe.vprobe.enabled", return_value=True),
              patch("app.probe.vprobe.codecs_of", side_effect=fake_codecs)):
            r = self._run(probe.probe(url, 1_000_000, ttfb_max=10,
                                      expect_secs=1440))
        self.assertFalse(r.ok)
        self.assertIn("clip", r.reason)

    def test_direct_file_with_matching_duration_passes(self):
        url = "https://cdn.example/ep.mkv"
        probe._client = _FakeClient({
            url: _FakeResponse(200, {}, [b"\x1aE\xdf\xa3"
                                         + b"\x00" * (4 * 1024 * 1024)], url)})

        async def fake_codecs(target, timeout=5.0):
            return ["aac"], "h264", 1420.0

        with (patch.object(probe, "CODEC_SNIFF", True),
              patch("app.probe.vprobe.enabled", return_value=True),
              patch("app.probe.vprobe.codecs_of", side_effect=fake_codecs)):
            r = self._run(probe.probe(url, 1_000_000, ttfb_max=10,
                                      expect_secs=1440))
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(("aac",), r.acodecs)


class CooledExcludedFromPicksTests(unittest.TestCase):
    def test_cooled_release_is_not_usable(self):
        s = {"name": "X 1080p", "url": "https://h.example/f",
             "behaviorHints": {"filename": "Some.Show.S01E01.1080p.WEB-GRP.mkv"}}
        from app import telemetry
        sig = telemetry.signature(s)
        with patch.object(reputation, "_save_cooldowns", lambda: None):
            self.assertTrue(picker._usable(s, picker.PROFILES["full"], 1440))
            reputation.cooldown(sig, 3600)
            self.assertFalse(picker._usable(s, picker.PROFILES["full"], 1440))
            reputation._cooldowns.pop(sig, None)


class InvalidateTests(unittest.TestCase):
    def test_invalidate_drops_all_pickers_for_the_title_only(self):
        picker._store("full:series:tt777:1:1", [{"url": "a"}])
        picker._store("slow:full:series:tt777:1:1", [{"url": "b"}])
        picker._store("full:series:tt888:1:1", [{"url": "c"}])
        picker.invalidate("tt777:1:1")
        self.assertNotIn("full:series:tt777:1:1", picker._cache)
        self.assertNotIn("slow:full:series:tt777:1:1", picker._cache)
        self.assertIn("full:series:tt888:1:1", picker._cache)
        picker._cache.pop("full:series:tt888:1:1", None)


if __name__ == "__main__":
    unittest.main()
