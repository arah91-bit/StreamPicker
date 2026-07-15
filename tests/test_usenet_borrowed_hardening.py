"""Hardening borrowed from the Usenet-Ultimate survey (2026-07-15).

Five behaviors: diacritics/ampersand folding in the title matcher, bare
archive-part junk rejection, encrypted-archive hard strikes, free-text
fallback when the imdbid query yields nothing usable, and season packs as a
last resort with a mount-time episode gate.
"""
import contextlib
import unittest
from unittest.mock import AsyncMock, patch

from app import usenet, usenet_health


class TitleFoldingTests(unittest.TestCase):
    def test_norm_folds_diacritics_and_ampersand(self):
        self.assertEqual("amelie", usenet._norm("Amélie"))
        self.assertEqual("fastandfurious", usenet._norm("Fast & Furious"))

    def test_accented_expected_title_matches_ascii_release(self):
        self.assertTrue(usenet._release_title_match(
            "Amelie.2001.1080p.BluRay.x264-GRP", "Amélie"))

    def test_ascii_expected_title_matches_accented_release(self):
        self.assertTrue(usenet._release_title_match(
            "Amélie.2001.FRENCH.1080p", "Amelie"))

    def test_ampersand_matches_spelled_out_and(self):
        self.assertTrue(usenet._release_title_match(
            "Fast.and.Furious.2009.1080p.BluRay", "Fast & Furious"))

    def test_wrong_title_still_dropped(self):
        self.assertFalse(usenet._release_title_match(
            "Another.Movie.2001.1080p", "Amélie"))

    def test_bare_season_token_is_valid_title_tail(self):
        # "Show.S01.COMPLETE" packs and "Show.S01.E02" spaced styles must
        # survive the title check; a longer different title still must not.
        self.assertTrue(usenet._release_title_match(
            "Example.Show.S01.COMPLETE.1080p.WEB-DL", "Example Show"))
        self.assertTrue(usenet._release_title_match(
            "Example.Show.S01.E02.1080p.WEB-DL", "Example Show"))
        self.assertFalse(usenet._release_title_match(
            "Example.Show.Returns.S01E02.1080p", "Example Show"))


class ArchivePartJunkTests(unittest.TestCase):
    def test_bare_archive_parts_are_not_mountable(self):
        for title in ("Movie.2020.1080p.BluRay.par2",
                      "Some.Movie.2019.nzb",
                      "Movie.2020.1080p.rar",
                      "Movie.2020.1080p.r01",
                      "Movie.2020.1080p.part.001"):
            self.assertFalse(usenet._mountable_release(title), title)

    def test_codec_numerals_and_plain_titles_survive(self):
        for title in ("Movie.2020.1080p.H.264",
                      "Movie.2020.2160p.HDR10",
                      "Movie.2020.720p.HDTV.x264",
                      "Example Show - 099"):
            self.assertTrue(usenet._mountable_release(title), title)


class EncryptedStrikeTests(unittest.TestCase):
    def test_encrypted_import_failures_are_hard(self):
        for message in ("Password-protected rar archives cannot be solid.",
                        "The password did not match.",
                        "Archive is encrypted"):
            self.assertEqual(("hard", "encrypted"),
                             usenet._history_failure_class(message), message)

    def test_provider_credential_errors_stay_transient(self):
        # "password" near auth/login wording is a provider problem, not proof
        # the release is unplayable — transient must win the classification.
        for message in ("NNTP authentication failed for provider",
                        "Wrong password for provider login"):
            self.assertEqual(("transient", "transport"),
                             usenet._history_failure_class(message), message)

    def test_health_store_accepts_encrypted_reason(self):
        self.assertEqual("encrypted",
                         usenet_health._safe_reason("encrypted", "hard"))
        self.assertEqual("encrypted", usenet_health._safe_reason(
            "Password-protected archive", "hard"))


