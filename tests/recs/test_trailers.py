"""Trailer playback: ad-free by construction, not by filtering.

The requirement was a trailer with no ads. An embed cannot deliver one — ads
are the uploader's monetisation setting, not a player parameter — so the player
is skipped and the underlying file is served instead. Ads live in the player,
so there is nothing left to block. These tests pin the properties that keeps
true: a muxed format, a proxy rather than a redirect, and a refusal to fetch
anything that is not a YouTube id.
"""

import time
import unittest
from unittest.mock import AsyncMock, patch

from app.recs import tmdb, trailers


class KeyChoiceTests(unittest.IsolatedAsyncioTestCase):
    async def videos(self, results):
        with patch.object(tmdb, "_get", AsyncMock(return_value={"results": results})):
            return await tmdb.trailer_key("movie", 1)

    async def test_a_full_trailer_beats_a_teaser(self):
        """A teaser is regularly thirty seconds of a logo."""
        self.assertEqual("full", await self.videos([
            {"site": "YouTube", "key": "teasr", "type": "Teaser", "official": True},
            {"site": "YouTube", "key": "full", "type": "Trailer", "official": True},
        ]))

    async def test_an_official_upload_beats_a_fan_one(self):
        self.assertEqual("real", await self.videos([
            {"site": "YouTube", "key": "fan", "type": "Trailer", "official": False},
            {"site": "YouTube", "key": "real", "type": "Trailer", "official": True},
        ]))

    async def test_only_youtube_is_considered(self):
        self.assertIsNone(await self.videos([
            {"site": "Vimeo", "key": "v", "type": "Trailer", "official": True}]))

    async def test_a_title_with_nothing_playable_says_so(self):
        self.assertIsNone(await self.videos([]))

    async def test_a_tmdb_failure_costs_the_button_not_the_session(self):
        with patch.object(tmdb, "_get", AsyncMock(side_effect=RuntimeError("502"))):
            self.assertIsNone(await tmdb.trailer_key("movie", 1))


class KeyGuardTests(unittest.IsolatedAsyncioTestCase):
    """The playback endpoint takes an id from the client, so the shape of that
    id is the whole boundary. Nothing that is not a YouTube video id may ever
    reach the extractor."""

    def test_a_real_id_passes(self):
        self.assertTrue(trailers.KEY_RE.match("dQw4w9WgXcQ"))

    def test_a_url_is_refused(self):
        for bad in ("https://evil.test/x", "../../etc/passwd", "a" * 40,
                    "short", "has space!!", "", "file:///etc/hosts"):
            self.assertFalse(trailers.KEY_RE.match(bad), bad)

    async def test_a_malformed_key_from_tmdb_is_dropped(self):
        trailers._keys.clear()
        with patch.object(trailers, "tmdb", create=True):
            with patch("app.recs.tmdb.trailer_key",
                       AsyncMock(return_value="https://youtu.be/abc")):
                self.assertIsNone(await trailers.key_for("movie", 7))


class StreamCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        trailers._streams.clear()
        trailers._locks.clear()

    async def test_extraction_happens_once_for_repeated_plays(self):
        calls = []

        def extract(key):
            calls.append(key)
            return f"https://x.test/v?expire={int(time.time()) + 7200}"

        with patch.object(trailers, "_extract", extract):
            await trailers.stream_url("dQw4w9WgXcQ")
            await trailers.stream_url("dQw4w9WgXcQ")
        self.assertEqual(1, len(calls))

    async def test_a_url_near_its_own_expiry_is_re_extracted(self):
        """The URL carries the deadline Google will enforce, so that is what
        the cache is checked against rather than a TTL of our invention."""
        calls = []

        def extract(key):
            calls.append(key)
            # Already inside the safety margin.
            return f"https://x.test/v?expire={int(time.time()) + 30}"

        with patch.object(trailers, "_extract", extract):
            await trailers.stream_url("dQw4w9WgXcQ")
            await trailers.stream_url("dQw4w9WgXcQ")
        self.assertEqual(2, len(calls))

    async def test_a_failed_extraction_is_not_cached_as_success(self):
        with patch.object(trailers, "_extract", lambda k: None):
            self.assertIsNone(await trailers.stream_url("dQw4w9WgXcQ"))
        self.assertEqual({}, trailers._streams)

    def test_an_unreadable_expiry_still_gets_a_lifetime(self):
        self.assertGreater(trailers._expiry_of("https://x.test/v"), time.time())

    def test_the_caches_cannot_grow_without_bound(self):
        cache = {}
        for n in range(trailers._MAX_CACHED + 50):
            cache[n] = n
            trailers._trim(cache)
        self.assertLessEqual(len(cache), trailers._MAX_CACHED)


