"""Engagement scoring, viewing context, and the diversity of Top Picks.

The numbers in these tests are the ones the live 570-play history produced.
Where a case names a real title it is because that title is what exposed the
behaviour, and reproducing it is the point.
"""

import collections
import math
import random
import time
import unittest

from app.recs import taste

DAY = 86400
NOW = 1_800_000_000


FILE_MB = 1000.0


def play(imdb_id, *, media="series", season=1, episode=1, at=NOW,
         pct=None, secs=None, picker="fast"):
    """One play. `pct` is the share of the file actually delivered — None
    means unmeasured, which is what an imported row looks like."""
    row = {"imdb_id": imdb_id, "media_type": media, "season": season,
           "episode": episode, "played_at": at, "picker": picker,
           "seconds": 0.0 if secs is None else secs}
    if pct is not None:
        row["total_bytes"] = int(FILE_MB * 1e6)
        row["megabytes"] = FILE_MB * pct / 100.0
        # Every real play also reports a position, and for most of them it
        # reads ~100% because the player indexed the file. Present here on
        # purpose: nothing in the model may depend on it.
        row["position_pct"] = 100.0
    return row


def episodes(imdb_id, count, *, start=NOW, spacing=DAY, pct=100.0,
             secs=1500.0, picker="fast"):
    """One episode per session, `spacing` apart."""
    return [play(imdb_id, episode=n + 1, at=start - n * spacing, pct=pct,
                 secs=secs, picker=picker) for n in range(count)]


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

    def test_delivery_is_clamped_so_an_overrun_cannot_inflate_a_title(self):
        """Re-requested ranges can deliver more bytes than the file holds."""
        model = taste.build(episodes("tt-over", 3, pct=200.0), now=NOW)
        signal = model.signal_for("tt-over")
        self.assertEqual(100.0, signal.best_pct)

    def test_a_players_index_read_is_not_a_completed_viewing(self):
        """The mistake this model was built on. Every player reads the
        container index at EOF before it can show a frame, which reports
        ~100% position having moved 2 MB of a 5 GB file. `play()` sets that
        position on every measured row; nothing may be fooled by it."""
        indexed = [play("tt-idx", episode=n + 1, at=NOW - n * DAY,
                        pct=0.04, secs=0.0) for n in range(10)]
        model = taste.build(indexed, now=NOW)
        signal = model.signal_for("tt-idx")
        self.assertEqual(100.0, indexed[0]["position_pct"])
        self.assertEqual(0, signal.finished)
        self.assertEqual(0, signal.started_episodes)
        self.assertFalse(model.may_seed(signal))

    def test_failed_starts_do_not_count_against_a_series_that_was_finished(self):
        """Retries and re-opens are not viewing decisions. Counting them in
        the denominator scored a fully-watched series 0.51."""
        watched = [play("tt-them", episode=n + 1, at=NOW - n * DAY, pct=100.0)
                   for n in range(10)]
        retries = [play("tt-them", episode=n + 1, at=NOW - n * DAY + 60, pct=0.4)
                   for n in range(10)]
        clean = taste.build(watched, now=NOW)
        noisy = taste.build(watched + retries, now=NOW)
        self.assertAlmostEqual(clean.engagement_of("tt-them"),
                               noisy.engagement_of("tt-them"), places=2)

    def test_a_title_only_ever_failed_to_start_earns_no_finish_credit(self):
        model = taste.build([play("tt-dead", media="movie", at=NOW, pct=2.0),
                             play("tt-dead", media="movie", at=NOW + 60,
                                  pct=3.0)], now=NOW)
        self.assertEqual(0, model.signal_for("tt-dead", "movie").started)

    def test_taste_fades_but_does_not_vanish(self):
        old = taste.build(episodes("tt-old", 6, start=NOW - 400 * DAY), now=NOW)
        new = taste.build(episodes("tt-new", 6), now=NOW)
        self.assertGreater(new.engagement_of("tt-new"),
                           old.engagement_of("tt-old"))
        self.assertGreater(old.engagement_of("tt-old"), 0)


