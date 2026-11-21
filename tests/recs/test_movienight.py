"""Movie night: a shared deck several people vote on until one film wins.

The two rules that must not bend are that a vote here never reaches anybody's
taste model — "not tonight, with these people" is not "not for me" — and that a
guest leaves nothing behind.
"""

import asyncio
import contextlib
import unittest
from unittest.mock import AsyncMock, patch

from app.recs import features, movienight


class PlaylistTests(unittest.IsolatedAsyncioTestCase):
    def films(self, n, start=0):
        return [{"id": start + i} for i in range(n)]

    def meta(self, tid):
        return {"id": f"tt{tid:04d}", "name": f"Film {tid}", "poster": "p",
                "genres": ["Drama"], "releaseInfo": "2015", "imdbRating": "7.5"}

    async def test_a_room_of_strangers_gets_popular_films(self):
        async def discover(media, params):
            self.assertEqual("movie", media)
            self.assertGreaterEqual(params["vote_count.gte"],
                                    movienight.MIN_VOTES)
            return self.films(20)

        async def resolve(media, tid, **kw):
            return self.meta(tid)

        with (patch.object(movienight.tmdb, "discover", discover),
              patch.object(movienight.tmdb, "resolve_meta", resolve),
              patch.object(movienight, "_fingerprints",
                           AsyncMock(return_value=[]))):
            playlist = await movienight.build_playlist([])
        self.assertTrue(playlist)
        self.assertEqual(len(playlist), len({m["id"] for m in playlist}))

    async def test_the_pool_spans_more_than_this_month(self):
        """Sweeping only by popularity gave a room a wall of current
        blockbusters and nothing to discover about itself, whatever anybody
        voted."""
        sorts, windows = set(), 0

        async def discover(media, params):
            nonlocal windows
            sorts.add(params.get("sort_by"))
            if "primary_release_date.lte" in params:
                windows += 1
            return self.films(6)

        async def resolve(media, tid, **kw):
            return self.meta(tid)

        with (patch.object(movienight.tmdb, "discover", discover),
              patch.object(movienight.tmdb, "resolve_meta", resolve),
              patch.object(movienight, "_fingerprints",
                           AsyncMock(return_value=[]))):
            await movienight.build_playlist([])
        self.assertIn("popularity.desc", sorts)
        self.assertIn("vote_average.desc", sorts)
        self.assertGreaterEqual(windows, 3)

    async def test_a_room_with_regulars_is_swept_by_their_own_keywords(self):
        """Otherwise picking a known viewer only reorders whatever the generic
        sweeps happened to return."""
        seen = []

        class Print:
            def top_features(self, limit=40, families=("k", "p")):
                return [f"k:{n}" for n in range(limit)]

            def lift(self, tokens):
                return 1.0

        async def discover(media, params):
            seen.append(params)
            return self.films(4)

        async def resolve(media, tid, **kw):
            return self.meta(tid)

        with (patch.object(movienight.tmdb, "discover", discover),
              patch.object(movienight.tmdb, "resolve_meta", resolve),
              patch.object(movienight.db, "features_by_imdb",
                           AsyncMock(return_value={})),
              patch.object(movienight, "_fingerprints",
                           AsyncMock(return_value=[Print()]))):
            await movienight.build_playlist(["known"])
        self.assertTrue([p for p in seen if "with_keywords" in p])

    async def test_the_least_keen_person_decides_the_order(self):
        """A film one person loves and another cannot stand is exactly the
        film that will not end the evening. An average hides that; the
        minimum does not."""
        class Print:
            def __init__(self, lifts):
                self.lifts = lifts

            def lift(self, tokens):
                return self.lifts.get(tokens[0], 0.0)

        # `split` is adored by one and hated by the other; `ok` suits both
        # mildly. Averaged, split wins. On the minimum, ok wins — correctly.
        store = {"tt0000": ["split"], "tt0001": ["ok"]}
        prints = [Print({"split": 9.0, "ok": 2.0}),
                  Print({"split": 0.0, "ok": 2.0})]

        async def discover(media, params):
            return self.films(2) if params["page"] == 1 else []

        async def resolve(media, tid, **kw):
            return self.meta(tid)

        with (patch.object(movienight.tmdb, "discover", discover),
              patch.object(movienight.tmdb, "resolve_meta", resolve),
              patch.object(movienight.db, "features_by_imdb",
                           AsyncMock(return_value=store)),
              patch.object(movienight, "_fingerprints",
                           AsyncMock(return_value=prints))):
            playlist = await movienight.build_playlist(["a", "b"])
        self.assertEqual("tt0001", playlist[0]["id"])

    async def test_a_room_with_a_child_in_it_is_filtered_to_the_child(self):
        seen = {}

        async def discover(media, params):
            return self.films(4)

        async def resolve(media, tid, max_age=None, **kw):
            seen["max_age"] = max_age
            return self.meta(tid)

        with (patch.object(movienight.tmdb, "discover", discover),
              patch.object(movienight.tmdb, "resolve_meta", resolve),
              patch.object(movienight, "_fingerprints",
                           AsyncMock(return_value=[]))):
            await movienight.build_playlist([], kid_age=7)
        self.assertEqual(7, seen["max_age"])


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_session_needs_at_least_two_people(self):
        with self.assertRaises(ValueError):
            await movienight.create([{"label": "Solo"}])

    async def test_it_refuses_rather_than_opening_an_empty_session(self):
        with (patch.object(movienight, "build_playlist",
                           AsyncMock(return_value=[])),
              self.assertRaises(RuntimeError)):
            await movienight.create([{"label": "A"}, {"label": "B"}])

    async def test_every_seat_gets_its_own_unguessable_key(self):
        created = {}

        async def capture(session_id, seats, playlist, ttl, kid_age=None):
            created["seats"] = seats

        with (patch.object(movienight, "build_playlist",
                           AsyncMock(return_value=[{"id": f"tt{n}"}
                                                   for n in range(9)])),
              patch.object(movienight.db, "create_match", capture)):
            await movienight.create([{"label": "A"}, {"label": "B"},
                                     {"label": "C"}])
        keys = [s["seat_key"] for s in created["seats"]]
        self.assertEqual(3, len(set(keys)))
        self.assertTrue(all(len(k) > 12 for k in keys))

    async def test_a_guest_seat_carries_no_identity(self):
        created = {}

        async def capture(session_id, seats, playlist, ttl, kid_age=None):
            created["seats"] = seats

        with (patch.object(movienight, "build_playlist",
                           AsyncMock(return_value=[{"id": f"tt{n}"}
                                                   for n in range(9)])),
              patch.object(movienight.db, "create_match", capture)):
            await movienight.create([{"label": ""}, {"label": "",
                                                     "user_token": "known"}])
        guest, known = created["seats"]
        self.assertIsNone(guest["user_token"])
        self.assertEqual("Guest 1", guest["label"])
        self.assertEqual("known", known["user_token"])


