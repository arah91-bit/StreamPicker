import unittest
from unittest.mock import AsyncMock, patch

from app.recs import preseed


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
