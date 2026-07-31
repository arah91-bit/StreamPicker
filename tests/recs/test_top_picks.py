"""Top Picks: which seeds get to contribute, and what reaches the row.

The row this covers is the one a viewer sees first, and it used to be built by
concatenating each seed's similars and truncating — so the first seed to fill
thirty slots won all of them. These tests pin the properties that replaced it.
"""

import unittest
from unittest.mock import AsyncMock, patch

from app.recs import taste
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


if __name__ == "__main__":
    unittest.main()
