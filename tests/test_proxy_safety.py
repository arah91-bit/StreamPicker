import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import content_identity, proxy, telemetry


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

    def test_reputation_signature_is_scoped_to_requested_imdb(self):
        item = self.stream("Same.Title.1080p-GROUP.mkv")
        token = telemetry.request_ctx.set({"media_id": "tt0000001"})
        try:
            first = telemetry.signature(item)
        finally:
            telemetry.request_ctx.reset(token)
        token = telemetry.request_ctx.set({"media_id": "tt0000002"})
        try:
            second = telemetry.signature(item)
        finally:
            telemetry.request_ctx.reset(token)
        self.assertNotEqual(first, second)

    def test_legacy_session_signatures_are_scrubbed(self):
        entry = {"cands": [{"sig": "old-truncated"}],
                 "pool": [{"sig": "nzb:" + "a" * 64}],
                 "bufsig": "old-truncated"}
        self.assertEqual(1, proxy._scrub_legacy_sigs(entry))
        self.assertEqual("", entry["cands"][0]["sig"])
        self.assertNotIn("bufsig", entry)


class ContentIdentityTests(unittest.TestCase):
    def test_same_filename_different_urls_do_not_share_bytes(self):
        base = {"behaviorHints": {"filename": "Movie.2025.1080p-GRP.mkv"}}
        a = proxy._cand({**base, "url": "https://one.example/a"})
        b = proxy._cand({**base, "url": "https://two.example/b"})
        self.assertEqual(a["sig"], b["sig"])       # same reputation release
        self.assertNotEqual(a["cid"], b["cid"])    # not byte-identity proof

    def test_torrent_hash_and_file_identity_allow_real_twins(self):
        common = {"infoHash": "a" * 40, "fileIdx": 3,
                  "behaviorHints": {"filename": "Movie.mkv"}}
        one = {**common, "url": "https://one.example/a"}
        two = {**common, "url": "https://two.example/b"}
        content_identity.mark_auto_eligible(one)
        content_identity.mark_auto_eligible(two)
        a = proxy._cand(one)
        b = proxy._cand(two)
        self.assertEqual(a["cid"], b["cid"])

    def test_unmarked_torrent_candidates_are_url_local(self):
        common = {"infoHash": "a" * 40, "fileIdx": 3,
                  "behaviorHints": {"filename": "Movie.mkv"}}
        a = proxy._cand({**common, "url": "https://one.example/a"})
        b = proxy._cand({**common, "url": "https://two.example/b"})
        self.assertNotEqual(a["cid"], b["cid"])

    def test_selected_candidate_survives_buffer_scrub(self):
        cands = [{"u": "u1", "key": "one", "cid": "url:one"},
                 {"u": "u2", "key": "two", "cid": "url:two"}]
        entry = {"cands": cands, "pin": None, "bufcid": "url:two"}
        with patch.object(proxy, "_persist"):
            proxy._pin_selection("tok", entry, cands, cands[1])
        proxy._scrub_legacy_sigs(entry)               # restart wipes byte cache only
        self.assertIs(cands[1], proxy._selected_candidate(entry, cands))
        self.assertEqual("url:two", entry["selected_cid"])


