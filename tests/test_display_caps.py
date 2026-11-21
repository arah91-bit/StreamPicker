"""A player that describes its display must be believed; one that says nothing
must be served exactly as before.

The second half is the load-bearing one. stream-picker is public and almost
every player that talks to it is stock — Stremio, Jellyfin, unmodified Nuvio.
If the absent header changed a single pick, this feature would be a regression
for everyone who does not run our fork.
"""

import unittest

from app import display_caps, picker


class ParseTests(unittest.TestCase):
    def test_a_full_header(self):
        caps = display_caps.parse(
            "hdr=hdr10,hlg; dv=0; maxlum=215; audio=ac3,eac3; override=sdr")
        self.assertEqual(frozenset({"hdr10", "hlg"}), caps.hdr)
        self.assertIs(False, caps.dv)
        self.assertEqual(215, caps.max_nits)
        self.assertEqual(frozenset({"ac3", "eac3"}), caps.audio)
        self.assertEqual("sdr", caps.override)

    def test_nothing_is_nothing(self):
        for raw in (None, "", "   ", "garbage", "hdr=", "=1", "x" * 600):
            self.assertIsNone(display_caps.parse(raw), raw)

    def test_junk_fields_are_dropped_not_fatal(self):
        """A bad field costs a slightly worse pick. Raising costs the film."""
        caps = display_caps.parse("hdr=notathing; dv=maybe; maxlum=banana; dv=1")
        self.assertIs(True, caps.dv)
        self.assertEqual(frozenset(), caps.hdr)
        self.assertIsNone(caps.max_nits)

    def test_zero_nits_means_unknown_not_dark(self):
        """Displays report 0 when they have no EDID luminance to give."""
        self.assertIsNone(display_caps.parse("hdr=hdr10; maxlum=0").max_nits)

    def test_absurd_luminance_is_a_bug_at_the_other_end(self):
        self.assertIsNone(display_caps.parse("hdr=hdr10; maxlum=99999").max_nits)
        # And a header whose only field is nonsense carries no information.
        self.assertIsNone(display_caps.parse("maxlum=99999"))

    def test_hdr10_plus_spelling(self):
        self.assertIn("hdr10plus", display_caps.parse("hdr=hdr10+").hdr)


class DemotionTests(unittest.TestCase):
    def test_a_bright_hdr_display_keeps_hdr(self):
        self.assertIs(False, display_caps.parse(
            "hdr=hdr10; maxlum=1000").demotes_hdr())

    def test_a_dim_hdr_display_is_demoted(self):
        """The projector: accepts HDR10, manages about a fifth of the grade."""
        self.assertIs(True, display_caps.parse(
            "hdr=hdr10; maxlum=215").demotes_hdr())

    def test_an_sdr_panel_is_demoted(self):
        self.assertIs(True, display_caps.parse("dv=0; audio=ac3").demotes_hdr())

    def test_hdr_with_no_luminance_reported_is_taken_at_its_word(self):
        self.assertIs(False, display_caps.parse("hdr=hdr10,hlg").demotes_hdr())

    def test_the_override_beats_the_measurement_both_ways(self):
        self.assertIs(True, display_caps.parse(
            "hdr=hdr10; maxlum=4000; override=sdr").demotes_hdr())
        self.assertIs(False, display_caps.parse(
            "hdr=hdr10; maxlum=100; override=hdr").demotes_hdr())

    def test_silence_is_not_an_answer(self):
        self.assertIsNone(display_caps.UNKNOWN.demotes_hdr())


class CacheTokenTests(unittest.TestCase):
    def test_displays_that_rank_alike_share_a_token(self):
        a = display_caps.parse("hdr=hdr10; maxlum=1200; dv=1")
        b = display_caps.parse("hdr=hdr10,hlg; maxlum=4000; dv=1")
        self.assertEqual(a.cache_token(), b.cache_token())

    def test_the_projector_and_the_oled_do_not(self):
        projector = display_caps.parse("hdr=hdr10; maxlum=215; dv=0")
        oled = display_caps.parse("hdr=hdr10; maxlum=1000; dv=1")
        self.assertNotEqual(projector.cache_token(), oled.cache_token())

    def test_unknown_takes_the_untagged_key(self):
        self.assertEqual("", display_caps.UNKNOWN.cache_token())

    def test_the_tag_is_a_prefix_so_invalidation_still_matches(self):
        """`invalidate()` finds keys by `endswith(f":{media_id}")`, and the slow
        picker finds the fast picker's work by stripping a leading "slow:".
        A suffix tag would silently break both."""
        caps = display_caps.parse("hdr=hdr10; maxlum=215; dv=0")
        token = display_caps.current_caps.set(caps)
        try:
            tag = picker._caps_tag()
        finally:
            display_caps.current_caps.reset(token)
        fast = f"{tag}full:movie:tt1375666"
        slow = f"slow:{tag}full:movie:tt1375666"
        self.assertTrue(tag, "a described display must tag its cache key")
        # invalidate() drops keys by this suffix.
        self.assertTrue(fast.endswith(":tt1375666"), fast)
        self.assertTrue(slow.endswith(":tt1375666"), slow)
        # The slow picker finds the fast picker's verified links this way.
        self.assertEqual(fast, slow[len("slow:"):])


class _CapsCase(unittest.TestCase):
    """Runs a picker decision with a given header in effect."""

    def _with(self, header, fn, *args):
        caps = display_caps.parse(header)
        token = display_caps.current_caps.set(caps)
        try:
            return fn(*args)
        finally:
            display_caps.current_caps.reset(token)


