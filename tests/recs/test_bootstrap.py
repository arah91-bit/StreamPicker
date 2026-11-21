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
        self.assertTrue(seen)
        self.assertNotIn("tv", seen)

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

    async def test_a_deck_never_repeats_a_title_the_phone_already_holds(self):
        """A dealt-but-unanswered card is not a verdict, so it exists nowhere
        on the server. Without the client sending back what it holds, the next
        deck re-deals it — which is exactly what people saw."""
        async def discover(media, params):
            return [{"id": 1}, {"id": 2}]

        metas = {1: self.resolved("tt-a"), 2: self.resolved("tt-b")}

        async def resolve(media, tid, **kw):
            return metas[tid]

        with (patch.object(bootstrap.tmdb, "discover", discover),
              patch.object(bootstrap.tmdb, "resolve_meta", resolve)):
            cards = await bootstrap.deck(self.user(), {"tt-a"}, size=8)
        self.assertEqual(["tt-b"], [c["id"] for c in cards])

    async def test_a_single_deck_cannot_contain_a_duplicate(self):
        async def discover(media, params):
            return [{"id": 1}, {"id": 1}, {"id": 1}]

        with (patch.object(bootstrap.tmdb, "discover", discover),
              patch.object(bootstrap.tmdb, "resolve_meta",
                           AsyncMock(return_value=self.resolved("tt-same")))):
            cards = await bootstrap.deck(self.user(), set(), size=8)
        self.assertEqual(1, len(cards))

    async def test_an_exhausted_page_is_retried_elsewhere(self):
        """Each request picks a page at random, so one attempt routinely
        landed on a page already used up and returned an empty deck."""
        pages = []

        async def discover(media, params):
            pages.append(params["page"])
            # Only one page has anything left to offer.
            return [{"id": 99}] if params["page"] == 3 else []

        with (patch.object(bootstrap.tmdb, "discover", discover),
              patch.object(bootstrap.tmdb, "resolve_meta",
                           AsyncMock(return_value=self.resolved("tt-rare")))):
            cards = await bootstrap.deck(
                self.user(), set(), size=1, rng=__import__("random").Random(4))
        self.assertGreater(len(pages), 1)
        self.assertEqual(["tt-rare"], [c["id"] for c in cards])

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


class BootstrapWeightTests(unittest.TestCase):
    """What a tap is worth beside something actually watched.

    A flat per-tap weight did not survive a thorough person: one profile rated
    114 titles in a sitting and, at full weight, that became 78% of their
    fingerprint against 22% for seventy-five titles they had genuinely
    watched. Their nature documentaries fell from 13.7 lift to 3.4. Stated
    preference is cheap to give and cheap to be wrong about; watching
    something is expensive and therefore honest.
    """

    def share(self, play_weight, likes):
        total = fingerprint.bootstrap_weight(play_weight, likes) * likes
        return total / (play_weight + total) if play_weight + total else 1.0

    def test_a_viewer_with_no_history_is_carried_entirely_by_their_taps(self):
        """The cold start is the whole point of asking; a proportional cap
        would crush exactly the person it exists for."""
        self.assertEqual(fingerprint.BOOTSTRAP_LIKE_WEIGHT,
                         fingerprint.bootstrap_weight(0.0, 20))
        self.assertEqual(1.0, self.share(0.0, 20))

    def test_a_thin_history_is_still_led_by_the_session(self):
        self.assertGreater(self.share(1.0, 20), 0.6)

    def test_a_long_session_cannot_swamp_a_rich_history(self):
        self.assertLessEqual(self.share(22.2, 114), 0.45)

    def test_a_short_session_is_not_penalised_for_someone_elses_marathon(self):
        """Ten deliberate answers should each count fully — the cap is about
        volume drowning evidence, not about distrusting the mechanism."""
        self.assertEqual(fingerprint.BOOTSTRAP_LIKE_WEIGHT,
                         fingerprint.bootstrap_weight(22.2, 10))

    def test_more_watching_earns_a_bigger_say_for_the_same_taps(self):
        """The budget is proportional, so a heavy viewer's session is not
        squeezed as hard as a light viewer's for the same number of taps."""
        self.assertGreater(fingerprint.bootstrap_weight(60.0, 114),
                           fingerprint.bootstrap_weight(22.2, 114))

    def test_a_tap_never_outweighs_a_thoroughly_watched_title(self):
        for play_weight in (0.0, 1.0, 22.2, 200.0):
            self.assertLessEqual(fingerprint.bootstrap_weight(play_weight, 5),
                                 fingerprint.BOOTSTRAP_LIKE_WEIGHT)

    def test_no_likes_costs_nothing(self):
        self.assertEqual(0.0, fingerprint.bootstrap_weight(10.0, 0))


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


