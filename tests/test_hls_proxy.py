"""HLS playlist-rewriting proxy.

Every URI in a playlist must come back as a signed /proxy/{token}/hls URL so
the player only ever talks to us (fixes referer-gated/IP-locked hosts and
makes credentialed playlists servable), and the signature must bind (token,
upstream URL) so the endpoint can't be used as an open proxy.
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("ADDON_SECRET", "test-secret")

from app import hlsproxy, proxy

MASTER = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",URI="audio/eng.m3u8",NAME="English"
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2"
variants/1080p.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720,CODECS="avc1.64001f,mp4a.40.2"
https://cdn.example/abs/720p.m3u8
"""

MEDIA = """#EXTM3U
#EXT-X-TARGETDURATION:6
#EXT-X-KEY:METHOD=AES-128,URI="keys/k1.bin",IV=0x1234
#EXT-X-MAP:URI="init.mp4"
#EXTINF:6.0,
seg-000.ts
#EXTINF:6.0,
seg-001.ts
#EXT-X-ENDLIST
"""

BASE = "https://cdn.example/live/master.m3u8"


def _uris(rewritten: str) -> list[str]:
    """Decode every proxied URI in a rewritten playlist back to upstream."""
    out = []
    for line in rewritten.splitlines():
        for part in line.split('URI="'):
            cand = part.split('"')[0] if line.startswith("#") else line
            if "/hls?u=" in cand:
                u = cand.split("u=")[1].split("&")[0]
                out.append(hlsproxy._decode_u(u))
    return out


class RewriteTests(unittest.TestCase):
    def test_master_variants_and_media_renditions_are_rewritten(self):
        out = hlsproxy.rewrite(MASTER, BASE, "tok1")
        self.assertNotIn("variants/1080p.m3u8\n", out)
        ups = _uris(out)
        self.assertIn("https://cdn.example/live/variants/1080p.m3u8", ups)
        self.assertIn("https://cdn.example/abs/720p.m3u8", ups)       # absolute kept
        self.assertIn("https://cdn.example/live/audio/eng.m3u8", ups)  # EXT-X-MEDIA
        # STREAM-INF metadata is untouched
        self.assertIn("BANDWIDTH=5000000", out)

    def test_media_playlist_segments_keys_and_map_are_rewritten(self):
        base = "https://cdn.example/live/variants/1080p.m3u8"
        out = hlsproxy.rewrite(MEDIA, base, "tok1")
        ups = _uris(out)
        self.assertIn("https://cdn.example/live/variants/seg-000.ts", ups)
        self.assertIn("https://cdn.example/live/variants/keys/k1.bin", ups)
        self.assertIn("https://cdn.example/live/variants/init.mp4", ups)
        self.assertIn("#EXT-X-ENDLIST", out)
        self.assertIn("IV=0x1234", out)               # key attrs preserved

    def test_every_rewritten_url_verifies_and_foreign_ones_do_not(self):
        out = hlsproxy.rewrite(MEDIA, BASE, "tok1")
        for line in out.splitlines():
            if "/hls?u=" not in line or line.startswith("#"):
                continue
            u = line.split("u=")[1].split("&")[0]
            s = line.split("s=")[1].split('"')[0]
            url = hlsproxy._decode_u(u)
            self.assertEqual(hlsproxy._sign("tok1", url), s)
            # same URL, different token → invalid; forged URL → invalid
            self.assertNotEqual(hlsproxy._sign("tok2", url), s)
            self.assertNotEqual(
                hlsproxy._sign("tok1", "https://evil.example/x"), s)

    def test_segment_order_is_captured_for_prefetch(self):
        base = "https://cdn.example/v/list.m3u8"
        seq = hlsproxy.segment_urls(MEDIA, base)
        self.assertEqual(["https://cdn.example/v/seg-000.ts",
                          "https://cdn.example/v/seg-001.ts"], seq)
        self.assertEqual([], hlsproxy.segment_urls(MASTER, base))  # not media


