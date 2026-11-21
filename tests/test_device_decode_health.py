"""One APK, several televisions, and they do not decode the same things.

Playback rejection on one device must not demote a codec on other devices.
Use the probed profile when the filename omits the bit depth.
"""

import unittest

from app import decode_health, display_caps, vprobe


class ProfileDetectionTests(unittest.TestCase):
    """10-bit AVC has to be its own attribute, or it cannot be learned apart
    from the 8-bit AVC that every device plays."""

    def test_high_10_profile_is_named(self):
        self.assertEqual("h264-10bit", vprobe._video_key(
            {"codec_name": "h264", "profile": "High 10", "pix_fmt": "yuv420p10le"}))

    def test_ten_bit_pixel_format_alone_is_enough(self):
        self.assertEqual("h264-10bit", vprobe._video_key(
            {"codec_name": "h264", "pix_fmt": "yuv420p10le"}))

    def test_ordinary_avc_is_untouched(self):
        self.assertEqual("h264", vprobe._video_key(
            {"codec_name": "h264", "profile": "High", "pix_fmt": "yuv420p"}))

    def test_hevc_is_not_split(self):
        """Main 10 is universally supported; splitting its key would fragment
        what we learn for no gain."""
        self.assertEqual("hevc", vprobe._video_key(
            {"codec_name": "hevc", "profile": "Main 10", "pix_fmt": "yuv420p10le"}))


class DeviceIdentityTests(unittest.TestCase):
    def test_the_header_carries_a_device(self):
        caps = display_caps.parse("hdr=hdr10; dv=0; dev=nuvio-tv-abc123")
        self.assertEqual("nuvio-tv-abc123", caps.device)

    def test_a_device_alone_is_worth_reporting(self):
        self.assertTrue(display_caps.parse("dev=nuvio-tv-abc123").known())

    def test_junk_is_stripped_because_it_becomes_a_stored_key(self):
        caps = display_caps.parse("dev=../../etc/passwd; hdr=hdr10")
        self.assertNotIn("/", caps.device)

    def test_two_devices_do_not_share_a_cache_entry(self):
        a = display_caps.parse("hdr=hdr10; dv=1; dev=shield-lounge")
        b = display_caps.parse("hdr=hdr10; dv=1; dev=lg-bedroom")
        self.assertNotEqual(a.cache_token(), b.cache_token())


class PerDeviceLearningTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(decode_health._store)
        decode_health._store.clear()

    def tearDown(self):
        decode_health._store.clear()
        decode_health._store.update(self._saved)

    def _strike(self, device, n=None):
        for _ in range(n or decode_health.BAD_REJECTS):
            decode_health.record_reject([], "h264-10bit", label="Example S01E01",
                                        device=device)

    def test_the_shield_stops_being_offered_what_it_refused(self):
        self._strike("shield-lounge")
        self.assertTrue(decode_health.suspect(
            "x", [], "h264-10bit", device="shield-lounge"))

    def test_the_other_television_is_unaffected(self):
        """The requirement, stated plainly."""
        self._strike("shield-lounge")
        self.assertFalse(decode_health.suspect(
            "x", [], "h264-10bit", device="lg-bedroom"))

    def test_ordinary_avc_still_plays_on_the_striking_device(self):
        self._strike("shield-lounge")
        self.assertFalse(decode_health.suspect(
            "x", [], "h264", device="shield-lounge"))

    def test_one_failure_is_not_a_verdict(self):
        self._strike("shield-lounge", n=1)
        self.assertFalse(decode_health.suspect(
            "x", [], "h264-10bit", device="shield-lounge"))

    def test_a_successful_play_clears_the_strike(self):
        """How a device recovers when a decoder is fixed, or we were wrong."""
        self._strike("shield-lounge")
        decode_health.record_play([], "h264-10bit", device="shield-lounge")
        self.assertFalse(decode_health.suspect(
            "x", [], "h264-10bit", device="shield-lounge"))

    def test_a_shared_strike_still_applies_to_a_known_device(self):
        """Records predating device reporting are evidence about every player,
        and a codec nothing has ever managed is not a good bet on a television
        that has not tried it."""
        for _ in range(decode_health.BAD_REJECTS):
            decode_health.record_reject([], "av1", label="x")
        self.assertTrue(decode_health.suspect("x", [], "av1", device="shield-lounge"))

    def test_a_device_strike_does_not_leak_into_the_shared_record(self):
        self._strike("shield-lounge")
        self.assertFalse(decode_health.suspect("x", [], "h264-10bit"))


if __name__ == "__main__":
    unittest.main()
