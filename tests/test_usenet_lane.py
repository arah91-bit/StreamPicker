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