class AutoEligibilityPoolTests(unittest.TestCase):
    @staticmethod
    def stream(url, *, hls=False, auto=False, debrid="RD+"):
        stream = {
            "name": f"{debrid} 1080p",
            "url": url + (".m3u8" if hls else ".mkv"),
            "behaviorHints": {"filename": "Movie.2024.1080p.mkv"},
        }
        if auto:
            content_identity.mark_auto_eligible(stream)
        return stream

    def test_candidate_persists_only_a_boolean_not_the_sentinel(self):
        stream = self.stream("https://one.example/marked", auto=True)
        cand = proxy._cand(stream)
        self.assertIs(cand["auto"], True)
        encoded = json.dumps(cand)
        self.assertNotIn(content_identity._AUTO_ELIGIBLE_KEY, encoded)
        self.assertNotIn("object at", encoded)

    def test_mint_reasserts_marked_and_unmarked_pool_boundaries(self):
        marked_a = {"u": "marked-a", "auto": True}
        unmarked = {"u": "unmarked", "auto": False}
        marked_b = {"u": "marked-b", "auto": True}
        with (patch.object(proxy.secrets, "token_urlsafe", return_value="marked"),
              patch.object(proxy, "_persist"),
              patch.object(proxy, "_prune_sessions")):
            proxy._mint([marked_a, unmarked, marked_b],
                        [marked_a, unmarked, marked_b],
                        "movie", "tt1", "fast")
        entry = proxy._sessions.pop("marked")[1]
        json.dumps(entry)  # persisted form contains booleans/data, no sentinel
        self.assertIs(entry["auto"], True)
        self.assertEqual(["marked-a", "marked-b"],
                         [c["u"] for c in entry["cands"]])
        self.assertEqual(["marked-a", "marked-b"],
                         [c["u"] for c in entry["pool"]])

        with (patch.object(proxy.secrets, "token_urlsafe", return_value="single"),
              patch.object(proxy, "_persist"),
              patch.object(proxy, "_prune_sessions")):
            proxy._mint([unmarked, marked_a], [unmarked, marked_a, marked_b],
                        "movie", "tt1", "fast")
        entry = proxy._sessions.pop("single")[1]
        self.assertIs(entry["auto"], False)
        self.assertEqual(["unmarked"], [c["u"] for c in entry["cands"]])
        self.assertEqual(["unmarked"], [c["u"] for c in entry["pool"]])

    def test_wrap_marked_file_leader_can_fail_over_only_to_marked_files(self):
        marked_a = self.stream("https://one.example/marked-a", auto=True)
        unmarked = self.stream("https://two.example/unmarked")
        marked_b = self.stream("https://three.example/marked-b", auto=True)
        calls = []

        def mint(cands, pool, media, media_id, picker, hls=False,
                 viewer="", viewer_key=""):
            calls.append((cands, pool, hls))
            return f"tok-{len(calls)}"

        with patch.object(proxy, "_mint", side_effect=mint):
            out = proxy.wrap([marked_a, unmarked, marked_b],
                             "movie", "tt1", "fast")

        self.assertEqual(3, len(out))
        self.assertEqual(
            [marked_a["url"], marked_b["url"]],
            [c["u"] for c in calls[0][0]],
        )
        self.assertEqual(
            [marked_a["url"], marked_b["url"]],
            [c["u"] for c in calls[0][1]],
        )
        self.assertTrue(all(c["auto"] is True for c in calls[0][1]))
        self.assertEqual([unmarked["url"]], [c["u"] for c in calls[1][0]])
        self.assertEqual([unmarked["url"]], [c["u"] for c in calls[1][1]])
        self.assertIs(calls[1][0][0]["auto"], False)
        self.assertEqual([marked_b["url"]], [c["u"] for c in calls[2][0]])

    def test_wrap_hls_uses_the_same_identity_boundary(self):
        marked_a = self.stream(
            "https://one.example/marked-a", hls=True, auto=True)
        unmarked = self.stream("https://two.example/unmarked", hls=True)
        marked_b = self.stream(
            "https://three.example/marked-b", hls=True, auto=True)
        calls = []

        def mint(cands, pool, media, media_id, picker, hls=False,
                 viewer="", viewer_key=""):
            calls.append((cands, pool, hls))
            return f"hls-{len(calls)}"

        with (patch.object(proxy, "_mint", side_effect=mint),
              patch.object(proxy.hlsproxy, "ENABLED", True)):
            proxy.wrap([marked_a, unmarked, marked_b],
                       "movie", "tt1", "fast")

        self.assertEqual(3, len(calls))
        self.assertTrue(all(call[2] for call in calls))
        self.assertEqual(
            [marked_a["url"], marked_b["url"]],
            [c["u"] for c in calls[0][0]],
        )
        self.assertTrue(all(c["auto"] is True for c in calls[0][1]))
        self.assertEqual([unmarked["url"]], [c["u"] for c in calls[1][0]])
        self.assertEqual([unmarked["url"]], [c["u"] for c in calls[1][1]])

    def test_twin_lookup_cannot_cross_eligibility_boundary(self):
        current = {"u": "one", "cid": "torrent:same:auto", "dbr": "RD+",
                   "auto": True}
        marked = {"u": "two", "cid": "torrent:same:auto", "dbr": "TB+",
                  "auto": True}
        unmarked = {"u": "three", "cid": "torrent:same:auto", "dbr": "AD+",
                    "auto": False}
        entry = {"auto": True, "cands": [current],
                 "pool": [current, unmarked, marked]}
        self.assertEqual([marked], proxy._twin_cands(entry, current))

    def test_persisted_pool_scrub_is_fail_closed(self):
        marked = {"u": "marked", "auto": True}
        unmarked = {"u": "unmarked", "auto": False}
        entry = {"auto": True, "cands": [marked, unmarked],
                 "pool": [unmarked, marked], "pin": None}
        proxy._scrub_legacy_sigs(entry)
        self.assertEqual([marked], entry["cands"])
        self.assertEqual([marked], entry["pool"])

        legacy = {"cands": [marked.copy(), unmarked.copy()],
                  "pool": [marked.copy(), unmarked.copy()], "pin": 1}
        proxy._scrub_legacy_sigs(legacy)
        self.assertIs(legacy["auto"], False)
        self.assertEqual(["unmarked"], [c["u"] for c in legacy["cands"]])
        self.assertEqual(["unmarked"], [c["u"] for c in legacy["pool"]])


class ProxyAsyncSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_upstream_headers_are_forwarded_with_range(self):
        class Client:
            def __init__(self):
                self.headers = None

            def build_request(self, method, url, headers=None):
                self.headers = headers
                return object()

            async def send(self, *args, **kwargs):
                return object()

        client = Client()
        cand = {"u": "https://cdn.example/movie", "rh": {
            "Referer": "https://site.example/", "Cookie": "token=x"}}
        with patch.object(proxy, "_client", client):
            await proxy._send(cand, "bytes=50-")
        self.assertEqual("bytes=50-", client.headers["Range"])
        self.assertEqual("https://site.example/", client.headers["Referer"])
        self.assertEqual("token=x", client.headers["Cookie"])

    async def test_cold_start_is_single_flight_per_token(self):
        active = calls = peak = 0

        async def once(*args):
            nonlocal active, calls, peak
            calls += 1
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return object()

        proxy._start_locks.pop("single", None)
        with patch.object(proxy, "_get_or_start_entry_once", side_effect=once):
            await asyncio.gather(
                proxy._get_or_start_entry("single", {}, []),
                proxy._get_or_start_entry("single", {}, []))
        proxy._start_locks.pop("single", None)
        self.assertEqual(2, calls)
        self.assertEqual(1, peak)

    async def test_cached_range_past_eof_is_416(self):
        e = proxy._Entry("url:x", "/nonexistent", [], {}, "video/mp4", 100,
                         "fast", "tt1")
        e.avail = 100
        e.complete = True
        proxy._entries["url:x"] = e
        try:
            resp = await proxy._serve_buffered(
                "tok", {"bufcid": "url:x"}, [], 150, None, True, None)
        finally:
            proxy._entries.pop("url:x", None)
        self.assertEqual(416, resp.status_code)
        self.assertEqual("bytes */100", resp.headers["content-range"])

    async def test_clean_truncated_eof_fails_buffer_entry(self):
        class Resp:
            headers = {}

            async def aclose(self):
                pass

        class Empty:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        with tempfile.TemporaryDirectory() as td:
            e = proxy._Entry("url:short", os.path.join(td, "short.bin"), [],
                             {"u": "u", "sig": "file:" + "a" * 64,
                              "lbl": "short", "nzb_indexers": []},
                             "video/mp4", 100, "fast", "tt1")
            e.consumers = 1
            with (patch.object(proxy, "_connect_resume", new=AsyncMock(return_value=None)),
                  patch("app.proxy.telemetry.record_buffer"),
                  patch("app.proxy.reputation.observe"),
                  patch("app.proxy.reputation.cooldown")):
                await proxy._produce(e, "tok", Resp(), Empty(), [b"1234567890"])
        self.assertTrue(e.failed)
        self.assertFalse(e.complete)
        self.assertEqual(10, e.avail)

    async def test_fresh_load_starts_maintenance(self):
        with tempfile.TemporaryDirectory() as td:
            missing = os.path.join(td, "sessions.jsonl")
            with (patch.object(proxy, "SESS_FILE", missing),
                  patch.object(proxy, "_bufcache_startup") as startup,
                  patch.object(proxy, "_sessions", {})):
                proxy.load()
        startup.assert_called_once()


