"""A resolution label is a claim, not evidence.

Free HTTP addons tag anything "4K"/"1080p"; quality must be verified for
compression before a link is served. These tests pin the evidence rules:
size-less claims rank no higher than UNPROVEN_MAX_RES until measured, and the
probe's HLS-playlist findings (declared bandwidth/resolution/codecs) re-rank a
stream as what it really is.
"""

import unittest

from app import picker, probe

RUNTIME = 7_200.0


def _claim(name, size=None):
    s = {"name": name, "url": "https://host.example/f", "behaviorHints": {}}
    if size:
        s["behaviorHints"]["videoSize"] = size
    return s


class UnprovenClaimTests(unittest.TestCase):
    def test_sizeless_4k_claim_ranks_as_1080_at_best(self):
        s = _claim("Addon 4K")
        self.assertEqual(1080, picker._effective_resolution(s, RUNTIME))

    def test_sized_real_4k_keeps_its_tier(self):
        s = _claim("Addon 4K", size=20_000_000_000)   # ~22 Mbps over 2h
        self.assertEqual(2160, picker._effective_resolution(s, RUNTIME))

    def test_sizeless_1080_claim_is_unaffected(self):
        s = _claim("Addon 1080p")
        self.assertEqual(1080, picker._effective_resolution(s, RUNTIME))

    def test_unproven_4k_does_not_satisfy_the_fast_4k_bar(self):
        s = _claim("Addon 4K")
        picker._annotate_quality([s], RUNTIME)
        self.assertEqual((0, 1), picker._count_tiers([(s, None)]))


class SizeEvidenceTests(unittest.TestCase):
    def test_malformed_debridio_size_is_unknown_not_an_exception(self):
        s = _claim("[TB] Debridio HDRip · Size: .2.91 GB")

        self.assertIsNone(picker._size_bytes(s))
        picker._annotate_quality([s], RUNTIME)
        self.assertEqual(0, s["_qbps"])

    def test_valid_decimal_and_leading_decimal_sizes_still_parse(self):
        self.assertEqual(
            2_910_000_000,
            picker._size_bytes(_claim("Debridio · Size: 2.91 GB")),
        )
        self.assertEqual(
            750_000_000,
            picker._size_bytes(_claim("Debridio · Size: .75 GB")),
        )

    def test_malformed_structured_size_falls_back_to_valid_label(self):
        s = _claim("Debridio · Size: 850 MB")
        s["behaviorHints"]["videoSize"] = ".2.91"

        self.assertEqual(850_000_000, picker._size_bytes(s))
        self.assertTrue(picker._release_ident(s))


class ProbeEvidenceTests(unittest.TestCase):
    def _apply(self, s, **media):
        r = probe.ProbeResult(True, ttfb=0.2, speed_bps=20_000_000, **media)
        picker._apply_probe_quality(s, r, RUNTIME)
        return s

    def test_starved_playlist_bandwidth_demotes_a_4k_label(self):
        s = self._apply(_claim("Addon 4K"), media_bps=2_500_000.0)
        self.assertEqual(720, s["_effres"])           # what 2.5 Mbps really is

    def test_healthy_bandwidth_restores_a_sizeless_4k(self):
        s = self._apply(_claim("Addon 4K"), media_bps=16_000_000.0)
        self.assertEqual(2160, s["_effres"])

    def test_declared_720_resolution_caps_a_4k_label_outright(self):
        s = self._apply(_claim("Addon 4K"), media_bps=16_000_000.0,
                        media_height=720)
        self.assertEqual(720, s["_effres"])

    def test_hevc_codec_earns_its_bitrate_discount(self):
        # 6.5 Mbps AVC is nowhere near 4K, but 6.5 Mbps HEVC ≈ 11 Mbps AVC-equiv.
        avc = self._apply(_claim("Addon 4K"), media_bps=6_500_000.0)
        hevc = self._apply(_claim("Addon 4K"), media_bps=6_500_000.0,
                           media_codecs="hvc1.2.4.L153.B0,mp4a.40.2")
        self.assertLess(avc["_effres"], 2160)
        self.assertEqual(2160, hevc["_effres"])

    def test_direct_file_probe_changes_nothing(self):
        s = _claim("Addon 4K", size=20_000_000_000)
        picker._annotate_quality([s], RUNTIME)
        before = dict(s)
        picker._apply_probe_quality(
            s, probe.ProbeResult(True, ttfb=0.2, speed_bps=20_000_000), RUNTIME)
        self.assertEqual(before, s)

    def test_evidence_survives_to_the_output_boundary_strip(self):
        s = self._apply(_claim("Addon 4K"), media_bps=2_500_000.0,
                        media_height=720, media_codecs="avc1.640028")
        clean = picker._strip_internal(s)
        for key in ("_vbitrate", "_vheight", "_vcodec", "_effres", "_qbps"):
            self.assertNotIn(key, clean)


class DetailLadderTests(unittest.TestCase):
    """The sort ranks real picture detail (codec-adjusted video bitrate), not
    labels: a starved "4K" WEBRip loses to an honest 1080p BluRay, and a
    "REMUX" tag on a small file can't buy its way up."""

    def _ranked(self, *claims):
        streams = [_claim(name, size=size) for name, size in claims]
        picker._annotate_quality(streams, RUNTIME)
        return [s["name"] for s in sorted(
            streams, key=picker._quality_key, reverse=True)]

    def test_detail_ladder_remux_web_bluray_webrip(self):
        ranked = self._ranked(
            ("Movie 2160p WEBRip", 8_000_000_000),         # ~8.9 Mbps, starved
            ("Movie 1080p BluRay", 12_000_000_000),        # ~13.3 Mbps
            ("Movie 2160p WEB-DL", 16_000_000_000),        # ~17.8 Mbps
            ("Movie 1080p BluRay REMUX", 25_000_000_000),  # ~27.8 Mbps
            ("Movie 2160p BluRay REMUX", 60_000_000_000),  # ~66.7 Mbps
        )
        self.assertEqual([
            "Movie 2160p BluRay REMUX",
            "Movie 1080p BluRay REMUX",
            "Movie 2160p WEB-DL",
            "Movie 1080p BluRay",
            "Movie 2160p WEBRip",
        ], ranked)

    def test_lying_remux_label_loses_to_honest_bluray(self):
        # ~3.3 Mbps is no remux whatever the filename claims.
        ranked = self._ranked(
            ("Movie 1080p BluRay REMUX", 3_000_000_000),
            ("Movie 1080p BluRay", 12_000_000_000),
        )
        self.assertEqual("Movie 1080p BluRay", ranked[0])

    def test_hevc_detail_counts_more_than_raw_bits(self):
        # 10 Mbps HEVC ≈ 17 Mbps AVC-equivalent — more real detail than the
        # 13.3 Mbps AVC BluRay, so it leads despite the lower tier name.
        ranked = self._ranked(
            ("Movie 1080p BluRay", 12_000_000_000),
            ("Movie 2160p WEB-DL HEVC", 9_000_000_000),
        )
        self.assertEqual("Movie 2160p WEB-DL HEVC", ranked[0])


if __name__ == "__main__":
    unittest.main()
