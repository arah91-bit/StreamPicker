"""The taste bootstrapper, and the negative labels it is the only source of.

Everything else in this system infers taste from plays, where "did not watch"
and "never heard of" are the same observation — so nothing may be scored badly
in case the real cause was our own delivery failing. A tap on "not for me" is
unambiguous, and is what lets the fingerprint carry a negative term at all.
"""

import unittest
from unittest.mock import AsyncMock, patch

from app.recs import bootstrap, features, fingerprint


class DeckTests(unittest.IsolatedAsyncioTestCase):
    def user(self, genres=None, countries=None):
        import json
        return {"token": "t", "name": "V",
                "bootstrap_genres": json.dumps(genres or []),
                "bootstrap_countries": json.dumps(countries or [])}

    def resolved(self, imdb_id):
        return {"id": imdb_id, "name": imdb_id, "poster": "p",
                "genres": ["Drama"], "releaseInfo": "2011",
                "imdbRating": "8.0", "description": "x" * 400}

    async def test_cards_are_drawn_from_the_picked_genres(self):
        seen_params = []

        async def discover(media, params):
            seen_params.append((media, params))
            return [{"id": 1}]

        with (patch.object(bootstrap.tmdb, "discover", discover),
              patch.object(bootstrap.tmdb, "resolve_meta",
                           AsyncMock(return_value=self.resolved("tt1")))):
            await bootstrap.deck(self.user(["drama"]), set(), size=4)
        self.assertTrue(seen_params)
        self.assertTrue(all("with_genres" in p for _, p in seen_params))

    async def test_a_medium_without_the_picked_genre_is_skipped_not_widened(self):
        """TMDB's TV taxonomy has no Horror. Sweeping it unfiltered would hand
        back generic popular television and present it as their pick."""
        seen = []

        async def discover(media, params):
            seen.append(media)
            return [{"id": 1}]

        with (patch.object(bootstrap.tmdb, "discover", discover),
              patch.object(bootstrap.tmdb, "resolve_meta",
                           AsyncMock(return_value=self.resolved("tt1")))):
            await bootstrap.deck(self.user(["horror"]), set(), size=4)
        self.assertEqual(["movie"], seen)

    async def test_the_deck_is_famous_by_construction(self):
        """A verdict on a title nobody recognises is a coin flip dressed as
        data, so the floor is what makes the whole exercise worth anything."""
        seen_params = []

        async def discover(media, params):
            seen_params.append(params)
            return []

        with patch.object(bootstrap.tmdb, "discover", discover):
            await bootstrap.deck(self.user(["drama"]), set())
        for params in seen_params:
            self.assertGreaterEqual(params["vote_count.gte"], 900)
            self.assertEqual("false", params["include_adult"])

    async def test_a_title_already_ruled_on_is_never_asked_about_again(self):
        async def discover(media, params):
            return [{"id": 1}]

        with (patch.object(bootstrap.tmdb, "discover", discover),
              patch.object(bootstrap.tmdb, "resolve_meta",
                           AsyncMock(return_value=self.resolved("tt-known")))):
            cards = await bootstrap.deck(self.user(["drama"]), {"tt-known"})
        self.assertEqual([], cards)

    async def test_no_picks_still_produces_a_deck(self):
        """Someone who skips the picker gets popular titles rather than an
        empty screen."""
        async def discover(media, params):
            self.assertNotIn("with_genres", params)
            return [{"id": 1}]

        with (patch.object(bootstrap.tmdb, "discover", discover),
              patch.object(bootstrap.tmdb, "resolve_meta",
                           AsyncMock(return_value=self.resolved("tt1")))):
            cards = await bootstrap.deck(self.user(), set(), size=1)
        self.assertEqual(1, len(cards))

    async def test_a_failing_lookup_does_not_end_the_session(self):
        async def discover(media, params):
            raise RuntimeError("TMDB said no")

        with patch.object(bootstrap.tmdb, "discover", discover):
            self.assertEqual([], await bootstrap.deck(self.user(), set()))

    async def test_a_card_carries_what_a_person_needs_to_recognise_it(self):
        async def discover(media, params):
            return [{"id": 1}]

        with (patch.object(bootstrap.tmdb, "discover", discover),
              patch.object(bootstrap.tmdb, "resolve_meta",
                           AsyncMock(return_value=self.resolved("tt1")))):
            card = (await bootstrap.deck(self.user(), set(), size=1))[0]
        for field in ("id", "type", "title", "year", "poster", "genres"):
            self.assertIn(field, card)
        self.assertLessEqual(len(card["overview"]), 220)


