import unittest
from unittest.mock import AsyncMock, patch

from app import library


def stream(item="movie-1"):
    return {
        "url": (f"https://jellyfin.example/videos/{item}/stream.mkv"
                "?api_key=secret-value&MediaSourceId=source-1"),
        "name": "Library",
    }


class JellyfinIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_movie_requires_exact_provider_imdb(self):
        with patch.object(library, "_item", AsyncMock(return_value={
            "Type": "Movie", "ProviderIds": {"Imdb": "tt0113568"},
        })):
            self.assertTrue(await library._identity_of(
                stream(), "movie", "tt0113568"))
            self.assertFalse(await library._identity_of(
                stream(), "movie", "tt1219827"))

    async def test_episode_requires_coordinates_and_parent_series_imdb(self):
        async def item(_origin, item_id, _key):
            if item_id == "episode-1":
                return {"Type": "Episode", "ParentIndexNumber": 1,
                        "IndexNumber": 2, "SeriesId": "series-1",
                        "ProviderIds": {"Imdb": "tt-episode"}}
            return {"Type": "Series", "ProviderIds": {"IMDB": "tt0386676"}}

        with patch.object(library, "_item", side_effect=item):
            self.assertTrue(await library._identity_of(
                stream("episode-1"), "series", "tt0386676:1:2"))
            self.assertFalse(await library._identity_of(
                stream("episode-1"), "series", "tt0386676:1:3"))
            self.assertFalse(await library._identity_of(
                stream("episode-1"), "series", "tt0290978:1:2"))

    async def test_unavailable_item_metadata_is_unknown_not_a_false_match(self):
        with patch.object(library, "_item", AsyncMock(return_value=None)):
            self.assertIsNone(await library._identity_of(
                stream(), "movie", "tt0113568"))

    async def test_prepare_drops_mismatch_and_marks_exact_match(self):
        s = stream()
        with (patch.object(library, "ENRICH", False),
              patch.object(library, "_identity_of", AsyncMock(return_value=False))):
            self.assertIsNone(await library._prepare(dict(s), "movie", "tt0113568"))

        with (patch.object(library, "ENRICH", False),
              patch.object(library, "_identity_of", AsyncMock(return_value=True))):
            found = await library._prepare(dict(s), "movie", "tt0113568")
        self.assertEqual("strong", found["_library_identity_confidence"])
        self.assertEqual("jellyfin-imdb", found["_library_identity_evidence"])

    def test_playback_url_parser_does_not_return_a_secretless_reference(self):
        self.assertIsNone(library._jellyfin_ref(
            "https://jellyfin.example/videos/id/stream.mkv"))


if __name__ == "__main__":
    unittest.main()