class HlsPassThroughTests(unittest.TestCase):
    """With the rewriting proxy DISABLED (PROXY_HLS=0) the legacy rules hold:
    playlists must never be wrapped un-rewritten (relative segment URIs would
    404 against our host), public ones pass raw, and a playlist that is only
    safe wrapped (credentials/internal host) has no servable form and is
    dropped. The enabled path is covered in test_hls_proxy.py."""

    def setUp(self):
        from app import hlsproxy
        self._mint = proxy._mint
        proxy._mint = lambda *a, **k: "tok"
        self._hls = hlsproxy.ENABLED
        hlsproxy.ENABLED = False

    def tearDown(self):
        from app import hlsproxy
        proxy._mint = self._mint
        hlsproxy.ENABLED = self._hls

    def test_is_hls_matches_playlist_paths_only(self):
        self.assertTrue(proxy._is_hls("https://cdn.example/live/master.m3u8"))
        self.assertTrue(proxy._is_hls("https://cdn.example/x.M3U8?token=abc"))
        self.assertTrue(proxy._is_hls("https://cdn.example/radio.m3u"))
        self.assertFalse(proxy._is_hls("https://cdn.example/movie.mp4"))
        self.assertFalse(proxy._is_hls("https://cdn.example/m3u8/movie.mkv"))

    def test_probe_content_kind_overrides_url_extension(self):
        self.assertTrue(proxy._stream_is_hls(
            {"url": "https://cdn.example/live", "_content_kind": "hls"}))
        self.assertFalse(proxy._stream_is_hls(
            {"url": "https://cdn.example/wrong.m3u8", "_content_kind": "file"}))

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
        self._saved_direct = dict(proxy._direct)
        proxy._entries.clear()
        proxy._direct.clear()

    def tearDown(self):
        proxy._entries.clear()
        proxy._entries.update(self._saved)
        proxy._direct.clear()
        proxy._direct.update(self._saved_direct)

    def test_tail_range_is_served_from_memory(self):
        """The on-open index read (MKV cues at EOF) must not go upstream."""
        e = proxy._Entry("sigT", "/tmp/sigT", [], {"lbl": "X"},
                         "video/x-matroska", 1000, "fast", "tt1")
        e.tail, e.tail_off = b"0123456789", 990

        r = proxy._tail_response(e, 995, None)

        self.assertEqual(206, r.status_code)
        self.assertEqual(b"56789", r.body)
        self.assertEqual("bytes 995-999/1000", r.headers["content-range"])
        self.assertEqual("video/x-matroska", r.headers["content-type"])

    def test_range_before_the_tail_falls_through(self):
        e = proxy._Entry("sigT2", "/tmp/sigT2", [], {"lbl": "X"},
                         None, 1000, "fast", "tt1")
        e.tail, e.tail_off = b"0123456789", 990

        self.assertIsNone(proxy._tail_response(e, 100, None))
        self.assertIsNone(proxy._tail_response(e, 989, None))

    def test_no_tail_yet_falls_through(self):
        e = proxy._Entry("sigT3", "/tmp/sigT3", [], {"lbl": "X"},
                         None, 1000, "fast", "tt1")

        self.assertIsNone(proxy._tail_response(e, 995, None))

    def test_bounded_tail_range_respects_end(self):
        e = proxy._Entry("sigT4", "/tmp/sigT4", [], {"lbl": "X"},
                         None, 1000, "fast", "tt1")
        e.tail, e.tail_off = b"0123456789", 990

        r = proxy._tail_response(e, 992, 994)

        self.assertEqual(b"234", r.body)
        self.assertEqual("bytes 992-994/1000", r.headers["content-range"])

    def test_a_frozen_fill_with_a_waiting_reader_is_recorded(self):
        """The event that was missing. A producer that stops writing matched no
        edge-triggered event, so the only way to establish a frozen buffer was
        stat()-ing the cache file twice by hand."""
        e = proxy._Entry("sigS", "/tmp/sigS", [], {"lbl": "X"}, None, 1000,
                         "fast", "tt1")
        e.avail, e.consumers, e.last = 500, 0, time.time()
        proxy._entries["sigS"] = e
        proxy._fill_marks.clear()

        with patch("app.proxy.telemetry.record_buffer") as rec:
            asyncio.run(proxy._watch_fill(time.time()))       # first tick: mark
            self.assertEqual(0, rec.call_count)
            asyncio.run(proxy._watch_fill(time.time() + 60))  # second: no gain

        self.assertEqual("stalled", rec.call_args.args[0])
        self.assertEqual(0.0, rec.call_args.kwargs["mbps"])
        self.assertEqual(500, rec.call_args.kwargs["avail"])
        proxy._fill_marks.clear()

    def test_a_healthy_fill_records_its_rate(self):
        e = proxy._Entry("sigR", "/tmp/sigR", [], {"lbl": "X"}, None, 10_000_000,
                         "fast", "tt1")
        e.avail, e.consumers, e.last = 0, 1, time.time()
        proxy._entries["sigR"] = e
        proxy._fill_marks.clear()

        with patch("app.proxy.telemetry.record_buffer") as rec:
            asyncio.run(proxy._watch_fill(time.time()))
            e.avail = 8_000_000                              # 8 MB in 60s
            asyncio.run(proxy._watch_fill(time.time() + 60))

        self.assertEqual("fill", rec.call_args.args[0])
        self.assertAlmostEqual(1.07, rec.call_args.kwargs["mbps"], places=1)
        proxy._fill_marks.clear()

    def test_an_abandoned_stream_does_not_alarm(self):
        e = proxy._Entry("sigA2", "/tmp/sigA2", [], {"lbl": "X"}, None, 1000,
                         "fast", "tt1")
        e.avail, e.consumers = 500, 0
        e.last = time.time() - proxy.BUFFER_IDLE_GRACE - 1   # nobody waiting
        proxy._entries["sigA2"] = e
        proxy._fill_marks.clear()

        with patch("app.proxy.telemetry.record_buffer") as rec:
            asyncio.run(proxy._watch_fill(time.time()))
            asyncio.run(proxy._watch_fill(time.time() + 60))

        rec.assert_not_called()
        proxy._fill_marks.clear()

    def test_a_tail_probe_does_not_disable_backpressure(self):
        """Players read the container index from the end of the file on open.
        Letting that seek advance the backpressure anchor would leave
        avail-head negative forever, so the producer would never pause and
        would fetch a whole 4K remux for a viewer who watched ten minutes."""
        e = proxy._Entry("sigB", "/tmp/sigB", [], {"lbl": "X"}, None,
                         50_000_000_000, "fast", "tt1")
        e.avail, e.head = 1_000_000, 500_000

        self.assertEqual(500_000, e.head)
        self.assertLess(e.avail - e.head, proxy.BUFFER_AHEAD_BYTES)

    def test_producer_keeps_filling_across_a_reader_gap(self):
        """The bug that froze a 4.66 GB stream at 14.9%.

        A player fetching discrete ranges drops `consumers` to zero between
        them, and a range past the write head is served directly without ever
        taking a consumer ref. Pausing strictly on `consumers > 0` parked the
        producer for good: the buffer never grew again and every later range
        was fetched cold from upstream — endless buffering, no error logged.
        """
        e = proxy._Entry("sigF", "/tmp/sigF", [], None, None, None, "fast", "tt1")
        e.consumers = 0
        e.last = time.time()                       # a range was just served

        self.assertTrue(proxy._reader_interested(e))

    def test_producer_stops_for_a_genuinely_abandoned_stream(self):
        e = proxy._Entry("sigG", "/tmp/sigG", [], None, None, None, "fast", "tt1")
        e.consumers = 0
        e.last = time.time() - proxy.BUFFER_IDLE_GRACE - 1

        self.assertFalse(proxy._reader_interested(e))

    def test_a_live_reader_always_counts(self):
        e = proxy._Entry("sigH", "/tmp/sigH", [], None, None, None, "fast", "tt1")
        e.consumers = 1
        e.last = time.time() - 10_000              # ancient, but reading now

        self.assertTrue(proxy._reader_interested(e))

    def test_a_direct_read_is_playing_too(self):
        """A seek, or a session that never cached, is served straight through
        with no cache entry behind it. Home reported only the buffered half,
        so a stream plainly playing left the page blank."""
        proxy._direct[1] = {
            "media_id": "tt7", "label": "Show.S01E01.1080p.mkv", "debrid": "RD",
            "res": 1080, "node": "", "avail": 0, "total": None,
            "consumers": 1, "picker": "fast", "mode": "direct",
        }

        out = proxy.active_stream_details()

        self.assertEqual(1, len(out))
        self.assertEqual("tt7", out[0]["media_id"])
        self.assertEqual("direct", out[0]["mode"])
        # No cache file behind it, so no buffered-ahead figure to invent.
        self.assertIsNone(out[0]["total"])

    def test_buffered_and_direct_are_reported_together(self):
        entry = proxy._Entry("sigA", "/tmp/sigA", [], {"lbl": "A"}, None, None,
                             "fast", "tt1")
        entry.consumers = 1
        proxy._entries["sigA"] = entry
        proxy._direct[1] = {
            "media_id": "tt2", "label": "B", "debrid": "", "res": 0,
            "node": "", "avail": 0, "total": None, "consumers": 1,
            "picker": "fast", "mode": "direct",
        }

        out = proxy.active_stream_details()

        self.assertEqual({"tt1", "tt2"}, {d["media_id"] for d in out})
        self.assertEqual({"buffered", "direct"}, {d["mode"] for d in out})

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