class ImportFailureHarvestTests(unittest.TestCase):
    """Every FailMessage nzbdav actually emitted (harvested 2026-07-15 from
    1130 live HistoryItems) must classify correctly."""

    def test_real_messages_classify_correctly(self):
        cases = [
            ("Timeout reading from NNTP stream.", ("transient", "transport")),
            ("No importable videos found.", ("hard", "not-video")),
            ("Only rar files with compression method m0 are supported.",
             ("hard", "broken-archive")),
            ("Missing rar volumes detected.", ("hard", "missing-articles")),
            ("Rar archive has duplicate volume numbers.",
             ("hard", "broken-archive")),
            ("Rar signature not found", ("hard", "broken-archive")),
            ("Encrypted Rar archive has no password specified.",
             ("hard", "encrypted")),
            ("Unable to load shared library 'rapidyenc' or one of its "
             "dependencies.", ("backend", "nzbdav-backend")),
            ("Value cannot be null. (Parameter 'Folder')",
             ("backend", "nzbdav-backend")),
            ("SQLite Error 14: 'unable to open database file'.",
             ("backend", "nzbdav-backend")),
        ]
        for message, expected in cases:
            self.assertEqual(expected,
                             usenet._history_failure_class(message), message)

    def test_backend_faults_never_strike_release_or_indexer(self):
        release = {"release_key": "k1", "title": "Example.Show.S01E02",
                   "offers": [{"indexer": "one"}]}
        with (patch("app.usenet_health.record_failure") as strikes,
              patch("app.telemetry.record_usenet_failure") as telemetry_rec):
            usenet._record_import_failure(
                release, "backend", "nzbdav-backend", "ev1", "SQLite Error")
            strikes.assert_not_called()
            telemetry_rec.assert_called_once()
            usenet._record_import_failure(
                release, "hard", "broken-archive", "ev2", "Rar signature")
            strikes.assert_called_once()

    def test_broken_archive_is_a_hard_health_reason(self):
        self.assertEqual("broken-archive",
                         usenet_health._safe_reason("broken-archive", "hard"))
        self.assertTrue(usenet_health._HARD_REASON_RE.search("broken-archive"))


class SeasonPackMatchTests(unittest.TestCase):
    def test_season_containers_match(self):
        self.assertTrue(usenet._season_pack_match(
            "Example.Show.S01.COMPLETE.1080p.WEB-DL", 1, 2))
        self.assertTrue(usenet._season_pack_match(
            "Example.Show.Season.1.1080p", 1, 2))
        self.assertTrue(usenet._season_pack_match(
            "Example.Show.S01E01-E06.1080p", 1, 2))

    def test_wrong_or_partial_containers_do_not_match(self):
        self.assertFalse(usenet._season_pack_match(
            "Example.Show.S02.COMPLETE.1080p", 1, 2))
        self.assertFalse(usenet._season_pack_match(
            "Example.Show.S01E03-E06.1080p", 1, 2))
        self.assertFalse(usenet._season_pack_match(
            "Example.Show.S01E05.1080p", 1, 2))

    def test_pack_mount_only_serves_explicit_episode_files(self):
        release = {"_nzb_pack": True,
                   "_nzb_expected": {"media": "series",
                                     "titles": ["Example Show"],
                                     "year": None}}
        entries = [
            ("/content/tv/job/Example.Show.S01E01.1080p.mkv", 8_000_000_000),
            ("/content/tv/job/Example.Show.S01E02.1080p.mkv", 4_000_000_000),
        ]
        video, confidence, _, _ = usenet._pick_video_identity(
            entries, release, (1, 2))
        self.assertEqual(entries[1], video)
        self.assertEqual("strong", confidence)

    def test_pack_mount_refuses_files_without_episode_token(self):
        # Title-only file names rank "compatible" and the largest sibling
        # would win — a pack may only serve a file that positively names the
        # requested episode.
        release = {"_nzb_pack": True,
                   "_nzb_expected": {"media": "series",
                                     "titles": ["Example Show"],
                                     "year": None}}
        entries = [
            ("/content/tv/job/Example.Show.1080p.WEB-DL.mkv", 4_000_000_000),
            ("/content/tv/job/Example.Show.2160p.WEB-DL.mkv", 9_000_000_000),
        ]
        video, _, _, _ = usenet._pick_video_identity(entries, release, (1, 2))
        self.assertIsNone(video)

    def test_single_episode_mount_keeps_compatible_files(self):
        release = {"_nzb_expected": {"media": "series",
                                     "titles": ["Example Show"],
                                     "year": None}}
        entries = [("/content/tv/job/Example.Show.1080p.WEB-DL.mkv",
                    4_000_000_000)]
        video, confidence, _, _ = usenet._pick_video_identity(
            entries, release, (1, 2))
        self.assertEqual(entries[0], video)
        self.assertEqual("compatible", confidence)


class TextQueryTests(unittest.TestCase):
    def test_query_text_folds_for_indexer_fulltext(self):
        self.assertEqual("Amelie", usenet._query_text("Amélie"))
        self.assertEqual("Its a Test Story",
                         usenet._query_text("It's a Test: Story!"))

    def test_text_queries_carry_episode_or_year(self):
        self.assertEqual(["Example Show S01E02"], usenet._text_queries(
            "series", ["tt1", "1", "2"], ["Example Show"], 2024))
        self.assertEqual(["Amelie 2001"], usenet._text_queries(
            "movie", ["tt1"], ["Amélie"], 2001))

    def test_text_queries_dedupe_folded_duplicates(self):
        self.assertEqual(["Amelie 2001"], usenet._text_queries(
            "movie", ["tt1"], ["Amélie", "Amelie"], 2001))


