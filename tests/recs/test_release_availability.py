import os
import datetime
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx


os.environ.setdefault("TMDB_API_KEY", "test")
os.environ.setdefault("TRAKT_CLIENT_ID", "test")
os.environ.setdefault("TRAKT_CLIENT_SECRET", "test")
os.environ.setdefault("SETUP_SECRET", "test")

from app.recs import config, db, tmdb


AS_OF = date(2026, 7, 14)


def movie_detail(*country_releases: tuple[str, list[tuple[int, object]]],
                 primary_release_date: str | None = None) -> dict:
    """Build the small part of a TMDB movie detail response used by the gate."""
    detail = {
        "release_dates": {
            "results": [
                {
                    "iso_3166_1": country,
                    "release_dates": [
                        {"type": release_type, "release_date": release_date}
                        for release_type, release_date in releases
                    ],
                }
                for country, releases in country_releases
            ],
        },
    }
    if primary_release_date is not None:
        detail["release_date"] = primary_release_date
    return detail


class HomeReleaseAvailabilityTests(unittest.TestCase):
    """The browse surface must not recommend movies available only as cams.

    TMDB release type values are 1 premiere, 2 limited theatrical,
    3 theatrical, 4 digital, 5 physical and 6 TV.  Types 4-6 establish a
    legitimate home-viewing release, but only once their date has arrived.
    """

    def test_past_or_same_day_digital_release_is_available(self):
        for release_date in (
            "2026-07-01T00:00:00.000Z",
            "2026-07-14T23:59:59.000Z",
        ):
            with self.subTest(release_date=release_date):
                detail = movie_detail(("US", [(4, release_date)]))
                self.assertTrue(
                    tmdb.is_home_released("movie", detail, as_of=AS_OF))

    def test_future_digital_release_is_not_yet_available(self):
        detail = movie_detail(("US", [
            (3, "2026-06-26T00:00:00.000Z"),
            (4, "2026-08-11T00:00:00.000Z"),
        ]))

        self.assertFalse(
            tmdb.is_home_released("movie", detail, as_of=AS_OF))

    def test_theatrical_and_premiere_releases_do_not_qualify(self):
        detail = movie_detail(("US", [
            (1, "2026-06-01T00:00:00.000Z"),
            (2, "2026-06-12T00:00:00.000Z"),
            (3, "2026-06-26T00:00:00.000Z"),
        ]))

        self.assertFalse(
            tmdb.is_home_released("movie", detail, as_of=AS_OF))

    def test_physical_and_tv_releases_each_qualify(self):
        for release_type in (5, 6):
            with self.subTest(release_type=release_type):
                detail = movie_detail(
                    ("US", [(release_type, "2026-07-01T00:00:00.000Z")]))
                self.assertTrue(
                    tmdb.is_home_released("movie", detail, as_of=AS_OF))

    def test_non_us_data_is_used_when_us_release_data_is_absent(self):
        detail = movie_detail(
            ("FR", [(3, "2026-05-01T00:00:00.000Z")]),
            ("GB", [(4, "2026-07-10T00:00:00.000Z")]),
        )

        self.assertTrue(
            tmdb.is_home_released("movie", detail, as_of=AS_OF))

    def test_foreign_digital_proves_a_clean_source_despite_us_theatrical_data(self):
        detail = movie_detail(
            ("US", [(3, "2026-06-26T00:00:00.000Z")]),
            ("GB", [(4, "2026-07-01T00:00:00.000Z")]),
        )

        self.assertTrue(
            tmdb.is_home_released("movie", detail, as_of=AS_OF))

    def test_old_movie_without_home_metadata_uses_conservative_fallback(self):
        old = movie_detail(
            ("US", [(3, "1999-01-01T00:00:00.000Z")]),
            primary_release_date="1999-01-01",
        )
        recent = movie_detail(
            ("US", [(3, "2026-06-26T00:00:00.000Z")]),
            primary_release_date="2026-06-26",
        )

        self.assertTrue(tmdb.is_home_released("movie", old, as_of=AS_OF))
        self.assertFalse(tmdb.is_home_released("movie", recent, as_of=AS_OF))

    def test_known_future_home_date_overrides_old_title_fallback(self):
        detail = movie_detail(
            ("US", [
                (3, "2025-01-01T00:00:00.000Z"),
                (4, "2026-08-01T00:00:00.000Z"),
            ]),
            primary_release_date="2025-01-01",
        )

        self.assertFalse(tmdb.is_home_released("movie", detail, as_of=AS_OF))

    def test_malformed_or_missing_home_release_dates_do_not_qualify(self):
        bad_details = [
            {},
            {"release_dates": None},
            {"release_dates": {"results": None}},
            movie_detail(("US", [(4, None)])),
            movie_detail(("US", [(4, "")])),
            movie_detail(("US", [(4, "not-a-date")])),
            movie_detail(("US", [(7, "2026-07-01T00:00:00.000Z")])),
        ]

        for detail in bad_details:
            with self.subTest(detail=detail):
                self.assertFalse(
                    tmdb.is_home_released("movie", detail, as_of=AS_OF))

    def test_series_are_unchanged_by_the_movie_release_gate(self):
        # Series availability is represented by first-air dates, not the
        # movie-only release_dates types; the new gate must not hide them.
        for detail in ({}, {"first_air_date": "2026-07-01"}):
            with self.subTest(detail=detail):
                self.assertTrue(
                    tmdb.is_home_released("tv", detail, as_of=AS_OF))


class HomeReleaseCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_path = config.DB_PATH
        config.DB_PATH = str(Path(self.tmp.name) / "release-cache.db")
        await db.init()

    async def asyncTearDown(self):
        await db.close()
        db._conn = None
        config.DB_PATH = self.original_path
        self.tmp.cleanup()

    @staticmethod
    def detail(primary: date, home: date | None = None) -> dict:
        releases = [(3, f"{primary.isoformat()}T00:00:00.000Z")]
        if home is not None:
            releases.append((4, f"{home.isoformat()}T00:00:00.000Z"))
        return {
            **movie_detail(("US", releases),
                           primary_release_date=primary.isoformat()),
            "id": 1081003,
            "title": "Example",
            "overview": "",
            "genres": [{"id": 28, "name": "Action"}],
            "external_ids": {"imdb_id": "tt8814476"},
            "adult": False,
        }

    async def test_current_year_legacy_cache_is_refetched_and_blocked(self):
        today = datetime.datetime.now(datetime.timezone.utc).date()
        primary = today - datetime.timedelta(days=18)
        legacy_meta = {
            "id": "tt8814476", "type": "movie", "name": "Example",
            "releaseInfo": str(today.year),
        }
        await db.cache_put_meta(
            1081003, "movie", "tt8814476", legacy_meta, "13")
        await db.conn().execute(
            "UPDATE meta_cache SET updated_at=0 WHERE tmdb_id=1081003")
        await db.conn().commit()
        get = AsyncMock(return_value=self.detail(primary))

        with patch.object(tmdb, "_get", get):
            self.assertIsNone(await tmdb.resolve_meta("movie", 1081003))

        cached = await db.cache_get_meta(1081003, "movie")
        expected = primary + datetime.timedelta(
            days=config.HOME_RELEASE_FALLBACK_DAYS)
        self.assertEqual(cached["home_release_date"], expected.isoformat())
        self.assertEqual(cached["home_release_verified"], 0)

        # A recent negative is cached so every row containing the same movie
        # does not cause another details request during this refresh.
        get.reset_mock()
        with patch.object(tmdb, "_get", get):
            self.assertIsNone(await tmdb.resolve_meta("movie", 1081003))
        get.assert_not_awaited()

    async def test_stale_blocked_cache_is_rechecked_when_digital_arrives(self):
        today = datetime.datetime.now(datetime.timezone.utc).date()
        primary = today - datetime.timedelta(days=45)
        future_fallback = primary + datetime.timedelta(
            days=config.HOME_RELEASE_FALLBACK_DAYS)
        await db.cache_put_meta(
            1081003, "movie", "tt8814476",
            {"id": "tt8814476", "type": "movie", "name": "Example",
             "releaseInfo": str(today.year)},
            "13", future_fallback.isoformat())
        await db.conn().execute(
            "UPDATE meta_cache SET updated_at=0 WHERE tmdb_id=1081003")
        await db.conn().commit()
        get = AsyncMock(return_value=self.detail(primary, today))

        with patch.object(tmdb, "_get", get):
            meta = await tmdb.resolve_meta("movie", 1081003)

        self.assertEqual(meta["id"], "tt8814476")
        self.assertEqual(
            (await db.cache_get_meta(1081003, "movie"))["home_release_date"],
            today.isoformat(),
        )
        self.assertEqual(
            (await db.cache_get_meta(1081003, "movie"))["home_release_verified"],
            1,
        )

    async def test_old_legacy_cache_is_grandfathered_without_api_work(self):
        today = datetime.datetime.now(datetime.timezone.utc).date()
        old_year = today.year - 3
        old_meta = {
            "id": "tt0000001", "type": "movie", "name": "Classic",
            "releaseInfo": str(old_year),
        }
        await db.cache_put_meta(1, "movie", "tt0000001", old_meta, "13")
        get = AsyncMock()

        with patch.object(tmdb, "_get", get):
            resolved = await tmdb.resolve_meta("movie", 1)

        self.assertEqual(resolved, old_meta)
        get.assert_not_awaited()

    async def test_failed_recheck_preserves_previously_good_cache_data(self):
        today = datetime.datetime.now(datetime.timezone.utc).date()
        cached_meta = {
            "id": "tt8814476", "type": "movie", "name": "Example",
            "releaseInfo": str(today.year),
        }
        future = today + datetime.timedelta(days=30)
        await db.cache_put_meta(
            1081003, "movie", "tt8814476", cached_meta, "13",
            future.isoformat())
        await db.conn().execute(
            "UPDATE meta_cache SET updated_at=0 WHERE tmdb_id=1081003")
        await db.conn().commit()
        request = httpx.Request("GET", "https://api.themoviedb.org/3/movie/1081003")
        response = httpx.Response(429, request=request)
        error = httpx.HTTPStatusError(
            "rate limited", request=request, response=response)

        with patch.object(tmdb, "_get", AsyncMock(side_effect=error)):
            self.assertIsNone(await tmdb.resolve_meta("movie", 1081003))

        cached = await db.cache_get_meta(1081003, "movie")
        self.assertEqual(cached["imdb_id"], "tt8814476")
        self.assertEqual(cached["meta"], cached_meta)
        self.assertEqual(cached["home_release_date"], future.isoformat())
        self.assertIsNone(cached["home_release_verified"])
        self.assertGreater(cached["updated_at"], 0)

    async def test_legacy_timeout_is_debounced_instead_of_retrying_every_row(self):
        today = datetime.datetime.now(datetime.timezone.utc).date()
        legacy_meta = {
            "id": "tt8814476", "type": "movie", "name": "Example",
            "releaseInfo": str(today.year),
        }
        await db.cache_put_meta(
            1081003, "movie", "tt8814476", legacy_meta, "13")
        await db.conn().execute(
            "UPDATE meta_cache SET updated_at=0 WHERE tmdb_id=1081003")
        await db.conn().commit()
        request = httpx.Request(
            "GET", "https://api.themoviedb.org/3/movie/1081003")
        timeout = httpx.ReadTimeout("timed out", request=request)
        get = AsyncMock(side_effect=timeout)

        with patch.object(tmdb, "_get", get):
            self.assertIsNone(await tmdb.resolve_meta("movie", 1081003))
            self.assertIsNone(await tmdb.resolve_meta("movie", 1081003))

        self.assertEqual(get.await_count, 1)
        self.assertEqual(
            (await db.cache_get_meta(1081003, "movie"))["meta"], legacy_meta)

    async def test_arrived_fallback_remains_recheckable_and_survives_timeout(self):
        today = datetime.datetime.now(datetime.timezone.utc).date()
        fallback = today - datetime.timedelta(days=1)
        cached_meta = {
            "id": "tt0000002", "type": "movie", "name": "Sparse Classic",
            "releaseInfo": str(today.year - 2),
        }
        await db.cache_put_meta(
            2, "movie", "tt0000002", cached_meta, "13",
            fallback.isoformat(), False)
        await db.conn().execute(
            "UPDATE meta_cache SET updated_at=0 WHERE tmdb_id=2")
        await db.conn().commit()
        request = httpx.Request("GET", "https://api.themoviedb.org/3/movie/2")
        timeout = httpx.ReadTimeout("timed out", request=request)
        get = AsyncMock(side_effect=timeout)

        with patch.object(tmdb, "_get", get):
            resolved = await tmdb.resolve_meta("movie", 2)

        self.assertEqual(resolved, cached_meta)
        get.assert_awaited_once()


class LegacyCacheMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_init_adds_home_release_column_without_losing_cache(self):
        tmp = tempfile.TemporaryDirectory()
        original_path = config.DB_PATH
        path = str(Path(tmp.name) / "legacy.db")
        legacy = sqlite3.connect(path)
        legacy.execute(
            "CREATE TABLE meta_cache (tmdb_id INTEGER NOT NULL,"
            " media_type TEXT NOT NULL, imdb_id TEXT, meta TEXT,"
            " updated_at INTEGER NOT NULL, PRIMARY KEY (tmdb_id, media_type))")
        legacy.execute(
            "INSERT INTO meta_cache VALUES (1, 'movie', 'tt0000001',"
            " '{\"id\":\"tt0000001\",\"releaseInfo\":\"1999\"}', 10)")
        legacy.commit()
        legacy.close()
        config.DB_PATH = path
        try:
            await db.init()
            cached = await db.cache_get_meta(1, "movie")
            self.assertEqual(cached["imdb_id"], "tt0000001")
            self.assertIsNone(cached["home_release_date"])
            self.assertIsNone(cached["home_release_verified"])
        finally:
            await db.close()
            db._conn = None
            config.DB_PATH = original_path
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
