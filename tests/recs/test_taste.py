"""Engagement scoring, viewing context, and the diversity of Top Picks.

The numbers in these tests are the ones the live 570-play history produced.
Where a case names a real title it is because that title is what exposed the
behaviour, and reproducing it is the point.
"""

import random
import time
import unittest

from app.recs import taste

DAY = 86400
NOW = 1_800_000_000


def play(imdb_id, *, media="series", season=1, episode=1, at=NOW,
         pct=None, picker="fast"):
    return {"imdb_id": imdb_id, "media_type": media, "season": season,
            "episode": episode, "played_at": at, "position_pct": pct,
            "picker": picker}


def episodes(imdb_id, count, *, start=NOW, spacing=DAY, pct=100.0,
             picker="fast"):
    """One episode per session, `spacing` apart."""
    return [play(imdb_id, episode=n + 1, at=start - n * spacing, pct=pct,
                 picker=picker) for n in range(count)]


class EngagementTests(unittest.TestCase):
    """The signal that replaced `plays >= 2 -> rating 9`."""

    def test_a_series_watched_through_beats_one_sampled_twice(self):
        """The bug this module exists for. Under the old derived rating both
        of these scored an identical 9, leaving recency as the only
        tie-break — which is how two recent shows came to own the whole row."""
        model = taste.build(episodes("tt-deep", 10) + episodes("tt-sample", 2),
                            now=NOW)
        self.assertGreater(model.engagement_of("tt-deep"),
                           model.engagement_of("tt-sample"))

    def test_coming_back_another_day_outweighs_volume_in_one_sitting(self):
        """25 rows in one evening is one decision; four evenings is four."""
        binge = [play("tt-binge", episode=n + 1, at=NOW + n * 60, pct=100.0)
                 for n in range(8)]
        spread = episodes("tt-spread", 8, spacing=3 * DAY)
        model = taste.build(binge + spread, now=NOW)
        self.assertGreater(model.engagement_of("tt-spread"),
                           model.engagement_of("tt-binge"))

    def test_finishing_counts_for_more_than_starting(self):
        model = taste.build(episodes("tt-fin", 5, pct=100.0)
                            + episodes("tt-part", 5, pct=30.0), now=NOW)
        self.assertGreater(model.engagement_of("tt-fin"),
                           model.engagement_of("tt-part"))

    def test_position_is_clamped_so_an_overrun_cannot_inflate_a_title(self):
        """`position_pct` has been observed at 200: bytes delivered run past
        the file when a player re-requests ranges."""
        model = taste.build(episodes("tt-over", 3, pct=200.0), now=NOW)
        signal = model.signal_for("tt-over")
        self.assertEqual(100.0, signal.best_pct)

    def test_taste_fades_but_does_not_vanish(self):
        old = taste.build(episodes("tt-old", 6, start=NOW - 400 * DAY), now=NOW)
        new = taste.build(episodes("tt-new", 6), now=NOW)
        self.assertGreater(new.engagement_of("tt-new"),
                           old.engagement_of("tt-old"))
        self.assertGreater(old.engagement_of("tt-old"), 0)


class BounceTests(unittest.TestCase):
    """Dislike, which the previous model could not express at all."""

    def test_repeatedly_opened_and_never_watched_scores_negative(self):
        """Supergirl: two plays, 0.0% and 23.3%, and it was seed number one."""
        model = taste.build([play("tt-bounce", media="movie", at=NOW, pct=0.0),
                             play("tt-bounce", media="movie", at=NOW + 300,
                                  pct=4.0)], now=NOW)
        self.assertTrue(model.signal_for("tt-bounce", "movie").bounced)
        self.assertLess(model.engagement_of("tt-bounce", "movie"), 0)

    def test_one_abandoned_play_is_not_evidence_of_anything(self):
        """A stream can fail. Two independent failures are a pattern; one is
        as likely to be our own delivery problem as a verdict."""
        model = taste.build([play("tt-once", media="movie", pct=1.0)], now=NOW)
        self.assertFalse(model.signal_for("tt-once", "movie").bounced)

    def test_getting_going_and_stopping_is_not_a_bounce(self):
        model = taste.build([play("tt-part", media="movie", at=NOW, pct=40.0),
                             play("tt-part", media="movie", at=NOW + 300,
                                  pct=55.0)], now=NOW)
        self.assertFalse(model.signal_for("tt-part", "movie").bounced)

    def test_imported_history_can_never_be_scored_badly(self):
        """Trakt rows carry no position at all. Absence of evidence must not
        become evidence of dislike — two thirds of this history is imported."""
        rows = [play("tt-import", media="movie", at=NOW - n * DAY, pct=None,
                     picker=taste.IMPORT_PICKER) for n in range(4)]
        model = taste.build(rows, now=NOW)
        self.assertFalse(model.signal_for("tt-import", "movie").bounced)
        self.assertGreater(model.engagement_of("tt-import", "movie"), 0)

    def test_measured_history_carries_more_confidence_than_imported(self):
        model = taste.build(
            episodes("tt-seen", 4)
            + episodes("tt-told", 4, pct=None, picker=taste.IMPORT_PICKER),
            now=NOW)
        self.assertGreater(model.signal_for("tt-seen").confidence,
                           model.signal_for("tt-told").confidence)


