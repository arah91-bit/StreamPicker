"""How much say the fingerprint gets over each row, and the row it gets none of.

Rolling one tailoring strength across the surface would leave the already
narrow rows untouched and hollow out the ones that make a page feel like a
catalogue. Measured on a real 22-row surface before any of this: Popular Now
was 83% titles the fingerprint rates below average, Hidden Gems 57%,
Critically Acclaimed 53%. Those rows are the point of having a catalogue.
"""

import unittest
from unittest.mock import AsyncMock, patch

from app.recs import taste
from app.recs.catalogs import (
    EXPLORE_ROW_ID,
    EXPLORE_ROW_MIN_VOTES,
    SCORE_EXPLORE,
    Generator,
)


class FakePrint:
    """Lift comes straight from the token, so tests can state it plainly."""

    def __init__(self, lifts):
        self.lifts = lifts

    def lift(self, tokens):
        return max((self.lifts.get(t, 0.0) for t in tokens), default=0.0)

    def summary(self):
        return {}


def generator(lifts=None, store=None, **user):
    gen = Generator({"token": "surface", "is_kid": 0, **user})
    gen.fingerprint = FakePrint(lifts or {})
    gen.feature_store = dict(store or {})
    return gen


def metas(*ids):
    return [{"id": i, "name": i, "type": "series", "genres": ["Drama"]}
            for i in ids]


class TailoringStrengthTests(unittest.TestCase):
    def test_the_personal_rows_are_the_only_fully_tailored_ones(self):
        gen = generator()
        self.assertEqual(1.0, gen.tailoring_for("nr-top-picks"))
        self.assertEqual(0.8, gen.tailoring_for("nr-byw-1"))

    def test_rows_whose_promise_is_breadth_are_barely_touched(self):
        """"Popular Now" that is secretly "popular things you would like" is
        a lie, and on real data it is also an 83% cull."""
        gen = generator()
        for row in ("nr-popular", "nr-trending", "nr-new"):
            self.assertLessEqual(gen.tailoring_for(row), 0.05)

    def test_discovery_rows_are_not_tailored_at_all(self):
        gen = generator()
        for row in ("nr-acclaimed", "nr-gems", EXPLORE_ROW_ID):
            self.assertEqual(0.0, gen.tailoring_for(row))

    def test_an_unknown_row_gets_the_light_default(self):
        self.assertEqual(0.25, generator().tailoring_for("nr-something-new"))

    def test_the_longest_matching_prefix_wins(self):
        """`nr-top-picks` must not be read as an `nr-top` family."""
        self.assertEqual(1.0, generator().tailoring_for("nr-top-picks"))


class TailoringEffectTests(unittest.TestCase):
    def rows(self, cat_id):
        gen = generator(lifts={"hot": 9.0, "cold": 0.1},
                        store={"tt-low": ["cold"], "tt-high": ["hot"]})
        return [m["id"] for m in gen._tailor(cat_id, metas("tt-low", "tt-high"))]

    def test_a_personal_row_is_reordered_toward_the_viewer(self):
        self.assertEqual(["tt-high", "tt-low"], self.rows("nr-top-picks"))

    def test_a_zero_strength_row_keeps_its_own_order_exactly(self):
        self.assertEqual(["tt-low", "tt-high"], self.rows("nr-acclaimed"))

    def test_a_genre_row_moves_less_than_a_personal_one(self):
        """Light tailoring reorders within a row; it must not be able to turn
        one row into a different row. The comparison is the assertion — an
        absolute position would only be pinning today's constants."""
        def position(cat_id):
            gen = generator(lifts={"hot": 9.0},
                            store={f"tt{n}": ["cold"] for n in range(20)})
            gen.feature_store["tt19"] = ["hot"]
            ordered = gen._tailor(cat_id,
                                  metas(*[f"tt{n}" for n in range(20)]))
            return [m["id"] for m in ordered].index("tt19")

        self.assertEqual(0, position("nr-top-picks"))
        self.assertGreater(position("nr-genre-1"), 0)

    def test_titles_scoring_alike_do_not_collapse_into_one_rank(self):
        """Mapping each distinct lift to its last sorted index made a row
        where most titles score the same immovable: every tie took the top
        rank and the ordering never changed."""
        gen = generator(lifts={"hot": 9.0},
                        store={f"tt{n}": ["cold"] for n in range(20)})
        gen.feature_store["tt19"] = ["hot"]
        ordered = gen._tailor("nr-byw-1", metas(*[f"tt{n}" for n in range(20)]))
        self.assertLess([m["id"] for m in ordered].index("tt19"), 19)

    def test_a_row_of_titles_with_no_features_is_left_alone(self):
        gen = generator(lifts={"hot": 9.0}, store={})
        original = metas("tt-a", "tt-b", "tt-c")
        self.assertEqual([m["id"] for m in original],
                         [m["id"] for m in gen._tailor("nr-top-picks", original)])

    def test_without_a_fingerprint_nothing_is_reordered(self):
        gen = generator()
        gen.fingerprint = None
        original = metas("tt-a", "tt-b")
        self.assertEqual([m["id"] for m in original],
                         [m["id"] for m in gen._tailor("nr-top-picks", original)])


