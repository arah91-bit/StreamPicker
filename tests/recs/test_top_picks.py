"""Top Picks: which seeds get to contribute, and what reaches the row.

The row this covers is the one a viewer sees first, and it used to be built by
concatenating each seed's similars and truncating — so the first seed to fill
thirty slots won all of them. These tests pin the properties that replaced it.
"""

import unittest
from unittest.mock import AsyncMock, patch

from app.recs import db, taste
from app.recs.catalogs import TOP_PICKS_SEED_COUNT, Generator


def profile(seeds):
    return {
        "genres": {"movie": [("drama", 5.0)], "show": [("drama", 5.0)]},
        "decades": [(2010, 5.0)], "languages": [("en", 10.0)],
        "seeds": seeds, "loved": [],
        "watched_imdb": set(), "watched_tmdb_movie": set(),
        "watched_tmdb_show": set(),
    }


def seed(imdb_id, kind="show", tmdb=1, title=None):
    return {"imdb": imdb_id, "tmdb": tmdb, "type": kind,
            "title": title or imdb_id, "year": 2020, "genres": ["drama"],
            "rating": 9, "last_watched": 0, "score": 1.0}


def signal(imdb_id, media="series", engagement=0.5,
           context=taste.CONTEXT_SOLO, confidence=1.0):
    return taste.TitleSignal(imdb_id=imdb_id, media_type=media,
                             engagement=engagement, context=context,
                             confidence=confidence)


def model(*signals):
    return taste.TasteModel(signals={s.key: s for s in signals})


class SeedSelectionTests(unittest.TestCase):
    def generator(self, seeds, signals, **user):
        gen = Generator({"token": "seed-viewer", "is_kid": 0, **user})
        gen.profile = profile(seeds)
        gen.taste = model(*signals)
        return gen

    def test_seeds_are_ordered_by_engagement_not_by_recency(self):
        """The whole complaint: the row was built from whatever was watched
        most recently, because every title scored an identical rating."""
        gen = self.generator(
            [seed("tt-recent"), seed("tt-loved")],
            [signal("tt-recent", engagement=0.1),
             signal("tt-loved", engagement=0.9)])
        ordered = [s["imdb"] for s, _ in gen._ranked_seeds(5)]
        self.assertEqual(["tt-loved", "tt-recent"], ordered)

    def test_a_title_somebody_else_chose_is_never_a_seed(self):
        gen = self.generator(
            [seed("tt-kid"), seed("tt-own")],
            [signal("tt-kid", engagement=0.9, context=taste.CONTEXT_FAMILY),
             signal("tt-own", engagement=0.2)])
        self.assertEqual(["tt-own"], [s["imdb"] for s, _ in gen._ranked_seeds(5)])

    def test_a_child_keeps_their_own_childrens_television_as_a_seed(self):
        """The family/solo split exists to keep other people's choices out of
        an ADULT's taste. Applied to a kid profile it deleted most of their
        history — one eleven-year-old lost four of seven seeds."""
        gen = self.generator(
            [seed("tt-kid"), seed("tt-other")],
            [signal("tt-kid", engagement=0.9, context=taste.CONTEXT_FAMILY),
             signal("tt-other", engagement=0.2)],
            is_kid=1, kid_age=11)
        self.assertEqual(["tt-kid", "tt-other"],
                         [s["imdb"] for s, _ in gen._ranked_seeds(5)])

    def test_a_bounced_title_is_dropped_rather_than_demoted(self):
        """One seed is a dozen candidates. A negative one is a visible chunk
        of the row, not a rounding error."""
        gen = self.generator(
            [seed("tt-bounce"), seed("tt-ok")],
            [signal("tt-bounce", engagement=-0.3), signal("tt-ok")])
        self.assertEqual(["tt-ok"], [s["imdb"] for s, _ in gen._ranked_seeds(5)])

    def test_films_are_allocated_seed_slots_of_their_own(self):
        """Ranked in one list, series win every slot: episodes accumulate
        breadth and a film has only ever one thing to finish. A live run
        produced ten series seeds out of ten and a row with no films at all."""
        seeds = ([seed(f"tt-s{n}") for n in range(12)]
                 + [seed(f"tt-m{n}", kind="movie") for n in range(4)])
        signals = ([signal(f"tt-s{n}", engagement=0.9) for n in range(12)]
                   + [signal(f"tt-m{n}", media="movie", engagement=0.2)
                      for n in range(4)])
        gen = self.generator(seeds, signals)
        with patch.object(Generator, "_movie_share", return_value=0.3):
            chosen = gen._ranked_seeds(TOP_PICKS_SEED_COUNT)
        films = [s for s, _ in chosen if s["type"] == "movie"]
        self.assertEqual(3, len(films))

    def test_a_weak_film_seed_earns_a_place_but_not_a_strong_score(self):
        """Normalising strength per medium as well as allocating slots per
        medium was tried, and inflated a film abandoned at 23% to 0.87."""
        gen = self.generator(
            [seed("tt-show"), seed("tt-film", kind="movie")],
            [signal("tt-show", engagement=0.9),
             signal("tt-film", media="movie", engagement=0.1)])
        with patch.object(Generator, "_movie_share", return_value=0.3):
            strengths = dict((s["imdb"], v) for s, v in gen._ranked_seeds(5))
        self.assertIn("tt-film", strengths)
        self.assertLess(strengths["tt-film"], 0.3)

    def test_without_a_taste_model_the_profile_order_is_used_unchanged(self):
        gen = Generator({"token": "cold", "is_kid": 0})
        gen.profile = profile([seed("tt-a"), seed("tt-b")])
        gen.taste = None
        self.assertEqual(["tt-a", "tt-b"],
                         [s["imdb"] for s, _ in gen._ranked_seeds(5)])

    def test_a_seed_with_no_plays_behind_it_still_gets_the_row_built(self):
        """Preseed anchors for a new viewer have no history by definition.
        Dropping them left a cold-start profile with no Top Picks at all."""
        gen = self.generator([seed("tt-anchor")], [])
        self.assertEqual(["tt-anchor"],
                         [s["imdb"] for s, _ in gen._ranked_seeds(5)])

    def test_measured_taste_outranks_a_hand_picked_anchor(self):
        gen = self.generator(
            [seed("tt-anchor"), seed("tt-real")],
            [signal("tt-real", engagement=0.6)])
        self.assertEqual(["tt-real", "tt-anchor"],
                         [s["imdb"] for s, _ in gen._ranked_seeds(5)])


