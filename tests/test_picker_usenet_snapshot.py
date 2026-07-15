import unittest
from unittest import mock

from app import picker, sources


class CompletedUsenetSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_finished_lane_replaces_early_one_item_snapshot(self):
        early = [{"url": "https://dav.invalid/one"}]
        complete = [
            {"url": "https://dav.invalid/one"},
            {"url": "https://dav.invalid/two"},
            {"url": "https://dav.invalid/three"},
            {"url": "https://dav.invalid/four"},
        ]
        with mock.patch.object(picker.nzb_lane, "in_progress",
                               return_value=False), \
                mock.patch.object(
                    picker.nzb_lane, "wait_for_more",
                    new=mock.AsyncMock(return_value=complete)) as refresh:
            result = await picker._latest_nzb_snapshot(
                "movie", "tt0437086", early, 20)

        refresh.assert_awaited_once_with("movie", "tt0437086", 1, 0)
        self.assertEqual(4, len(result))
        self.assertTrue(all(s["_source_key"] == sources.NZB for s in result))
        self.assertTrue(all(sources.trusted_nzb(s) for s in result))


if __name__ == "__main__":
    unittest.main()