class RebuildCadenceTests(unittest.TestCase):
    """There is no "done" button — people close the tab — so the rebuild has
    to fire on its own or a session's work waits for the nightly run."""

    def test_nothing_is_rebuilt_before_there_is_enough_to_go_on(self):
        for rated in range(bootstrap.ENOUGH_RATED):
            self.assertFalse(bootstrap.should_rebuild(rated))

    def test_it_fires_the_moment_there_is_enough(self):
        self.assertTrue(bootstrap.should_rebuild(bootstrap.ENOUGH_RATED))

    def test_it_keeps_up_with_a_long_session_without_firing_every_tap(self):
        fired = [n for n in range(bootstrap.ENOUGH_RATED, 40)
                 if bootstrap.should_rebuild(n)]
        self.assertEqual([12, 18, 24, 30, 36], fired)


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    def counts(self, counts, saved=0):
        return (patch.object(bootstrap.db, "feedback_counts",
                             AsyncMock(return_value=counts)),
                patch.object(bootstrap.db, "watchlist_count",
                             AsyncMock(return_value=saved)))

    async def progress(self, counts, saved=0):
        a, b = self.counts(counts, saved)
        with a, b:
            return await bootstrap.progress("t")

    async def test_enough_is_reached_on_rated_titles_not_on_skips(self):
        """"Never seen it" is the honest answer to most of a deck and says
        nothing about taste, so it cannot be what completes a session."""
        self.assertFalse((await self.progress({"unknown": 50}))["enough"])
        self.assertTrue(
            (await self.progress({"liked": 8, "disliked": 4}))["enough"])

    async def test_it_was_fine_is_not_a_direction_either(self):
        self.assertFalse((await self.progress({"seen": 40}))["enough"])

    async def test_a_save_counts_the_same_way_a_like_does(self):
        """The rule is that an answer counts if it says which way this person's
        taste runs. "I want to watch that" plainly does."""
        state = await self.progress({"liked": 4}, saved=8)
        self.assertEqual(12, state["rated"])
        self.assertTrue(state["enough"])

    async def test_an_unreadable_watchlist_does_not_break_the_readout(self):
        with (patch.object(bootstrap.db, "feedback_counts",
                           AsyncMock(return_value={"liked": 3})),
              patch.object(bootstrap.db, "watchlist_count",
                           AsyncMock(side_effect=RuntimeError("db gone")))):
            state = await bootstrap.progress("t")
        self.assertEqual(0, state["saved"])
        self.assertEqual(3, state["rated"])


class AlreadyKnownTests(unittest.IsolatedAsyncioTestCase):
    """A card that has been answered must not come back, and a save is an
    answer — but it is stored somewhere the verdicts are not."""

    async def known(self, **kw):
        with (patch.object(bootstrap.db, "feedback_for",
                           AsyncMock(return_value=kw.get("feedback", {}))),
              patch.object(bootstrap.db, "watchlist_ids",
                           AsyncMock(return_value=kw.get("saved", set()))),
              patch.object(bootstrap.db, "dealt_recently",
                           AsyncMock(return_value=kw.get("dealt", set()))),
              patch.object(bootstrap.db, "played_titles",
                           AsyncMock(return_value=kw.get("played", [])))):
            return await bootstrap.already_known("t", "vk")

    async def test_a_card_already_shown_is_not_dealt_again(self):
        """The verdicts stop an *answered* title coming back. Most of a deck is
        never answered — people close the tab — and those were the ones being
        offered again on the next visit. The client tracks what it holds, but
        that is a page variable: one reload and it is gone."""
        self.assertEqual({"tt-shown"}, await self.known(dealt={"tt-shown"}))

    async def test_an_unreadable_deal_log_still_yields_the_verdicts(self):
        with (patch.object(bootstrap.db, "feedback_for",
                           AsyncMock(return_value={"tt-a": {"verdict": "liked"}})),
              patch.object(bootstrap.db, "watchlist_ids",
                           AsyncMock(return_value=set())),
              patch.object(bootstrap.db, "dealt_recently",
                           AsyncMock(side_effect=RuntimeError("db gone"))),
              patch.object(bootstrap.db, "played_titles",
                           AsyncMock(return_value=[]))):
            self.assertEqual({"tt-a"}, await bootstrap.already_known("t", "vk"))

    async def test_a_saved_title_is_never_dealt_again(self):
        self.assertEqual({"tt-saved"}, await self.known(saved={"tt-saved"}))

    async def test_verdicts_saves_and_plays_are_all_counted(self):
        known = await self.known(
            feedback={"tt-rated": {"verdict": "liked"}},
            saved={"tt-saved"},
            played=[{"imdb_id": "tt-played"}])
        self.assertEqual({"tt-rated", "tt-saved", "tt-played"}, known)

    async def test_an_unreadable_watchlist_still_yields_the_verdicts(self):
        with (patch.object(bootstrap.db, "feedback_for",
                           AsyncMock(return_value={"tt-a": {"verdict": "liked"}})),
              patch.object(bootstrap.db, "watchlist_ids",
                           AsyncMock(side_effect=RuntimeError("db gone"))),
              patch.object(bootstrap.db, "dealt_recently",
                           AsyncMock(return_value=set())),
              patch.object(bootstrap.db, "played_titles",
                           AsyncMock(return_value=[]))):
            self.assertEqual({"tt-a"}, await bootstrap.already_known("t", "vk"))


