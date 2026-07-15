import unittest
from unittest.mock import AsyncMock, patch

from app import picker, usenet


class EpisodeSafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_episode_token_is_exact(self):
        self.assertTrue(usenet._episode_match(
            "Example.Show.S01E02.1080p.WEB-DL", 1, 2))
        self.assertTrue(usenet._episode_match(
            "Example Show 1x02 1080p", 1, 2))
        self.assertFalse(usenet._episode_match(
            "Example.Show.S01E03.1080p.WEB-DL", 1, 2))
        self.assertFalse(usenet._episode_match(
            "Example.Show.S01.COMPLETE.1080p", 1, 2))

    def test_video_picker_cannot_choose_another_episode(self):
        entries = [
            ("/content/job/Example.Show.S01E01.mkv", 8_000_000_000),
            ("/content/job/Example.Show.S01E02.mkv", 4_000_000_000),
        ]
        self.assertEqual(entries[1], usenet._pick_video(entries, (1, 2)))
        self.assertIsNone(usenet._pick_video(entries, (1, 3)))

    def test_video_picker_ignores_episode_token_in_parent_job_directory(self):
        entries = [
            ("/content/tv/Example.Show.S01E02-job/Example.Show.S01E03.mkv",
             8_000_000_000),
            ("/content/tv/Example.Show.S01E02-job/Example.Show.S01E02.mkv",
             4_000_000_000),
        ]
        self.assertEqual(entries[1], usenet._pick_video(entries, (1, 2)))

    async def test_search_drops_wrong_episode_pack_and_wrong_title(self):
        rows = [
            {"title": "Example.Show.S01E02.1080p.WEB-DL-GOOD",
             "size": 4_000_000_000, "link": "https://indexer/good",
             "indexer": "one"},
            {"title": "Example.Show.S01E03.2160p.WEB-DL-WRONG",
             "size": 8_000_000_000, "link": "https://indexer/wrong-ep",
             "indexer": "one"},
            {"title": "Example.Show.S01.COMPLETE.2160p-WRONG",
             "size": 80_000_000_000, "link": "https://indexer/pack",
             "indexer": "one"},
            {"title": "Another.Show.S01E02.2160p-WRONG",
             "size": 8_000_000_000, "link": "https://indexer/wrong-show",
             "indexer": "one"},
        ]
        with (patch.object(usenet, "INDEXERS", [("one", "https://x", "k")]),
              patch.object(usenet, "_search_one", AsyncMock(return_value=rows)),
              patch("app.meta.title_year", AsyncMock(
                  return_value=("Example Show", "Example Show", 2024))),
              patch("app.usenet_health.should_skip", return_value=False),
              patch("app.usenet_health.status", return_value={}),
              patch("app.usenet_health.indexer_score", return_value=0.5)):
            found = await usenet.search("series", "tt1234567:1:2")
        self.assertEqual(["Example.Show.S01E02.1080p.WEB-DL-GOOD"],
                         [r["title"] for r in found])

    async def test_search_rejects_contradictory_newznab_identity_attrs(self):
        rows = [
            {"title": "Example.Show.S01E02.1080p.WEB-DL-WRONG-ID",
             "size": 4_000_000_000, "link": "https://indexer/wrong",
             "indexer": "one",
             "_newznab_identity_attrs": {
                 "imdb": ["tt1234567", "tt7654321"],
                 "season": ["1"], "episode": ["2"]}},
            {"title": "Example.Show.S01E02.1080p.WEB-DL-EXACT",
             "size": 4_100_000_000, "link": "https://indexer/exact",
             "indexer": "one",
             "_newznab_identity_attrs": {
                 "imdb": ["1234567"], "season": ["01"], "episode": ["002"]}},
            {"title": "Example.Show.S01E02.720p.WEB-DL-WRONG-EP",
             "size": 2_000_000_000, "link": "https://indexer/wrong-ep",
             "indexer": "one",
             "_newznab_identity_attrs": {
                 "imdb": ["tt1234567"], "season": ["1"], "episode": ["3"]}},
        ]
        with (patch.object(usenet, "INDEXERS", [("one", "https://x", "k")]),
              patch.object(usenet, "_search_one", AsyncMock(return_value=rows)),
              patch("app.meta.title_year", AsyncMock(
                  return_value=("Example Show", "Example Show", 2024))),
              patch("app.usenet_health.should_skip", return_value=False),
              patch("app.usenet_health.status", return_value={}),
              patch("app.usenet_health.indexer_score", return_value=0.5)):
            found = await usenet.search("series", "tt1234567:1:2")

        self.assertEqual(["Example.Show.S01E02.1080p.WEB-DL-EXACT"],
                         [r["title"] for r in found])
        self.assertTrue(found[0]["_nzb_attrs_trusted"])
        self.assertEqual(
            ["newznab-imdb", "newznab-season", "newznab-episode"],
            found[0]["_nzb_attr_evidence"],
        )
        self.assertNotEqual(found[0]["release_key"],
                            found[0]["legacy_release_key"])

    async def test_search_suppresses_persistently_dead_fetch_endpoints(self):
        dead_only = {
            "title": "Example.Show.S01E02.1080p.WEB-DL-DEAD",
            "size": 4_000_000_000, "link": "https://dead/nzb/1",
            "indexer": "dead",
        }
        dead_copy = {
            "title": "Example.Show.S01E02.1080p.WEB-DL-SHARED",
            "size": 4_100_000_000, "link": "https://dead/nzb/2",
            "indexer": "dead",
        }
        good_copy = dict(dead_copy, indexer="good",
                         link="https://good/nzb/2")
        search = AsyncMock(side_effect=[[dead_only, dead_copy], [good_copy]])
        with (patch.object(usenet, "INDEXERS", [
                  ("dead", "https://dead", "k"),
                  ("good", "https://good", "k")]),
              patch.object(usenet, "_search_one", search),
              patch("app.meta.title_year", AsyncMock(
                  return_value=("Example Show", "Example Show", 2024))),
              patch("app.usenet_health.should_skip", return_value=False),
              patch("app.usenet_health.fetch_allowed",
                    side_effect=lambda name: name != "dead"),
              patch("app.usenet_health.status", return_value={}),
              patch("app.usenet_health.indexer_score", return_value=0.5)):
            found = await usenet.search("series", "tt1234567:1:2")

        self.assertEqual(
            ["Example.Show.S01E02.1080p.WEB-DL-SHARED"],
            [r["title"] for r in found])
        self.assertEqual(["good"],
                         [o["indexer"] for o in found[0]["offers"]])