class MuxedFormatTests(unittest.TestCase):
    """A <video> tag needs one file with both streams in it. YouTube's
    preferred adaptive formats are video-only, and picking one produces a
    silent trailer — which looks like a broken player, not a missing track."""

    def test_the_format_string_asks_for_progressive_first(self):
        first = trailers.FORMAT.split("/")[0]
        self.assertIn(first, ("22", "18"))

    def test_every_fallback_still_demands_audio(self):
        for choice in trailers.FORMAT.split("/"):
            if choice.isdigit():
                continue   # 18 and 22 are muxed by definition
            self.assertIn("acodec!=none", choice)


class SharedFragmentTests(unittest.TestCase):
    """One copy of the playback code, folded into both card pages.

    Duplicating it would let the ad-free path drift between them, which is the
    one thing on these pages that must not."""

    def test_both_pages_get_the_player(self):
        from app.recs import main

        for page in (main.BOOTSTRAP_HTML, main.MOVIE_NIGHT_HTML):
            self.assertIn("function playTrailer", page)
            self.assertIn("function attachTrailer", page)
            self.assertIn(".sheet video", page)          # the overlay styles

    def test_no_placeholder_survives_into_a_served_page(self):
        from app.recs import main

        for page in (main.BOOTSTRAP_HTML, main.MOVIE_NIGHT_HTML):
            self.assertNotIn("__TRAILER__", page)

    def test_each_page_points_the_player_at_its_own_authority(self):
        """A movie-night seat is deliberately not a viewer token. The shared
        fragment is only safe because the page decides where it looks."""
        from app.recs import main

        self.assertIn('base + "/trailer"', main.BOOTSTRAP_HTML)
        self.assertIn('base + "/trailer"', main.MOVIE_NIGHT_HTML)
        self.assertIn("/taste/api", main.BOOTSTRAP_HTML)

    def test_the_player_is_never_a_youtube_embed(self):
        """The whole reason this is not an <iframe>: there is no player flag
        that turns ads off, so the player is skipped entirely."""
        from app.recs import main

        for page in (main.BOOTSTRAP_HTML, main.MOVIE_NIGHT_HTML):
            self.assertNotIn("youtube.com/embed", page)
            self.assertNotIn("youtube-nocookie", page)
            self.assertNotIn("<iframe", page)


class SeatScopedTrailerTests(unittest.IsolatedAsyncioTestCase):
    """Movie night reaches trailers through its seat, and only its seat."""

    async def test_an_unknown_seat_gets_nothing(self):
        import httpx
        from app.recs import main

        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="http://testserver") as client:
            with patch.object(main.db, "match_by_seat_key",
                              AsyncMock(return_value=None)):
                r = await client.get("/night/not-a-seat/trailer/movie/27205")
                v = await client.get(
                    "/night/not-a-seat/trailer-video/dQw4w9WgXcQ.mp4")
        self.assertEqual(404, r.status_code)
        self.assertEqual(404, v.status_code)

    async def test_a_seat_never_needs_a_viewer_token(self):
        """The point of the separate route: holding a seat must not become a
        way into somebody's profile."""
        import httpx
        from app.recs import main

        seat = {"seat": 0, "seat_key": "k", "user_token": None, "label": "Guest"}
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="http://testserver") as client:
            with (patch.object(main.db, "match_by_seat_key",
                               AsyncMock(return_value=seat)),
                  patch.object(main.trailers, "key_for",
                               AsyncMock(return_value="dQw4w9WgXcQ"))):
                r = await client.get("/night/k/trailer/movie/27205")
        self.assertEqual(200, r.status_code)
        self.assertEqual("/night/k/trailer-video/dQw4w9WgXcQ.mp4",
                         r.json()["trailer"])


if __name__ == "__main__":
    unittest.main()