class DolbyVisionTests(_CapsCase):
    BARE_DV = "Movie.2024.2160p.DV.HEVC-GRP"          # Profile 5, no HDR10 base
    DV_HDR10 = "Movie.2024.2160p.DV.HDR10.HEVC-GRP"   # Profile 8.x

    def test_a_dv_display_is_shown_dolby_vision(self):
        """The user's ask: stop never showing DV — read whether it can be
        played, and show it when it can."""
        self.assertFalse(self._with("dv=1", picker._dv_rejected, self.BARE_DV))
        self.assertFalse(self._with("dv=1", picker._dv_rejected, self.DV_HDR10))

    def test_a_non_dv_display_still_loses_bare_profile_5(self):
        self.assertTrue(self._with("dv=0", picker._dv_rejected, self.BARE_DV))

    def test_a_non_dv_display_keeps_dv_that_falls_back_to_hdr10(self):
        self.assertFalse(self._with("dv=0", picker._dv_rejected, self.DV_HDR10))

    def test_the_guard_applies_even_where_the_operator_turned_it_off(self):
        real, picker.DV_REJECT = picker.DV_REJECT, "off"
        try:
            self.assertTrue(self._with("dv=0", picker._dv_rejected, self.BARE_DV))
        finally:
            picker.DV_REJECT = real

    def test_operator_escalation_to_all_survives_a_non_dv_display(self):
        """"all" is set for players that tint even on Profile 8 — a display
        saying "no DV" is not new information that should relax it."""
        real, picker.DV_REJECT = picker.DV_REJECT, "all"
        try:
            self.assertTrue(self._with("dv=0", picker._dv_rejected, self.DV_HDR10))
        finally:
            picker.DV_REJECT = real

    def test_a_dv_display_overrides_even_all(self):
        real, picker.DV_REJECT = picker.DV_REJECT, "all"
        try:
            self.assertFalse(self._with("dv=1", picker._dv_rejected, self.DV_HDR10))
        finally:
            picker.DV_REJECT = real

    def test_no_header_leaves_the_configured_policy_alone(self):
        real, picker.DV_REJECT = picker.DV_REJECT, "bare"
        try:
            self.assertTrue(picker._dv_rejected(self.BARE_DV))
            self.assertFalse(picker._dv_rejected(self.DV_HDR10))
        finally:
            picker.DV_REJECT = real


class DisplayOkTests(_CapsCase):
    HDR = {"name": "Movie.2024.2160p.HDR10.HEVC-GRP"}
    SDR = {"name": "Movie.2024.1080p.BluRay.x264-GRP"}

    def test_a_dim_display_demotes_hdr_without_any_env_change(self):
        real, picker.HDR_DEMOTE = picker.HDR_DEMOTE, "off"
        try:
            self.assertEqual(0, self._with("hdr=hdr10; maxlum=215",
                                           picker._display_ok, self.HDR))
            self.assertEqual(1, self._with("hdr=hdr10; maxlum=215",
                                           picker._display_ok, self.SDR))
        finally:
            picker.HDR_DEMOTE = real

    def test_a_bright_display_keeps_hdr_even_when_the_env_says_demote(self):
        """One server, two rooms. The env is a guess; the display is a fact."""
        real, picker.HDR_DEMOTE = picker.HDR_DEMOTE, "sdr-first"
        try:
            self.assertEqual(1, self._with("hdr=hdr10; maxlum=1000",
                                           picker._display_ok, self.HDR))
        finally:
            picker.HDR_DEMOTE = real

    def test_no_header_falls_back_to_the_env(self):
        for setting, expected in (("off", 1), ("sdr-first", 0)):
            real, picker.HDR_DEMOTE = picker.HDR_DEMOTE, setting
            try:
                self.assertEqual(expected, picker._display_ok(self.HDR), setting)
            finally:
                picker.HDR_DEMOTE = real


class AudioPassthroughTests(_CapsCase):
    TRUEHD = "Movie.2024.2160p.TrueHD.Atmos.7.1-GRP"

    def test_a_chain_that_cannot_pass_truehd_stops_paying_for_it(self):
        full = picker._audio_worth(5000, self.TRUEHD)
        capped = self._with("audio=ac3,eac3", picker._audio_worth,
                            5000, self.TRUEHD)
        self.assertEqual(5000, full)
        self.assertLess(capped, full)

    def test_a_chain_that_can_pass_it_pays_in_full(self):
        self.assertEqual(5000, self._with("audio=truehd,atmos,eac3",
                                          picker._audio_worth, 5000, self.TRUEHD))

    def test_a_discounted_lossless_track_still_beats_ac3(self):
        """It is worth less, not worthless — a downmixed lossless source is
        still better than a lossy one."""
        capped = self._with("audio=ac3", picker._audio_worth, 5000, self.TRUEHD)
        self.assertGreater(capped, 1000)

    def test_no_header_leaves_every_score_untouched(self):
        for score, text in ((5000, self.TRUEHD), (1000, "Movie.AC3-GRP"),
                            (200, "Movie.AAC-GRP")):
            self.assertEqual(score, picker._audio_worth(score, text))

    def test_ordinary_codecs_are_never_discounted(self):
        self.assertEqual(1000, self._with("audio=truehd", picker._audio_worth,
                                          1000, "Movie.AC3-GRP"))


class StockPlayerTests(unittest.TestCase):
    """The invariant: no header, no change. Guards the public repo."""

    def test_every_decision_is_unchanged_when_nothing_is_reported(self):
        self.assertIsNone(display_caps.current())
        self.assertEqual("", picker._caps_tag())
        self.assertEqual("unknown display", display_caps.describe(None))


if __name__ == "__main__":
    unittest.main()