class MountedIdentityTests(unittest.TestCase):
    @staticmethod
    def movie(*, trusted=False):
        return {
            "title": "Example.Movie.2024.1080p.WEB-DL",
            "size": 8_000_000_000,
            "_nzb_attrs_trusted": trusted,
            "_nzb_attr_evidence": ["newznab-imdb"] if trusted else [],
            "_nzb_expected": {
                "media": "movie", "media_id": "tt1234567",
                "titles": ["Example Movie"], "year": 2024,
            },
        }

    @staticmethod
    def episode(*, trusted=False):
        return {
            "title": "Example.Show.S01E02.1080p.WEB-DL",
            "size": 4_000_000_000,
            "_nzb_attrs_trusted": trusted,
            "_nzb_attr_evidence": (
                ["newznab-imdb", "newznab-season", "newznab-episode"]
                if trusted else []),
            "_nzb_expected": {
                "media": "series", "media_id": "tt1234567:1:2",
                "titles": ["Example Show"], "year": 2024,
            },
        }

    def test_movie_basename_requires_title_and_year_for_strong_identity(self):
        exact = [("/content/job/Example.Movie.2024.1080p.mkv", 5_000)]
        tagged = [("/content/job/[GROUP] Example.Movie.2024.1080p.mkv", 5_500)]
        yearless = [("/content/job/Example.Movie.1080p.mkv", 6_000)]
        wrong = [("/content/job/Example.Movie.1995.1080p.mkv", 7_000)]

        self.assertEqual("strong", usenet._pick_video_identity(
            exact, self.movie())[1])
        self.assertEqual("strong", usenet._pick_video_identity(
            tagged, self.movie())[1])
        self.assertEqual("compatible", usenet._pick_video_identity(
            yearless, self.movie())[1])
        selected = usenet._pick_video_identity(wrong, self.movie())
        self.assertIsNone(selected[0])
        self.assertEqual("contradiction", selected[1])
        self.assertEqual("wrong-year", selected[3])

    def test_tv_basename_requires_title_and_exact_episode_for_strong_identity(self):
        exact = [("/content/job/Example.Show.S01E02.1080p.mkv", 5_000)]
        episode_only = [("/content/job/S01E02.1080p.mkv", 6_000)]
        wrong = [("/content/job/Example.Show.S01E03.1080p.mkv", 7_000)]

        self.assertEqual("strong", usenet._pick_video_identity(
            exact, self.episode(), (1, 2))[1])
        self.assertEqual("compatible", usenet._pick_video_identity(
            episode_only, self.episode(), (1, 2))[1])
        selected = usenet._pick_video_identity(wrong, self.episode(), (1, 2))
        self.assertIsNone(selected[0])
        self.assertEqual("wrong-episode", selected[3])

    def test_obfuscated_basename_is_unknown_unless_exact_attrs_are_trusted(self):
        for basename in ("a8f4c21d9920b77e9d818ee37a",
                         "qwertyuiopasdfghjklzxcvbnm"):
            with self.subTest(basename=basename):
                entries = [(f"/content/job/{basename}.mkv", 5_000)]
                unknown = usenet._pick_video_identity(entries, self.movie())
                trusted = usenet._pick_video_identity(
                    entries, self.movie(trusted=True))

                self.assertEqual("unknown", unknown[1])
                self.assertEqual("strong", trusted[1])
                self.assertIn("newznab-imdb", trusted[2])

    def test_explicit_inner_title_contradiction_is_never_rescued_by_attrs(self):
        entries = [("/content/job/Another.Movie.2024.1080p.mkv", 5_000)]

        selected = usenet._pick_video_identity(
            entries, self.movie(trusted=True))

        self.assertIsNone(selected[0])
        self.assertEqual("contradiction", selected[1])
        self.assertEqual("wrong-title", selected[3])


