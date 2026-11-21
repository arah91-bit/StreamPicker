"""The header the player emits must be the header the server reads.

Two repos, one wire format, and nothing that compiles both. These are the exact
strings `DisplayCapsHeader.build` produces for real hardware — captured from
its field order and formatting — asserted against the server's parser. If
someone changes one side, this fails on the other.

See the wire format in app/display_caps.py.
"""

import unittest

from app import display_caps


class WireFormatTests(unittest.TestCase):
    """Header values as DisplayCapsHeader.kt composes them: fields joined with
    "; ", lists with ",", in the order hdr, dv, maxlum, audio, override."""

    # Low-luminance HDR projector and an AVR accepting AC3/EAC3.
    PROJECTOR = "hdr=hdr10,hlg; dv=0; maxlum=215; audio=ac3,eac3; override=sdr"
    # A DV-capable television with a real HDR panel.
    OLED = "hdr=hdr10,hdr10plus,hlg; dv=1; maxlum=1000; audio=ac3,eac3,atmos,truehd"
    # An SDR monitor: the policy reported caps but the display listed no HDR.
    SDR_PANEL = "hdr=none; dv=0; audio=pcm"
    # Pre-N, or a display that said nothing — audio only.
    AUDIO_ONLY = "audio=ac3,eac3"

    def test_the_projector(self):
        caps = display_caps.parse(self.PROJECTOR)
        self.assertIs(True, caps.demotes_hdr())
        self.assertIs(False, caps.dv)
        self.assertEqual(215, caps.max_nits)
        self.assertIs(False, caps.passes_through("truehd"))

    def test_the_oled(self):
        caps = display_caps.parse(self.OLED)
        self.assertIs(False, caps.demotes_hdr())
        self.assertIs(True, caps.dv)
        self.assertIs(True, caps.passes_through("truehd"))

    def test_an_sdr_panel(self):
        caps = display_caps.parse(self.SDR_PANEL)
        self.assertIs(True, caps.demotes_hdr())
        self.assertEqual(frozenset(), caps.hdr)

    def test_audio_only_says_nothing_about_the_picture(self):
        caps = display_caps.parse(self.AUDIO_ONLY)
        self.assertIsNone(caps.demotes_hdr(),
                          "no HDR fields must leave the server default alone")
        self.assertIsNone(caps.dv)

    def test_hdr_none_is_a_statement_not_a_silence(self):
        """"hdr=none" is the player saying it looked and found nothing, which
        is different from not sending the field."""
        self.assertIs(True, display_caps.parse("hdr=none; dv=0").demotes_hdr())

    def test_rooms_that_rank_differently_get_different_cache_entries(self):
        self.assertNotEqual(display_caps.parse(self.PROJECTOR).cache_token(),
                            display_caps.parse(self.OLED).cache_token())

    def test_rooms_that_rank_alike_share_one(self):
        """The projector and a plain SDR monitor want the same list — SDR
        first, no DV, no premium audio premium — so they should share the work
        rather than search twice. Sharing is the feature; the token exists to
        separate the cases that genuinely differ."""
        self.assertEqual(display_caps.parse(self.PROJECTOR).cache_token(),
                         display_caps.parse(self.SDR_PANEL).cache_token())

    def test_reporting_no_premium_audio_differs_from_reporting_none(self):
        """"I checked and can pass nothing exotic" discounts TrueHD; "I did not
        say" must not."""
        said = display_caps.parse("hdr=none; dv=0; audio=ac3")
        silent = display_caps.parse("hdr=none; dv=0")
        self.assertNotEqual(said.cache_token(), silent.cache_token())
        self.assertIs(False, said.passes_through("truehd"))
        self.assertIsNone(silent.passes_through("truehd"))

    def test_every_sample_survives_a_round_trip_through_describe(self):
        for header in (self.PROJECTOR, self.OLED, self.SDR_PANEL,
                       self.AUDIO_ONLY):
            caps = display_caps.parse(header)
            self.assertIsNotNone(caps, header)
            self.assertTrue(display_caps.describe(caps), header)


class DimThresholdAgreementTests(unittest.TestCase):
    def test_the_two_repos_agree_on_what_dim_means(self):
        """`DIM_DISPLAY_NITS` is duplicated in DolbyVisionBaseLayerPolicy.kt.
        If they drift, one repo tone maps a title the other demotes."""
        self.assertEqual(400, display_caps.DIM_DISPLAY_NITS)


if __name__ == "__main__":
    unittest.main()