class SteeringTests(unittest.IsolatedAsyncioTestCase):
    """What makes a long session worth more than a short one: the answers
    change what gets asked next.

    Everything here is really one worry — that a deck which chases its own
    answers stops asking anything new, and leaves the recommender with a single
    genre to work from.
    """

    async def steer(self, liked=(), disliked=(), saved=(), genres=None):
        feedback = {i: {"verdict": "liked", "media_type": "movie"}
                    for i in liked}
        feedback.update({i: {"verdict": "disliked", "media_type": "movie"}
                         for i in disliked})
        with (patch.object(bootstrap.db, "feedback_for",
                           AsyncMock(return_value=feedback)),
              patch.object(bootstrap.db, "watchlist_ids",
                           AsyncMock(return_value=set(saved))),
              patch.object(bootstrap.db, "cached_genres_by_imdb",
                           AsyncMock(return_value=genres or {}))):
            return await bootstrap.steer("t")

    async def test_a_couple_of_answers_steer_nothing(self):
        """Two likes is a mood, not a taste. Steering on it would spend the
        rest of the session confirming an accident."""
        favoured, avoided = await self.steer(
            liked=["tt1", "tt2"],
            genres={"tt1": ["Horror"], "tt2": ["Horror"]})
        self.assertEqual(([], []), (favoured, avoided))

    async def test_a_genre_that_keeps_landing_is_favoured(self):
        favoured, _ = await self.steer(
            liked=[f"tt{n}" for n in range(6)],
            genres={f"tt{n}": ["Horror"] for n in range(6)})
        self.assertEqual(["horror"], favoured)

    async def test_one_liked_film_cannot_drag_three_genres_with_it(self):
        """A liked film carries three genres, usually only one of which is the
        reason it was liked."""
        favoured, _ = await self.steer(
            liked=[f"tt{n}" for n in range(6)],
            genres={**{f"tt{n}": ["Horror"] for n in range(6)},
                    "tt0": ["Horror", "Music"]})
        self.assertEqual(["horror"], favoured)

    async def test_a_genre_being_turned_down_is_avoided(self):
        _, avoided = await self.steer(
            disliked=[f"tt{n}" for n in range(6)],
            genres={f"tt{n}": ["Western"] for n in range(6)})
        self.assertEqual(["western"], avoided)

    async def test_a_save_steers_the_same_way_a_like_does(self):
        """The premise, the poster and the genre landed. That is a direction
        even though nobody has watched anything."""
        favoured, _ = await self.steer(
            saved=[f"tt{n}" for n in range(6)],
            genres={f"tt{n}": ["Documentary"] for n in range(6)})
        self.assertEqual(["documentary"], favoured)

    async def test_liking_and_disliking_a_genre_equally_steers_neither_way(self):
        favoured, avoided = await self.steer(
            liked=[f"tt-y{n}" for n in range(3)],
            disliked=[f"tt-n{n}" for n in range(3)],
            genres={**{f"tt-y{n}": ["Comedy"] for n in range(3)},
                    **{f"tt-n{n}": ["Comedy"] for n in range(3)}})
        self.assertEqual(([], []), (favoured, avoided))

    async def test_a_genre_that_cannot_be_asked_for_never_wins_a_slot(self):
        """TMDB's TV taxonomy has compound display names that exist in neither
        id table. Left in, one of them takes a focus slot, resolves to no
        genre, and the sweep is skipped — a wasted round trip out of the six a
        deck gets, spent on the genre the viewer likes most."""
        favoured, _ = await self.steer(
            liked=[f"tt{n}" for n in range(6)],
            genres={f"tt{n}": ["Sci-Fi & Fantasy", "Drama"] for n in range(6)})
        self.assertEqual(["drama"], favoured)

    async def test_only_so_many_genres_may_be_avoided_at_once(self):
        """A run of dislikes must not be able to filter the sweep down to
        nothing."""
        genres = {f"tt{n}": [g] for n, g in enumerate(
            ["Western", "Music", "History", "War", "Documentary"] * 2)}
        _, avoided = await self.steer(disliked=list(genres), genres=genres)
        self.assertLessEqual(len(avoided), bootstrap.MAX_AVOIDED)