def _lane_patches(search_one):
    stack = contextlib.ExitStack()
    for p in (patch.object(usenet, "INDEXERS", [("one", "https://x", "k")]),
              patch.object(usenet, "_search_one", search_one),
              patch("app.meta.title_year", AsyncMock(
                  return_value=("Example Show", "Example Show", 2024))),
              patch("app.usenet_health.should_skip", return_value=False),
              patch("app.usenet_health.status", return_value={}),
              patch("app.usenet_health.fetch_allowed", return_value=True),
              patch("app.usenet_health.indexer_score", return_value=0.5)):
        stack.enter_context(p)
    return stack


class SearchFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_fallback_recovers_when_id_query_is_empty(self):
        text_rows = [{"title": "Example.Show.S01E02.1080p.WEB-DL-GOOD",
                      "size": 4_000_000_000, "link": "https://indexer/good",
                      "indexer": "one"}]

        async def search_one(name, base, key, params):
            return text_rows if "q" in params else []

        mock = AsyncMock(side_effect=search_one)
        with _lane_patches(mock):
            found = await usenet.search("series", "tt1234567:1:2")
        self.assertEqual(["Example.Show.S01E02.1080p.WEB-DL-GOOD"],
                         [r["title"] for r in found])
        text_calls = [c for c in mock.await_args_list if "q" in c.args[3]]
        self.assertTrue(text_calls)
        self.assertEqual("Example Show S01E02", text_calls[0].args[3]["q"])

    async def test_no_text_fallback_when_id_query_lands(self):
        id_rows = [{"title": "Example.Show.S01E02.1080p.WEB-DL-GOOD",
                    "size": 4_000_000_000, "link": "https://indexer/good",
                    "indexer": "one"}]

        async def search_one(name, base, key, params):
            return [] if "q" in params else id_rows

        mock = AsyncMock(side_effect=search_one)
        with _lane_patches(mock):
            found = await usenet.search("series", "tt1234567:1:2")
        self.assertEqual(1, len(found))
        self.assertFalse([c for c in mock.await_args_list
                          if "q" in c.args[3]])

    async def test_pack_admitted_only_when_no_single_episode(self):
        pack_only = [{"title": "Example.Show.S01.COMPLETE.1080p.WEB-DL",
                      "size": 40_000_000_000, "link": "https://indexer/pack",
                      "indexer": "one"}]
        mock = AsyncMock(return_value=pack_only)
        with _lane_patches(mock):
            found = await usenet.search("series", "tt1234567:1:2")
        self.assertEqual(["Example.Show.S01.COMPLETE.1080p.WEB-DL"],
                         [r["title"] for r in found])
        self.assertTrue(found[0].get("_nzb_pack"))

    async def test_pack_identity_is_season_scoped(self):
        # The same pack requested for two different episodes must share one
        # identity (one mount, one health record); a single-episode release
        # must not.
        pack_only = [{"title": "Example.Show.S01.COMPLETE.1080p.WEB-DL",
                      "size": 40_000_000_000, "link": "https://indexer/pack",
                      "indexer": "one"}]
        keys = {}
        for episode in ("2", "3"):
            mock = AsyncMock(return_value=pack_only)
            with _lane_patches(mock):
                found = await usenet.search("series", f"tt1234567:1:{episode}")
            self.assertEqual(1, len(found))
            keys[episode] = found[0]["release_key"]
        self.assertEqual(keys["2"], keys["3"])
        self.assertEqual(keys["2"], usenet_health.release_key(
            pack_only[0]["title"], pack_only[0]["size"],
            "series", "tt1234567:1"))

    def test_season_scope_is_distinct_and_stable(self):
        season = usenet_health._content_scope("series", "tt1234567:1")
        self.assertEqual("series:tt1234567:1", season)
        self.assertNotEqual(season,
                            usenet_health._content_scope("series",
                                                         "tt1234567:1:2"))

    async def test_pack_excluded_when_single_episode_exists(self):
        rows = [{"title": "Example.Show.S01E02.1080p.WEB-DL-GOOD",
                 "size": 4_000_000_000, "link": "https://indexer/good",
                 "indexer": "one"},
                {"title": "Example.Show.S01.COMPLETE.1080p.WEB-DL",
                 "size": 40_000_000_000, "link": "https://indexer/pack",
                 "indexer": "one"}]
        mock = AsyncMock(return_value=rows)
        with _lane_patches(mock):
            found = await usenet.search("series", "tt1234567:1:2")
        self.assertEqual(["Example.Show.S01E02.1080p.WEB-DL-GOOD"],
                         [r["title"] for r in found])


if __name__ == "__main__":
    unittest.main()
