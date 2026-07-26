"""Watch history built from what this service actually played.

The point of this pipeline is that it replaces Trakt, so it has to be honest
about who watched what and durable enough to be worth building a taste profile
on later.
"""

import queue
import unittest
from unittest.mock import AsyncMock, patch

from app.recs import playhistory


class ContentIdTests(unittest.TestCase):
    def test_a_movie_id(self):
        self.assertEqual(("tt0113568", "movie", None, None),
                         playhistory.parse_content_id("tt0113568"))

    def test_an_episode_id_keeps_the_show_and_the_position(self):
        self.assertEqual(("tt8050756", "series", 1, 8),
                         playhistory.parse_content_id("tt8050756:1:8"))

    def test_junk_is_rejected_rather_than_half_parsed(self):
        for bad in ("", None, "kitsu:123", "tt", "tt12:x:1", "  "):
            self.assertIsNone(playhistory.parse_content_id(bad), bad)


class SinkTests(unittest.TestCase):
    def setUp(self):
        playhistory._events = queue.Queue(maxsize=playhistory._QUEUE_MAX)

    def _record(self, **over):
        rec = {"id": "tt8050756:1:8", "ts": 1_700_000_000.4, "secs": 1320.0,
               "mb": 900.0, "watched": 96.2, "picker": "fast"}
        rec.update(over)
        return rec

    def test_a_play_becomes_one_queued_event(self):
        playhistory.sink(self._record(), {"viewer_key": "uabc"})
        event = playhistory._events.get_nowait()
        self.assertEqual("uabc", event["viewer_key"])
        self.assertEqual("tt8050756:1:8", event["content_id"])
        self.assertEqual("tt8050756", event["imdb_id"])
        self.assertEqual("series", event["media_type"])
        self.assertEqual((1, 8), (event["season"], event["episode"]))
        self.assertEqual(1_700_000_000, event["played_at"])
        self.assertEqual(96.2, event["watched_pct"])

    def test_an_unattributed_play_is_not_recorded(self):
        """The shared stream-only addon cannot know who asked, and a history
        row with no viewer would be worse than no row."""
        playhistory.sink(self._record(), {})
        self.assertTrue(playhistory._events.empty())

    def test_a_non_imdb_id_is_skipped(self):
        playhistory.sink(self._record(id="kitsu:1234"), {"viewer_key": "uabc"})
        self.assertTrue(playhistory._events.empty())

    def test_the_sink_never_raises_into_the_playback_path(self):
        for bad in ({"id": None}, {}, {"id": "tt1", "ts": "nonsense"}):
            playhistory.sink(bad, {"viewer_key": "uabc"})

    def test_a_full_queue_drops_rather_than_growing_without_limit(self):
        playhistory._events = queue.Queue(maxsize=2)
        for _ in range(5):
            playhistory.sink(self._record(), {"viewer_key": "uabc"})
        self.assertEqual(2, playhistory._events.qsize())


class DrainTests(unittest.IsolatedAsyncioTestCase):
    async def test_everything_queued_is_written_once(self):
        playhistory._events = queue.Queue(maxsize=playhistory._QUEUE_MAX)
        for i in range(3):
            playhistory.sink({"id": f"tt{i}", "ts": 1_700_000_000 + i},
                             {"viewer_key": "uabc"})
        writes = AsyncMock()
        with patch.object(playhistory.db, "record_play", writes):
            stored = await playhistory.drain_once()

        self.assertEqual(3, stored)
        self.assertEqual(3, writes.await_count)
        self.assertTrue(playhistory._events.empty())

    async def test_one_bad_row_does_not_abandon_the_rest(self):
        playhistory._events = queue.Queue(maxsize=playhistory._QUEUE_MAX)
        for i in range(3):
            playhistory.sink({"id": f"tt{i}", "ts": 1_700_000_000 + i},
                             {"viewer_key": "uabc"})
        writes = AsyncMock(side_effect=[RuntimeError("locked"), None, None])
        with patch.object(playhistory.db, "record_play", writes):
            stored = await playhistory.drain_once()

        self.assertEqual(2, stored)
        self.assertTrue(playhistory._events.empty())


if __name__ == "__main__":
    unittest.main()
