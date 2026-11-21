"""Continue Watching must not filter on the column that lies.

`position_pct` is the offset of the last range request. Every player reads the
container index at EOF before it can show a frame, so it pins near 100% on the
*first* open of any file regardless of what was watched — the same trap CLAUDE.md
documents for the byte cache ("never advance `e.head` from a tail probe"),
surfacing in telemetry instead. Delivered bytes and elapsed seconds are the
honest signals, because the proxy counted them.
"""

import unittest

from app.recs import history


def _play(**kw):
    row = {"content_id": "tt1", "seconds": 0, "megabytes": 0,
           "total_bytes": 0, "position_pct": None, "watched_pct": None}
    row.update(kw)
    return row


class ProgressSignalTests(unittest.TestCase):
    def test_progress_comes_from_delivered_bytes(self):
        row = _play(megabytes=500, total_bytes=1_000_000_000)
        self.assertAlmostEqual(50.0, history._progress_of(row), places=1)

    def test_position_pct_is_ignored_even_when_present(self):
        """The index read pins this at 100% on a barely-touched file."""
        row = _play(seconds=600, megabytes=200, total_bytes=5_000_000_000,
                    position_pct=100.0)
        self.assertLess(history._progress_of(row), 10.0)

    def test_falls_back_to_watched_pct_when_no_size_is_known(self):
        """Trakt-era imports carry no bytes at all."""
        self.assertEqual(42.0, history._progress_of(_play(watched_pct=42.0)))

    def test_unknown_stays_unknown(self):
        self.assertIsNone(history._progress_of(_play()))


class StartedTests(unittest.TestCase):
    def test_an_index_read_is_not_a_start(self):
        """2 MB against a 5 GB file with no elapsed time is a player reading
        the container index, not somebody watching."""
        self.assertFalse(history._started(
            _play(seconds=0, megabytes=2, total_bytes=5_000_000_000)))

    def test_a_minute_of_playback_counts(self):
        self.assertTrue(history._started(_play(seconds=90)))

    def test_enough_delivered_bytes_count_without_a_clock(self):
        self.assertTrue(history._started(_play(megabytes=64)))


class InProgressTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, rows):
        async def fake_history(_key, limit=0):
            return rows
        real, history.db.play_history = history.db.play_history, fake_history
        try:
            return await history.in_progress("viewer")
        finally:
            history.db.play_history = real

    async def test_a_part_watched_title_pinned_at_100_pct_is_recovered(self):
        """The regression this exists for: real playback, hidden by the old
        `position_pct < 85` gate."""
        out = await self._run([_play(content_id="tt9", seconds=1800,
                                     megabytes=900, total_bytes=5_000_000_000,
                                     position_pct=100.0)])
        self.assertEqual(["tt9"], [r["content_id"] for r in out])

    async def test_a_finished_title_stays_out(self):
        out = await self._run([_play(content_id="tt2", seconds=6000,
                                     megabytes=4800,
                                     total_bytes=5_000_000_000)])
        self.assertEqual([], out)

    async def test_an_untouched_open_stays_out(self):
        out = await self._run([_play(content_id="tt3", seconds=0, megabytes=2,
                                     total_bytes=5_000_000_000,
                                     position_pct=100.0)])
        self.assertEqual([], out)

    async def test_one_card_per_title_newest_first(self):
        rows = [_play(content_id="tt4", seconds=600, megabytes=300,
                      total_bytes=5_000_000_000),
                _play(content_id="tt4", seconds=120, megabytes=60,
                      total_bytes=5_000_000_000)]
        self.assertEqual(1, len(await self._run(rows)))


if __name__ == "__main__":
    unittest.main()
