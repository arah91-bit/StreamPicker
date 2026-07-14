"""Probe speed ladder + HLS awareness.

The graduated bail-out thresholds come from live telemetry: passing streams
finish the 4 MiB window in ~1-2s, so a probe still reading at 3s+ is already
far below need. The HLS tests pin the playlist descent that lets custom HTTP
addons (which often serve .m3u8) verify honestly instead of failing forever
as "short body".
"""

import asyncio
import os
import unittest

os.environ.setdefault("ADDON_SECRET", "test-secret")

from app import probe


class TooSlowLadderTests(unittest.TestCase):
    REQ = 6_000_000  # 6 MB/s required

    def _speed(self, bps: float, measured: float) -> bool:
        return probe._too_slow(int(bps * measured), measured, self.REQ)

    def test_never_bails_inside_rampup_window(self):
        self.assertFalse(self._speed(0, 3.0))          # zero bytes but ≤3s

    def test_deep_trickle_bails_at_three_seconds(self):
        self.assertTrue(self._speed(self.REQ / 10, 3.5))
        self.assertFalse(self._speed(self.REQ / 2, 3.5))   # marginal survives

    def test_moderate_trickle_bails_at_five_seconds(self):
        self.assertTrue(self._speed(self.REQ / 4, 5.5))
        self.assertFalse(self._speed(self.REQ / 2.5, 5.5))

    def test_original_half_speed_check_still_fires_at_eight(self):
        self.assertTrue(self._speed(self.REQ / 2.5, 8.5))
        self.assertFalse(self._speed(self.REQ / 1.5, 8.5))


class HlsHelperTests(unittest.TestCase):
    def test_detects_playlist_by_body_and_content_type(self):
        self.assertTrue(probe._looks_hls("", b"#EXTM3U\n#EXT-X-VERSION:3"))
        self.assertTrue(probe._looks_hls("application/vnd.apple.mpegURL", b""))
        self.assertTrue(probe._looks_hls("application/x-mpegurl", b""))
        self.assertFalse(probe._looks_hls("video/mp4", b"\x00\x00\x00 ftypisom"))

    def test_text_sniff_catches_error_pages_not_video_magic(self):
        self.assertTrue(probe._looks_text(b"<!doctype html><html>"))
        self.assertTrue(probe._looks_text(b'{"error":"expired"}'))
        self.assertFalse(probe._looks_text(b"\x1a\x45\xdf\xa3"))       # MKV
        self.assertFalse(probe._looks_text(b"\x00\x00\x00 ftypisom"))  # MP4
        self.assertFalse(probe._looks_text(b"G\x40\x00\x10"))          # MPEG-TS

    def test_first_uri_from_master_and_media_playlists(self):
        master = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=5000000\n"
                  "variants/1080p.m3u8\n")
        media = ("#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6.0,\n"
                 "seg-000.ts\n#EXTINF:6.0,\nseg-001.ts\n")
        self.assertEqual("variants/1080p.m3u8", probe._hls_first_uri(master))
        self.assertEqual("seg-000.ts", probe._hls_first_uri(media))
        self.assertEqual("", probe._hls_first_uri("#EXTM3U\n# only comments\n"))

    def test_variant_info_parses_declared_quality(self):
        master = ('#EXTM3U\n'
                  '#EXT-X-STREAM-INF:BANDWIDTH=15840000,'
                  'RESOLUTION=3840x2160,CODECS="hvc1.2.4.L153.B0,mp4a.40.2"\n'
                  'v0.m3u8\n')
        info = probe._hls_variant_info(master)
        self.assertEqual(15_840_000.0, info["media_bps"])
        self.assertEqual(2160, info["media_height"])
        self.assertIn("hvc1", info["media_codecs"])

    def test_variant_info_reads_the_first_uri_not_the_best(self):
        master = ('#EXTM3U\n'
                  '#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720\n'
                  'low.m3u8\n'
                  '#EXT-X-STREAM-INF:BANDWIDTH=16000000,RESOLUTION=3840x2160\n'
                  'high.m3u8\n')
        info = probe._hls_variant_info(master)
        self.assertEqual(2_500_000.0, info["media_bps"])
        self.assertEqual(720, info["media_height"])

    def test_media_playlist_declares_nothing(self):
        media = "#EXTM3U\n#EXTINF:6.0,\nseg-000.ts\n"
        self.assertEqual({}, probe._hls_variant_info(media))


class _FakeResponse:
    def __init__(self, status: int, headers: dict, chunks: list[bytes], url: str):
        self.status_code = status
        self.headers = headers
        self.url = url
        self._chunks = chunks

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c