class SteeredDeckTests(unittest.IsolatedAsyncioTestCase):
    """The steer as the sweep actually sees it."""

    def user(self, genres=None):
        import json
        return {"token": "t", "name": "V",
                "bootstrap_genres": json.dumps(genres or []),
                "bootstrap_countries": json.dumps([])}

    async def sweeps(self, steering, picked=None, seed=1, size=24):
        """Every discover param set one deck produces."""
        seen = []

        async def discover(media, params):
            seen.append((media, params))
            return []

        with patch.object(bootstrap.tmdb, "discover", discover):
            await bootstrap.deck(self.user(picked), set(), size=size,
                                 rng=__import__("random").Random(seed),
                                 steering=steering)
        return seen

    async def test_a_favoured_genre_reaches_the_sweep(self):
        sweeps = await self.sweeps((["horror"], []))
        wanted = {p.get("with_genres") for _, p in sweeps}
        self.assertIn(str(bootstrap.tmdb.MOVIE_GENRES["horror"]), wanted)

    async def test_the_deck_never_narrows_to_one_genre(self):
        """A deck that only asks about the thing it already knows you like
        confirms itself, learns nothing, and leaves the recommender with one
        genre to work from. Some share of every session has to stay on what
        the viewer actually picked."""
        sweeps = await self.sweeps((["horror"], []), picked=["drama"], seed=7)
        drama = str(bootstrap.tmdb.MOVIE_GENRES["drama"])
        self.assertIn(drama, {p.get("with_genres") for _, p in sweeps})

    async def test_favoured_genres_are_swept_one_at_a_time(self):
        """TMDB reads a `|`-joined list as "any of these", so pooling every
        favourite into one sweep just returns the popularity ordering the
        unsteered deck already gives back."""
        sweeps = await self.sweeps((["horror", "comedy", "crime"], []))
        for _, params in sweeps:
            self.assertNotIn("|", params.get("with_genres", ""))

    async def test_an_avoided_genre_is_excluded_from_the_sweep(self):
        sweeps = await self.sweeps(([], ["western"]))
        self.assertTrue(sweeps)
        for media, params in sweeps:
            table = (bootstrap.tmdb.MOVIE_GENRES if media == "movie"
                     else bootstrap.tmdb.TV_GENRES)
            self.assertEqual(str(table["western"]), params["without_genres"])

    async def test_something_they_asked_for_by_name_is_never_avoided(self):
        """The answers may suggest their horror picks are not working out. The
        tick box is still the more explicit instruction of the two."""
        sweeps = await self.sweeps(([], ["horror"]), picked=["horror"])
        self.assertTrue(sweeps)
        for _, params in sweeps:
            self.assertNotIn("without_genres", params)

    async def test_a_sweep_can_never_exclude_the_genre_it_is_asking_for(self):
        """TMDB's taxonomy is many-to-one — `thriller` and `suspense` are both
        53 on the movie side — so subtracting slugs rather than ids produces
        `with_genres=53&without_genres=53` and a sweep that returns nothing by
        construction."""
        sweeps = await self.sweeps((["thriller"], ["suspense"]))
        asked = [p for m, p in sweeps if m == "movie"]
        self.assertTrue(asked)
        for params in asked:
            wanted = set((params.get("with_genres") or "").split("|"))
            shunned = set((params.get("without_genres") or "").split("|"))
            self.assertEqual(set(), wanted & shunned - {""})

    async def test_an_unavailable_steer_costs_the_tailoring_and_nothing_else(self):
        """The unsteered deck is the deck this page shipped with, so falling
        back to it is never worse than not answering."""
        async def discover(media, params):
            return [{"id": 1}]

        with (patch.object(bootstrap.db, "feedback_for",
                           AsyncMock(side_effect=RuntimeError("db gone"))),
              patch.object(bootstrap.tmdb, "discover", discover),
              patch.object(bootstrap.tmdb, "resolve_meta",
                           AsyncMock(return_value={
                               "id": "tt1", "name": "T", "poster": "p",
                               "genres": ["Drama"], "releaseInfo": "2011",
                               "imdbRating": "8.0", "description": "x"}))):
            cards = await bootstrap.deck(self.user(), set(), size=1)
        self.assertEqual(["tt1"], [c["id"] for c in cards])