class RowCompositionTests(unittest.IsolatedAsyncioTestCase):
    def generator(self, similars, **user):
        gen = Generator({"token": "row-viewer", "is_kid": 0, **user})
        gen.profile = profile([seed("tt-a", tmdb=1), seed("tt-b", tmdb=2)])
        gen.taste = model(signal("tt-a", engagement=0.9),
                          signal("tt-b", engagement=0.8))
        gen._seed_similar = AsyncMock(side_effect=lambda s, limit: similars(s))
        return gen

    @staticmethod
    def similar(prefix, count, genres, media="series"):
        return [{"id": f"tt{prefix}{n:03d}", "name": f"{prefix} {n}",
                 "type": media, "genres": list(genres), "imdbRating": "7.5"}
                for n in range(count)]

    async def test_every_seed_that_returns_candidates_reaches_the_row(self):
        def similars(s):
            return (self.similar("A", 30, ["Drama", "Mystery"])
                    if s["imdb"] == "tt-a"
                    else self.similar("B", 30, ["Documentary"]))

        row = await self.generator(similars)._top_picks()
        seeds_used = {meta["id"][:3] for meta in row["metas"]}
        self.assertEqual({"ttA", "ttB"}, seeds_used)

    async def test_childrens_television_is_kept_out_of_an_adults_row(self):
        """Excluding kids' titles from the seeds is not enough — an adult seed
        still reaches children's TV through a shared genre. A live run
        surfaced Huckleberry Hound and a 1983 Dungeons & Dragons cartoon."""
        def similars(s):
            return (self.similar("K", 10, ["Animation", "Kids"])
                    + self.similar("N", 10, ["Drama"]))

        row = await self.generator(similars)._top_picks()
        self.assertTrue(all("Kids" not in (m.get("genres") or [])
                            for m in row["metas"]))
        self.assertTrue(row["metas"])

    async def test_a_kid_profile_keeps_its_own_childrens_television(self):
        def similars(s):
            return self.similar("K", 20, ["Animation", "Kids"])

        gen = self.generator(similars, is_kid=1, kid_age=6)
        row = await gen._top_picks()
        self.assertTrue(row["metas"])

    async def test_a_row_that_cannot_be_filled_is_not_offered(self):
        row = await self.generator(lambda s: self.similar("A", 1, ["Drama"]))._top_picks()
        self.assertIsNone(row)

    async def test_a_failing_seed_does_not_take_the_row_down_with_it(self):
        def similars(s):
            if s["imdb"] == "tt-a":
                raise RuntimeError("TMDB said no")
            return self.similar("B", 30, ["Drama"])

        row = await self.generator(similars)._top_picks()
        self.assertEqual(30, len(row["metas"]))


