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


if __name__ == "__main__":
    unittest.main()