class EvidenceTests(unittest.TestCase):
    """What has to be true before a title may lead a row.

    A title is disqualified from seeding by lack of evidence, never convicted
    of being disliked: unwatched and unplayable look identical from here, and
    guessing between them would let delivery failures delete a genre.
    """

    def test_a_series_only_ever_opened_cannot_seed_a_row(self):
        """The case the viewer had to point out. `Them` was opened 37 times
        across ten episodes while being used to test playback, and four
        minutes were watched in total. It seeded eighteen of thirty slots."""
        tested = [play("tt-them", episode=n % 10 + 1, at=NOW - n * 600,
                       pct=0.5, secs=4.0) for n in range(37)]
        model = taste.build(tested, now=NOW)
        signal = model.signal_for("tt-them")
        self.assertTrue(signal.unproven)
        self.assertFalse(model.may_seed(signal))
        self.assertEqual([], model.seed_order())

    def test_one_imported_play_outweighs_any_number_of_empty_measured_ones(self):
        """Planet Earth III: three episodes imported from Trakt, then two
        measured plays of a fourth that delivered nothing — one of them the
        index read. Reading only the measured plays disqualified a title the
        viewer had watched a great deal of. An import asserts viewing, and
        unconsumed measured plays cannot contradict it."""
        imported = [play("tt-planet", episode=n + 2, at=NOW - (10 - n) * DAY,
                         pct=None, picker=taste.IMPORT_PICKER)
                    for n in range(3)]
        empty = [play("tt-planet", episode=5, at=NOW, pct=0.02, secs=0.0),
                 play("tt-planet", episode=5, at=NOW + 5, pct=0.09, secs=0.0)]
        model = taste.build(imported + empty, now=NOW)
        signal = model.signal_for("tt-planet")
        self.assertFalse(signal.unproven)
        self.assertTrue(model.may_seed(signal))
        self.assertEqual(3, signal.started_episodes)

    def test_a_fully_measured_title_is_still_judged_on_what_it_delivered(self):
        """The distinction that keeps the rule honest: no imported plays
        means nothing is vouching for it, so the measurements stand."""
        model = taste.build(
            [play("tt-test", episode=n % 4 + 1, at=NOW - n * 600, pct=0.5,
                  secs=3.0) for n in range(20)], now=NOW)
        self.assertTrue(model.signal_for("tt-test").unproven)

    def test_being_unproven_is_not_the_same_as_being_disliked(self):
        """It still scores positively and stays in the history — it just
        cannot lead. A title that never streamed would look identical."""
        model = taste.build([play("tt-x", media="movie", at=NOW, pct=1.0),
                             play("tt-x", media="movie", at=NOW + 300,
                                  pct=2.0)], now=NOW)
        self.assertGreaterEqual(model.engagement_of("tt-x", "movie"), 0)

    def test_one_abandoned_play_is_not_evidence_of_anything(self):
        """A stream can fail. Two independent failures are a pattern; one is
        as likely to be our own delivery problem as a verdict."""
        model = taste.build([play("tt-once", media="movie", pct=1.0)], now=NOW)
        self.assertFalse(model.signal_for("tt-once", "movie").unproven)

    def test_two_minutes_of_playback_is_enough_to_count_as_watched(self):
        """Byte share alone is not sufficient evidence either way: a long
        episode of a nature series measured 30% of the file across two hours
        of viewing. The clock settles it."""
        model = taste.build([play("tt-doc", at=NOW, pct=3.0, secs=1800.0),
                             play("tt-doc", at=NOW + DAY, pct=3.0, secs=1800.0)],
                            now=NOW)
        signal = model.signal_for("tt-doc")
        self.assertFalse(signal.unproven)
        self.assertTrue(model.may_seed(signal))

    def test_time_watched_raises_a_title_that_never_pulled_the_whole_file(self):
        brief = taste.build(episodes("tt-brief", 3, pct=20.0, secs=90.0),
                            now=NOW)
        long = taste.build(episodes("tt-long", 3, pct=20.0, secs=2400.0),
                           now=NOW)
        self.assertGreater(long.engagement_of("tt-long"),
                           brief.engagement_of("tt-brief"))

    def test_imported_history_is_trusted_without_consumption_data(self):
        """Trakt rows carry no bytes and no duration. Demanding evidence of
        viewing from them would discard two thirds of this history."""
        rows = [play("tt-import", media="movie", at=NOW - n * DAY, pct=None,
                     picker=taste.IMPORT_PICKER) for n in range(4)]
        model = taste.build(rows, now=NOW)
        signal = model.signal_for("tt-import", "movie")
        self.assertFalse(signal.unproven)
        self.assertTrue(model.may_seed(signal))
        self.assertGreater(signal.engagement, 0)

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

    def test_a_kid_profile_counts_every_context(self):
        """On a child's own profile there is nobody else's taste to exclude."""
        model = taste.build(episodes("tt-kid", 30),
                            genres_for=lambda _: ["Kids"], now=NOW)
        self.assertEqual({}, model.genre_affinity())
        self.assertIn("kids", model.genre_affinity(None))


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
        """Every candidate here is its own source, so seed pressure cannot be
        what limits the genre — only calibration can."""
        pool = ([taste.Candidate(f"tt-h{n}", "series", 0.90 - n * 0.001,
                                 ("horror",), f"h{n}") for n in range(25)]
                + [taste.Candidate(f"tt-c{n}", "series", 0.80 - n * 0.001,
                                   ("comedy",), f"c{n}") for n in range(25)]
                + [taste.Candidate(f"tt-d{n}", "series", 0.75 - n * 0.001,
                                   ("documentary",), f"d{n}") for n in range(25)])
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