class _FakeStream:
    def __init__(self, resp: _FakeResponse):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    """Maps URL → response; records the URLs the probe actually fetched."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.fetched: list[str] = []

    def stream(self, method, url, headers=None, timeout=None):
        self.fetched.append(url)
        return _FakeStream(self.routes[url])


class HlsProbeDescentTests(unittest.TestCase):
    def setUp(self):
        self._client = probe._client

    def tearDown(self):
        probe._client = self._client

    def _run(self, coro):
        return asyncio.run(coro)

    def test_master_to_variant_to_segment_verifies(self):
        base = "https://cdn.example/live/master.m3u8"
        variant = "https://cdn.example/live/variants/1080p.m3u8"
        segment = "https://cdn.example/live/variants/seg-000.ts"
        client = _FakeClient({
            base: _FakeResponse(200, {"content-type": "application/vnd.apple.mpegurl"},
                                [b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n"
                                 b"variants/1080p.m3u8\n"], base),
            variant: _FakeResponse(200, {}, [b"#EXTM3U\n#EXTINF:6.0,\nseg-000.ts\n"],
                                   variant),
            segment: _FakeResponse(200, {"content-type": "video/mp2t"},
                                   [b"G" + b"\x00" * (2 * 1024 * 1024)], segment),
        })
        probe._client = client
        r = self._run(probe.probe(base, None, ttfb_max=10))
        self.assertTrue(r.ok, r.reason)
        self.assertEqual([base, variant, segment], client.fetched)

    def test_master_declaration_rides_down_to_the_result(self):
        base = "https://cdn.example/live/master.m3u8"
        variant = "https://cdn.example/live/720p.m3u8"
        segment = "https://cdn.example/live/seg-000.ts"
        client = _FakeClient({
            base: _FakeResponse(200, {}, [
                b"#EXTM3U\n"
                b'#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720,'
                b'CODECS="avc1.640028,mp4a.40.2"\n'
                b"720p.m3u8\n"], base),
            variant: _FakeResponse(200, {}, [b"#EXTM3U\n#EXTINF:6.0,\nseg-000.ts\n"],
                                   variant),
            segment: _FakeResponse(200, {"content-type": "video/mp2t"},
                                   [b"G" + b"\x00" * (2 * 1024 * 1024)], segment),
        })
        probe._client = client
        r = self._run(probe.probe(base, None, ttfb_max=10))
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(2_500_000.0, r.media_bps)
        self.assertEqual(720, r.media_height)
        self.assertIn("avc1", r.media_codecs)

    def test_relative_uris_resolve_against_playlist_url(self):
        base = "https://cdn.example/a/b/list.m3u8"
        seg = "https://cdn.example/a/b/s1.ts"
        client = _FakeClient({
            base: _FakeResponse(200, {}, [b"#EXTM3U\n#EXTINF:4,\ns1.ts\n"], base),
            seg: _FakeResponse(200, {}, [b"G" + b"\x00" * (1024 * 1024)], seg),
        })
        probe._client = client
        r = self._run(probe.probe(base, None, ttfb_max=10))
        self.assertTrue(r.ok, r.reason)
        self.assertIn(seg, client.fetched)

    def test_endless_playlist_nesting_fails(self):
        u = ["https://x.example/0.m3u8", "https://x.example/1.m3u8",
             "https://x.example/2.m3u8", "https://x.example/3.m3u8"]
        client = _FakeClient({
            u[i]: _FakeResponse(200, {}, [f"#EXTM3U\n{u[i+1]}\n".encode()], u[i])
            for i in range(3)
        })
        probe._client = client
        r = self._run(probe.probe(u[0], None, ttfb_max=10))
        self.assertFalse(r.ok)
        self.assertIn("playlist nested too deep", r.reason)

    def test_html_error_page_fails_as_not_video(self):
        url = "https://cdn.example/stream.mp4"
        client = _FakeClient({
            url: _FakeResponse(200, {"content-type": "text/html"},
                               [b"<!doctype html><html>token expired</html>"], url),
        })
        probe._client = client
        r = self._run(probe.probe(url, None, ttfb_max=10))
        self.assertFalse(r.ok)
        self.assertIn("not video", r.reason)

    def test_direct_file_short_body_still_fails(self):
        # the segment tolerance must not weaken the missing-articles detector
        url = "https://dav.example/content/movie.mkv"
        client = _FakeClient({
            url: _FakeResponse(200, {}, [b"\x1a\x45\xdf\xa3" + b"\x00" * 1000], url),
        })
        probe._client = client
        r = self._run(probe.probe(url, None, ttfb_max=10))
        self.assertFalse(r.ok)
        self.assertIn("short body", r.reason)


if __name__ == "__main__":
    unittest.main()
