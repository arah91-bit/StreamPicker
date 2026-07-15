"""Semantic title identity independent of transport/playability checks."""

import dataclasses
import unittest

from app import content_identity as identity


class ProfileTests(unittest.TestCase):
    def test_profile_is_immutable_and_canonicalizes_inputs(self):
        profile = identity.IdentityProfile(
            media="TV", imdb_id="TT0386676:1:1",
            aliases=("The Office", "The Office"), years=frozenset({2005}),
            season=1, episode=1,
            region_tags=frozenset({"United States", "USA"}),
        )
        self.assertEqual("series", profile.media)
        self.assertEqual("tt0386676", profile.imdb_id)
        self.assertEqual(("The Office",), profile.aliases)
        self.assertEqual(frozenset({"us"}), profile.region_tags)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            profile.media = "movie"

    def test_profile_rejects_partial_episode_and_movie_episode(self):
        with self.assertRaises(ValueError):
            identity.IdentityProfile("series", "tt1", ("Show",), season=1)
        with self.assertRaises(ValueError):
            identity.IdentityProfile(
                "movie", "tt1", ("Movie",), season=1, episode=1)


class MovieIdentityTests(unittest.TestCase):
    def setUp(self):
        self.ghost_1995 = identity.IdentityProfile(
            media="movie", imdb_id="tt0113568",
            aliases=("Ghost in the Shell", "Koukaku Kidoutai"),
            years=frozenset({1995}), region_tags=frozenset({"Japan"}),
        )

    def test_exact_title_and_year_is_strong(self):
        self.assertEqual(identity.STRONG, identity.classify(
            self.ghost_1995,
            "Ghost.in.the.Shell.1995.1080p.BluRay.x265.mkv"))

    def test_wrong_explicit_year_is_a_contradiction(self):
        self.assertEqual(identity.CONTRADICTION, identity.classify(
            self.ghost_1995,
            "Ghost.in.the.Shell.2015.1080p.WEB-DL.mkv",
            trusted_imdb=True))

    def test_yearless_same_title_movie_is_only_compatible(self):
        self.assertEqual(identity.COMPATIBLE, identity.classify(
            self.ghost_1995,
            "Ghost.in.the.Shell.1080p.BluRay.x265.mkv"))

    def test_exact_per_item_imdb_resolves_a_yearless_movie(self):
        result = identity.assess(
            self.ghost_1995, "Ghost.in.the.Shell.1080p.BluRay.x265.mkv",
            trusted_imdb=True)
        self.assertEqual(identity.STRONG, result.state)
        self.assertEqual(identity.EVIDENCE_TRUSTED_IMDB, result.evidence)

    def test_english_or_canonical_original_alias_can_be_strong(self):
        self.assertEqual(identity.STRONG, identity.classify(
            self.ghost_1995,
            "Koukaku.Kidoutai.1995.1080p.BluRay.mkv"))

    def test_short_title_never_prefix_matches_another_title(self):
        it = identity.IdentityProfile(
            "movie", "tt1396484", ("It",), frozenset({2017}))
        up = identity.IdentityProfile(
            "movie", "tt1049413", ("Up",), frozenset({2009}))
        self.assertEqual(identity.CONTRADICTION, identity.classify(
            it, "It.Follows.2017.1080p.WEB-DL.mkv", trusted_imdb=True))
        self.assertEqual(identity.CONTRADICTION, identity.classify(
            up, "Upgrade.2009.1080p.WEB-DL.mkv", trusted_imdb=True))
        self.assertEqual(identity.STRONG, identity.classify(
            it, "It.2017.1080p.WEB-DL.mkv"))
        self.assertEqual(identity.STRONG, identity.classify(
            up, "Up.2009.1080p.BluRay.mkv"))

    def test_scene_punctuation_variants_preserve_exact_title_match(self):
        profile = identity.IdentityProfile(
            "movie", "tt0108052", ("Schindler's List",),
            frozenset({1993}))
        self.assertEqual(identity.STRONG, identity.classify(
            profile, "Schindlers.List.1993.1080p.BluRay.mkv"))

    def test_missing_or_obfuscated_evidence_is_unknown(self):
        for text in ("", "video.mkv", "9f3d77989a6b4d5f8c04a11e.mkv"):
            with self.subTest(text=text):
                self.assertEqual(identity.UNKNOWN, identity.classify(
                    self.ghost_1995, text, trusted_imdb=True))

    def test_alias_year_is_not_mistaken_for_release_year(self):
        title = identity.IdentityProfile(
            "movie", "tt0087803", ("Nineteen Eighty-Four", "1984"),
            frozenset({1984}))
        # The year after the consumed numeric alias is the release evidence.
        self.assertEqual(identity.STRONG, identity.classify(
            title, "1984.1984.1080p.BluRay.mkv"))
        self.assertEqual(identity.COMPATIBLE, identity.classify(
            title, "1984.1080p.BluRay.mkv"))