class ExplorationRowTests(unittest.IsolatedAsyncioTestCase):
    """The row the fingerprint had no hand in.

    Serves the viewer (a service locked into one shape stops being worth
    opening) and the model (plays come from what was recommended, so a closed
    loop only confirms itself; degeneracy is a proven consequence of that loop
    and random exploration over a large pool is the documented remedy).
    """

    def generator(self, found, **user):
        gen = Generator({"token": "explore", "is_kid": 0, **user})
        gen.profile = {"genres": {"movie": [], "show": []}, "decades": [],
                       "languages": [], "seeds": [], "loved": [],
                       "watched_imdb": set(), "watched_tmdb_movie": set(),
                       "watched_tmdb_show": set()}
        gen._resolve_discover = AsyncMock(
            side_effect=lambda media, params, limit, pages=1: found(media, params))
        return gen

    @staticmethod
    def pool(prefix, count, media="series"):
        return [{"id": f"tt{prefix}{n:03d}", "name": f"{prefix} {n}",
                 "type": media, "genres": ["Drama"], "imdbRating": "8.0"}
                for n in range(count)]

    async def test_the_row_is_built_and_placed_where_a_miss_is_cheap(self):
        gen = self.generator(lambda m, p: self.pool("E", 20))
        await gen._exploration_row()
        rows = {cat["id"]: score for score, cat in gen.rows}
        self.assertIn(EXPLORE_ROW_ID, rows)
        # Mid-page: below the personal rows, above the filler.
        self.assertLess(rows[EXPLORE_ROW_ID], 62)
        self.assertGreater(rows[EXPLORE_ROW_ID], 30)

    async def test_it_asks_for_quality_and_means_it(self):
        """An unfiltered rating sort returns titles with a single 10/10 vote.
        A row nobody asked for has to earn its slot."""
        seen = []

        def found(media, params):
            seen.append(params)
            return self.pool("E", 20)

        await self.generator(found)._exploration_row()
        self.assertTrue(seen)
        for params in seen:
            self.assertGreaterEqual(params["vote_count.gte"],
                                    EXPLORE_ROW_MIN_VOTES)
            self.assertIn("vote_average.gte", params)

    async def test_the_fingerprint_never_touches_it(self):
        """Tailoring it would defeat both of its jobs at once."""
        gen = self.generator(lambda m, p: self.pool("E", 20))
        gen.fingerprint = FakePrint({"hot": 9.0})
        gen.feature_store = {"ttE000": ["cold"], "ttE019": ["hot"]}
        await gen._exploration_row()
        self.assertEqual(0.0, gen.tailoring_for(EXPLORE_ROW_ID))

    async def test_two_viewers_do_not_get_the_same_row(self):
        """Shared randomness would be its own lock state."""
        def found(media, params):
            return self.pool(f"P{params['page']}", 20)

        first = self.generator(found, token="viewer-one")
        first.token = "viewer-one"
        second = self.generator(found, token="viewer-two")
        second.token = "viewer-two"
        await first._exploration_row()
        await second._exploration_row()
        self.assertNotEqual(
            [m["id"] for m in first.rows[0][1]["metas"]],
            [m["id"] for m in second.rows[0][1]["metas"]])

    async def test_a_thin_result_produces_no_row_rather_than_a_stub(self):
        gen = self.generator(lambda m, p: self.pool("E", 1))
        await gen._exploration_row()
        self.assertEqual([], gen.rows)

    async def test_a_failing_sweep_does_not_break_the_build(self):
        def boom(media, params):
            raise RuntimeError("TMDB said no")

        gen = self.generator(boom)
        await gen._exploration_row()
        self.assertEqual([], gen.rows)


if __name__ == "__main__":
    unittest.main()
