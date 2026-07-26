import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.recs import config, db


def meta(content_id: str, media_type: str = "movie", **extra) -> dict:
    return {
        "id": content_id,
        "type": media_type,
        "name": f"Title {content_id}",
        **extra,
    }


def movie(content_id: str, plays: int, watched_at: str) -> dict:
    return {
        "plays": plays,
        "last_watched_at": watched_at,
        "movie": {"ids": {"imdb": content_id, "trakt": int(content_id[-2:])}},
    }


def show(content_id: str, episode_numbers: list[int], watched_at: str) -> dict:
    return {
        "last_watched_at": watched_at,
        "show": {"ids": {"imdb": content_id, "trakt": int(content_id[-2:])}},
        "seasons": [{
            "number": 1,
            "episodes": [
                {"number": number, "plays": 1, "last_watched_at": watched_at}
                for number in episode_numbers
            ],
        }],
    }


def watchlist(content_id: str, kind: str, listed_at: str) -> dict:
    return {
        "listed_at": listed_at,
        kind: {"ids": {"imdb": content_id, "trakt": int(content_id[-2:])}},
    }


class OutcomeLedgerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_path = config.DB_PATH
        config.DB_PATH = str(Path(self.tmp.name) / "test.db")
        await db.init()
        with patch("app.recs.db.time.time", return_value=900):
            await db.create_user(
                "viewer", "Viewer", None, "access", "refresh", 99_999)

    async def asyncTearDown(self):
        await db.close()
        db._conn = None
        config.DB_PATH = self.original_path
        self.tmp.cleanup()

    async def test_generations_are_immutable_and_keep_ranking_provenance(self):
        first = await db.replace_catalogs(
            "viewer",
            [{
                "id": "top",
                "type": "movie",
                "name": "Top Picks",
                "measurement": {"strategy": "best_bets"},
                "metas": [meta(
                    "tt000001",
                    measurement={
                        "candidate_source": "trakt",
                        "seed_content_id": "ttseed",
                        "rank_score": 8.5,
                        "score_components": {"taste": 0.9},
                    },
                )],
            }],
            policy_id="deep-home-v2",
            variant="A",
            trigger="nightly",
            generation_metadata={"row_target": 22},
            generated_at=1_000,
        )
        first_served = await db.get_catalog_metas("viewer", "movie", "top")
        self.assertNotIn("measurement", first_served[0])
        second = await db.replace_catalogs(
            "viewer",
            [{"id": "new", "type": "series", "name": "New",
              "metas": [meta("tt000002", "series")]}],
            generated_at=1_100,
        )

        self.assertNotEqual(first, second)
        generation = await db.get_generation(first)
        self.assertEqual(generation["policy_id"], "deep-home-v2")
        self.assertEqual(generation["variant"], "A")
        self.assertEqual(generation["metadata"], {"row_target": 22})
        item = (await db.get_generation_items(first))[0]
        self.assertEqual(item["content_id"], "tt000001")
        self.assertEqual(item["strategy"], "best_bets")
        self.assertEqual(item["candidate_source"], "trakt")
        self.assertEqual(item["seed_content_id"], "ttseed")
        self.assertEqual(item["rank_score"], 8.5)
        self.assertEqual(item["score_components"], {"taste": 0.9})
        self.assertEqual(item["meta"]["name"], "Title tt000001")
        self.assertEqual((await db.latest_generation("viewer"))["id"], second)
        self.assertEqual(
            (await db.get_catalog_defs("viewer"))[0]["id"], "new")

        # A build is availability, not an impression/exposure.
        async with db.conn().execute(
            "SELECT COUNT(*) AS n FROM recommendation_exposure"
        ) as cur:
            self.assertEqual((await cur.fetchone())["n"], 0)

    async def test_deliveries_infer_sessions_and_weight_exposure_by_depth(self):
        catalogs = [
            {"id": f"row-{n}", "type": "movie", "name": f"Row {n}",
             "metas": [meta(f"tt{n:06d}")]}
            for n in range(13)
        ]
        generation = await db.replace_catalogs(
            "viewer", catalogs, generated_at=1_000)

        top = await db.record_catalog_delivery(
            "viewer", "movie", "row-0", requested_at=2_000)
        middle = await db.record_catalog_delivery(
            "viewer", "movie", "row-7", requested_at=2_001)
        deep = await db.record_catalog_delivery(
            "viewer", "movie", "row-12", requested_at=2_002)
        self.assertEqual(top["generation_id"], generation)
        self.assertTrue(top["is_new_session"])
        self.assertFalse(middle["is_new_session"])
        self.assertEqual(top["session_id"], deep["session_id"])
        session = await db.get_session(top["session_id"])
        self.assertEqual(session["request_count"], 3)
        self.assertEqual(len(await db.get_session_deliveries(session["id"])), 3)

        async with db.conn().execute(
            "SELECT imdb_id, last_shown_at FROM recommendation_exposure"
            " WHERE user_token='viewer' ORDER BY imdb_id"
        ) as cur:
            exposures = {row["imdb_id"]: row["last_shown_at"]
                         for row in await cur.fetchall()}
        self.assertEqual(exposures["tt000000"], 2_000)
        self.assertEqual(exposures["tt000007"], 2_001 - 5 * 86400)
        self.assertNotIn("tt000012", exposures)

        # The one-time cleanup of pre-ledger phantom exposures must not erase
        # real delivery data on an ordinary application restart.
        await db.close()
        db._conn = None
        await db.init()
        async with db.conn().execute(
            "SELECT COUNT(*) AS n FROM recommendation_exposure"
            " WHERE user_token='viewer'"
        ) as cur:
            self.assertEqual((await cur.fetchone())["n"], 2)

        later = await db.record_catalog_delivery(
            "viewer", "movie", "row-0", requested_at=4_000)
        self.assertTrue(later["is_new_session"])
        self.assertNotEqual(later["session_id"], top["session_id"])

    async def test_outcomes_are_idempotent_and_attribute_only_exact_deliveries(self):
        generation = await db.replace_catalogs(
            "viewer",
            [
                {"id": "served", "type": "movie", "name": "Served",
                 "metas": [meta("tt000011")]},
                {"id": "not-served", "type": "movie", "name": "Not served",
                 "metas": [meta("tt000022")]},
            ],
            generated_at=900,
        )
        delivery = await db.record_catalog_delivery(
            "viewer", "movie", "served", requested_at=1_000)
        first = await db.record_trakt_outcome(
            "viewer", "first_movie_watch", "tt000011", "movie", 1_200,
            event_key="watch-11", observed_at=1_300)
        duplicate = await db.record_trakt_outcome(
            "viewer", "first_movie_watch", "tt000011", "movie", 1_200,
            event_key="watch-11", observed_at=1_400)
        self.assertEqual(first, duplicate)
        await db.record_trakt_outcome(
            "viewer", "first_movie_watch", "tt000022", "movie", 1_250,
            event_key="watch-22", observed_at=1_300)
        await db.record_trakt_outcome(
            "viewer", "first_movie_watch", "tt000011", "movie",
            1_000 + 73 * 3600, event_key="too-late", observed_at=300_000)

        self.assertEqual(await db.attribute_outcomes(
            "viewer", as_of=400_000), 1)
        self.assertEqual(await db.attribute_outcomes(
            "viewer", as_of=400_001), 0)
        outcomes = await db.get_outcomes("viewer")
        attributed = next(row for row in outcomes if row["id"] == first)
        self.assertEqual(attributed["attributed_generation_id"], generation)
        self.assertEqual(attributed["attributed_session_id"], delivery["session_id"])
        self.assertEqual(attributed["attributed_delivery_id"], delivery["delivery_id"])
        self.assertEqual(len(await db.get_outcomes(
            "viewer", unattributed_only=True)), 2)

    async def test_trakt_state_baselines_then_distinguishes_watch_outcomes(self):
        baseline = await db.upsert_trakt_state_and_record_outcomes(
            "viewer",
            [movie("tt000001", 1, "2026-07-01T00:00:00Z")],
            [show("tt000002", [1], "2026-07-01T01:00:00Z")],
            watchlist_movies=[watchlist(
                "tt000003", "movie", "2026-07-01T02:00:00Z")],
            watchlist_shows=[],
            observed_at=100,
        )
        self.assertEqual(baseline, [])

        changed = await db.upsert_trakt_state_and_record_outcomes(
            "viewer",
            [
                movie("tt000001", 1, "2026-07-01T00:00:00Z"),
                movie("tt000004", 1, "2026-07-02T00:00:00Z"),
            ],
            [
                show("tt000002", [1, 2], "2026-07-02T01:00:00Z"),
                show("tt000005", [1], "2026-07-02T02:00:00Z"),
            ],
            watchlist_movies=[
                watchlist("tt000003", "movie", "2026-07-01T02:00:00Z"),
                watchlist("tt000006", "movie", "2026-07-02T03:00:00Z"),
            ],
            watchlist_shows=[],
            observed_at=200,
        )
        self.assertEqual(
            {event["event_type"] for event in changed},
            {"first_movie_watch", "series_continuation",
             "first_series_episode", "watchlist_add"},
        )

        repeated = await db.upsert_trakt_state_and_record_outcomes(
            "viewer",
            [
                movie("tt000001", 1, "2026-07-01T00:00:00Z"),
                movie("tt000004", 1, "2026-07-02T00:00:00Z"),
            ],
            [
                show("tt000002", [1, 2], "2026-07-02T01:00:00Z"),
                show("tt000005", [1], "2026-07-02T02:00:00Z"),
            ],
            watchlist_movies=[
                watchlist("tt000003", "movie", "2026-07-01T02:00:00Z"),
                watchlist("tt000006", "movie", "2026-07-02T03:00:00Z"),
            ],
            watchlist_shows=[],
            observed_at=300,
        )
        self.assertEqual(repeated, [])

        rewatch = await db.upsert_trakt_state_and_record_outcomes(
            "viewer",
            [movie("tt000001", 2, "2026-07-03T00:00:00Z")],
            [show("tt000002", [1, 2], "2026-07-02T01:00:00Z")],
            observed_at=400,
        )
        self.assertEqual([event["event_type"] for event in rewatch],
                         ["movie_rewatch"])

    async def test_preference_defaults_and_validation_are_backward_compatible(self):
        user = await db.get_user("viewer")
        self.assertEqual(user["preferred_media"], "balanced")
        self.assertEqual(user["adventurousness"], 30)
        await db.update_preferences("viewer", "series", 75)
        user = await db.get_user("viewer")
        self.assertEqual((user["preferred_media"], user["adventurousness"]),
                         ("series", 75))
        with self.assertRaises(ValueError):
            await db.update_preferences("viewer", "anything", 30)

    async def test_delayed_first_watchlist_snapshot_is_a_baseline(self):
        with patch("app.recs.db.time.time", return_value=901):
            await db.create_user(
                "delayed", "Delayed", None, "access", "refresh", 99_999)
        await db.upsert_trakt_state_and_record_outcomes(
            "delayed", [], [], observed_at=100)
        first_watchlist = await db.upsert_trakt_state_and_record_outcomes(
            "delayed", [], [],
            watchlist_movies=[watchlist(
                "tt000031", "movie", "2026-07-01T00:00:00Z")],
            observed_at=200,
        )
        self.assertEqual(first_watchlist, [])
        addition = await db.upsert_trakt_state_and_record_outcomes(
            "delayed", [], [],
            watchlist_movies=[
                watchlist("tt000031", "movie", "2026-07-01T00:00:00Z"),
                watchlist("tt000032", "movie", "2026-07-02T00:00:00Z"),
            ],
            observed_at=300,
        )
        self.assertEqual([event["event_type"] for event in addition],
                         ["watchlist_add"])

    async def test_recommendation_summary_is_empty_and_rate_is_nullable(self):
        summary = await db.get_recommendation_summary(
            "viewer", window_days=30, as_of=1_000_000)
        self.assertEqual(summary, {
            "window_days": 30,
            "window_start": 1_000_000 - 30 * 86400,
            "as_of": 1_000_000,
            "generations": 0,
            "sessions": 0,
            "delivered_rows": 0,
            "outcome_events": 0,
            "attributed_outcomes": 0,
            "winning_sessions": 0,
            "assisted_pick_rate": None,
        })
        self.assertNotIn("user_token", summary)
        with self.assertRaises(ValueError):
            await db.get_recommendation_summary("viewer", window_days=0)

    async def test_recommendation_summary_uses_one_window_and_new_picks_win(self):
        # A fully attributed old session must not leak into the current window.
        await db.replace_catalogs(
            "viewer",
            [{"id": "old", "type": "movie", "name": "Old",
              "metas": [meta("tt000041")]}],
            generated_at=1_000,
        )
        await db.record_catalog_delivery(
            "viewer", "movie", "old", requested_at=2_000)
        await db.record_trakt_outcome(
            "viewer", "first_movie_watch", "tt000041", "movie", 3_000,
            event_key="old-win", observed_at=3_100)
        self.assertEqual(await db.attribute_outcomes(
            "viewer", as_of=4_000), 1)

        await db.replace_catalogs(
            "viewer",
            [
                {"id": "top", "type": "movie", "name": "Top",
                 "metas": [meta("tt000042")]},
                {"id": "more", "type": "movie", "name": "More",
                 "metas": [meta("tt000043")]},
            ],
            generated_at=20_000,
        )
        first_delivery = await db.record_catalog_delivery(
            "viewer", "movie", "top", requested_at=21_000)
        await db.record_catalog_delivery(
            "viewer", "movie", "more", requested_at=21_001)
        await db.record_trakt_outcome(
            "viewer", "first_movie_watch", "tt000042", "movie", 22_000,
            event_key="current-win", observed_at=22_100)
        await db.record_trakt_outcome(
            "viewer", "movie_rewatch", "tt000042", "movie", 22_001,
            event_key="current-rewatch", observed_at=22_100)
        await db.record_trakt_outcome(
            "viewer", "first_movie_watch", "tt999999", "movie", 22_002,
            event_key="current-unmatched", observed_at=22_100)
        self.assertEqual(await db.attribute_outcomes(
            "viewer", as_of=23_000), 2)

        second_session = await db.record_catalog_delivery(
            "viewer", "movie", "top", requested_at=24_000)
        self.assertNotEqual(first_delivery["session_id"],
                            second_session["session_id"])

        # At as_of=100,000 a one-day window starts at 13,600.
        summary = await db.get_recommendation_summary(
            "viewer", window_days=1, as_of=100_000)
        self.assertEqual(summary["generations"], 1)
        self.assertEqual(summary["sessions"], 2)
        self.assertEqual(summary["delivered_rows"], 3)
        self.assertEqual(summary["outcome_events"], 3)
        self.assertEqual(summary["attributed_outcomes"], 2)
        # The exact-title first watch wins the first session. The attributed
        # rewatch is measured but does not turn another session into a pick.
        self.assertEqual(summary["winning_sessions"], 1)
        self.assertEqual(summary["assisted_pick_rate"], 0.5)
        self.assertFalse(any("token" in key or key.endswith("_id")
                             for key in summary))


if __name__ == "__main__":
    unittest.main()