class ViewingContextTests(unittest.TestCase):
    """Whose choice was it — a household plays kids' TV on a parent's key."""

    def test_the_kids_genre_marks_family_viewing(self):
        model = taste.build(episodes("tt-batwheels", 20),
                            genres_for=lambda _: ["Kids", "Animation"],
                            now=NOW)
        self.assertEqual(taste.CONTEXT_FAMILY,
                         model.context_of("tt-batwheels"))

    def test_adult_animation_watched_in_the_afternoon_stays_the_viewers_own(self):
        """The regression that killed the hour-of-day classifier: Castlevania:
        Nocturne and Kaiju No. 8 are TV-MA and were both filed as children's
        viewing purely because their plays landed after lunch."""
        afternoon = [play("tt-castlevania", episode=n + 1,
                          at=NOW - n * DAY + 14 * 3600, pct=100.0)
                     for n in range(6)]
        model = taste.build(
            afternoon, genres_for=lambda _: ["Animation", "Drama"], now=NOW)
        self.assertEqual(taste.CONTEXT_SOLO, model.context_of("tt-castlevania"))

    def test_family_titles_are_not_offered_as_seeds(self):
        def genres(imdb_id):
            return ["Kids"] if imdb_id == "tt-kid" else ["Drama"]

        model = taste.build(episodes("tt-kid", 20) + episodes("tt-own", 6),
                            genres_for=genres, now=NOW)
        self.assertEqual(["tt-own"],
                         [s.imdb_id for s in model.seed_order()])

    def test_genre_affinity_ignores_what_somebody_else_chose(self):
        def genres(imdb_id):
            return ["Kids", "Animation"] if imdb_id == "tt-kid" else ["Horror"]

        model = taste.build(episodes("tt-kid", 30) + episodes("tt-own", 4),
                            genres_for=genres, now=NOW)
        self.assertNotIn("kids", model.genre_affinity())
        self.assertIn("horror", model.genre_affinity())


class SessionTests(unittest.TestCase):
    def test_plays_close_together_are_one_sitting(self):
        stamps = [NOW, NOW + 600, NOW + 1200]
        self.assertEqual(1, taste.sessionise(stamps))

    def test_a_long_gap_starts_a_new_sitting(self):
        stamps = [NOW, NOW + 600, NOW + 5 * 3600]
        self.assertEqual(2, taste.sessionise(stamps))

    def test_order_does_not_matter(self):
        self.assertEqual(2, taste.sessionise([NOW + 5 * 3600, NOW]))


