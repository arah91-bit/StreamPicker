import unittest

from app import proxy, telemetry


class _Response:
    def __init__(self, status, content_range="", content_length=""):
        self.status_code = status
        self.headers = {}
        if content_range:
            self.headers["content-range"] = content_range
        if content_length:
            self.headers["content-length"] = content_length


class ProxyRangeSafetyTests(unittest.TestCase):
    def test_suffix_range_is_recognized(self):
        self.assertEqual((0, None, True), proxy._parse_range("bytes=-65536"))
        self.assertEqual(65536, proxy._suffix_length("bytes=-65536"))

    def test_nonzero_range_rejects_full_file_200(self):
        self.assertFalse(proxy._range_response_ok(
            _Response(200, content_length="1000"), "bytes=500-"))

    def test_nonzero_range_requires_exact_content_range_start(self):
        self.assertTrue(proxy._range_response_ok(
            _Response(206, "bytes 500-999/1000"), "bytes=500-"))
        self.assertFalse(proxy._range_response_ok(
            _Response(206, "bytes 0-499/1000"), "bytes=500-"))

    def test_suffix_validates_tail_length_and_end(self):
        self.assertTrue(proxy._range_response_ok(
            _Response(206, "bytes 900-999/1000"), "bytes=-100"))
        self.assertFalse(proxy._range_response_ok(
            _Response(206, "bytes 800-899/1000"), "bytes=-100"))


class StrongSignatureTests(unittest.TestCase):
    @staticmethod
    def stream(filename):
        return {"behaviorHints": {"filename": filename}}

    def test_long_shared_prefixes_do_not_collide(self):
        prefix = "Very.Long.Release.Name." + "A" * 100
        one = telemetry.signature(self.stream(prefix + ".CUT-ONE.mkv"))
        two = telemetry.signature(self.stream(prefix + ".CUT-TWO.mkv"))
        self.assertTrue(one.startswith("file:"))
        self.assertNotEqual(one, two)

    def test_legacy_session_signatures_are_scrubbed(self):
        entry = {"cands": [{"sig": "old-truncated"}],
                 "pool": [{"sig": "nzb:" + "a" * 64}],
                 "bufsig": "old-truncated"}
        self.assertEqual(1, proxy._scrub_legacy_sigs(entry))
        self.assertEqual("", entry["cands"][0]["sig"])
        self.assertNotIn("bufsig", entry)


class HlsPassThroughTests(unittest.TestCase):
    """HLS playlists must never be wrapped: served from /proxy/ their relative
    segment URIs resolve against our host and 404. Public playlists pass raw;
    a playlist that is only safe wrapped (credentials/internal host) has no
    servable form and is dropped."""

    def setUp(self):
        self._mint = proxy._mint
        proxy._mint = lambda *a, **k: "tok"

    def tearDown(self):
        proxy._mint = self._mint

    def test_is_hls_matches_playlist_paths_only(self):
        self.assertTrue(proxy._is_hls("https://cdn.example/live/master.m3u8"))
        self.assertTrue(proxy._is_hls("https://cdn.example/x.M3U8?token=abc"))
        self.assertTrue(proxy._is_hls("https://cdn.example/radio.m3u"))
        self.assertFalse(proxy._is_hls("https://cdn.example/movie.mp4"))
        self.assertFalse(proxy._is_hls("https://cdn.example/m3u8/movie.mkv"))

    def test_public_hls_passes_raw_while_files_still_wrap(self):
        streams = [
            {"name": "A 2160p", "url": "https://cdn.example/movie.mkv"},
            {"name": "B 1080p", "url": "https://cdn.example/live.m3u8"},
        ]
        out = proxy.wrap(streams, "movie", "tt1", "fast")
        self.assertEqual(2, len(out))
        self.assertIn("/proxy/", out[0]["url"])              # file wrapped
        self.assertEqual(streams[1]["url"], out[1]["url"])   # playlist raw

    def test_credentialed_hls_is_dropped_not_leaked(self):
        streams = [
            {"name": "A", "url": "https://user:pw@dav.example/x.m3u8"},
            {"name": "B", "url": "http://internal-host/y.m3u8"},
            {"name": "C", "url": "https://cdn.example/ok.m3u8"},
        ]
        out = proxy.wrap(streams, "movie", "tt1", "fast")
        urls = [s["url"] for s in out]
        self.assertEqual(["https://cdn.example/ok.m3u8"], urls)


class ActiveStreamDetailsTests(unittest.TestCase):
    """active_stream_details() feeds the overview's Now Playing cards — it must
    list exactly the entries with a live reader and never raise on the sparse
    ones (source=None, total=None) that exist mid-startup."""

    def setUp(self):
        self._saved = dict(proxy._entries)
        proxy._entries.clear()

    def tearDown(self):
        proxy._entries.clear()
        proxy._entries.update(self._saved)

    def test_lists_only_entries_with_readers(self):
        watched = proxy._Entry(
            "sig1", "/tmp/sig1", [],
            {"lbl": "Movie.2024.2160p.mkv", "dbr": "TB+", "res": 2160},
            "video/mp4", 8_000_000_000, "slow", "tt9")
        watched.consumers = 2
        watched.avail = 1_000_000_000
        watched.node = "node-1.example.com"
        idle = proxy._Entry("sig2", "/tmp/sig2", [], None, None, None,
                            "fast", "tt1")            # producer alive, no reader
        proxy._entries.update({"sig1": watched, "sig2": idle})

        out = proxy.active_stream_details()
        self.assertEqual(1, len(out))
        d = out[0]
        self.assertEqual("tt9", d["media_id"])
        self.assertEqual("Movie.2024.2160p.mkv", d["label"])
        self.assertEqual("TB+", d["debrid"])
        self.assertEqual(2160, d["res"])
        self.assertEqual("node-1.example.com", d["node"])
        self.assertEqual(1_000_000_000, d["avail"])
        self.assertEqual(8_000_000_000, d["total"])
        self.assertEqual(2, d["consumers"])

    def test_sparse_entry_with_reader_does_not_raise(self):
        bare = proxy._Entry("sig3", "/tmp/sig3", [], None, None, None, "", None)
        bare.consumers = 1
        proxy._entries["sig3"] = bare
        out = proxy.active_stream_details()
        self.assertEqual(1, len(out))
        self.assertEqual("", out[0]["media_id"])
        self.assertIsNone(out[0]["total"])


if __name__ == "__main__":
    unittest.main()
