"""Feature extraction, the shared vocabulary, and scoring a title against a viewer.

The controls in `DiscriminationTests` are the ones the live prototype was
judged on. They are chosen so that a fingerprint which is merely responding to
"English, recent, animated" fails them, because that is exactly how the first
version failed.
"""

import math
import unittest

from app.recs import features, fingerprint


def detail(*, genres=(), keywords=(), cast=(), crew=(), networks=(),
           companies=(), language="en", date="2020-01-01", tv=False):
    doc = {
        "genres": [{"id": g} for g in genres],
        "keywords": ({"results": [{"id": k} for k in keywords]} if tv
                     else {"keywords": [{"id": k} for k in keywords]}),
        "networks": [{"id": n} for n in networks],
        "production_companies": [{"id": c} for c in companies],
        "original_language": language,
    }
    doc["first_air_date" if tv else "release_date"] = date
    credits = {"cast": [{"id": p} for p in cast],
               "crew": [{"id": p, "job": "Director"} for p in crew]}
    doc["aggregate_credits" if tv else "credits"] = credits
    return doc


def tokens(**kw):
    return features.extract(detail(**kw), "tv" if kw.get("tv") else "movie")


class ExtractionTests(unittest.TestCase):
    def test_every_family_is_represented(self):
        got = set(tokens(genres=[18], keywords=[900], cast=[1], crew=[2],
                         companies=[7], language="ja", date="1988-04-16"))
        self.assertEqual({"g:18", "k:900", "p:1", "p:2", "c:7", "l:ja", "d:1980"},
                         got)

    def test_television_keywords_live_under_a_different_key(self):
        """Movies return `keywords.keywords`, television `keywords.results` —
        same request parameter, different shape, and reading only one of them
        silently halves the vocabulary for half the catalogue."""
        self.assertIn("k:900", tokens(keywords=[900], tv=True))
        self.assertIn("k:900", tokens(keywords=[900]))

    def test_only_top_billing_is_taken_from_the_cast(self):
        """A long-running series lists hundreds of guest actors. Letting all of
        them in makes every procedural look like every other one."""
        got = tokens(cast=list(range(100, 140)))
        self.assertEqual(features.CAST_DEPTH,
                         len([t for t in got if t.startswith("p:")]))

    def test_crew_is_filtered_to_the_jobs_that_shape_a_title(self):
        doc = detail(cast=[], crew=[])
        doc["credits"]["crew"] = [
            {"id": 1, "job": "Director"},
            {"id": 2, "job": "Best Boy Electric"},
            {"id": 3, "job": "Executive Producer"},
        ]
        got = features.extract(doc, "movie")
        self.assertIn("p:1", got)
        self.assertIn("p:3", got)
        self.assertNotIn("p:2", got)

    def test_a_title_missing_everything_optional_still_yields_something(self):
        self.assertEqual(["d:2020", "l:en"],
                         features.extract(
                             {"original_language": "en",
                              "release_date": "2020-06-01"}, "movie"))

    def test_unusable_input_returns_empty_rather_than_raising(self):
        """This runs inside metadata resolution. It may not throw."""
        self.assertEqual([], features.extract({"genres": "not a list"}, "movie"))
        self.assertEqual([], features.extract({}, "movie"))

    def test_tokens_survive_a_round_trip(self):
        original = ["k:1", "p:2", "g:18"]
        self.assertEqual(original, features.decode(features.encode(original)))

    def test_corrupt_stored_features_decode_to_empty(self):
        self.assertEqual([], features.decode("{oh no"))
        self.assertEqual([], features.decode(None))