class DiversitySelectionTests(unittest.TestCase):
    """The row-shaping rule. Every case here is the live failure or a
    boundary that the fix must not break in passing."""

    def candidates(self, seed, genres, count, base=0.9, media="series"):
        return [taste.Candidate(f"tt-{seed}-{n}", media, base - n * 0.01,
                                tuple(genres), seed) for n in range(count)]

    def test_no_single_seed_can_own_the_row(self):
        """Live: 'Them' took eighteen slots and 'Planet Earth III' the other
        twelve, so four healthy seeds contributed nothing at all."""
        pool = (self.candidates("them", ["drama", "mystery"], 30, base=0.90)
                + self.candidates("planet", ["documentary"], 30, base=0.85)
                + self.candidates("mythic", ["comedy"], 12, base=0.80)
                + self.candidates("sunny", ["comedy"], 12, base=0.78))
        chosen = taste.select_diverse(pool, 30, rng=random.Random(1))
        used = {c.seed_id for c in chosen}
        self.assertEqual({"them", "planet", "mythic", "sunny"}, used)
        biggest = max(sum(1 for c in chosen if c.seed_id == s) for s in used)
        self.assertLess(biggest, 15)

    def test_the_strongest_seed_still_leads(self):
        pool = (self.candidates("strong", ["drama"], 20, base=0.95)
                + self.candidates("weak", ["comedy"], 20, base=0.40))
        chosen = taste.select_diverse(pool, 10, rng=random.Random(2))
        self.assertEqual("strong", chosen[0].seed_id)

    def test_one_genre_cannot_take_the_whole_row(self):
        pool = (self.candidates("a", ["horror"], 25, base=0.90)
                + self.candidates("b", ["comedy"], 25, base=0.80)
                + self.candidates("c", ["documentary"], 25, base=0.75))
        chosen = taste.select_diverse(pool, 30, rng=random.Random(3))
        horror = sum(1 for c in chosen if "horror" in c.genres)
        self.assertLess(horror, 18)

    def test_the_row_still_fills_when_only_one_seed_is_usable(self):
        """Soft quotas, not filters. A viewer with one strong title must get a
        full row, not a short one — the pressure is paid, not enforced."""
        pool = self.candidates("only", ["drama"], 40)
        chosen = taste.select_diverse(pool, 30, rng=random.Random(4))
        self.assertEqual(30, len(chosen))

    def test_a_short_pool_returns_what_there_is(self):
        chosen = taste.select_diverse(self.candidates("a", ["drama"], 4), 30)
        self.assertEqual(4, len(chosen))

    def test_films_appear_when_the_viewer_watches_them(self):
        pool = (self.candidates("s", ["drama"], 30, base=0.90)
                + self.candidates("m", ["action"], 30, base=0.60,
                                  media="movie"))
        chosen = taste.select_diverse(pool, 30, rng=random.Random(5),
                                      movie_share=0.3)
        movies = sum(1 for c in chosen if c.media_type == "movie")
        self.assertGreater(movies, 3)

    def test_nothing_is_selected_twice(self):
        pool = (self.candidates("a", ["drama"], 20)
                + self.candidates("b", ["comedy"], 20))
        chosen = taste.select_diverse(pool, 30, rng=random.Random(6))
        self.assertEqual(len(chosen), len({c.imdb_id for c in chosen}))

    def test_the_row_is_stable_within_a_day_and_moves_between_days(self):
        """The randomisation has to be reproducible: a row that reshuffles on
        every request cannot be debugged, and one that never moves is the
        complaint this work started from."""
        pool = (self.candidates("a", ["drama"], 25)
                + self.candidates("b", ["comedy"], 25)
                + self.candidates("c", ["documentary"], 25))
        monday = [c.imdb_id for c in
                  taste.select_diverse(pool, 30, rng=random.Random("tok:mon"))]
        again = [c.imdb_id for c in
                 taste.select_diverse(pool, 30, rng=random.Random("tok:mon"))]
        tuesday = [c.imdb_id for c in
                   taste.select_diverse(pool, 30, rng=random.Random("tok:tue"))]
        self.assertEqual(monday, again)
        self.assertNotEqual(monday, tuesday)


class ModelSummaryTests(unittest.TestCase):
    def test_summary_separates_measured_from_imported(self):
        model = taste.build(
            episodes("tt-seen", 3)
            + episodes("tt-told", 3, pct=None, picker=taste.IMPORT_PICKER),
            now=NOW)
        summary = model.summary()
        self.assertEqual(2, summary["titles"])
        self.assertEqual(1, summary["measured"])

    def test_an_empty_history_produces_an_empty_model(self):
        model = taste.build([], now=NOW)
        self.assertEqual([], model.seed_order())
        self.assertEqual({}, model.genre_affinity())
        self.assertEqual(0.5, model.media_share())

    def test_rows_without_an_imdb_id_are_skipped(self):
        model = taste.build([{"media_type": "series", "played_at": NOW}],
                            now=NOW)
        self.assertEqual(0, len(model.signals))


if __name__ == "__main__":
    unittest.main()