class DynamicRankingTests(unittest.IsolatedAsyncioTestCase):
    """The queue re-ranks from the room's votes. A fixed list cannot converge —
    it just runs out."""

    def setUp(self):
        # Two clusters. `war` films share tokens with each other, `funny`
        # films with each other, and nothing crosses.
        self.playlist = ([{"id": f"tt-war{n}"} for n in range(5)]
                         + [{"id": f"tt-fun{n}"} for n in range(5)])
        self.store = {f"tt-war{n}": ["k:war", f"k:w{n}"] for n in range(5)}
        self.store.update({f"tt-fun{n}": ["k:funny", f"k:f{n}"]
                           for n in range(5)})
        frequency = {}
        for doc in self.store.values():
            for token in set(doc):
                frequency[token] = frequency.get(token, 0) + 1
        self.vocab = features.Vocabulary(frequency, len(self.store))

    def patches(self, votes, prints=None):
        return (
            patch.object(movienight.db, "match_votes_for",
                         AsyncMock(return_value=votes)),
            patch.object(movienight.db, "features_by_imdb",
                         AsyncMock(return_value=self.store)),
            patch.object(movienight.features, "vocabulary",
                         AsyncMock(return_value=self.vocab)),
            # Ranking consults each seat's standing taste now, so a case that
            # is only about tonight's votes has to say there is none.
            patch.object(movienight, "seat_prints",
                         AsyncMock(return_value=prints or {})),
        )

    async def test_one_no_is_seen_it_not_a_verdict_on_the_genre(self):
        """The commonest reason for a thumbs down here is having already seen
        the film — often one that person loved. Treating that as "no more of
        this sort of thing" steers the room away from exactly what they like
        best.

        `_centroid` normalises, so before this a single no pushed against a
        whole cluster precisely as hard as five did.
        """
        one = {0: {"tt-war0": 0}}
        several = {0: {"tt-war0": 0, "tt-war1": 0, "tt-war2": 0}}
        scores = {}
        for label, votes in (("one", one), ("several", several)):
            with contextlib.ExitStack() as stack:
                for p in self.patches(votes):
                    stack.enter_context(p)
                ranked = await movienight.rank("s", self.playlist, 2)
            scores[label] = [m["id"] for m in ranked]
        # After one rejection the remaining war films are still in contention;
        # after three that agree with each other, they are not.
        self.assertTrue(scores["one"][0].startswith("tt-war"), scores["one"][:3])
        self.assertTrue(scores["several"][0].startswith("tt-fun"),
                        scores["several"][:3])

    async def test_a_no_still_removes_that_film_completely(self):
        """"Not right now" is still not tonight. Softening what a no says about
        *other* films must not soften what it says about this one."""
        with contextlib.ExitStack() as stack:
            for p in self.patches({0: {"tt-war0": 0}}):
                stack.enter_context(p)
            ranked = await movienight.rank("s", self.playlist, 2)
        self.assertNotIn("tt-war0", [m["id"] for m in ranked])

    async def test_a_film_anybody_refused_leaves_the_queue(self):
        """Winning takes a unanimous yes, so one no makes a film
        arithmetically dead. Leaving it in spends everyone else's attention on
        an impossible outcome."""
        with contextlib.ExitStack() as stack:
            for p in self.patches({1: {"tt-war0": 0}}):
                stack.enter_context(p)
            ranked = await movienight.rank("s", self.playlist, 3)
        self.assertNotIn("tt-war0", [m["id"] for m in ranked])
        self.assertEqual(9, len(ranked))

    async def test_what_the_room_likes_rises(self):
        """A few yeses each on comedies and comedies should lead. Three, not
        one: a single vote is a third of a seat's weight by design, because
        "they keep thumbing this down" is a pattern and not one tap."""
        votes = {0: {"tt-fun0": 1, "tt-fun2": 1, "tt-fun3": 1},
                 1: {"tt-fun1": 1, "tt-fun2": 1, "tt-fun4": 1}}
        with contextlib.ExitStack() as stack:
            for p in self.patches(votes):
                stack.enter_context(p)
            ranked = await movienight.rank("s", self.playlist, 2)
        top = [m["id"] for m in ranked[:3]]
        self.assertTrue(all(t.startswith("tt-fun") for t in top), top)

    async def test_one_persons_enthusiasm_cannot_carry_a_film(self):
        """Seat 0 loves war films, seat 1 has said no to one. The room is
        limited by whoever is hardest to please, so comedy should lead
        regardless of how keen seat 0 is.

        This is also the test that caught the near-agreement experiment: under
        that ordering seat 0's yes on a war film pulled it to the front for
        seat 1, who had just refused three of them.
        """
        votes = {0: {"tt-war0": 1, "tt-war1": 1, "tt-war3": 1},
                 1: {"tt-war2": 0, "tt-war4": 0, "tt-war0": 0}}
        with contextlib.ExitStack() as stack:
            for p in self.patches(votes):
                stack.enter_context(p)
            ranked = await movienight.rank("s", self.playlist, 2)
        self.assertTrue(ranked[0]["id"].startswith("tt-fun"),
                        [m["id"] for m in ranked[:4]])

    async def test_a_known_seats_own_taste_leads_before_they_vote(self):
        """The reason picking a viewer for a seat is worth doing. Their
        fingerprint used to touch only the opening pool order, which the first
        few votes then overwrote — so choosing a known viewer bought almost
        nothing."""
        class Print:
            def lift(self, tokens):
                return 8.0 if "k:funny" in tokens else 0.1

        with contextlib.ExitStack() as stack:
            for p in self.patches({}):
                stack.enter_context(p)
            ranked = await movienight.rank("s", self.playlist, 2,
                                           prints={0: Print()})
        self.assertTrue(ranked[0]["id"].startswith("tt-fun"),
                        [m["id"] for m in ranked[:4]])

    async def test_tonights_thumbs_overrule_standing_taste(self):
        """Someone who usually likes comedies but is refusing them tonight
        should stop being shown comedies."""
        class Print:
            def lift(self, tokens):
                return 8.0 if "k:funny" in tokens else 0.1

        votes = {0: {f"tt-fun{n}": 0 for n in range(4)}}
        with contextlib.ExitStack() as stack:
            for p in self.patches(votes):
                stack.enter_context(p)
            ranked = await movienight.rank("s", self.playlist, 1,
                                           prints={0: Print()})
        self.assertTrue(ranked[0]["id"].startswith("tt-war"),
                        [m["id"] for m in ranked[:4]])

    async def test_a_guest_is_carried_entirely_by_their_thumbs(self):
        """No fingerprint, so a room of strangers has only tonight to go on —
        and must still converge."""
        votes = {0: {"tt-fun0": 1, "tt-fun2": 1, "tt-fun3": 1},
                 1: {"tt-fun1": 1, "tt-fun2": 1, "tt-fun4": 1}}
        with contextlib.ExitStack() as stack:
            for p in self.patches(votes):
                stack.enter_context(p)
            ranked = await movienight.rank("s", self.playlist, 2, prints={})
        self.assertTrue(ranked[0]["id"].startswith("tt-fun"))

    async def test_with_no_votes_the_opening_order_is_kept(self):
        with contextlib.ExitStack() as stack:
            for p in self.patches({}):
                stack.enter_context(p)
            ranked = await movienight.rank("s", self.playlist, 3)
        self.assertEqual([m["id"] for m in self.playlist],
                         [m["id"] for m in ranked])

    async def test_a_room_that_has_ruled_everything_out_ranks_empty(self):
        votes = {0: {m["id"]: 0 for m in self.playlist}}
        with contextlib.ExitStack() as stack:
            for p in self.patches(votes):
                stack.enter_context(p)
            self.assertEqual([], await movienight.rank("s", self.playlist, 2))

    async def test_titles_with_no_features_are_kept_not_dropped(self):
        """A film we have no vectors for should fall down the order, never out
        of it — it may still be the thing everyone agrees on."""
        playlist = self.playlist + [{"id": "tt-unknown"}]
        with contextlib.ExitStack() as stack:
            for p in self.patches({0: {"tt-fun0": 1}}):
                stack.enter_context(p)
            ranked = await movienight.rank("s", playlist, 2)
        self.assertIn("tt-unknown", [m["id"] for m in ranked])