class SuffixRangeTests(unittest.TestCase):
    """`bytes=-N` is legal HTTP that a real upstream gets wrong.

    nzbdav parses the Range header by splitting on "-" and dropping empty
    parts, so `bytes=-8388608` is answered with the *first* 8 MB of the file.
    Verified live: Content-Range came back `bytes 0-8388608/6108448173`. Our
    validator correctly refuses that — which meant tail warming silently never
    ran on direct-usenet sources, the one lane it was written for, and a
    player's opening index read became a 502. Whenever the total is known,
    ask the unambiguous way.
    """

    def test_tail_warming_asks_for_an_absolute_range(self):
        e = proxy._Entry("sigA", "/tmp/sigA", [], {"u": "https://h/f.mkv"},
                         None, 1000, "fast", "tt1")
        sent = {}

        async def fake_send(cand, rh):
            sent["rh"] = rh
            raise RuntimeError("stop here — the header is the assertion")

        with patch("app.proxy._send", fake_send), \
             patch("app.proxy.BUFFER_TAIL_BYTES", 100):
            asyncio.run(proxy._fetch_tail(e))

        self.assertEqual("bytes=900-999", sent["rh"])
        self.assertNotIn("bytes=-", sent["rh"])

    def test_a_player_suffix_range_is_translated_when_the_total_is_known(self):
        session = {"id": "tt1", "picker": "fast"}
        cand = {"sig": "file:" + "a" * 64, "lbl": "X", "u": "https://h/f.mkv"}
        req = type("R", (), {"headers": {"range": "bytes=-65536"}})()
        sent = {}

        async def fake_send(c, rh):
            sent["rh"] = rh
            raise RuntimeError("stop here")

        with patch("app.proxy._send", fake_send), \
             patch("app.proxy.telemetry.record_buffer"):
            asyncio.run(proxy._serve_direct(
                session, [cand], req, "tok", source=cand,
                expected_total=1_000_000))

        self.assertEqual("bytes=934464-999999", sent["rh"])

    def test_without_a_known_total_the_suffix_is_left_alone(self):
        """No total, no safe rewrite — forward it and let validation judge."""
        session = {"id": "tt1", "picker": "fast"}
        cand = {"sig": "file:" + "a" * 64, "lbl": "X", "u": "https://h/f.mkv"}
        req = type("R", (), {"headers": {"range": "bytes=-65536"}})()
        sent = {}

        async def fake_send(c, rh):
            sent["rh"] = rh
            raise RuntimeError("stop here")

        with patch("app.proxy._send", fake_send), \
             patch("app.proxy.telemetry.record_buffer"):
            asyncio.run(proxy._serve_direct(
                session, [cand], req, "tok", source=cand))

        self.assertEqual("bytes=-65536", sent["rh"])