class FakeFingerprint:
    """Stands in for a real fingerprint: lift comes from a lookup table."""

    def __init__(self, lifts, keywords=("k:11", "k:12", "k:13"),
                 people=("p:99",)):
        self.lifts = lifts
        self._keywords = list(keywords)
        self._people = list(people)

    def lift(self, tokens):
        return max((self.lifts.get(t, 0.0) for t in tokens), default=0.0)

    def top_features(self, limit=40, families=("k", "p")):
        pool = ([t for t in self._keywords if "k" in families]
                + [t for t in self._people if "p" in families])
        return pool[:limit]

    def summary(self):
        return {"titles": 20, "dimensions": 900}


class FingerprintRowTests(unittest.IsolatedAsyncioTestCase):
    """Candidate generation and scoring once the viewer has a fingerprint."""

    def generator(self, *, similars=None, swept=None, lifts=None, **user):
        gen = Generator({"token": "fp-viewer", "is_kid": 0, **user})
        gen.profile = profile([seed("tt-a", tmdb=1), seed("tt-b", tmdb=2)])
        gen.taste = model(signal("tt-a", engagement=0.9),
                          signal("tt-b", engagement=0.8))
        gen.fingerprint = FakeFingerprint(lifts or {})
        gen._seed_similar = AsyncMock(
            side_effect=lambda s, limit: (similars or (lambda _: []))(s))
        gen._resolve_discover = AsyncMock(
            side_effect=lambda media, params, limit, pages=1:
                (swept or (lambda m, p: []))(media, params))
        return gen

    @staticmethod
    def metas(prefix, count, genres=("Drama",), media="series"):
        return [{"id": f"tt{prefix}{n:03d}", "name": f"{prefix} {n}",
                 "type": media, "genres": list(genres), "imdbRating": "7.0"}
                for n in range(count)]

    async def test_the_sweep_reaches_titles_no_seed_is_linked_to(self):
        """The ceiling this lifts: seeds can only return what TMDB already
        lists as similar to something watched."""
        gen = self.generator(
            similars=lambda s: self.metas("SEED", 10),
            swept=lambda m, p: self.metas("SWEPT", 20))
        with patch.object(db, "features_by_imdb", AsyncMock(return_value={})):
            row = await gen._top_picks()
        ids = {m["id"][:5] for m in row["metas"]}
        self.assertIn("ttSWE", ids)
        self.assertIn("ttSEE", ids)

    async def test_discover_queries_are_built_from_the_top_features(self):
        seen = []

        def sweep(media, params):
            seen.append(params)
            return self.metas("S", 5)

        gen = self.generator(similars=lambda s: self.metas("A", 10),
                             swept=sweep)
        with patch.object(db, "features_by_imdb", AsyncMock(return_value={})):
            await gen._top_picks()
        keyword_queries = [p["with_keywords"] for p in seen if "with_keywords" in p]
        self.assertTrue(keyword_queries)
        # OR-ed, not AND-ed: an intersection of three keywords is almost empty.
        self.assertIn("|", keyword_queries[0])
        self.assertTrue(any("with_people" in p for p in seen))

    async def test_the_sweep_does_not_sort_everything_by_popularity(self):
        """Sorting every query by popularity returns whatever is biggest this
        month. A live run gave a nature-and-animation viewer Bridgerton,
        Modern Family and two Harry Potters."""
        seen = []

        def sweep(media, params):
            seen.append(params)
            return self.metas("S", 5)

        gen = self.generator(similars=lambda s: self.metas("A", 10), swept=sweep)
        with patch.object(db, "features_by_imdb", AsyncMock(return_value={})):
            await gen._top_picks()
        sorts = {p.get("sort_by") for p in seen}
        self.assertIn("popularity.desc", sorts)
        self.assertIn("vote_average.desc", sorts)
        rated = [p for p in seen if p.get("sort_by") == "vote_average.desc"]
        # A rating sort with no vote floor returns titles with one 10/10.
        self.assertTrue(all(p.get("vote_count.gte") for p in rated))

    async def test_people_are_only_asked_for_on_the_movie_endpoint(self):
        """`with_people` is a movie-only discover filter. Sent to /discover/tv
        it is ignored, and an unfiltered popularity list comes back."""
        seen = []

        def sweep(media, params):
            seen.append((media, params))
            return self.metas("S", 5)

        gen = self.generator(similars=lambda s: self.metas("A", 10), swept=sweep)
        with patch.object(db, "features_by_imdb", AsyncMock(return_value={})):
            await gen._top_picks()
        self.assertTrue(seen)
        for media, params in seen:
            if "with_people" in params:
                self.assertEqual("movie", media)

    async def test_a_high_lift_candidate_outranks_a_low_lift_one(self):
        """LOW is offered first by the seed, so seed order alone would keep it
        ahead. Only the lift can overturn that."""
        gen = self.generator(
            similars=lambda s: (self.metas("LOW", 8) + self.metas("HIGH", 1)),
            swept=lambda m, p: [],
            lifts={"k:match": 5.0})
        store = {"ttHIGH000": ["k:match"]}
        store.update({f"ttLOW{n:03d}": ["k:other"] for n in range(8)})
        with patch.object(db, "features_by_imdb", AsyncMock(return_value=store)):
            row = await gen._top_picks()
        order = [m["id"] for m in row["metas"]]
        self.assertLess(order.index("ttHIGH000"), order.index("ttLOW000"))

    async def test_the_row_still_builds_when_no_title_has_features_yet(self):
        """Before the backfill finishes, the store is empty. That has to
        degrade to seed ranking, not to an empty row."""
        gen = self.generator(similars=lambda s: self.metas("A", 30),
                             swept=lambda m, p: [])
        with patch.object(db, "features_by_imdb", AsyncMock(return_value={})):
            row = await gen._top_picks()
        self.assertEqual(30, len(row["metas"]))

    async def test_without_a_fingerprint_nothing_is_swept_or_looked_up(self):
        gen = self.generator(similars=lambda s: self.metas("A", 30),
                             swept=lambda m, p: self.metas("S", 10))
        gen.fingerprint = None
        lookup = AsyncMock(return_value={})
        with patch.object(db, "features_by_imdb", lookup):
            row = await gen._top_picks()
        gen._resolve_discover.assert_not_awaited()
        lookup.assert_not_awaited()
        self.assertTrue(all(m["id"].startswith("ttA") for m in row["metas"]))

    async def test_a_failing_sweep_leaves_the_seed_candidates_intact(self):
        def boom(media, params):
            raise RuntimeError("TMDB said no")

        gen = self.generator(similars=lambda s: self.metas("A", 30), swept=boom)
        with patch.object(db, "features_by_imdb", AsyncMock(return_value={})):
            row = await gen._top_picks()
        self.assertEqual(30, len(row["metas"]))

    async def test_swept_candidates_are_kids_filtered_like_any_other(self):
        gen = self.generator(
            similars=lambda s: self.metas("A", 10),
            swept=lambda m, p: self.metas("K", 20, genres=("Animation", "Kids")))
        with patch.object(db, "features_by_imdb", AsyncMock(return_value={})):
            row = await gen._top_picks()
        self.assertTrue(all("Kids" not in (m.get("genres") or [])
                            for m in row["metas"]))


if __name__ == "__main__":
    unittest.main()