class AgreementTests(unittest.IsolatedAsyncioTestCase):
    """Ending the evening is the whole point."""

    def seat_row(self, seat=0, seats=3, playlist=None, winner=None):
        import json
        return {
            "session_id": "s1", "seat": seat, "seat_key": f"k{seat}",
            "label": f"P{seat}", "seat_count": seats,
            "playlist": json.dumps(playlist or [{"id": "tt1"}, {"id": "tt2"}]),
            "winner_imdb": winner,
        }

    async def test_a_film_everyone_wants_ends_it(self):
        settled = {}

        with (patch.object(movienight.db, "record_match_vote", AsyncMock()),
              patch.object(movienight.db, "match_unanimous",
                           AsyncMock(return_value="tt2")),
              patch.object(movienight.db, "set_match_winner",
                           AsyncMock(side_effect=lambda s, i:
                                     settled.update(id=i))),
              patch.object(movienight, "state",
                           AsyncMock(return_value={"winner": {"id": "tt2"}}))):
            out = await movienight.vote(self.seat_row(), "tt2", True)
        self.assertEqual("tt2", settled["id"])
        self.assertEqual("tt2", out["winner"]["id"])

    async def test_a_no_can_never_settle_it(self):
        """Only a yes can complete a set. Checking on a no would let a title
        win off votes that were against it."""
        unanimous = AsyncMock(return_value="tt2")
        with (patch.object(movienight.db, "record_match_vote", AsyncMock()),
              patch.object(movienight.db, "match_unanimous", unanimous),
              patch.object(movienight.db, "set_match_winner", AsyncMock()),
              patch.object(movienight, "state", AsyncMock(return_value={}))):
            await movienight.vote(self.seat_row(), "tt2", False)
        unanimous.assert_not_awaited()

    async def test_a_seat_is_never_asked_about_the_same_film_twice(self):
        playlist = [{"id": "tt1"}, {"id": "tt2"}, {"id": "tt3"}]
        with (patch.object(movienight.db, "touch_match_seat", AsyncMock()),
              patch.object(movienight.db, "match_votes_for",
                           AsyncMock(return_value={0: {"tt1": 1, "tt2": 0}})),
              patch.object(movienight, "rank",
                           AsyncMock(return_value=[{"id": "tt1"},
                                                   {"id": "tt3"}])),
              patch.object(movienight.db, "match_by_seat_key",
                           AsyncMock(return_value=None)),
              patch.object(movienight.db, "match_seats",
                           AsyncMock(return_value=[]))):
            out = await movienight.state(self.seat_row(playlist=playlist))
        self.assertEqual([{"id": "tt3"}], out["next"])
        self.assertEqual(2, out["voted"])

    async def test_running_out_of_film_is_not_an_error(self):
        """One person can finish the list while the others are still going —
        their yes votes still count toward an agreement."""
        playlist = [{"id": "tt1"}]
        with (patch.object(movienight.db, "touch_match_seat", AsyncMock()),
              patch.object(movienight.db, "match_votes_for",
                           AsyncMock(return_value={0: {"tt1": 1}})),
              patch.object(movienight, "rank",
                           AsyncMock(return_value=[{"id": "tt1"}])),
              patch.object(movienight.db, "match_by_seat_key",
                           AsyncMock(return_value=None)),
              patch.object(movienight.db, "match_seats",
                           AsyncMock(return_value=[]))):
            out = await movienight.state(self.seat_row(playlist=playlist))
        self.assertTrue(out["exhausted"])
        self.assertEqual([], out["next"])

    async def test_a_winner_settled_by_someone_else_is_picked_up(self):
        """The seat row was read before another player agreed. Trusting it
        would leave that player voting after the evening had been decided."""
        playlist = [{"id": "tt1"}, {"id": "tt2"}]
        fresh = dict(self.seat_row(playlist=playlist), winner_imdb="tt2")
        with (patch.object(movienight.db, "touch_match_seat", AsyncMock()),
              patch.object(movienight.db, "match_votes_for",
                           AsyncMock(return_value={})),
              patch.object(movienight.db, "match_by_seat_key",
                           AsyncMock(return_value=fresh)),
              patch.object(movienight.db, "match_seats",
                           AsyncMock(return_value=[]))):
            out = await movienight.state(self.seat_row(playlist=playlist))
        self.assertEqual("tt2", out["winner"]["id"])