class NegativeFingerprintTests(unittest.TestCase):
    """The only two-sided signal in the system."""

    def setUp(self):
        self.corpus = {}
        for n in range(100):
            self.corpus[f"tt-like{n}"] = ["k:liked", f"k:f{n}"]
            self.corpus[f"tt-hate{n}"] = ["k:hated", f"k:g{n}"]
        frequency = {}
        for doc in self.corpus.values():
            for token in set(doc):
                frequency[token] = frequency.get(token, 0) + 1
        self.vocab = features.Vocabulary(frequency, len(self.corpus))

    def build(self, liked, disliked=None):
        return fingerprint.build(
            {i: 1.0 for i in liked}, self.corpus, self.vocab,
            list(self.corpus.values()),
            disliked={i: 1.0 for i in (disliked or [])})

    def test_a_rejected_flavour_scores_below_an_accepted_one(self):
        print_ = self.build([f"tt-like{n}" for n in range(6)],
                            [f"tt-hate{n}" for n in range(6)])
        self.assertGreater(print_.cosine(["k:liked"]),
                           print_.cosine(["k:hated"]))

    def test_rejection_actually_lowers_the_score_it_would_have_had(self):
        without = self.build([f"tt-like{n}" for n in range(6)])
        with_no = self.build([f"tt-like{n}" for n in range(6)],
                             [f"tt-hate{n}" for n in range(6)])
        self.assertLess(with_no.cosine(["k:liked", "k:hated"]),
                        without.cosine(["k:liked", "k:hated"]))

    def test_a_couple_of_taps_is_a_mood_not_a_direction(self):
        print_ = self.build([f"tt-like{n}" for n in range(6)],
                            ["tt-hate0", "tt-hate1"])
        self.assertEqual({}, print_.negative)

    def test_scores_never_go_negative(self):
        """Lift is a ratio; a negative numerator inverts the whole scale."""
        print_ = self.build([f"tt-like{n}" for n in range(4)],
                            [f"tt-hate{n}" for n in range(20)])
        self.assertGreaterEqual(print_.cosine(["k:hated"]), 0.0)
        self.assertGreaterEqual(print_.lift(["k:hated"]), 0.0)

    def test_dislikes_are_reported_so_the_effect_is_visible(self):
        print_ = self.build([f"tt-like{n}" for n in range(6)],
                            [f"tt-hate{n}" for n in range(6)])
        self.assertEqual(6, print_.summary()["disliked"])

    def test_no_dislikes_leaves_the_fingerprint_exactly_as_it_was(self):
        plain = self.build([f"tt-like{n}" for n in range(6)])
        self.assertEqual({}, plain.negative)
        self.assertNotIn("disliked", plain.summary())


class SeenItTests(unittest.IsolatedAsyncioTestCase):
    """"It was fine" — watched, no opinion. A real answer with a real effect.

    It carries no direction, so it must never reach either centroid. But it
    does say they have seen the thing, and handing it back as a recommendation
    is the plainest way to look like nobody was listening.
    """

    def generator(self, feedback):
        from app.recs.catalogs import Generator
        gen = Generator({"token": "t", "name": "V", "is_kid": 0})
        gen.profile = {"watched_imdb": set(), "watched_tmdb_movie": set(),
                       "watched_tmdb_show": set()}
        gen.rated_titles = {i for i, e in feedback.items()
                            if e["verdict"] in ("liked", "disliked", "seen")}
        return gen

    def test_everything_they_have_seen_is_kept_out_of_the_rows(self):
        gen = self.generator({
            "tt-liked": {"verdict": "liked"},
            "tt-meh": {"verdict": "seen"},
            "tt-no": {"verdict": "disliked"},
        })
        self.assertEqual({"tt-liked", "tt-meh", "tt-no"}, gen._exclude())

    def test_never_seen_it_stays_recommendable(self):
        """Excluding a skip would turn every "not sure" into a permanent ban
        on a title they might well enjoy."""
        gen = self.generator({"tt-skipped": {"verdict": "unknown"}})
        self.assertEqual(set(), gen._exclude())

    async def test_it_reaches_neither_centroid(self):
        """Only liked and disliked may weigh anything. Verified by watching
        what `for_viewer` hands to the builder, rather than by inspecting a
        finished fingerprint — a corpus small enough to write out by hand
        cannot clear the usability floor, and shrinking that floor to make a
        test pass would be the wrong thing to change."""
        import app.recs.db as real_db
        from app.recs import taste

        store = {f"tt-{k}{n}": [f"k:{k}"]
                 for k in ("l", "m", "d") for n in range(6)}
        feedback = {}
        for kind, verdict in (("l", "liked"), ("m", "seen"), ("d", "disliked")):
            for n in range(6):
                feedback[f"tt-{kind}{n}"] = {"verdict": verdict,
                                             "media_type": "movie"}
        seen_args = {}

        def spy(weights, features_by_imdb, vocabulary, baseline_pool=None,
                rng=None, disliked=None):
            seen_args["weights"] = weights
            seen_args["disliked"] = disliked
            return fingerprint.Fingerprint({}, vocabulary, [], 0)

        with (patch.object(real_db, "features_by_imdb",
                           AsyncMock(return_value=store)),
              patch.object(real_db, "feedback_for",
                           AsyncMock(return_value=feedback)),
              patch.object(features, "vocabulary", AsyncMock(
                  return_value=features.Vocabulary({"k:l": 6}, 18))),
              patch.object(fingerprint, "build", spy)):
            await fingerprint.for_viewer(taste.TasteModel(), None,
                                         user_token="t")

        self.assertEqual({f"tt-l{n}" for n in range(6)},
                         set(seen_args["weights"]))
        self.assertEqual({f"tt-d{n}" for n in range(6)},
                         set(seen_args["disliked"]))


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_enough_is_reached_on_rated_titles_not_on_skips(self):
        """"Never seen it" is the honest answer to most of a deck and says
        nothing about taste, so it cannot be what completes a session."""
        with patch.object(bootstrap.db, "feedback_counts",
                          AsyncMock(return_value={"unknown": 50})):
            self.assertFalse((await bootstrap.progress("t"))["enough"])
        with patch.object(bootstrap.db, "feedback_counts",
                          AsyncMock(return_value={"liked": 8, "disliked": 4})):
            self.assertTrue((await bootstrap.progress("t"))["enough"])


if __name__ == "__main__":
    unittest.main()
