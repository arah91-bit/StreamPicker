import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.recs import config, db, scheduler
from app.recs.catalogs import Generator


class RefreshPolicyTests(unittest.IsolatedAsyncioTestCase):
    def user(self, **updates):
        row = {
            "token": "user-token",
            "last_generated_at": 1_000,
            "last_served_at": None,
            "last_error": None,
        }
        row.update(updates)
        return row

    async def test_open_profile_gets_daily_freshness(self):
        reason = await scheduler._refresh_reason(
            self.user(last_served_at=1_001))
        self.assertEqual(reason, "daily freshness")

    async def test_idle_profile_still_gets_daily_freshness(self):
        self.assertEqual(
            await scheduler._refresh_reason(self.user()), "daily freshness")

    async def test_open_before_latest_build_is_still_refreshed_nightly(self):
        self.assertEqual(
            await scheduler._refresh_reason(self.user(last_served_at=999)),
            "daily freshness",
        )

    async def test_missing_or_failed_catalogs_are_retried(self):
        self.assertEqual(
            await scheduler._refresh_reason(self.user(last_generated_at=None)),
            "missing catalogs",
        )
        self.assertEqual(
            await scheduler._refresh_reason(self.user(last_error="boom")),
            "retry after error",
        )


class RotationTests(unittest.TestCase):
    def test_recent_exposure_moves_fresh_relevant_options_forward(self):
        metas = [{"id": f"tt{i:04d}"} for i in range(30)]
        now = time.time()

        first = Generator({"token": "viewer", "is_kid": 0})
        first.recently_shown = {m["id"]: int(now) for m in metas[:12]}
        result = first._freshen(metas, 12)

        # The daily seed makes a rebuild reproducible for the rest of the day.
        second = Generator({"token": "viewer", "is_kid": 0})
        second.recently_shown = dict(first.recently_shown)
        self.assertEqual(result, second._freshen(metas, 12))

        fresh_ids = {m["id"] for m in metas[12:]}
        self.assertGreaterEqual(sum(m["id"] in fresh_ids for m in result), 4)
        self.assertEqual(len(result), 12)
        self.assertEqual(len({m["id"] for m in result}), 12)


class OpenTrackingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_path = config.DB_PATH
        config.DB_PATH = str(Path(self.tmp.name) / "test.db")
        await db.init()
        with patch("app.recs.db.time.time", return_value=1_000):
            await db.create_user(
                "token", "Viewer", None, "access", "refresh", 99_999)
            await db.mark_generated("token", last_activity="", last_holiday="")

    async def asyncTearDown(self):
        await db.close()
        db._conn = None
        config.DB_PATH = self.original_path
        self.tmp.cleanup()

    async def test_serving_is_debounced_but_records_the_open(self):
        with patch("app.recs.db.time.time", return_value=1_002):
            await db.mark_served("token")
        user = await db.get_user("token")
        self.assertEqual(user["last_served_at"], 1_002)

        # A rebuild consumes that signal. The next real open must be recorded
        # even when it happens inside the normal one-hour debounce window.
        with patch("app.recs.db.time.time", return_value=1_050):
            await db.mark_generated("token", last_activity="", last_holiday="")
        with patch("app.recs.db.time.time", return_value=1_100):
            await db.mark_served("token")
        user = await db.get_user("token")
        self.assertEqual(user["last_served_at"], 1_100)
        self.assertGreater(user["last_served_at"], user["last_generated_at"])

        # A burst of catalog row requests must keep the first signal rather
        # than performing another write for every row.
        with patch("app.recs.db.time.time", return_value=1_200):
            await db.mark_served("token")
        user = await db.get_user("token")
        self.assertEqual(user["last_served_at"], 1_100)

    async def test_only_catalog_delivery_creates_recent_exposure(self):
        with patch("app.recs.db.time.time", return_value=1_010):
            await db.replace_catalogs("token", [{
                "id": "row", "type": "movie", "name": "Row",
                "metas": [{"id": "tt123", "name": "Example"}],
            }])
        with patch("app.recs.db.time.time", return_value=1_012):
            self.assertNotIn("tt123", await db.get_recently_shown("token"))
        with patch("app.recs.db.time.time", return_value=1_015):
            await db.record_catalog_delivery("token", "movie", "row")
        with patch("app.recs.db.time.time", return_value=1_020):
            recent = await db.get_recently_shown("token")
        self.assertEqual(recent["tt123"], 1_015)


if __name__ == "__main__":
    unittest.main()
