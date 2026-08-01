"""Movie night: a shared deck several people vote on until one film wins.

The two rules that must not bend are that a vote here never reaches anybody's
taste model — "not tonight, with these people" is not "not for me" — and that a
guest leaves nothing behind.
"""

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

        async def capture(session_id, seats, playlist, ttl):
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

        async def capture(session_id, seats, playlist, ttl):
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

    def patches(self, votes):
        return (
            patch.object(movienight.db, "match_votes_for",
                         AsyncMock(return_value=votes)),
            patch.object(movienight.db, "features_by_imdb",
                         AsyncMock(return_value=self.store)),
            patch.object(movienight.features, "vocabulary",
                         AsyncMock(return_value=self.vocab)),
        )

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
        """Two seats have said yes to comedies. Comedies should now lead."""
        votes = {0: {"tt-fun0": 1}, 1: {"tt-fun1": 1}}
        with contextlib.ExitStack() as stack:
            for p in self.patches(votes):
                stack.enter_context(p)
            ranked = await movienight.rank("s", self.playlist, 2)
        top = [m["id"] for m in ranked[:3]]
        self.assertTrue(all(t.startswith("tt-fun") for t in top), top)

    async def test_one_persons_enthusiasm_cannot_carry_a_film(self):
        """Seat 0 loves war films, seat 1 has said no to one. The room is
        limited by whoever is hardest to please, so comedy should lead
        regardless of how keen seat 0 is."""
        votes = {0: {"tt-war0": 1, "tt-war1": 1}, 1: {"tt-war2": 0}}
        with contextlib.ExitStack() as stack:
            for p in self.patches(votes):
                stack.enter_context(p)
            ranked = await movienight.rank("s", self.playlist, 2)
        self.assertTrue(ranked[0]["id"].startswith("tt-fun"),
                        [m["id"] for m in ranked[:4]])

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


if __name__ == "__main__":
    unittest.main()
