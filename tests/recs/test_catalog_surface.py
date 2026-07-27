import datetime
import time
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

from app.recs import config
from app.recs.catalogs import (
    MIN_CATALOG_ROWS,
    ROW_TARGET_ITEMS,
    TARGET_CATALOG_ROWS,
    Generator,
)


def profile(movie_weight=10.0, show_weight=10.0):
    return {
        "genres": {
            "movie": [("science-fiction", movie_weight), ("drama", 5.0),
                      ("comedy", 3.0), ("adventure", 2.0)],
            "show": [("mystery", show_weight), ("drama", 5.0),
                     ("comedy", 3.0), ("science-fiction", 2.0)],
        },
        "decades": [(2010, 5.0), (2000, 3.0)],
        "languages": [("en", 10.0), ("ko", 2.0)],
        "seeds": [],
        "loved": [],
        "watched_imdb": set(),
        "watched_tmdb_movie": set(),
        "watched_tmdb_show": set(),
    }


def metas(prefix, count, media_type):
    return [
        {"id": f"tt{prefix}{index:04d}", "name": f"Title {prefix}-{index}",
         "type": media_type}
        for index in range(count)
    ]


class AdaptiveMediaTests(unittest.TestCase):
    def generator(self, movie_weight, show_weight, **user):
        viewer = {"token": "adaptive-viewer", "is_kid": 0, **user}
        generator = Generator(viewer)
        generator.profile = profile(movie_weight, show_weight)
        return generator

    def test_mixed_rows_follow_history_instead_of_forcing_half_and_half(self):
        generator = self.generator(90, 1)
        mixed = generator._mix_media(
            metas("m", 30, "movie"), metas("s", 30, "series"))

        self.assertEqual(len(mixed), ROW_TARGET_ITEMS)
        self.assertEqual(sum(item["type"] == "movie" for item in mixed), 24)
        self.assertEqual(sum(item["type"] == "series" for item in mixed), 6)

    def test_a_short_preferred_pool_is_filled_from_the_other_medium(self):
        generator = self.generator(90, 1)
        mixed = generator._mix_media(
            metas("m", 3, "movie"), metas("s", 30, "series"))

        self.assertEqual(len(mixed), ROW_TARGET_ITEMS)
        self.assertEqual(sum(item["type"] == "movie" for item in mixed), 3)
        self.assertEqual(sum(item["type"] == "series" for item in mixed), 27)

    def test_explicit_cold_start_media_preference_influences_the_mix(self):
        generator = self.generator(
            0, 0, preferred_media="series")
        generator.profile["genres"] = {"movie": [], "show": []}
        mixed = generator._mix_media(
            metas("m", 30, "movie"), metas("s", 30, "series"))

        self.assertEqual(sum(item["type"] == "series" for item in mixed), 21)


class DepthAwareFreshnessTests(unittest.TestCase):
    def test_deep_prefetched_rows_are_not_rotated_like_opening_rows(self):
        candidates = metas("f", 60, "movie")
        now = int(time.time())
        recent = {item["id"]: now for item in candidates[:30]}

        opening = Generator({"token": "fresh-viewer", "is_kid": 0})
        opening.recently_shown = recent
        opening_result = opening._freshen(candidates, 30, depth=0)

        deep = Generator({"token": "fresh-viewer", "is_kid": 0})
        deep.recently_shown = recent
        deep_result = deep._freshen(candidates, 30, depth=MIN_CATALOG_ROWS)

        unseen = {item["id"] for item in candidates[30:]}
        opening_unseen = sum(item["id"] in unseen for item in opening_result)
        deep_unseen = sum(item["id"] in unseen for item in deep_result)
        self.assertGreater(opening_unseen, deep_unseen)
        self.assertLessEqual(deep_unseen, 2)