class VocabularyTests(unittest.TestCase):
    def vocab(self, corpus):
        frequency = {}
        for doc in corpus:
            for token in set(doc):
                frequency[token] = frequency.get(token, 0) + 1
        return features.Vocabulary(frequency, len(corpus))

    def test_a_token_on_everything_is_worth_less_than_a_rare_one(self):
        """`l:en` and `d:2020` are true of most of the catalogue. Treating
        them as informative is what ranked Love Island above Arrested
        Development in the first prototype."""
        vocab = self.vocab([["l:en", f"k:{n}"] for n in range(100)])
        self.assertLess(vocab.idf("l:en"), vocab.idf("k:7"))

    def test_an_unseen_token_is_treated_as_rare_but_not_infinite(self):
        vocab = self.vocab([["k:1"] for _ in range(50)])
        self.assertGreater(vocab.idf("k:never"), vocab.idf("k:1"))
        self.assertLess(vocab.idf("k:never"), 100)

    def test_vectors_are_unit_length(self):
        vocab = self.vocab([["k:1", "k:2"], ["k:2", "k:3"]])
        vector = vocab.vector(["k:1", "k:2"])
        self.assertAlmostEqual(
            1.0, math.sqrt(sum(v * v for v in vector.values())), places=6)

    def test_a_title_with_no_features_has_no_vector(self):
        self.assertEqual({}, self.vocab([["k:1"]]).vector([]))

    def test_keywords_outweigh_decade_at_equal_rarity(self):
        vocab = self.vocab([["k:1", "d:2020"], ["k:2", "d:2010"]])
        vector = vocab.vector(["k:1", "d:2020"])
        self.assertGreater(vector["k:1"], vector["d:2020"])


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        # A corpus where every title is English and recent, so those tokens
        # carry no information, and taste has to come from the keywords.
        self.corpus = {f"tt{n}": ["l:en", "d:2020", f"k:{n % 10}"]
                       for n in range(120)}
        frequency = {}
        for doc in self.corpus.values():
            for token in set(doc):
                frequency[token] = frequency.get(token, 0) + 1
        self.vocab = features.Vocabulary(frequency, len(self.corpus))

    def build(self, weights):
        return fingerprint.build(weights, self.corpus, self.vocab,
                                 list(self.corpus.values()))

    def test_a_title_sharing_the_watched_keyword_scores_above_baseline(self):
        print_ = self.build({f"tt{n}": 1.0 for n in range(0, 40, 10)})  # k:0
        self.assertGreater(print_.lift(["l:en", "d:2020", "k:0"]), 1.5)

    def test_a_title_sharing_only_the_universal_tokens_does_not(self):
        """The whole point of lift: matching on what everything matches on
        must read as no match at all."""
        print_ = self.build({f"tt{n}": 1.0 for n in range(0, 40, 10)})
        self.assertLess(print_.lift(["l:en", "d:2020", "k:7"]), 1.0)

    def test_engagement_decides_which_taste_dominates(self):
        loved = self.build({"tt0": 1.0, "tt1": 0.05})
        self.assertGreater(loved.lift(["k:0"]), loved.lift(["k:1"]))

    def test_percentile_places_a_title_against_the_catalogue(self):
        print_ = self.build({f"tt{n}": 1.0 for n in range(0, 40, 10)})
        self.assertGreater(print_.percentile(["k:0"]), 80)
        self.assertLess(print_.percentile(["k:7"]), 60)

    def test_top_features_returns_queryable_families_only(self):
        """Discover can filter on keywords and people. Asking it for a decade
        would return a third of the catalogue."""
        print_ = self.build({f"tt{n}": 1.0 for n in range(0, 40, 10)})
        self.assertTrue(all(t.startswith(("k:", "p:"))
                            for t in print_.top_features(10)))

    def test_a_fingerprint_from_almost_nothing_is_refused(self):
        """Better no fingerprint than one that ranks the catalogue off two
        titles with unearned confidence."""
        self.assertFalse(self.build({"tt0": 1.0}).usable)

    def test_titles_with_no_stored_features_are_skipped_not_counted(self):
        print_ = self.build({"tt0": 1.0, "tt-unknown": 1.0})
        self.assertEqual(1, print_.titles)

    def test_zero_and_negative_engagement_never_enters_the_vector(self):
        weights = {f"tt{n}": 1.0 for n in range(0, 40, 10)}
        weights["tt7"] = 0.0
        print_ = self.build(weights)
        self.assertEqual(4, print_.titles)

    def test_scoring_an_unknown_title_is_zero_rather_than_an_error(self):
        print_ = self.build({f"tt{n}": 1.0 for n in range(0, 40, 10)})
        self.assertEqual(0.0, print_.cosine([]))


if __name__ == "__main__":
    unittest.main()
