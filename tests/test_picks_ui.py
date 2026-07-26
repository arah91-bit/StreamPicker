"""The catalog builder's age control.

Age is the one viewer setting on this page that changes what a person is
shown rather than which rows they get, so the page has to state what each
band does — and it has to do that in the recommender's own terms.
"""

import json
import os
import re
import unittest

os.environ.setdefault("ADDON_SECRET", "test-secret")

from app import picks_ui  # noqa: E402
from app.recs import kids  # noqa: E402


class AgeSliderTests(unittest.TestCase):
    def setUp(self):
        self.page = picks_ui.render("setup-secret")

    def test_the_age_control_is_a_slider_over_the_supported_ages(self):
        slider = re.search(r'<input id="age"[^>]*>', self.page)

        self.assertIsNotNone(slider)
        self.assertIn('type="range"', slider.group())
        self.assertIn(f'min="{kids.MIN_AGE}"', slider.group())
        self.assertIn(f'max="{kids.MAX_AGE}"', slider.group())

    def test_every_band_the_ranker_uses_is_named_and_explained(self):
        for band in kids.AGE_BANDS:
            self.assertIn(band["label"], self.page, band["id"])
            # The bands reach the browser as JSON, which escapes the dashes.
            self.assertIn(json.dumps(band["effect"])[1:-1], self.page,
                          band["id"])

    def test_the_slider_labels_are_weighted_by_the_years_each_band_covers(self):
        # A label centred over the wrong stretch of track would put
        # "Preschool" under ages it does not apply to.
        weights = [int(w) for w in re.findall(r'<span style="flex:(\d+)">',
                                              self.page)]

        self.assertEqual(len(kids.AGE_BANDS), len(weights))
        self.assertEqual(kids.MAX_AGE - kids.MIN_AGE + 1, sum(weights))

    def test_no_birthday_is_asked_for(self):
        # The age is anchored internally so it can advance on its own; that
        # is not a reason to collect, or show back, a date of birth.
        self.assertNotIn('type="date"', self.page)
        self.assertNotIn("birthdate", self.page)
        self.assertNotRegex(self.page, r"(?i)birthday|date of birth")

    def test_kid_mode_can_never_be_switched_on_without_an_age(self):
        # A kid profile with no age filters nothing at all.
        self.assertIn("on ? null : age", self.page)
        self.assertIn("No age is set, so this profile is not being filtered",
                      self.page)

    def test_viewers_are_not_rendered_flush_against_each_other(self):
        # uitheme's .card is a bare surface: with neither gap nor padding the
        # viewers run together into one undivided wall of rows.
        self.assertRegex(picks_ui._CSS, r"#out\{[^}]*gap:\d+px")
        self.assertRegex(picks_ui._CSS, r"#out>\.card[^{]*\{[^}]*padding:")


class UnconfiguredPageTests(unittest.TestCase):
    def test_it_still_explains_itself_without_a_setup_secret(self):
        page = picks_ui.render_unconfigured()

        self.assertIn("SETUP_SECRET", page)
        self.assertNotIn('type="range"', page)


if __name__ == "__main__":
    unittest.main()
