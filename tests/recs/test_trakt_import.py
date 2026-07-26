"""One-time Trakt history import.

This runs once, while the OAuth grants still work, and its output is the seed
for everything the recommendation engine will later know about a viewer — so it
has to be faithful and it has to be idempotent.
"""

import unittest
from unittest.mock import AsyncMock, patch

from app.recs import trakt_import


def _movie(imdb, watched_at):
    return {"type": "movie", "watched_at": watched_at,
            "movie": {"ids": {"imdb": imdb}}}


def _episode(imdb, season, number, watched_at):
    return {"type": "episode", "watched_at": watched_at,
            "show": {"ids": {"imdb": imdb}},
            "episode": {"season": season, "number": number}}


class HistoryParsingTests(unittest.TestCase):
    def test_a_movie_becomes_one_row(self):
        rows = trakt_import._history_events(
            [_movie("tt0113568", "2024-03-02T20:00:00.000Z")])
        self.assertEqual(1, len(rows))
        self.assertEqual("tt0113568", rows[0]["content_id"])
        self.assertEqual("movie", rows[0]["media_type"])

    def test_an_episode_keeps_its_position(self):
        rows = trakt_import._history_events(
            [_episode("tt8050756", 1, 8, "2024-03-02T20:00:00.000Z")])
        self.assertEqual("tt8050756:1:8", rows[0]["content_id"])
        self.assertEqual((1, 8), (rows[0]["season"], rows[0]["episode"]))

    def test_entries_without_an_imdb_id_are_dropped(self):
        """Our whole content space is IMDb-addressed; a Trakt-only title has
        nothing the catalogs could ever match it to."""
        self.assertEqual([], trakt_import._history_events(
            [{"type": "movie", "watched_at": "2024-03-02T20:00:00.000Z",
              "movie": {"ids": {"tmdb": 603}}}]))

    def test_timestamps_are_read_as_utc(self):
        rows = trakt_import._history_events(
            [_movie("tt1", "2023-11-14T22:13:20.000Z")])
        self.assertEqual(1_700_000_000, rows[0]["played_at"])


class RollupTests(unittest.TestCase):
    def test_watched_shows_expand_to_one_row_per_episode(self):
        rows = trakt_import._watched_rollup([], [{
            "show": {"ids": {"imdb": "tt900"}},
            "seasons": [{"number": 2, "episodes": [
                {"number": 1, "last_watched_at": "2024-01-01T00:00:00.000Z"},
                {"number": 2, "last_watched_at": "2024-01-02T00:00:00.000Z"}]}],
        }])
        self.assertEqual(["tt900:2:1", "tt900:2:2"],
                         [r["content_id"] for r in rows])


class ImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_rollup_duplicates_of_history_events_collapse(self):
        """The same watch can appear in both feeds; it must land once."""
        stored = []
        with (
            patch.object(trakt_import.trakt, "ensure_fresh_token",
                         AsyncMock(return_value="a")),
            patch.object(trakt_import.trakt, "history", AsyncMock(
                return_value=[_movie("tt1", "2024-01-01T00:00:00.000Z")])),
            patch.object(trakt_import.trakt, "watched_movies", AsyncMock(
                return_value=[{"movie": {"ids": {"imdb": "tt1"}},
                               "last_watched_at": "2024-01-01T00:00:00.000Z"}])),
            patch.object(trakt_import.trakt, "watched_shows",
                         AsyncMock(return_value=[])),
            patch.object(trakt_import.db, "record_play",
                         AsyncMock(side_effect=lambda e: stored.append(e))),
        ):
            report = await trakt_import.import_user({"name": "A"}, "ukey")

        self.assertEqual(1, report["stored"])
        self.assertEqual(1, len(stored))

    async def test_imported_rows_are_marked_and_carry_no_resume_point(self):
        """An import has no byte offsets, so it must never look like something
        we served and could resume from."""
        stored = []
        with (
            patch.object(trakt_import.trakt, "ensure_fresh_token",
                         AsyncMock(return_value="a")),
            patch.object(trakt_import.trakt, "history", AsyncMock(
                return_value=[_movie("tt1", "2024-01-01T00:00:00.000Z")])),
            patch.object(trakt_import.trakt, "watched_movies",
                         AsyncMock(return_value=[])),
            patch.object(trakt_import.trakt, "watched_shows",
                         AsyncMock(return_value=[])),
            patch.object(trakt_import.db, "record_play",
                         AsyncMock(side_effect=lambda e: stored.append(e))),
        ):
            await trakt_import.import_user({"name": "A"}, "ukey")

        self.assertEqual("trakt-import", stored[0]["picker"])
        self.assertIsNone(stored[0]["position_bytes"])
        self.assertIsNone(stored[0]["position_pct"])

    async def test_an_expired_grant_reports_rather_than_raises(self):
        with patch.object(trakt_import.trakt, "ensure_fresh_token",
                          AsyncMock(side_effect=RuntimeError("expired"))):
            report = await trakt_import.import_user({"name": "A"}, "ukey")
        self.assertIn("no usable Trakt token", report["error"])
        self.assertEqual(0, report["stored"])


if __name__ == "__main__":
    unittest.main()
