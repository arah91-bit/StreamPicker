"""Asian Dramas descriptors must hide from *both* clients' home screens.

The two clients disagree about how a collection-backing catalog says "not a
home row". Mobile drops descriptors carrying a required extra; TV honours
`showInHome=false` and nothing else. A descriptor with only the required extra
looks correct on a phone and arrives as a loose row on the television.

That is what happened: 36 of 190 drama descriptors — the actor rows behind
Drama Stars — carried only the Mobile guard, so every viewer opted into dramas
had them scattered across the TV home screen instead of inside the imported
collection.
"""

import unittest

from app.recs import dramas


def _has_required_extra(catalog):
    return any(e.get("isRequired") for e in (catalog.get("extra") or []))


class DramaCollectionOnlyTests(unittest.TestCase):

    def setUp(self):
        self._saved = dramas.builder.state
        # Shaped like the real thing: actor rows historically carried the
        # required extra and no showInHome, which is the bug.
        dramas.builder.state = {
            "catalog_defs": [
                {"type": "series", "id": "ad_p_1879666", "name": "An Actor",
                 "extra": [{"name": "genre", "options": ["All"],
                            "isRequired": True}]},
                {"type": "series", "id": "ad_cc_kr", "name": "Korean Dramas",
                 "extra": [{"name": "genre", "options": ["Trending"],
                            "isRequired": True}]},
            ]
        }

    def tearDown(self):
        dramas.builder.state = self._saved

    def test_every_descriptor_carries_both_guards(self):
        defs = dramas.catalog_defs_for_user({"asian_dramas_row": 1})
        self.assertTrue(defs, "expected descriptors for an opted-in viewer")
        for catalog in defs:
            with self.subTest(catalog=catalog["id"]):
                self.assertIs(
                    catalog.get("showInHome"), False,
                    f"{catalog['id']} would show as a loose row on TV")
                self.assertTrue(
                    _has_required_extra(catalog),
                    f"{catalog['id']} would show as a loose row on Mobile")

    def test_stored_state_is_not_trusted(self):
        """The nightly state persists, so the guard is applied on the way out.

        Without this, fixing the builder would leave every existing viewer with
        stray rows until the next nightly build.
        """
        defs = dramas.catalog_defs_for_user({"asian_dramas_row": 1})
        actor = next(c for c in defs if c["id"] == "ad_p_1879666")
        self.assertIs(actor["showInHome"], False)
        # The stored definition is left alone; only the served copy is guarded.
        self.assertNotIn("showInHome", dramas.builder.state["catalog_defs"][0])

    def test_a_viewer_not_opted_in_gets_nothing(self):
        self.assertEqual([], dramas.catalog_defs_for_user({"asian_dramas_row": 0}))


if __name__ == "__main__":
    unittest.main()
