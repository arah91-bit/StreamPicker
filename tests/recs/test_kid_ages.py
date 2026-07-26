"""Kid ages: the band table, and the fact that an age is never a stored number.

The catalog builder's age slider is labelled from :data:`kids.AGE_BANDS`, and
tmdb's appeal scorer branches on the band for the same age.  If those two ever
disagree the page is telling an operator that moving the slider does something
it does not do, so the parity is asserted here rather than assumed.
"""

import unittest

from app.recs import kids, tmdb


class AgeBandTests(unittest.TestCase):
    def test_the_ranker_bands_come_from_the_table_the_builder_labels(self):
        for age in range(kids.MIN_AGE, kids.MAX_AGE + 1):
            self.assertEqual(tmdb._kid_age_band(age),
                             kids.band_for_age(age)["id"], age)

    def test_a_preschooler_and_a_preteen_are_not_the_same_band(self):
        self.assertEqual("preschool", kids.band_for_age(4)["id"])
        self.assertEqual("school-age", kids.band_for_age(12)["id"])

    def test_every_band_says_what_it_changes(self):
        for band in kids.AGE_BANDS:
            self.assertTrue(band["label"], band["id"])
            self.assertGreater(len(band["effect"]), 40, band["id"])

    def test_the_bands_cover_the_slider_without_a_gap(self):
        low = kids.MIN_AGE
        for band in kids.AGE_BANDS:
            self.assertGreaterEqual(band["max_age"], low, band["id"])
            low = band["max_age"] + 1
        self.assertEqual(kids.MAX_AGE + 1, low)

    def test_a_viewer_with_no_kid_age_has_no_band(self):
        self.assertIsNone(kids.band_for_age(None))
        self.assertEqual("not-applicable", tmdb._kid_age_band(None))

    def test_an_age_outside_the_slider_is_pulled_back_into_range(self):
        self.assertEqual(kids.MIN_AGE, kids.clamp_age(0))
        self.assertEqual(kids.MAX_AGE, kids.clamp_age(40))
        self.assertEqual(kids.DEFAULT_AGE, kids.clamp_age(None))


class LiveAgeTests(unittest.TestCase):
    """Nobody is asked for a birthday; an age is anchored when it is set and
    read back from the calendar, so it advances without anyone revisiting it."""

    def test_the_stored_number_never_overrides_the_elapsed_time(self):
        # Anchoring a 13-year-old a year ago leaves exactly the birthdate that
        # anchoring a 14-year-old today does.
        stale = {"is_kid": 1, "kid_age": 13,
                 "kid_birthdate": kids.birthdate_from_age(14)}

        self.assertEqual(14, kids.effective_kid_age(stale))

    def test_setting_an_age_today_reads_back_as_that_age(self):
        for age in (kids.MIN_AGE, 8, kids.MAX_AGE):
            user = {"is_kid": 1, "kid_age": age,
                    "kid_birthdate": kids.birthdate_from_age(age)}
            self.assertEqual(age, kids.effective_kid_age(user), age)

    def test_a_profile_that_has_aged_out_stops_being_filtered(self):
        user = {"is_kid": 1, "kid_age": 17,
                "kid_birthdate": kids.birthdate_from_age(18)}

        self.assertIsNone(kids.effective_kid_age(user))

    def test_a_viewer_who_is_not_a_kid_has_no_age(self):
        self.assertIsNone(kids.effective_kid_age(
            {"is_kid": 0, "kid_age": 8,
             "kid_birthdate": kids.birthdate_from_age(8)}))


if __name__ == "__main__":
    unittest.main()