class SeriesIdentityTests(unittest.TestCase):
    def setUp(self):
        self.office_uk = identity.IdentityProfile(
            media="series", imdb_id="tt0290978", aliases=("The Office",),
            years=frozenset({2001}), season=1, episode=1,
            region_tags=frozenset({"UK"}),
        )
        self.office_us = identity.IdentityProfile(
            media="series", imdb_id="tt0386676", aliases=("The Office",),
            years=frozenset({2005}), season=1, episode=1,
            region_tags=frozenset({"US"}),
        )

    def test_office_region_tags_disambiguate_uk_and_us(self):
        uk = "The.Office.UK.S01E01.1080p.WEB-DL.mkv"
        us = "The.Office.US.S01E01.1080p.WEB-DL.mkv"
        self.assertEqual(identity.STRONG,
                         identity.classify(self.office_uk, uk))
        self.assertEqual(identity.CONTRADICTION,
                         identity.classify(self.office_uk, us,
                                           trusted_imdb=True,
                                           trusted_episode=True))
        self.assertEqual(identity.STRONG,
                         identity.classify(self.office_us, us))
        self.assertEqual(identity.CONTRADICTION,
                         identity.classify(self.office_us, uk))

    def test_start_year_also_disambiguates_editions(self):
        self.assertEqual(identity.STRONG, identity.classify(
            self.office_uk, "The.Office.2001.S01E01.1080p.WEB-DL.mkv"))
        self.assertEqual(identity.CONTRADICTION, identity.classify(
            self.office_uk, "The.Office.2005.S01E01.1080p.WEB-DL.mkv"))

    def test_exact_episode_plus_trusted_imdb_can_resolve_yearless_title(self):
        text = "The.Office.S01E01.1080p.WEB-DL.mkv"
        self.assertEqual(identity.COMPATIBLE,
                         identity.classify(self.office_uk, text))
        self.assertEqual(identity.STRONG, identity.classify(
            self.office_uk, text, trusted_imdb=True))

    def test_wrong_episode_season_and_multi_episode_are_contradictions(self):
        wrong = (
            "The.Office.UK.S01E02.1080p.WEB-DL.mkv",
            "The.Office.UK.S02E01.1080p.WEB-DL.mkv",
            "The.Office.UK.S01E01-E03.1080p.WEB-DL.mkv",
            "The.Office.UK.S01E01E02.1080p.WEB-DL.mkv",
        )
        for text in wrong:
            with self.subTest(text=text):
                self.assertEqual(identity.CONTRADICTION,
                                 identity.classify(self.office_uk, text))

    def test_trusted_episode_is_explicit_context(self):
        text = "The.Office.UK.1080p.WEB-DL.mkv"
        self.assertEqual(identity.COMPATIBLE,
                         identity.classify(self.office_uk, text))
        self.assertEqual(identity.STRONG, identity.classify(
            self.office_uk, text, trusted_episode=True))


class EvidenceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.movie = identity.IdentityProfile(
            "movie", "tt0113568", ("Ghost in the Shell",),
            frozenset({1995}))

    def test_private_upstream_markers_cannot_confer_trust(self):
        # The public API accepts text, not an upstream mapping. Merely spelling
        # a private marker in the filename cannot promote ambiguous evidence.
        stream = {
            "behaviorHints": {
                "filename": "Ghost.in.the.Shell.1080p.BluRay.mkv"},
            "_identity_verified": True,
            "_trusted_imdb": True,
            "_picker_verified": True,
        }
        self.assertEqual(identity.COMPATIBLE, identity.classify(
            self.movie, stream["behaviorHints"]["filename"]))

    def test_filename_and_release_label_combiner_uses_contradiction_dominance(self):
        self.assertEqual(identity.CONTRADICTION, identity.classify_evidence(
            self.movie,
            filename="Another.Movie.1995.1080p.mkv",
            release_label="Ghost.in.the.Shell.1995.1080p.BluRay",
            trusted_imdb=True))
        self.assertEqual(identity.STRONG, identity.classify_evidence(
            self.movie,
            filename="9f3d77989a6b4d5f8c04a11e.mkv",
            release_label="Ghost.in.the.Shell.1995.1080p.BluRay"))

    def test_metadata_conflict_caps_positive_evidence_at_compatible(self):
        conflicted = identity.IdentityProfile(
            "movie", "tt0113568", ("Ghost in the Shell",),
            frozenset({1995, 2015}), metadata_conflict=True)
        self.assertEqual(identity.STRONG, identity.classify(
            conflicted, "Ghost.in.the.Shell.1995.1080p.BluRay.mkv",
            trusted_imdb=True))
        self.assertEqual(identity.CONTRADICTION, identity.classify(
            conflicted, "Another.Movie.1995.1080p.BluRay.mkv",
            trusted_imdb=True))

    def test_state_values_are_exact_public_strings(self):
        self.assertEqual(
            {"strong", "compatible", "unknown", "contradiction"},
            identity.STATES,
        )