class CatalogIntegrityTests(unittest.TestCase):
    def test_add_rechecks_watched_and_cross_row_duplicates(self):
        generator = Generator({"token": "integrity-viewer", "is_kid": 0})
        generator.profile = profile()
        generator.profile["watched_imdb"].add("ttwatched")

        first = [
            {"id": "ttwatched", "type": "movie"},
            {"id": "ttone", "type": "movie"},
            {"id": "ttone", "type": "movie"},
            {"id": "tttwo", "type": "movie"},
        ]
        second = [
            {"id": "ttone", "type": "movie"},
            {"id": "ttthree", "type": "movie"},
        ]
        self.assertTrue(generator._add(10, "one", "movie", "One", first, 1))
        self.assertTrue(generator._add(9, "two", "movie", "Two", second, 1))

        rows = [cat for _, cat in generator.rows]
        all_ids = [item["id"] for row in rows for item in row["metas"]]
        self.assertNotIn("ttwatched", all_ids)
        self.assertEqual(all_ids, ["ttone", "tttwo", "ttthree"])
        self.assertEqual(len(all_ids), len(set(all_ids)))


class DiscoverBoundsTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_discover_pool_is_bounded_by_today(self):
        generator = Generator({"token": "bounds-viewer", "is_kid": 0})
        generator.profile = profile()
        captured = []

        async def discover(media, params):
            captured.append((media, params))
            return []

        with patch("app.recs.catalogs.tmdb.discover", side_effect=discover), \
                patch.object(generator, "_resolve_ids", new=AsyncMock(return_value=[])):
            await generator._resolve_discover("movie", {"sort_by": "popularity.desc"}, 30)
            await generator._resolve_discover("tv", {"sort_by": "popularity.desc"}, 30)

        today = datetime.date.today().isoformat()
        self.assertEqual(captured[0][1]["primary_release_date.lte"], today)
        self.assertEqual(captured[1][1]["first_air_date.lte"], today)
        self.assertEqual(captured[0][1]["include_adult"], "false")


class SurfacePlannerTests(unittest.IsolatedAsyncioTestCase):
    async def test_planner_reaches_configured_depth_with_full_distinct_rows(self):
        generator = Generator({"token": "depth-viewer", "is_kid": 0})
        generator.profile = profile()
        generator.pinned_rows = 2
        call_number = 0

        async def candidates(media, params, limit, pages):
            nonlocal call_number
            call_number += 1
            media_type = "movie" if media == "movie" else "series"
            return metas(f"{call_number:02d}", limit, media_type)

        with patch.object(generator, "_resolve_discover", side_effect=candidates):
            await generator._ensure_catalog_depth()

        self.assertEqual(generator._catalog_count(), config.CATALOG_TARGET_ROWS)
        self.assertEqual(TARGET_CATALOG_ROWS, config.CATALOG_TARGET_ROWS)
        self.assertGreaterEqual(generator._catalog_count(), MIN_CATALOG_ROWS)
        self.assertTrue(all(
            len(cat["metas"]) == config.CATALOG_ROW_ITEMS
            for _, cat in generator.rows
        ))
        row_names = [cat["name"] for _, cat in generator.rows]
        self.assertEqual(len(row_names), len(set(row_names)))
        self.assertFalse(any(name.startswith("More Picks") for name in row_names))

        all_ids = [item["id"] for _, cat in generator.rows for item in cat["metas"]]
        self.assertEqual(len(all_ids), len(set(all_ids)))

    def test_strategy_plan_is_deterministic_and_contains_distinct_lenses(self):
        first = Generator({"token": "spec-viewer", "is_kid": 0})
        first.profile = profile()
        second = Generator({"token": "spec-viewer", "is_kid": 0})
        second.profile = profile()

        first_specs = first._depth_row_specs()
        second_specs = second._depth_row_specs()
        self.assertEqual(first_specs, second_specs)
        names = [spec["name"] for spec in first_specs]
        self.assertTrue(any(" + " in name for name in names))
        self.assertTrue(any(name.startswith("Recent ") for name in names))
        self.assertTrue(any(name.startswith("Under-the-Radar ") for name in names))
        self.assertTrue(any("Discoveries" in name for name in names))

    def test_adventurousness_only_promotes_exploration_in_the_deep_plan(self):
        cautious = Generator({
            "token": "adventure-viewer", "is_kid": 0,
            "adventurousness": 0,
        })
        adventurous = Generator({
            "token": "adventure-viewer", "is_kid": 0,
            "adventurousness": 100,
        })
        cautious.profile = profile()
        adventurous.profile = profile()

        cautious_names = [spec["name"] for spec in cautious._depth_row_specs()]
        adventurous_names = [spec["name"]
                             for spec in adventurous._depth_row_specs()]
        cautious_underseen = min(
            i for i, name in enumerate(cautious_names)
            if name.startswith("Under-the-Radar"))
        adventurous_underseen = min(
            i for i, name in enumerate(adventurous_names)
            if name.startswith("Under-the-Radar"))

        self.assertLess(adventurous_underseen, cautious_underseen)
        # High-confidence intent rows are generated outside this plan, so the
        # control cannot move Top Picks or Watchlist out of the opening zone.
        self.assertFalse(any(name == "Top Picks" for name in adventurous_names))


class OutcomePollingIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_polls_state_before_snapshotting_policy_metadata(self):
        user = {
            "token": "ledger-viewer", "is_kid": 0,
            "preferred_media": "series", "adventurousness": 72,
        }
        generator = Generator(user, trigger="nightly")
        watched_movies = [{"movie": {"ids": {"imdb": "tt1"}}}]
        watched_shows = [{"show": {"ids": {"imdb": "tt2"}}, "seasons": []}]
        movie_watchlist = [{"movie": {"ids": {"imdb": "tt3"}}}]
        show_watchlist = [{"show": {"ids": {"imdb": "tt4"}}}]
        replace = AsyncMock(return_value=11)
        upsert = AsyncMock(return_value=[])
        attribute = AsyncMock(return_value=0)
        no_row_methods = (
            "_because_you_watched", "_more_like_loved",
            "_new_releases", "_trending_row", "_genre_rows", "_person_rows",
            "_acclaimed_row", "_hidden_gems", "_decade_rows", "_language_row",
            "_popular_row", "_ensure_catalog_depth",
        )

        with ExitStack() as stack:
            stack.enter_context(patch("app.recs.catalogs.db.get_recently_shown",
                                      AsyncMock(return_value={})))
            stack.enter_context(patch("app.recs.catalogs.db.upsert_title_state_and_record_outcomes",
                                      upsert))
            stack.enter_context(patch("app.recs.catalogs.db.attribute_outcomes", attribute))
            stack.enter_context(patch("app.recs.catalogs.db.replace_catalogs", replace))
            stack.enter_context(patch("app.recs.catalogs.db.mark_generated", AsyncMock()))
            # Taste comes from this service's own play history now, not Trakt.
            stack.enter_context(patch(
                "app.recs.catalogs.local_history.watched_lists",
                AsyncMock(return_value=(watched_movies, watched_shows, [], []))))
            stack.enter_context(patch(
                "app.recs.catalogs.local_history.last_play_at",
                AsyncMock(return_value=0)))
            stack.enter_context(patch("app.recs.catalogs.preseed.load_for",
                                      return_value={"taste": [], "history": []}))
            stack.enter_context(patch.object(generator, "_holiday_row",
                                             AsyncMock(return_value=None)))
            stack.enter_context(patch.object(generator, "_top_picks",
                                             AsyncMock(return_value=None)))
            row_mocks = {}
            for method in no_row_methods:
                row_mocks[method] = stack.enter_context(
                    patch.object(generator, method, AsyncMock()))
            await generator.run()

        # A watchlist was Trakt's alone and has no local equivalent, so no
        # snapshot is supplied at all — the ledger keeps whatever intent it
        # already knows rather than being handed a guess.
        upsert.assert_awaited_once_with(
            "ledger-viewer", watched_movies, watched_shows,
        )
        attribute.assert_awaited_once_with(
            "ledger-viewer",
            lookback_seconds=config.OUTCOME_ATTRIBUTION_HOURS * 3600,
        )
        _, catalogs = replace.await_args.args
        self.assertEqual(catalogs, [])
        self.assertEqual(
            replace.await_args.kwargs["policy_id"],
            "deep-home-v3-home-release",
        )
        self.assertEqual(replace.await_args.kwargs["trigger"], "nightly")
        self.assertEqual(
            replace.await_args.kwargs["generation_metadata"]["adventurousness"],
            72,
        )


if __name__ == "__main__":
    unittest.main()