class CalibrationTests(unittest.TestCase):
    """Steck, "Calibrated Recommendations", RecSys 2018.

    The list should carry the viewer's *proportions*, not their single
    strongest taste and not an even spread. Stated by the household as
    "mostly animation and nature shows, but I really liked Mythic Quest" —
    the minor taste has to survive.
    """

    def pool(self, spec):
        out = []
        for genre, count, base in spec:
            out += [taste.Candidate(f"tt-{genre}{n}", "series", base - n * 0.001,
                                    (genre,), f"{genre}{n}")
                    for n in range(count)]
        return out

    def shares(self, chosen):
        counts = collections.Counter(g for c in chosen for g in c.genres)
        total = sum(counts.values()) or 1
        return {g: n / total for g, n in counts.items()}

    def test_a_minor_taste_is_not_swallowed_by_the_dominant_one(self):
        """Optimising relevance alone hands the whole list to the strongest
        genre and the viewer's smaller interests disappear — Steck's point,
        and the household's "I really liked Mythic Quest"."""
        pool = self.pool([("animation", 40, 0.90), ("comedy", 40, 0.60)])
        target = {"animation": 0.75, "comedy": 0.25}
        uncalibrated = taste.select_diverse(
            pool, 30, rng=random.Random(1), target_genres={"animation": 1.0})
        calibrated = taste.select_diverse(
            pool, 30, rng=random.Random(1), target_genres=target)
        self.assertEqual(0, self.shares(uncalibrated).get("comedy", 0))
        self.assertGreater(self.shares(calibrated).get("comedy", 0), 0)

    def test_calibration_moves_the_list_toward_the_viewer(self):
        """The property that matters, stated as the objective states it: the
        divergence of the list from the viewer must fall."""
        pool = self.pool([("animation", 40, 0.90), ("comedy", 40, 0.60)])
        target = {"animation": 0.75, "comedy": 0.25}
        off = taste.select_diverse(pool, 30, rng=random.Random(1),
                                   target_genres={"animation": 1.0})
        on = taste.select_diverse(pool, 30, rng=random.Random(1),
                                  target_genres=target)
        self.assertLess(
            taste.calibration_divergence(target, self.shares(on)),
            taste.calibration_divergence(target, self.shares(off)))

    def test_the_dominant_taste_still_leads(self):
        """Calibration is not equalisation: 75/25 must not become 50/50."""
        pool = self.pool([("animation", 40, 0.90), ("comedy", 40, 0.60)])
        target = {"animation": 0.75, "comedy": 0.25}
        shares = self.shares(taste.select_diverse(
            pool, 30, rng=random.Random(2), target_genres=target))
        self.assertGreater(shares.get("animation", 0),
                           shares.get("comedy", 0))

    def test_the_list_tracks_the_target_rather_than_the_scores(self):
        weak = self.pool([("animation", 40, 0.90), ("documentary", 40, 0.55)])
        heavy_doc = taste.select_diverse(
            weak, 30, rng=random.Random(3),
            target_genres={"animation": 0.3, "documentary": 0.7})
        light_doc = taste.select_diverse(
            weak, 30, rng=random.Random(3),
            target_genres={"animation": 0.9, "documentary": 0.1})
        self.assertGreater(self.shares(heavy_doc).get("documentary", 0),
                           self.shares(light_doc).get("documentary", 0))

    def test_divergence_is_zero_when_the_list_matches_the_viewer(self):
        target = {"animation": 0.6, "comedy": 0.4}
        self.assertAlmostEqual(
            0.0, taste.calibration_divergence(target, target), places=6)

    def test_a_missing_genre_costs_but_does_not_blow_up(self):
        """Unsmoothed KL is infinite for a genre the list has not reached, and
        the first pick of a greedy fill would then be undefined."""
        value = taste.calibration_divergence({"animation": 0.5, "comedy": 0.5},
                                             {"animation": 1.0})
        self.assertTrue(math.isfinite(value))
        self.assertGreater(value, 0)

    def test_a_title_splits_one_vote_across_its_genres(self):
        """Otherwise a four-genre title is four votes, and animation-comedy-
        adventure-family titles are exactly the kind that carry four."""
        one = taste.genre_distribution([(("animation",), 1.0)])
        four = taste.genre_distribution(
            [(("animation", "comedy", "adventure", "family"), 1.0)])
        self.assertEqual(1.0, one["animation"])
        self.assertAlmostEqual(0.25, four["animation"])

    def test_an_empty_history_has_no_target(self):
        self.assertEqual({}, taste.genre_distribution([]))