class SaveWeightTests(unittest.IsolatedAsyncioTestCase):
    """What a save is worth beside a like.

    Both are taps, so the difference is not effort — it is what each one can
    possibly know. A like is a verdict on a film somebody sat through; a save
    is a verdict on a poster and a premise, taken before the thing has been
    watched at all.
    """

    # `for_viewer` refuses to build below MIN_TITLES, so every case here needs
    # enough weighted titles to clear it before the interesting one is even
    # looked at.
    def padding(self, n=fingerprint.MIN_TITLES):
        return [f"tt-pad{i}" for i in range(n)]

    async def weights(self, liked=(), saved=(), store=None):
        import app.recs.db as real_db
        from app.recs import taste

        store = store or {i: ["k:x"] for i in set(liked) | set(saved)}
        feedback = {i: {"verdict": "liked", "media_type": "movie"}
                    for i in liked}
        seen = {}

        def spy(weights, features_by_imdb, vocabulary, baseline_pool=None,
                rng=None, disliked=None):
            seen.update(weights)
            return fingerprint.Fingerprint({}, vocabulary, [], 0)

        with (patch.object(real_db, "features_by_imdb",
                           AsyncMock(return_value=store)),
              patch.object(real_db, "feedback_for",
                           AsyncMock(return_value=feedback)),
              patch.object(real_db, "watchlist_ids",
                           AsyncMock(return_value=set(saved))),
              patch.object(features, "vocabulary", AsyncMock(
                  return_value=features.Vocabulary({"k:x": 6}, 18))),
              patch.object(fingerprint, "build", spy)):
            await fingerprint.for_viewer(taste.TasteModel(), None,
                                         user_token="t")
        return seen

    async def test_a_save_reaches_the_fingerprint(self):
        weights = await self.weights(saved=[f"tt-s{n}" for n in range(6)])
        self.assertEqual({f"tt-s{n}" for n in range(6)}, set(weights))

    async def test_a_save_weighs_less_than_a_like(self):
        weights = await self.weights(liked=["tt-l", *self.padding()],
                                     saved=["tt-s"])
        self.assertLess(weights["tt-s"], weights["tt-l"])
        self.assertAlmostEqual(fingerprint.BOOTSTRAP_SAVE_SHARE,
                               weights["tt-s"] / weights["tt-l"])

    async def test_a_liked_title_that_was_also_saved_counts_once_at_full_weight(self):
        """The like is the better evidence and the save adds nothing to it."""
        weights = await self.weights(liked=["tt-both", *self.padding()],
                                     saved=["tt-both"])
        self.assertEqual(weights["tt-pad0"], weights["tt-both"])

    async def test_saves_share_the_likes_budget_rather_than_adding_to_it(self):
        """A second channel outside the cap would quietly reopen exactly the
        hole the cap was dug to close. With nothing watched there is no budget
        to share, so this is measured against a real viewing history."""
        async def weights(saves):
            import app.recs.db as real_db
            from app.recs import taste

            liked = ["tt-l", *self.padding()]
            store = {i: ["k:x"] for i in liked + saves + ["tt-watched"]}
            model = taste.TasteModel()
            model.signals[("tt-watched", "movie")] = taste.TitleSignal(
                imdb_id="tt-watched", media_type="movie", engagement=30.0,
                plays=3)
            feedback = {i: {"verdict": "liked", "media_type": "movie"}
                        for i in liked}
            seen = {}

            def spy(w, features_by_imdb, vocabulary, baseline_pool=None,
                    rng=None, disliked=None):
                seen.update(w)
                return fingerprint.Fingerprint({}, vocabulary, [], 0)

            with (patch.object(real_db, "features_by_imdb",
                               AsyncMock(return_value=store)),
                  patch.object(real_db, "feedback_for",
                               AsyncMock(return_value=feedback)),
                  patch.object(real_db, "watchlist_ids",
                               AsyncMock(return_value=set(saves))),
                  patch.object(features, "vocabulary", AsyncMock(
                      return_value=features.Vocabulary({"k:x": 6}, 18))),
                  patch.object(fingerprint, "build", spy)):
                await fingerprint.for_viewer(model, None, user_token="t")
            return seen

        many = await weights([f"tt-s{n}" for n in range(60)])
        few = await weights(["tt-s0"])
        self.assertLess(many["tt-l"], few["tt-l"])

    async def test_a_title_with_no_features_cannot_be_weighed(self):
        weights = await self.weights(saved=["tt-s"], store={"tt-other": ["k:x"]})
        self.assertEqual({}, weights)


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