class _StartResponse:
    """An upstream that accepts the request, then behaves as told."""

    def __init__(self, chunks, stall=0.0):
        self.status_code = 200
        self.headers = {}
        self._chunks = chunks
        self._stall = stall
        self.closed = False

    async def aclose(self):
        self.closed = True

    def aiter_raw(self):
        async def gen():
            if self._stall:
                await asyncio.sleep(self._stall)
            for c in self._chunks:
                yield c
        return gen()


class DeadStartTests(unittest.TestCase):
    """A source that delivers no bytes is a corpse, not a slow link.

    Both shapes were previously scored "slow": the candidate burned the whole
    evaluation deadline before anyone said so, and one bad session was not
    enough for reputation to cool it, so the very next open served the same
    corpse again. Live, that read as ~40s of "Starting stream" across two 502
    rounds for a release nobody could ever have played.
    """

    def _select(self, cands, rep):
        with patch("app.proxy.reputation", rep), \
             patch("app.proxy.telemetry.record_buffer"), \
             patch("app.proxy.usenet_health"), \
             patch("app.proxy.START_TTFB", 0.5), \
             patch("app.proxy.NZB_START_RETRY_SECS", 0):
            return asyncio.run(
                proxy._select_start(cands, None, 0, None, "tok"))

    @staticmethod
    def _rep():
        rep = MagicMock()
        rep.blocked.return_value = False
        rep.cooled.return_value = False
        return rep

    @staticmethod
    def _cand():
        return {"sig": "file:" + "a" * 64, "lbl": "Movie.2024.mkv",
                "u": "https://host/f.mkv", "dbr": ""}

    def test_a_source_that_never_sends_a_byte_is_dead_not_slow(self):
        c, rep = self._cand(), self._rep()
        resp = _StartResponse([b"x" * 4096], stall=30.0)

        with patch("app.proxy._send", AsyncMock(return_value=resp)):
            self.assertIsNone(self._select([c], rep))

        self.assertTrue(resp.closed)
        self.assertEqual("dead", rep.observe.call_args.args[2])
        self.assertTrue(rep.observe.call_args.kwargs["extreme"])
        rep.cooldown.assert_called_once_with(c["sig"])

    def test_a_source_that_replies_and_sends_nothing_is_dead(self):
        """HTTP 200, clean EOF, zero bytes — an expired debrid link's calling
        card. It is not a media container either, but it must be judged dead
        before that, so the verdict names the real fault."""
        c, rep = self._cand(), self._rep()
        resp = _StartResponse([])

        with patch("app.proxy._send", AsyncMock(return_value=resp)):
            self.assertIsNone(self._select([c], rep))

        self.assertEqual("dead", rep.observe.call_args.args[2])
        self.assertTrue(rep.observe.call_args.kwargs["extreme"])

    def test_the_wait_for_a_first_byte_is_capped_at_the_ttfb_budget(self):
        """Waiting past START_TTFB for byte one is time nobody can spend: the
        candidate is rejected at that budget whenever the byte arrives. Before
        the cap this blocked on the client's 60s read timeout instead."""
        c, rep = self._cand(), self._rep()
        resp = _StartResponse([b"x" * 4096], stall=30.0)

        t0 = time.monotonic()
        with patch("app.proxy._send", AsyncMock(return_value=resp)):
            self._select([c], rep)
        elapsed = time.monotonic() - t0

        self.assertLess(elapsed, 5.0, "dead source held the start gate open")

    def test_a_live_source_after_a_dead_one_still_gets_served(self):
        """Failing fast is only worth anything if the failover still lands."""
        dead, live = self._cand(), dict(self._cand(), sig="file:" + "b" * 64,
                                        u="https://host/g.mkv")
        rep = self._rep()
        responses = [_StartResponse([]),
                     _StartResponse([b"\x1aE\xdf\xa3" + b"\0" * (3 * 1024 * 1024)])]

        with patch("app.proxy._send", AsyncMock(side_effect=responses)):
            got = self._select([dead, live], rep)

        self.assertIsNotNone(got)
        self.assertEqual(1, got[0])                 # served the second candidate
        self.assertEqual(live["sig"], got[1]["sig"])


if __name__ == "__main__":
    unittest.main()