class ExplorationTests(unittest.TestCase):
    """The reserved slots. Diversity pressure varies a row within whatever the
    candidates already are; if every candidate came from the viewer's own
    taste, a perfectly diverse row is still thirty flavours of one thing."""

    def pool(self, tailored=40, explorers=10):
        out = [taste.Candidate(f"tt-t{n}", "series", 0.90 - n * 0.001,
                               ("animation",), f"t{n}") for n in range(tailored)]
        out += [taste.Candidate(f"tt-x{n}", "series", 0.20,
                                ("western",), f"x{n}", explore_score=0.9 - n * 0.01)
                for n in range(explorers)]
        return out

    def test_low_scoring_departures_still_reach_the_row(self):
        """At least the reserved six. Possibly more — calibration can also
        pull a departure into a tailored slot on its own merits, and that is
        the system agreeing with itself, not a fault."""
        chosen = taste.select_diverse(self.pool(), 30, rng=random.Random(1),
                                      explore_share=0.2)
        explored = sum(1 for c in chosen if c.explore_score > 0)
        self.assertGreaterEqual(explored, 6)

    def test_the_row_still_leads_with_what_the_viewer_came_for(self):
        chosen = taste.select_diverse(self.pool(), 30, rng=random.Random(2),
                                      explore_share=0.2)
        self.assertEqual(0.0, chosen[0].explore_score)

    def test_a_short_exploration_pool_does_not_shorten_the_row(self):
        chosen = taste.select_diverse(self.pool(explorers=2), 30,
                                      rng=random.Random(3), explore_share=0.2)
        self.assertEqual(30, len(chosen))

    def test_the_budget_is_what_puts_departures_on_the_page(self):
        """Without it a departure only appears if it wins on relevance, which
        for a low-scoring title in an unfamiliar genre is close to never."""
        def count(share):
            chosen = taste.select_diverse(self.pool(), 30, rng=random.Random(4),
                                          explore_share=share)
            return sum(1 for c in chosen if c.explore_score > 0)

        self.assertGreater(count(0.2), count(0.0))


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
