import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TMDB_API_KEY", "test")
os.environ.setdefault("TRAKT_CLIENT_ID", "test")
os.environ.setdefault("TRAKT_CLIENT_SECRET", "test")
os.environ.setdefault("SETUP_SECRET", "test")

from app.recs import preseed, trakt


def _profile() -> dict:
    return {
        "genres": {"movie": [], "show": []},
        "seeds": [],
        "loved": [],
        "watched_imdb": set(),
        "watched_tmdb_movie": set(),
        "watched_tmdb_show": set(),
    }


class PreseedHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_dates_are_real_utc_unix_timestamps(self):
        found = {"media_type": "movie", "tmdb_id": 10, "genre_ids": []}
        entries = [{
            "type": "movie", "imdb": "tt10", "title": "Date only",
            "watched_at": "2024-01-02", "progress": 1,
        }, {
            "type": "movie", "imdb": "tt11", "title": "Full ISO",
            "watched_at": "2024-01-02T12:30:00Z", "progress": 1,
        }]
        with patch.object(preseed.tmdb, "find_by_imdb",
                          AsyncMock(return_value=found)):
            seeds = await preseed.history_seeds(entries)

        self.assertEqual(seeds[0]["last_watched"], 1_704_153_600)
        self.assertEqual(seeds[1]["last_watched"], 1_704_198_600)
        self.assertLess(seeds[0]["last_watched"], 2_000_000_000)

    async def test_invalid_history_date_falls_back_to_zero(self):
        found = {"media_type": "tv", "tmdb_id": 20, "genre_ids": []}
        entry = {
            "type": "show", "imdb": "tt20", "title": "Bad date",
            "watched_at": "not-a-date", "progress": 1, "episodes": 1,
        }
        with patch.object(preseed.tmdb, "find_by_imdb",
                          AsyncMock(return_value=found)):
            seeds = await preseed.history_seeds([entry])
        self.assertEqual(seeds[0]["last_watched"], 0.0)

    def test_only_imported_history_is_added_to_watched_exclusions(self):
        profile = _profile()
        history = {
            "type": "movie", "tmdb": 10, "imdb": "tt10", "genres": [],
            "last_watched": 100, "rating": 9, "source": "history",
            "watched": True, "engagement": 1,
        }
        taste = {
            "type": "show", "tmdb": 20, "imdb": "tt20", "genres": [],
            "last_watched": 0, "rating": 0, "source": "taste",
            "watched": False,
        }
        preseed.apply_to_profile(profile, [history, taste])

        self.assertEqual(profile["watched_imdb"], {"tt10"})
        self.assertEqual(profile["watched_tmdb_movie"], {10})
        self.assertEqual(profile["watched_tmdb_show"], set())

    def test_loved_and_seed_entries_are_not_duplicated(self):
        profile = _profile()
        real = {
            "type": "movie", "tmdb": 10, "imdb": "tt10", "title": "One",
            "genres": [], "last_watched": 100, "rating": 9, "score": 2,
        }
        duplicate_history = {
            **real, "last_watched": 200, "source": "history",
            "watched": True, "engagement": 1,
        }
        profile["seeds"] = [real]
        profile["loved"] = [real]

        preseed.apply_to_profile(profile, [duplicate_history])

        self.assertEqual(profile["seeds"], [real])
        self.assertEqual(profile["loved"], [real])


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Client:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def get(self, path, *, headers, params):
        self.calls.append((path, headers, params))
        return _Response(self.pages.get(params["page"], []))


class TraktPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_watched_movies_fetches_until_an_empty_page(self):
        client = _Client({
            1: [{"movie": {"ids": {"trakt": 1}}}],
            # Deliberately short: a short page must not end pagination.
            2: [{"movie": {"ids": {"trakt": 2}}}],
            3: [],
        })
        with patch.object(trakt, "_client", client):
            result = await trakt.watched_movies("token")

        self.assertEqual([x["movie"]["ids"]["trakt"] for x in result], [1, 2])
        self.assertEqual([call[2]["page"] for call in client.calls], [1, 2, 3])
        self.assertTrue(all(call[2]["limit"] == 250 for call in client.calls))
        self.assertTrue(all(call[2]["extended"] == "full"
                            for call in client.calls))

    async def test_watched_shows_preserves_episode_progress_and_full_metadata(self):
        watched = {
            "plays": 4,
            "last_watched_at": "2026-07-14T12:00:00.000Z",
            "show": {
                "title": "A Show",
                "genres": ["drama"],
                "ids": {"trakt": 3},
            },
            "seasons": [{
                "number": 1,
                "episodes": [
                    {"number": 1, "plays": 1,
                     "last_watched_at": "2026-07-13T12:00:00.000Z"},
                    {"number": 2, "plays": 3,
                     "last_watched_at": "2026-07-14T12:00:00.000Z"},
                ],
            }],
        }
        client = _Client({1: [watched], 2: []})
        with patch.object(trakt, "_client", client):
            result = await trakt.watched_shows("token")

        self.assertEqual(result, [watched])
        self.assertEqual(len(result[0]["seasons"][0]["episodes"]), 2)
        self.assertEqual(client.calls[0][0], "/sync/watched/shows")
        self.assertTrue(all(call[2]["extended"] == "full"
                            for call in client.calls))

    async def test_watchlist_is_paginated_with_full_metadata(self):
        client = _Client({
            1: [{"listed_at": "2026-07-01T00:00:00Z",
                 "movie": {"ids": {"trakt": 10}, "genres": ["comedy"]}}],
            # A short page is not proof that the snapshot is complete.
            2: [{"listed_at": "2026-07-02T00:00:00Z",
                 "movie": {"ids": {"trakt": 11}, "genres": ["drama"]}}],
            3: [],
        })
        with patch.object(trakt, "_client", client):
            result = await trakt.watchlist("token", "movies")

        self.assertEqual(
            [x["movie"]["ids"]["trakt"] for x in result], [10, 11])
        self.assertTrue(all(call[0] == "/sync/watchlist/movies"
                            for call in client.calls))
        self.assertTrue(all(call[2]["extended"] == "full"
                            for call in client.calls))
        self.assertEqual([call[2]["page"] for call in client.calls], [1, 2, 3])

    async def test_ratings_are_paginated_without_changing_entry_shape(self):
        first = {"rated_at": "2026-07-01T00:00:00Z", "rating": 9,
                 "show": {"ids": {"trakt": 20}}}
        second = {"rated_at": "2026-07-02T00:00:00Z", "rating": 7,
                  "show": {"ids": {"trakt": 21}}}
        client = _Client({1: [first], 2: [second], 3: []})
        with patch.object(trakt, "_client", client):
            result = await trakt.ratings("token", "shows")

        self.assertEqual(result, [first, second])
        self.assertTrue(all(call[0] == "/sync/ratings/shows"
                            for call in client.calls))
        self.assertTrue(all("extended" not in call[2]
                            for call in client.calls))
        self.assertTrue(all(call[2]["limit"] == 250
                            for call in client.calls))

    async def test_repeated_page_guard_prevents_an_infinite_loop(self):
        page = [{"movie": {"ids": {"trakt": 1}}}]
        client = _Client({1: page, 2: page})
        with patch.object(trakt, "_client", client):
            result = await trakt.watched_movies("token")

        self.assertEqual(result, page)
        self.assertEqual(len(client.calls), 2)

    async def test_repeated_page_guard_applies_to_watchlist_too(self):
        page = [{"show": {"ids": {"trakt": 30}}}]
        client = _Client({1: page, 2: page})
        with patch.object(trakt, "_client", client):
            result = await trakt.watchlist("token", "shows")

        self.assertEqual(result, page)
        self.assertEqual(len(client.calls), 2)

    async def test_non_list_watched_payload_is_rejected(self):
        client = _Client({1: {"unexpected": "shape"}})
        with patch.object(trakt, "_client", client):
            with self.assertRaises(TypeError):
                await trakt.watched_movies("token")


if __name__ == "__main__":
    unittest.main()