class DeclaredCodecTests(unittest.TestCase):
    def test_codecs_normalize_to_ffprobe_names(self):
        ac, vc = hlsproxy.declared_codecs(MASTER)
        self.assertEqual(["aac"], ac)
        self.assertEqual("h264", vc)

    def test_flac_and_hevc_map(self):
        text = '#EXT-X-STREAM-INF:CODECS="hvc1.2.4.L153,fLaC"\nv.m3u8\n'
        ac, vc = hlsproxy.declared_codecs(text)
        self.assertEqual(["flac"], ac)
        self.assertEqual("hevc", vc)


class RequestHeaderTests(unittest.TestCase):
    def test_allowlisted_headers_pass_and_junk_is_dropped(self):
        s = {"behaviorHints": {"proxyHeaders": {"request": {
            "Referer": "https://site.example/",
            "User-Agent": "special-agent",
            "X-Forwarded-For": "1.2.3.4",       # never ours to forward
            "Host": "evil.example",
        }}}}
        h = hlsproxy.request_headers(s)
        self.assertEqual({"Referer": "https://site.example/",
                          "User-Agent": "special-agent"}, h)

    def test_absent_or_malformed_hints_yield_empty(self):
        self.assertEqual({}, hlsproxy.request_headers({}))
        self.assertEqual({}, hlsproxy.request_headers(
            {"behaviorHints": {"proxyHeaders": "junk"}}))


class SegmentCacheTests(unittest.TestCase):
    def setUp(self):
        hlsproxy._seg_cache.clear()
        hlsproxy._seg_cache_bytes = 0

    tearDown = setUp

    def test_put_get_and_size_eviction(self):
        with patch.object(hlsproxy, "CACHE_BYTES", 250):
            hlsproxy._cache_put("u1", b"x" * 100, "video/mp2t")
            hlsproxy._cache_put("u2", b"y" * 100, "video/mp2t")
            self.assertIsNotNone(hlsproxy._cache_get("u1"))
            hlsproxy._cache_put("u3", b"z" * 100, "video/mp2t")  # evicts LRU (u2)
            self.assertIsNone(hlsproxy._cache_get("u2"))
            self.assertIsNotNone(hlsproxy._cache_get("u1"))
            self.assertIsNotNone(hlsproxy._cache_get("u3"))


class WrapIntegrationTests(unittest.TestCase):
    """With the HLS proxy on, playlists wrap like files (and credentialed ones
    become servable); off restores raw-or-drop."""

    def setUp(self):
        self._mint = proxy._mint
        self.minted = []

        def fake_mint(cands, pool, media, media_id, picker, hls=False):
            self.minted.append((cands, hls))
            return "tok"

        proxy._mint = fake_mint

    def tearDown(self):
        proxy._mint = self._mint

    def test_hls_streams_wrap_and_headers_are_stripped(self):
        streams = [
            {"name": "A 1080p", "url": "https://cdn.example/live.m3u8",
             "behaviorHints": {"proxyHeaders": {"request": {"Referer": "r"}},
                               "notWebReady": True}},
            {"name": "B cred", "url": "https://user:pw@dav.example/x.m3u8"},
        ]
        with patch.object(hlsproxy, "ENABLED", True):
            out = proxy.wrap(streams, "movie", "tt1", "fast")
        self.assertEqual(2, len(out))                 # credentialed now served
        for s in out:
            self.assertIn("/proxy/tok", s["url"])
            self.assertNotIn("proxyHeaders", s.get("behaviorHints") or {})
        hls_flags = [h for _, h in self.minted]
        self.assertTrue(all(hls_flags))
        # the candidate carries the headers for upstream use
        cands, _ = self.minted[0]
        self.assertEqual({"Referer": "r"}, cands[0]["rh"])

    def test_disabled_restores_raw_or_drop(self):
        streams = [
            {"name": "A", "url": "https://user:pw@dav.example/x.m3u8"},
            {"name": "C", "url": "https://cdn.example/ok.m3u8"},
        ]
        with patch.object(hlsproxy, "ENABLED", False):
            out = proxy.wrap(streams, "movie", "tt1", "fast")
        self.assertEqual(["https://cdn.example/ok.m3u8"],
                         [s["url"] for s in out])


class SessionRegistryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved = dict(hlsproxy._active)
        hlsproxy._active.clear()

    def tearDown(self):
        hlsproxy._active.clear()
        hlsproxy._active.update(self._saved)

    async def test_segless_sessions_leave_the_registry_after_idle(self):
        import time
        entry = {"picker": "fast", "id": "tt1"}
        st = hlsproxy._st("tok-idle", entry)
        st["last"] = time.monotonic() - hlsproxy._FLUSH_IDLE - 10
        with patch("app.hlsproxy.telemetry.record_play") as rec:
            await hlsproxy.flush_idle()
        self.assertNotIn("tok-idle", hlsproxy._active)   # no leak
        rec.assert_not_called()                          # nothing to account

    async def test_played_sessions_flush_one_play_record(self):
        import time
        entry = {"picker": "fast", "id": "tt1"}
        st = hlsproxy._st("tok-played", entry)
        st.update({"segs": 40, "bytes": 200_000_000,
                   "t0": time.monotonic() - 1200,
                   "last": time.monotonic() - hlsproxy._FLUSH_IDLE - 10,
                   "cand": {"sig": "x", "lbl": "HLS 1080p", "res": 1080}})
        with patch("app.hlsproxy.telemetry.record_play") as rec:
            await hlsproxy.flush_idle()
            await hlsproxy.flush_idle()                  # idempotent
        rec.assert_called_once()
        self.assertEqual(200_000_000, rec.call_args.kwargs["served"])
        self.assertNotIn("tok-played", hlsproxy._active)


class SegmentFailureTests(unittest.TestCase):
    def test_three_failures_strike_the_release_once(self):
        entry = {"picker": "fast", "id": "tt1"}
        st = hlsproxy._st("tok-f", entry)
        hlsproxy._active.pop("tok-f", None)
        st["cand"] = {"sig": "file:dead", "lbl": "HLS 1080p"}
        with (patch("app.hlsproxy.reputation.observe") as observe,
              patch("app.hlsproxy.reputation.cooldown") as cooldown,
              patch("app.hlsproxy.telemetry.record_buffer")):
            for _ in range(5):
                hlsproxy._note_seg_failure("tok-f", entry, st)
        observe.assert_called_once()
        self.assertEqual("hls-segments-dead", observe.call_args.args[2])
        cooldown.assert_called_once_with("file:dead")


class LargeBodyStreamingTests(unittest.IsolatedAsyncioTestCase):
    """A playlist URI pointing at a whole movie file must stream through,
    never sit in RAM."""

    async def test_oversized_body_streams_instead_of_buffering(self):
        from starlette.requests import Request
        from starlette.responses import StreamingResponse

        big_chunk = b"x" * (1024 * 1024)

        class FakeResp:
            status_code = 200
            headers = {"content-type": "video/mp4"}

            def __init__(self):
                self._sent = 0                            # one stream, like httpx

            async def aiter_bytes(self):
                while self._sent < 80:                    # 80 MB total
                    self._sent += 1
                    yield big_chunk

            async def aclose(self):
                pass

        class FakeClient:
            def build_request(self, method, url, headers=None):
                return (method, url)

            async def send(self, req, stream=False):
                return FakeResp()

        entry = {"picker": "fast", "id": "tt1",
                 "cands": [{"sig": "s", "lbl": "x", "u": "u", "rh": {}}]}
        url = "https://cdn.example/whole-movie.mp4"
        token = "tok-big"
        import base64
        u = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
        q = f"u={u}&s={hlsproxy._sign(token, url)}"
        req = Request({"type": "http", "method": "GET", "path": "/",
                       "query_string": q.encode(), "headers": []})
        with patch.object(hlsproxy, "_client", FakeClient()):
            with patch.object(hlsproxy, "_MAX_SEG_BUF", 4 * 1024 * 1024):
                resp = await hlsproxy.serve_resource(token, entry, req)
        hlsproxy._active.pop(token, None)
        self.assertIsInstance(resp, StreamingResponse)
        self.assertNotIn(url, hlsproxy._seg_cache)        # too big to cache
        # drain the generator to prove pass-through works end to end
        total = 0
        async for chunk in resp.body_iterator:
            total += len(chunk)
        self.assertEqual(80 * 1024 * 1024, total)


if __name__ == "__main__":
    unittest.main()
