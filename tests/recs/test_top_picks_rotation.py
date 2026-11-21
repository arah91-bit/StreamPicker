"""Top Picks rotation: turns, rests, a longer row, variety, and rebuilding only
for people who use it.

Rotation counts visits on which the viewer actually saw a card.
"""

import datetime
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.recs import config, db, playhistory, taste
from app.recs.catalogs import TOP_PICKS_ID, Generator


class TempDb(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_path = config.DB_PATH
        config.DB_PATH = str(Path(self.tmp.name) / "test.db")
        await db.init()

    async def asyncTearDown(self):
        await db.close()
        db._conn = None
        config.DB_PATH = self.original_path
        self.tmp.cleanup()

    async def seen(self, ids, day, now=1_000_000):
        return await db.record_top_picks_seen(
            "viewer", ids, day=day, now=now, turn_days=3, rest_days=21)


class TurnLedgerTests(TempDb):
    async def test_three_viewing_days_earn_a_rest(self):
        self.assertEqual([], await self.seen(["tt1"], "2026-10-01"))
        self.assertEqual([], await self.seen(["tt1"], "2026-10-02"))
        self.assertEqual(["tt1"], await self.seen(["tt1"], "2026-10-03"))
        resting, mid_turn = await db.top_picks_turns("viewer", now=1_000_001)
        self.assertIn("tt1", resting)
        self.assertNotIn("tt1", mid_turn)

    async def test_a_day_counts_once_however_often_the_row_loads(self):
        for _ in range(5):
            await self.seen(["tt1"], "2026-10-01")
        _, mid_turn = await db.top_picks_turns("viewer", now=1_000_001)
        self.assertEqual({"tt1": 1}, mid_turn)

    async def test_days_the_viewer_did_not_open_the_app_cost_nothing(self):
        """An occasional visitor: three visits a fortnight apart are still
        only three viewing days, and the turn is not over until the third."""
        await self.seen(["tt1"], "2026-09-01")
        await self.seen(["tt1"], "2026-09-15")
        _, mid_turn = await db.top_picks_turns("viewer", now=1_000_001)
        self.assertEqual(2, mid_turn["tt1"])

    async def test_a_resting_title_is_not_counted_while_it_waits(self):
        for day in ("2026-10-01", "2026-10-02", "2026-10-03"):
            await self.seen(["tt1"], day)
        # The row it sits in predates the rest; seeing it again is no new turn.
        self.assertEqual([], await self.seen(["tt1"], "2026-10-04", now=1_000_100))
        resting, _ = await db.top_picks_turns("viewer", now=1_000_101)
        self.assertEqual(1_000_000 + 21 * 86400, resting["tt1"])

    async def test_after_its_rest_a_title_starts_a_fresh_turn(self):
        for day in ("2026-10-01", "2026-10-02", "2026-10-03"):
            await self.seen(["tt1"], day)
        later = 1_000_000 + 22 * 86400
        await self.seen(["tt1"], "2026-10-26", now=later)
        resting, mid_turn = await db.top_picks_turns("viewer", now=later + 1)
        self.assertNotIn("tt1", resting)
        self.assertEqual({"tt1": 1}, mid_turn)

    async def test_deleting_a_viewer_clears_their_turns(self):
        await db.create_user("viewer", "Viewer")
        await self.seen(["tt1"], "2026-10-01")
        await db.delete_user("viewer")
        self.assertEqual(({}, {}), await db.top_picks_turns("viewer"))


class BackfillTests(TempDb):
    async def test_past_deliveries_start_the_ledger_where_reality_is(self):
        """Without this, a title already up front for weeks would get a full
        turn more on the day the rotation shipped."""
        await db.create_user("viewer", "Viewer")
        base = int(time.time()) - 5 * 86400
        for day in range(3):
            at = base + day * 86400
            with patch("app.recs.db.time.time", return_value=at):
                await db.replace_catalogs("viewer", [
                    {"id": TOP_PICKS_ID, "type": config.COMBINED_TYPE,
                     "name": "Top Picks",
                     "metas": [{"id": "tt-stale"}, {"id": f"tt-new{day}"}]}])
                await db.record_catalog_delivery(
                    "viewer", config.COMBINED_TYPE, TOP_PICKS_ID, requested_at=at)
        self.assertEqual(3, await db.backfill_top_picks_turns())
        resting, mid_turn = await db.top_picks_turns("viewer")
        self.assertIn("tt-stale", resting)
        self.assertEqual({"tt-new0": 1, "tt-new1": 1, "tt-new2": 1}, mid_turn)


class ServingTests(TempDb):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await db.create_user("viewer", "Viewer")
        self.metas = [{"id": f"tt{n:03d}", "name": str(n)} for n in range(50)]
        await db.replace_catalogs("viewer", [
            {"id": TOP_PICKS_ID, "type": config.COMBINED_TYPE,
             "name": "Top Picks", "metas": self.metas},
            {"id": "nr-genre-1", "type": "movie", "name": "Genre",
             "metas": [{"id": f"tg{n:03d}"} for n in range(50)]},
        ])
        await db.mark_generated("viewer", last_activity="", last_holiday="")

    async def serve(self, catalog_id, ctype=config.COMBINED_TYPE, skip=0):
        from app.recs import main
        with patch.object(main, "_refresh_if_stale"):
            return await main._serve_catalog("viewer", ctype, catalog_id, skip=skip)

    async def test_top_picks_is_served_whole(self):
        page = await self.serve(TOP_PICKS_ID)
        self.assertEqual(50, len(page["metas"]))

    async def test_other_rows_keep_their_page_size(self):
        page = await self.serve("nr-genre-1", ctype="movie")
        self.assertEqual(config.CATALOG_PAGE_SIZE, len(page["metas"]))

    async def test_only_the_front_of_the_row_counts_as_seen(self):
        await self.serve(TOP_PICKS_ID)
        _, mid_turn = await db.top_picks_turns("viewer")
        self.assertEqual(
            {m["id"] for m in self.metas[:config.TOP_PICKS_SEEN_POSITIONS]},
            set(mid_turn))

    async def test_served_cards_carry_no_ledger_fields(self):
        await db.replace_catalogs("viewer", [
            {"id": TOP_PICKS_ID, "type": config.COMBINED_TYPE, "name": "Top Picks",
             "metas": [{"id": "tt1", "_measurement": {"seed_content_id": "tt9"}}]}])
        page = await self.serve(TOP_PICKS_ID)
        self.assertEqual([{"id": "tt1"}], page["metas"])


class OpenRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_stale_slate_is_rebuilt_once_in_the_background(self):
        from app.recs import main
        built = []

        async def fake_generate(user, trigger):
            built.append(trigger)

        user = {"token": "viewer", "last_generated_at": time.time() - 3 * 86400}
        with patch.object(main, "generate_for_user", fake_generate):
            main._refresh_if_stale(user)
            main._refresh_if_stale(user)       # a second row in the same burst
            for task in list(main._background):
                await task
        self.assertEqual(["open-stale"], built)

    async def test_a_fresh_slate_is_left_alone(self):
        from app.recs import main
        generate = AsyncMock()
        with patch.object(main, "generate_for_user", generate):
            main._refresh_if_stale({"token": "viewer",
                                    "last_generated_at": time.time() - 3600})
        generate.assert_not_called()


def _signal(imdb_id, engagement=0.5, last_played_at=0, genres=("drama",)):
    return taste.TitleSignal(imdb_id=imdb_id, media_type="series",
                             engagement=engagement, confidence=1.0,
                             context=taste.CONTEXT_SOLO, genres=genres,
                             last_played_at=last_played_at)


def _seed(imdb_id, tmdb):
    return {"imdb": imdb_id, "tmdb": tmdb, "type": "show", "title": imdb_id,
            "year": 2020, "genres": ["drama"], "rating": 9, "last_watched": 0,
            "score": 1.0}


def _profile(seeds):
    return {"genres": {"movie": [("drama", 5.0)], "show": [("drama", 5.0)]},
            "decades": [(2010, 5.0)], "languages": [("en", 10.0)],
            "seeds": seeds, "loved": [], "watched_imdb": set(),
            "watched_tmdb_movie": set(), "watched_tmdb_show": set()}


class RecencyTests(unittest.TestCase):
    def generator(self, signals):
        gen = Generator({"token": "viewer", "is_kid": 0})
        gen.profile = _profile([_seed(s.imdb_id, n) for n, s in enumerate(signals)])
        gen.taste = taste.TasteModel(signals={s.key: s for s in signals})
        return gen

    def test_between_equals_what_they_are_watching_now_leads(self):
        day = 86400
        gen = self.generator([_signal("tt-old", 0.6, last_played_at=10 * day),
                              _signal("tt-now", 0.6, last_played_at=200 * day)])
        self.assertEqual("tt-now", gen._ranked_seeds(2)[0][0]["imdb"])

    def test_recency_never_overturns_a_big_engagement_gap(self):
        """The rule this replaced built the row out of whatever was opened
        last. A barely-watched title from last night must not beat a loved
        one from months ago."""
        day = 86400
        gen = self.generator([_signal("tt-loved", 0.9, last_played_at=10 * day),
                              _signal("tt-tried", 0.2, last_played_at=200 * day)])
        self.assertEqual("tt-loved", gen._ranked_seeds(2)[0][0]["imdb"])

    def test_age_is_measured_from_the_viewers_own_last_visit(self):
        """An occasional visitor whose last watch was months ago still has a
        recent past: what they watched on that visit."""
        day = 86400
        gen = self.generator([_signal("tt-last-visit", 0.5, last_played_at=100 * day)])
        self.assertAlmostEqual(1.0, gen._seed_recency(gen.taste.signals[
            next(iter(gen.taste.signals))]))


class RowTests(unittest.IsolatedAsyncioTestCase):
    def generator(self, similars, signals=None):
        gen = Generator({"token": "viewer", "is_kid": 0})
        signals = signals or [_signal("tt-a", 0.9), _signal("tt-b", 0.8)]
        gen.profile = _profile([_seed(s.imdb_id, n + 1) for n, s in enumerate(signals)])
        gen.taste = taste.TasteModel(signals={s.key: s for s in signals})
        gen._seed_similar = AsyncMock(side_effect=lambda s, limit: similars(s))
        return gen

    @staticmethod
    def similar(prefix, count, genres, rating="7.5"):
        return [{"id": f"tt{prefix}{n:03d}", "name": f"{prefix} {n}",
                 "type": "series", "genres": list(genres), "imdbRating": rating}
                for n in range(count)]

    async def test_the_row_holds_fifty(self):
        def similars(s):
            return self.similar("A" if s["imdb"] == "tt-a" else "B", 40, ["Drama"])
        row = await self.generator(similars)._top_picks()
        self.assertEqual(config.TOP_PICKS_ITEMS, len(row["metas"]))
        self.assertEqual(50, config.TOP_PICKS_ITEMS)

    async def test_a_resting_title_stays_out_of_the_row(self):
        def similars(s):
            return self.similar("A" if s["imdb"] == "tt-a" else "B", 40, ["Drama"])
        gen = self.generator(similars)
        gen.top_picks_resting = {"ttA000": time.time() + 86400}
        row = await gen._top_picks()
        self.assertNotIn("ttA000", {m["id"] for m in row["metas"]})

    async def test_a_thin_pool_backfills_with_rested_titles_last(self):
        gen = self.generator(lambda s: self.similar("A", 20, ["Drama"]))
        resting = {f"ttA{n:03d}": time.time() + 86400 for n in range(5)}
        gen.top_picks_resting = resting
        row = await gen._top_picks()
        ids = [m["id"] for m in row["metas"]]
        self.assertEqual(20, len(ids))
        self.assertEqual(set(resting), set(ids[-5:]))

    async def test_a_title_partway_through_its_turn_steps_back(self):
        gen = self.generator(lambda s: self.similar("A", 2, ["Drama"]) +
                             self.similar("B", 10, ["Drama"]))
        gen.rng.seed(1)
        gen.top_picks_seen = {"ttA000": 2}
        row = await gen._top_picks()
        ids = [m["id"] for m in row["metas"]]
        self.assertGreater(ids.index("ttA000"), ids.index("ttA001"))

    async def test_every_fifth_card_is_a_genre_they_also_watch(self):
        signals = [_signal("tt-a", 0.9, genres=("drama",)),
                   _signal("tt-b", 0.8, genres=("comedy",)),
                   _signal("tt-c", 0.7, genres=("crime",)),
                   _signal("tt-d", 0.5, genres=("documentary",))]

        def similars(s):
            return {"tt-a": self.similar("A", 30, ["Drama"]),
                    "tt-b": self.similar("B", 30, ["Comedy"]),
                    "tt-c": self.similar("C", 30, ["Crime"]),
                    "tt-d": self.similar("D", 30, ["Documentary"], rating="6.0")
                    }[s["imdb"]]

        row = await self.generator(similars, signals)._top_picks()
        flagged = [n for n, m in enumerate(row["metas"])
                   if m["_measurement"]["score_components"]["variety"]]
        self.assertTrue(flagged)
        self.assertTrue(all((n + 1) % config.TOP_PICKS_VARIETY_EVERY == 0
                            for n in flagged))
        self.assertTrue(all(row["metas"][n]["id"].startswith("ttD")
                            for n in flagged))

    async def test_each_pick_records_where_it_came_from(self):
        cached = self.similar("A", 30, ["Drama"])
        gen = self.generator(lambda s: cached)
        row = await gen._top_picks()
        first = row["metas"][0]["_measurement"]
        self.assertIn(first["seed_content_id"], {"tt-a", "tt-b"})
        self.assertIsInstance(first["rank_score"], float)
        # A copy, never the cached meta itself.
        self.assertTrue(all("_measurement" not in m for m in cached))


class VarietySelectionTests(unittest.TestCase):
    def cand(self, imdb_id, score, genres):
        return taste.Candidate(imdb_id=imdb_id, media_type="series",
                               score=score, genres=tuple(genres), seed_id="s")

    def test_off_by_default(self):
        pool = [self.cand(f"t{n}", 1 - n / 100, ["drama"]) for n in range(10)]
        pool.append(self.cand("doc", 0.1, ["documentary"]))
        chosen = taste.select_diverse(pool, 10)
        self.assertFalse(any(c.variety for c in chosen))

    def test_no_eligible_card_means_an_ordinary_one(self):
        pool = [self.cand(f"t{n}", 1 - n / 100, ["drama"]) for n in range(10)]
        chosen = taste.select_diverse(pool, 10, variety_every=5,
                                      variety_genres={"documentary"},
                                      core_genres={"drama"})
        self.assertEqual(10, len(chosen))
        self.assertFalse(any(c.variety for c in chosen))


class NewWatchTests(TempDb):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        playhistory._rebuilds.clear()
        await db.create_user("viewer", "Viewer")
        from app.recs.profile_streaming import private_namespace_for_user
        self.vk = private_namespace_for_user({"token": "viewer"})

    async def play(self, imdb_id, played_at, seconds=900):
        event = {"viewer_key": self.vk, "content_id": imdb_id, "imdb_id": imdb_id,
                 "media_type": "movie", "season": None, "episode": None,
                 "played_at": played_at, "seconds": seconds, "megabytes": 500,
                 "watched_pct": None, "picker": "fast", "position_bytes": None,
                 "total_bytes": None, "position_pct": None}
        await db.record_play(event)
        await playhistory._note_new_watch(event)

    async def run_rebuilds(self):
        built = []

        async def fake_generate(user, trigger):
            built.append(trigger)

        with (patch("app.recs.catalogs.generate_for_user", fake_generate),
              patch.object(config, "NEW_WATCH_REBUILD_DELAY_SECONDS", 0)):
            for task in list(playhistory._rebuilds.values()):
                await task
        return built

    async def test_starting_something_new_rebuilds_their_picks(self):
        with patch.object(config, "NEW_WATCH_REBUILD_DELAY_SECONDS", 0):
            await self.play("tt-new", int(time.time()))
        self.assertEqual(["new-watch"], await self.run_rebuilds())

    async def test_carrying_on_with_a_known_title_does_not(self):
        now = int(time.time())
        await db.record_play({"viewer_key": self.vk, "content_id": "tt-known",
                              "imdb_id": "tt-known", "media_type": "movie",
                              "season": None, "episode": None, "played_at": now - 86400,
                              "seconds": 900, "megabytes": 1, "watched_pct": None,
                              "picker": "fast", "position_bytes": None,
                              "total_bytes": None, "position_pct": None})
        await self.play("tt-known", now)
        self.assertEqual({}, playhistory._rebuilds)

    async def test_an_index_read_is_not_a_watch(self):
        await self.play("tt-new", int(time.time()), seconds=0)
        self.assertEqual({}, playhistory._rebuilds)

    async def test_a_slate_built_moments_ago_is_not_rebuilt(self):
        await db.mark_generated("viewer", last_activity="", last_holiday="")
        await self.play("tt-new", int(time.time()))
        self.assertEqual({}, playhistory._rebuilds)


if __name__ == "__main__":
    unittest.main()