class AdaptivePoolTests(unittest.IsolatedAsyncioTestCase):
    """The pool is a moving window, not a list decided before anybody spoke.

    A fixed 150 films chosen up front could not contain the answer for every
    room, and no amount of re-ranking finds a film that was never in it. Worse,
    they were chosen from the standing taste of the *known* seats, so a table
    of guests had its evening picked from nothing at all.
    """

    def film(self, n, tmdb=None):
        return {"id": f"tt{n}", "tmdb": tmdb if tmdb is not None else n,
                "media": "movie", "title": f"Film {n}"}

    def meta(self, tid):
        return {"id": f"tt{tid}", "name": f"Film {tid}", "poster": "p",
                "genres": ["Drama"], "releaseInfo": "2011", "imdbRating": "7.5"}

    def patches(self, votes, recs, added=None):
        return (
            patch.object(movienight.db, "match_votes_for",
                         AsyncMock(return_value=votes)),
            patch.object(movienight.tmdb, "tmdb_recommendations",
                         AsyncMock(return_value=recs)),
            patch.object(movienight.tmdb, "resolve_meta",
                         AsyncMock(side_effect=lambda m, tid, **kw:
                                   self.meta(tid))),
            patch.object(movienight.db, "extend_match_playlist",
                         added or AsyncMock(return_value=0)),
        )

    async def test_more_are_found_from_what_the_room_agreed_on(self):
        added = AsyncMock(return_value=2)
        playlist = [self.film(1), self.film(2)]
        with contextlib.ExitStack() as stack:
            for p in self.patches({0: {"tt1": 1}, 1: {"tt1": 1}},
                                  [{"id": 90}, {"id": 91}], added):
                stack.enter_context(p)
            n = await movienight.topup("s", playlist, None, limit=2)
        self.assertEqual(2, n)
        self.assertEqual(["tt90", "tt91"],
                         [f["id"] for f in added.await_args.args[1]])

    async def test_the_seed_is_what_the_room_agrees_on_not_one_persons_pick(self):
        """Two seats on one film outranks one seat on another. That difference
        is the only thing making this the *group's* signal."""
        seen = []

        async def recommend(media, tmdb_id):
            seen.append(tmdb_id)
            return []

        with (patch.object(movienight.db, "match_votes_for",
                           AsyncMock(return_value={0: {"tt1": 1, "tt2": 1},
                                                   1: {"tt2": 1}})),
              patch.object(movienight.tmdb, "tmdb_recommendations", recommend)):
            await movienight.topup("s", [self.film(1), self.film(2)], None)
        self.assertEqual(2, seen[0])          # tt2 has two yeses

    async def test_a_film_anybody_refused_never_seeds_a_search(self):
        """It is dead as a candidate, and a poor description of common
        ground."""
        seen = []

        async def recommend(media, tmdb_id):
            seen.append(tmdb_id)
            return []

        with (patch.object(movienight.db, "match_votes_for",
                           AsyncMock(return_value={0: {"tt1": 1}, 1: {"tt1": 0}})),
              patch.object(movienight.tmdb, "tmdb_recommendations", recommend)):
            await movienight.topup("s", [self.film(1)], None)
        self.assertEqual([], seen)

    async def test_nothing_is_fetched_before_the_room_has_agreed_on_anything(self):
        """With no yeses there is nothing to search outward from, and the
        opening batch is still the best guess available."""
        with (patch.object(movienight.db, "match_votes_for",
                           AsyncMock(return_value={0: {"tt1": 0}})),
              patch.object(movienight.tmdb, "tmdb_recommendations",
                           AsyncMock(side_effect=AssertionError("asked TMDB")))):
            self.assertEqual(0, await movienight.topup("s", [self.film(1)], None))

    async def test_a_childrens_session_keeps_its_age_ceiling(self):
        """The pool is no longer chosen once, so the age it was chosen under
        has to survive with it. Without that a top-up would post adult titles
        into a kids' night."""
        seen = {}

        async def resolve(media, tid, **kw):
            seen.update(kw)
            return self.meta(tid)

        with (patch.object(movienight.db, "match_votes_for",
                           AsyncMock(return_value={0: {"tt1": 1}})),
              patch.object(movienight.tmdb, "tmdb_recommendations",
                           AsyncMock(return_value=[{"id": 90}])),
              patch.object(movienight.tmdb, "resolve_meta", resolve),
              patch.object(movienight.db, "extend_match_playlist",
                           AsyncMock(return_value=1))):
            await movienight.topup("s", [self.film(1)], kid_age=8)
        self.assertEqual(8, seen.get("max_age"))

    async def test_a_film_already_in_the_pool_is_not_added_twice(self):
        added = AsyncMock(return_value=0)
        playlist = [self.film(1), self.film(90, tmdb=90)]
        with contextlib.ExitStack() as stack:
            for p in self.patches({0: {"tt1": 1}}, [{"id": 90}, {"id": 91}],
                                  added):
                stack.enter_context(p)
            await movienight.topup("s", playlist, None, limit=5)
        self.assertEqual(["tt91"], [f["id"] for f in added.await_args.args[1]])

    async def test_a_refused_film_is_never_offered_back(self):
        added = AsyncMock(return_value=0)
        with contextlib.ExitStack() as stack:
            for p in self.patches({0: {"tt1": 1}, 1: {"tt90": 0}},
                                  [{"id": 90}, {"id": 91}], added):
                stack.enter_context(p)
            await movienight.topup("s", [self.film(1)], None, limit=5)
        self.assertEqual(["tt91"], [f["id"] for f in added.await_args.args[1]])

    async def test_the_pool_stops_growing_eventually(self):
        """A long evening must not grow the row without bound."""
        big = [self.film(n) for n in range(movienight.MAX_POOL)]
        with patch.object(movienight.tmdb, "tmdb_recommendations",
                          AsyncMock(side_effect=AssertionError("asked TMDB"))):
            self.assertEqual(0, await movienight.topup("s", big, None))

    async def test_a_broken_top_up_costs_options_not_the_evening(self):
        with (patch.object(movienight.db, "match_votes_for",
                           AsyncMock(side_effect=RuntimeError("db gone")))):
            self.assertEqual(0, await movienight.topup("s", [self.film(1)], None))

    async def test_only_one_top_up_runs_per_session(self):
        """Every seat polls, so without this a room of five fetches the same
        batch five times over."""
        movienight._topping_up.clear()
        movienight._topped_up_at.clear()
        started = []

        async def slow(session_id, playlist, kid_age, limit=10):
            started.append(session_id)
            await asyncio.sleep(0.05)
            return 0

        with patch.object(movienight, "topup", slow):
            for _ in range(5):
                movienight._schedule_topup("s", [], None)
            await asyncio.sleep(0.12)
        self.assertEqual(1, len(started))

    async def test_a_room_at_the_end_of_its_batch_does_not_spin(self):
        """Sitting at the end of a batch is exactly the state that asks for a
        top-up, and every seat re-asks every few seconds. Without a cooldown a
        room with nothing agreed yet — where a top-up returns immediately —
        would pull TMDB for as long as the tab stayed open."""
        movienight._topping_up.clear()
        movienight._topped_up_at.clear()
        started = []

        async def quick(session_id, playlist, kid_age, limit=10):
            started.append(session_id)
            return 0

        with patch.object(movienight, "topup", quick):
            for _ in range(6):
                movienight._schedule_topup("s", [], None)
                await asyncio.sleep(0.01)
        self.assertEqual(1, len(started))


if __name__ == "__main__":
    unittest.main()