class RuntimeCorroborationTests(unittest.TestCase):
    def setUp(self):
        # Deliberately generic same-name works. The release text has no year, so
        # title evidence alone cannot decide which one it is.
        self.long_movie = identity.IdentityProfile(
            "movie", "tt1000001", ("The Crossing",), frozenset({1999}),
            runtime_seconds=7_200,
        )
        self.episode = identity.IdentityProfile(
            "series", "tt1000002", ("The Crossing",), frozenset({2020}),
            season=1, episode=2, runtime_seconds=2_700,
        )

    def test_matching_movie_runtime_elevates_exact_title_to_strong(self):
        text = "The.Crossing.1080p.BluRay.mkv"
        base = identity.classify(self.long_movie, text)
        result = identity.assess(
            self.long_movie, text,
            measured_runtime_seconds=7_500,
        )
        self.assertEqual(identity.COMPATIBLE, base)
        self.assertEqual(identity.STRONG, result.state)
        self.assertEqual(identity.EVIDENCE_RUNTIME, result.evidence)
        self.assertEqual(3, result.rank)

    def test_materially_different_same_name_runtime_stays_compatible(self):
        text = "The.Crossing.1080p.BluRay.mkv"
        result = identity.assess(
            self.long_movie, text,
            measured_runtime_seconds=4_200,
        )
        self.assertEqual(identity.COMPATIBLE, result.state)
        self.assertEqual(identity.EVIDENCE_COMPATIBLE, result.evidence)

    def test_runtime_never_promotes_unknown_or_contradiction(self):
        self.assertEqual(identity.UNKNOWN, identity.corroborate_runtime(
            self.long_movie, "video.mkv", 7_200, trusted_imdb=True))
        self.assertEqual(identity.CONTRADICTION, identity.corroborate_runtime(
            self.long_movie, "Another.Title.1999.1080p.mkv", 7_200,
            trusted_imdb=True))

    def test_tv_runtime_also_requires_exact_episode_evidence(self):
        exact = "The.Crossing.S01E02.1080p.WEB-DL.mkv"
        no_episode = "The.Crossing.1080p.WEB-DL.mkv"
        wrong_episode = "The.Crossing.S01E03.1080p.WEB-DL.mkv"

        exact_result = identity.assess(
            self.episode, exact, measured_runtime_seconds=2_700)
        self.assertEqual(identity.STRONG, exact_result.state)
        self.assertEqual(identity.EVIDENCE_RUNTIME, exact_result.evidence)
        self.assertEqual(identity.COMPATIBLE, identity.corroborate_runtime(
            self.episode, no_episode, 2_700))
        self.assertEqual(identity.STRONG, identity.corroborate_runtime(
            self.episode, no_episode, 2_700, trusted_episode=True))
        self.assertEqual(identity.CONTRADICTION, identity.corroborate_runtime(
            self.episode, wrong_episode, 2_700, trusted_episode=True))

    def test_series_level_runtime_without_exact_episode_cannot_promote(self):
        series = identity.IdentityProfile(
            "series", "tt1000002", ("The Crossing",),
            runtime_seconds=2_700)
        self.assertEqual(identity.COMPATIBLE, identity.corroborate_runtime(
            series, "The.Crossing.1080p.WEB-DL.mkv", 2_700))

    def test_runtime_tolerances_are_edit_friendly_but_bounded(self):
        text = "The.Crossing.1080p.BluRay.mkv"
        # Movie tolerance is max(8m, 15%): 1080 seconds for a two-hour film.
        self.assertEqual(identity.STRONG, identity.corroborate_runtime(
            self.long_movie, text, 8_280))
        self.assertEqual(identity.COMPATIBLE, identity.corroborate_runtime(
            self.long_movie, text, 8_281))

        ep = "The.Crossing.S01E02.1080p.WEB-DL.mkv"
        # Episode tolerance is max(5m, 20%): 540 seconds here.
        self.assertEqual(identity.STRONG, identity.corroborate_runtime(
            self.episode, ep, 3_240))
        self.assertEqual(identity.COMPATIBLE, identity.corroborate_runtime(
            self.episode, ep, 3_241))

    def test_evidence_rank_order_is_stable(self):
        expected = [
            identity.EVIDENCE_RANKS[identity.EVIDENCE_TRUSTED_IMDB],
            identity.EVIDENCE_RANKS[identity.EVIDENCE_CANONICAL],
            identity.EVIDENCE_RANKS[identity.EVIDENCE_RUNTIME],
            identity.EVIDENCE_RANKS[identity.EVIDENCE_COMPATIBLE],
            identity.EVIDENCE_RANKS[identity.EVIDENCE_UNKNOWN],
            identity.EVIDENCE_RANKS[identity.EVIDENCE_CONTRADICTION],
        ]
        self.assertEqual(sorted(expected, reverse=True), expected)
        self.assertEqual(5, identity.evidence_rank(
            self.long_movie, "The.Crossing.1999.1080p.mkv",
            trusted_imdb=True))

    def test_profile_rejects_invalid_runtime(self):
        for runtime in (0, -1, float("inf"), float("nan")):
            with self.subTest(runtime=runtime), self.assertRaises(ValueError):
                identity.IdentityProfile(
                    "movie", "tt1000003", ("Example",),
                    runtime_seconds=runtime)


if __name__ == "__main__":
    unittest.main()
