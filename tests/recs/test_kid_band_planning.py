"""Age as an input to recommendation, not just a certification ceiling.

A kid age used to reach three places: the certification gate, the discover
ceiling, and the order of items inside an already-built row.  It did not
reach the step that decides *which* rows get built, so a preschooler and a
preteen were handed the same adult-shaped row plan and told apart only by
what survived filtering.  These tests pin the two places that changed.
"""

import unittest

from app.recs import catalogs


def _profile(genres=None, decades=(), languages=()):
    return {
        "genres": genres or {"movie": [], "show": []},
        "decades": decades,
        "languages": languages,
        "watched_imdb": set(),
        "watched_tmdb_movie": set(),
        "watched_tmdb_show": set(),
    }


def _generator(kid_age, profile=None):
    gen = catalogs.Generator({
        "token": "t", "name": "Probe", "is_kid": kid_age is not None,
        "kid_age": kid_age, "adventurousness": 30,
        # No anchor: effective_kid_age falls back to the stored number, which
        # keeps these cases independent of today's date.
        "kid_birthdate": None,
    })
    gen.profile = profile or _profile()
    return gen


def _row(name, genres, description, year="2019"):
    slug = name.lower().replace(" ", "")[:6]
    return [{"id": f"tt-{slug}-{n}", "name": f"{name} {n}", "genres": genres,
             "description": description, "releaseInfo": year}
            for n in range(6)]


FAMILY = _row("Friendly Forest", ["Animation", "Family"],
              "Animal friends share a magical adventure.")
ADULT_DRAMA = _row("Minister's Choice", ["Drama", "History"],
                   "A political marriage reshapes a president's career.",
                   year="1965")


class RowOrderingTests(unittest.TestCase):
    def test_an_adult_surface_is_left_exactly_as_it_was(self):
        adult = _generator(None)

        self.assertIsNone(adult._band_fit(FAMILY))
        self.assertIsNone(adult._band_fit(ADULT_DRAMA))
        self.assertEqual(0.0, adult._band_strategy_shift(["drama"]))

    def test_a_preschooler_ranks_family_animation_over_adult_drama(self):
        gen = _generator(4)

        self.assertGreater(gen._band_fit(FAMILY), gen._band_fit(ADULT_DRAMA))
        self.assertLess(gen._band_fit(ADULT_DRAMA), 0)

    def test_the_same_two_rows_are_much_closer_for_a_teen(self):
        # The point is not that teens dislike family films; it is that the gap
        # a preschooler needs is not a gap a 15-year-old needs.
        kid, teen = _generator(4), _generator(15)

        self.assertGreater(kid._band_fit(FAMILY) - kid._band_fit(ADULT_DRAMA),
                           teen._band_fit(FAMILY) - teen._band_fit(ADULT_DRAMA))

    def test_the_fit_is_recorded_so_a_row_order_can_be_audited(self):
        gen = _generator(4)
        gen._add(catalogs.SCORE_DEPTH, "nr-depth-x", "movie", "X", FAMILY,
                 min_items=1)

        measurement = gen.rows[0][1]["measurement"]
        self.assertEqual(4, measurement["kid_age"])
        self.assertEqual("preschool", measurement["kid_age_band"])
        self.assertGreater(measurement["kid_band_fit"], 0)

    def test_an_adult_row_carries_no_kid_measurement(self):
        gen = _generator(None)
        gen._add(catalogs.SCORE_DEPTH, "nr-depth-x", "movie", "X", FAMILY,
                 min_items=1)

        self.assertNotIn("kid_band_fit", gen.rows[0][1]["measurement"])

    def test_band_fit_can_never_lift_a_discovery_row_over_an_intent_row(self):
        # Rows built from what this viewer actually watched are the intent
        # signal; no amount of developmental fit should outrank them. The
        # watchlist row used to be the anchor here — it went with Trakt, and
        # because-you-watched is now the lowest-scored intent row. (Top Picks
        # is pinned rather than scored, so nothing can outrank it at all.)
        best_depth = (catalogs.SCORE_DEPTH + 3 + catalogs.BAND_FIT_LIMIT + 4)

        self.assertLess(best_depth, catalogs.SCORE_BYW - 4)


class ColdStartPlanTests(unittest.TestCase):
    """With no history the planner falls back to a generic genre list, which
    is exactly when a new kid profile is most exposed to an adult row plan."""

    def _lead_genres(self, kid_age, count=6):
        names = [spec["name"]
                 for spec in _generator(kid_age)._depth_row_specs()[:count]]
        return " ".join(names).lower()

    def test_a_cold_adult_profile_still_leads_with_the_broad_genres(self):
        self.assertIn("drama", self._lead_genres(None))

    def test_a_cold_preschool_profile_leads_with_animation_and_family(self):
        lead = self._lead_genres(4)

        self.assertIn("animation", lead)
        self.assertNotIn("drama", lead)

    def test_a_preteen_gets_more_than_preschool_programming(self):
        # School age pulls up adventure/mystery/science fiction, which is the
        # whole reason the slider distinguishes 11 from 4.
        preteen = " ".join(
            spec["name"]
            for spec in _generator(11)._depth_row_specs()[:10]).lower()

        self.assertTrue(
            any(word in preteen for word in ("adventure", "sci-fi", "mystery")),
            preteen)


if __name__ == "__main__":
    unittest.main()
