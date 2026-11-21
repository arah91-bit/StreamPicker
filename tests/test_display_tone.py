"""HDR demotion for displays that cannot render it.

A low-luminance projector can accept HDR without reproducing its peak. ExoPlayer
does not avoid HDR tracks on displays that can't show them (androidx/media#1074)
and does not tone map, so an HDR release arrives dark and flat.

The contract under test is *demotion, never removal* — the same one _decode_ok
and the hardsub gate hold — so an HDR-only title still plays.
"""

import unittest
from unittest.mock import patch

from app import picker, vprobe


class TransferNormalisationTests(unittest.TestCase):
    def test_pq_spellings_normalise(self):
        for raw in ("smpte2084", "SMPTE2084", " smpte st 2084 "):
            self.assertEqual("pq", vprobe._norm_transfer(raw), raw)

    def test_hlg_spellings_normalise(self):
        for raw in ("arib-std-b67", "ARIB-STD-B67", "hlg"):
            self.assertEqual("hlg", vprobe._norm_transfer(raw), raw)

    def test_sdr_and_unknown_both_read_as_sdr(self):
        """An untagged file plays correctly everywhere, so 'unknown' must not
        be guessed as HDR — that would demote good releases on no evidence."""
        for raw in ("bt709", "smpte170m", "iec61966-2-1", "unknown", "", None):
            self.assertEqual("", vprobe._norm_transfer(raw), raw)


class IsHdrTests(unittest.TestCase):
    def test_measured_transfer_beats_a_silent_release_name(self):
        """The whole point of sniffing: a name with no HDR tag on a PQ file."""
        self.assertTrue(picker._is_hdr(
            {"title": "Movie 2025 2160p WEB-DL x265", "_transfer": "pq"}))

    def test_measured_sdr_beats_an_hdr_claim_in_the_name(self):
        self.assertFalse(picker._is_hdr(
            {"title": "Movie 2025 2160p HDR DV x265", "_transfer": ""}))

    def test_falls_back_to_the_name_when_nothing_was_sniffed(self):
        self.assertTrue(picker._is_hdr({"title": "Movie 2025 2160p HDR10 x265"}))
        self.assertFalse(picker._is_hdr({"title": "Movie 2025 1080p BluRay x264"}))

    def test_hlg_counts_as_hdr(self):
        self.assertTrue(picker._is_hdr({"title": "Doc 2024", "_transfer": "hlg"}))


class DemotionTests(unittest.TestCase):
    def test_sdr_outranks_hdr_above_the_resolution_key(self):
        """A 1080p SDR copy must beat a 4K HDR one on this display — which only
        works because the display key sits above resolution in the tuple."""
        hdr_4k = {"title": "Movie 2025 2160p BluRay REMUX HDR x265",
                  "url": "https://e/a", "_transfer": "pq"}
        sdr_1080 = {"title": "Movie 2025 1080p BluRay x264",
                    "url": "https://e/b", "_transfer": ""}
        with patch.object(picker, "HDR_DEMOTE", "sdr-first"):
            best = max([hdr_4k, sdr_1080], key=picker._quality_key)
        self.assertIs(sdr_1080, best)

    def test_hdr_still_wins_when_the_guard_is_off(self):
        hdr_4k = {"title": "Movie 2025 2160p BluRay REMUX HDR x265",
                  "url": "https://e/a", "_transfer": "pq"}
        sdr_1080 = {"title": "Movie 2025 1080p BluRay x264",
                    "url": "https://e/b", "_transfer": ""}
        with patch.object(picker, "HDR_DEMOTE", "off"):
            best = max([hdr_4k, sdr_1080], key=picker._quality_key)
        self.assertIs(hdr_4k, best)

    def test_an_hdr_only_title_is_still_playable(self):
        """Demotion, not a filter: with no SDR alternative the HDR release is
        still the answer rather than no answer."""
        only = [{"title": "Movie 2025 2160p HDR x265", "url": "https://e/a",
                 "_transfer": "pq"}]
        with patch.object(picker, "HDR_DEMOTE", "sdr-first"):
            self.assertEqual(1, sum(1 for s in only if picker._display_ok(s) in (0, 1)))
            self.assertEqual(0, picker._display_ok(only[0]))

    def test_two_hdr_releases_still_rank_against_each_other(self):
        """Everything below the display key must keep working when the whole
        field is demoted — otherwise an HDR-only title loses its ordering."""
        big = {"title": "Movie 2025 2160p BluRay REMUX HDR x265",
               "url": "https://e/a", "_transfer": "pq", "_qbps": 40_000_000}
        small = {"title": "Movie 2025 1080p WEBRip HDR x265",
                 "url": "https://e/b", "_transfer": "pq", "_qbps": 2_000_000}
        with patch.object(picker, "HDR_DEMOTE", "sdr-first"):
            self.assertIs(big, max([small, big], key=picker._quality_key))


class KeyShapeTests(unittest.TestCase):
    """_quality_key is unpacked by name in three other functions; a new element
    that isn't threaded through all of them silently reorders results."""

    def test_every_consumer_unpacks_the_same_width(self):
        s = {"title": "Movie 2025 1080p BluRay x264", "url": "https://e/a"}
        key = picker._quality_key(s)
        self.assertEqual(11, len(key))
        # Each of these unpacks _quality_key positionally; a mismatch raises.
        picker._probe_key(s)
        picker._delivery_key(key, 0.5, 5_000_000)
        picker._verified_quality_key((s, None))
        picker._marked_key(s)


if __name__ == "__main__":
    unittest.main()
