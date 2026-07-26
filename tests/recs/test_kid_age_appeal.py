import unittest

from app.recs import tmdb


class KidAgeAppealTests(unittest.TestCase):
    def test_preschool_family_animation_outranks_adult_oriented_classic(self):
        family_animation = {
            "name": "The Friendly Forest",
            "description": "Animal friends share a magical adventure.",
            "genres": ["Animation", "Family", "Comedy"],
            "releaseInfo": "1974",
        }
        adult_classic = {
            "name": "The Minister's Choice",
            "description": "A political marriage reshapes a president's career.",
            "genres": ["Drama", "History", "Romance"],
            "releaseInfo": "1965",
        }

        family_result = tmdb.kid_age_appeal(family_animation, 4)
        classic_result = tmdb.kid_age_appeal(adult_classic, 4)

        self.assertEqual(family_result["classification"], "strong")
        self.assertEqual(classic_result["classification"], "weak")
        self.assertGreater(family_result["score"], classic_result["score"])
        self.assertIn(
            "adult-skewing-genres-without-child-anchor",
            {signal["code"] for signal in classic_result["signals"]},
        )

    def test_legitimate_old_family_title_does_not_get_legacy_penalty(self):
        classic_family = {
            "name": "The Emerald Road",
            "description": "A young girl finds friendship on a magical journey.",
            "genres": ["Adventure", "Family", "Fantasy"],
            "releaseInfo": "1939",
        }

        result = tmdb.kid_age_appeal(classic_family, 5)
        codes = {signal["code"] for signal in result["signals"]}

        self.assertGreaterEqual(result["score"], 2)
        self.assertNotIn("legacy-title-without-child-anchor", codes)
        self.assertNotIn("adult-skewing-genres-without-child-anchor", codes)

    def test_explicit_preschool_documentary_language_is_a_child_anchor(self):
        educational = {
            "name": "Letters and Numbers",
            "description": "Preschool viewers learn the alphabet and counting.",
            "genres": ["Documentary"],
            "releaseInfo": "2024",
        }

        result = tmdb.kid_age_appeal(educational, 3)
        codes = {signal["code"] for signal in result["signals"]}

        self.assertEqual(result["classification"], "good")
        self.assertIn("explicit-preschool-language", codes)
        self.assertNotIn("adult-skewing-genres-without-child-anchor", codes)

    def test_weak_appeal_is_a_ranking_result_not_a_safety_decision(self):
        meta = {
            "name": "A Government at War",
            "description": "A historical account of a military campaign.",
            "genres": ["Documentary", "History", "War"],
            "releaseInfo": "1948",
        }

        result = tmdb.kid_age_appeal(meta, 4)

        self.assertIsInstance(result, dict)
        self.assertLess(result["score"], 0)
        self.assertEqual(result["classification"], "weak")

    def test_certification_safety_remains_independent_and_strict(self):
        appealing_but_not_age_allowed = {
            "name": "Colorful Friends",
            "description": "Preschool friends learn the alphabet together.",
            "genres": ["Animation", "Family", "Kids"],
        }

        self.assertGreater(
            tmdb.kid_age_appeal_score(appealing_but_not_age_allowed, 4), 0)
        self.assertFalse(tmdb.cert_allowed(
            "13", 4, appealing_but_not_age_allowed["genres"]))
        self.assertFalse(tmdb.cert_allowed(
            "?", 4, appealing_but_not_age_allowed["genres"]))

    def test_school_age_and_teen_profiles_downrank_preschool_framing(self):
        preschool_show = {
            "name": "Toddler Sing-Along",
            "description": "Preschool friends practice counting.",
            "genres": ["Animation", "Kids"],
        }
        school_age_adventure = {
            "name": "The Hidden Map",
            "description": "Schoolchildren solve a mystery on an adventure.",
            "genres": ["Adventure", "Mystery"],
        }

        self.assertGreater(
            tmdb.kid_age_appeal_score(school_age_adventure, 11),
            tmdb.kid_age_appeal_score(preschool_show, 11),
        )
        self.assertEqual(
            tmdb.kid_age_appeal(preschool_show, 15)["classification"], "weak")

    def test_result_is_deterministic_and_signals_explain_the_score(self):
        meta = {
            "name": "High School Detectives",
            "description": "Teen friends come of age while solving a mystery.",
            "genres": ["Comedy", "Mystery"],
            "releaseInfo": "2026",
        }

        first = tmdb.kid_age_appeal(meta, 15)
        second = tmdb.kid_age_appeal(dict(meta), 15)

        self.assertEqual(first, second)
        self.assertEqual(
            first["score"], sum(signal["weight"] for signal in first["signals"]))
        self.assertEqual(first["age_band"], "teen")
        self.assertEqual(first["classification"], "strong")
        self.assertEqual(tmdb.kid_age_appeal_score(meta, 15), first["score"])

    def test_non_kid_call_is_neutral(self):
        result = tmdb.kid_age_appeal({"genres": "Family"}, None)

        self.assertEqual(result, {
            "score": 0,
            "classification": "neutral",
            "age_band": "not-applicable",
            "signals": [],
        })


if __name__ == "__main__":
    unittest.main()