class LanguageEligibilityTests(unittest.TestCase):
    def setUp(self):
        self.token = picker._accept_langs.set(frozenset({"en", "ja"}))

    def tearDown(self):
        picker._accept_langs.reset(self.token)

    @staticmethod
    def stream(filename):
        return {"url": "https://video.example/file",
                "name": "1080p",
                "behaviorHints": {"filename": filename}}

    def test_proven_wrong_audio_is_not_usable(self):
        stream = self.stream("Example.Movie.2024.German.1080p-WEB.mkv")
        self.assertFalse(picker._usable(stream, picker.PROFILES["full"], 7200))

    def test_original_or_english_audio_is_usable(self):
        original = self.stream("Example.Movie.2024.Japanese.1080p-WEB.mkv")
        english = self.stream("Example.Movie.2024.English.1080p-WEB.mkv")
        self.assertTrue(picker._usable(original, picker.PROFILES["full"], 7200))
        self.assertTrue(picker._usable(english, picker.PROFILES["full"], 7200))

    def test_unknown_audio_gets_benefit_of_doubt(self):
        stream = self.stream("Example.Movie.2024.1080p-WEB.mkv")
        self.assertTrue(picker._usable(stream, picker.PROFILES["full"], 7200))


if __name__ == "__main__":
    unittest.main()
