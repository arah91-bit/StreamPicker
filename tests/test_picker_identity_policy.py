"""Picker regressions for semantic identity and automatic first-result policy.

These tests exercise the picker integration rather than the pure release-name
parser.  A successful byte probe is deliberately insufficient: only a stream
with strong content identity may be stamped verified or made auto-eligible.
"""

from __future__ import annotations

import unittest
from unittest import mock

from app import content_identity, library, picker, probe, sources


def _stream(filename: str, url: str, *, source: str = sources.FAST,
            resolution: str = "1080p", size: int = 8_000_000_000) -> dict:
    return {
        # Keep the addon brand generic.  All semantic evidence in these tests
        # comes from the declared filename (or the trusted provider marker).
        "name": "Stream",
        "url": url,
        "behaviorHints": {"filename": filename, "videoSize": size},
        "_source_key": source,
    }


class PickerIdentityPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = content_identity.IdentityProfile(
            media="movie",
            imdb_id="tt0113568",
            aliases=("Ghost in the Shell", "Koukaku Kidoutai"),
            years=frozenset({1995}),
            region_tags=frozenset({"Japan"}),
            runtime_seconds=83 * 60,
        )
        self._profile_token = picker._identity_profile_ctx.set(self.profile)
        self._logged_token = picker._identity_logged_ctx.set(set())
        self._langs_token = picker._accept_langs.set(None)
        self._original_token = picker._original_lang_known.set(False)

    def tearDown(self) -> None:
        picker._original_lang_known.reset(self._original_token)
        picker._accept_langs.reset(self._langs_token)
        picker._identity_logged_ctx.reset(self._logged_token)
        picker._identity_profile_ctx.reset(self._profile_token)

    def _assess(self, stream: dict, measured: float | None = None):
        return picker._assess_stream_identity(
            stream, measured_runtime_seconds=measured, record=False)

    def test_wrong_same_title_year_is_dropped_and_cannot_lead(self) -> None:
        wrong_edition = _stream(
            "Ghost.in.the.Shell.2017.2160p.WEB-DL.mkv",
            "https://stream.invalid/wrong-edition",
            resolution="2160p",
            size=28_000_000_000,
        )

        assessment = self._assess(wrong_edition)

        self.assertEqual(content_identity.CONTRADICTION, assessment.state)
        self.assertFalse(picker._identity_leader(wrong_edition))
        self.assertFalse(picker._usable(
            wrong_edition, picker.PROFILES["full"], 83 * 60))

    def test_year_bearing_exact_canonical_ranks_above_yearless(self) -> None:
        exact = _stream(
            "Ghost.in.the.Shell.1995.1080p.WEB-DL.mkv",
            "https://stream.invalid/exact",
            size=7_000_000_000,
        )
        yearless = _stream(
            "Ghost.in.the.Shell.2160p.BluRay.REMUX.mkv",
            "https://stream.invalid/yearless",
            resolution="2160p",
            size=45_000_000_000,
        )

        exact_identity = self._assess(exact)
        yearless_identity = self._assess(yearless)
        picker._annotate_quality([exact, yearless], 83 * 60)

        self.assertEqual(content_identity.STRONG, exact_identity.state)
        self.assertEqual(content_identity.COMPATIBLE, yearless_identity.state)
        self.assertGreater(exact_identity.rank, yearless_identity.rank)
        # Identity is deliberately ahead of nominal resolution/bitrate: a very
        # attractive ambiguous remake must not beat the known requested work.
        self.assertGreater(picker._quality_key(exact),
                           picker._quality_key(yearless))

    def test_exact_trusted_nzb_and_jellyfin_items_can_lead(self) -> None:
        nzb = _stream(
            "8f23c3d76f0842e98c95.mkv",
            "https://nzbdav.invalid/exact",
            source=sources.NZB,
        )
        nzb.update({
            "_nzb_identity_confidence": content_identity.STRONG,
            "_nzb_identity_evidence": ["newznab-imdb"],
            "_nzb_label": "8f23c3d76f0842e98c95",
            sources._SOURCE_TRUST_KEY: sources._NZB_TRUST_SENTINEL,
        })
        jellyfin = _stream(
            "video.mkv",
            "https://jellyfin.invalid/exact",
            source="library",
        )
        jellyfin.update({
            "_library_identity_confidence": content_identity.STRONG,
            "_library_identity_evidence": "jellyfin-imdb",
            library._IDENTITY_TRUST_KEY: library._IDENTITY_TRUST_SENTINEL,
        })
        result = probe.ProbeResult(
            True, ttfb=0.1, speed_bps=25_000_000)

        for candidate in (nzb, jellyfin):
            with self.subTest(source=candidate["_source_key"]):
                assessment = self._assess(candidate)
                assembled = picker._assemble(
                    [(candidate, None if candidate is jellyfin else result)],
                    [], None)

                self.assertEqual(content_identity.STRONG, assessment.state)
                self.assertEqual(candidate["url"], assembled[0]["url"])
                self.assertTrue(picker._is_ranked(assembled[0]))
                self.assertTrue(content_identity.auto_eligible(assembled[0]))

    def test_yearless_ordinary_result_is_never_verified_autoplay_leader(self) -> None:
        ambiguous = _stream(
            "Ghost.in.the.Shell.1080p.WEB-DL.mkv",
            "https://stream.invalid/ambiguous",
        )
        result = probe.ProbeResult(
            True, ttfb=0.05, speed_bps=80_000_000)

        assessment = self._assess(ambiguous)
        assembled = picker._assemble([(ambiguous, result)], [], None)
        returned = next(s for s in assembled
                        if s.get("url") == ambiguous["url"])

        self.assertEqual(content_identity.COMPATIBLE, assessment.state)
        self.assertEqual("checking",
                         assembled[0].get(picker._NOTICE_STATE_KEY))
        self.assertFalse(picker._is_ranked(returned))
        self.assertFalse(content_identity.auto_eligible(returned))

    def test_probe_runtime_promotes_exact_title_but_not_wrong_title(self) -> None:
        exact_title = _stream(
            "Ghost.in.the.Shell.1080p.BluRay.mkv",
            "https://stream.invalid/runtime-match",
        )
        wrong_title = _stream(
            "Alita.Battle.Angel.1080p.BluRay.mkv",
            "https://stream.invalid/wrong-title",
        )
        measured = probe.ProbeResult(
            True, ttfb=0.1, speed_bps=20_000_000, media_secs=82 * 60)

        self.assertEqual(content_identity.COMPATIBLE,
                         self._assess(exact_title).state)
        with mock.patch.object(picker.telemetry, "record_identity"):
            exact_can_lead = picker._apply_probe_evidence(
                exact_title, measured, 83 * 60)
            wrong_can_lead = picker._apply_probe_evidence(
                wrong_title, measured, 83 * 60)

        self.assertTrue(exact_can_lead)
        self.assertEqual(content_identity.STRONG,
                         exact_title[picker._IDENTITY_STATE_KEY])
        self.assertEqual(content_identity.EVIDENCE_RUNTIME,
                         exact_title[picker._IDENTITY_EVIDENCE_KEY])
        self.assertFalse(wrong_can_lead)
        self.assertEqual(content_identity.CONTRADICTION,
                         wrong_title[picker._IDENTITY_STATE_KEY])

    def test_assembly_refuses_non_strong_transport_success(self) -> None:
        exact = _stream(
            "Ghost.in.the.Shell.1995.1080p.WEB-DL.mkv",
            "https://stream.invalid/exact",
        )
        ambiguous = _stream(
            "Ghost.in.the.Shell.2160p.BluRay.REMUX.mkv",
            "https://stream.invalid/ambiguous",
            resolution="2160p",
            size=45_000_000_000,
        )
        self._assess(exact)
        self._assess(ambiguous)
        picker._annotate_quality([exact, ambiguous], 83 * 60)
        result = probe.ProbeResult(
            True, ttfb=0.1, speed_bps=30_000_000)

        assembled = picker._assemble(
            [(ambiguous, result), (exact, result)], [], None)
        exact_out = next(s for s in assembled if s.get("url") == exact["url"])
        ambiguous_out = next(
            s for s in assembled if s.get("url") == ambiguous["url"])

        self.assertEqual(exact["url"], assembled[0]["url"])
        self.assertTrue(picker._is_ranked(exact_out))
        self.assertTrue(content_identity.auto_eligible(exact_out))
        self.assertFalse(picker._is_ranked(ambiguous_out))
        self.assertFalse(content_identity.auto_eligible(ambiguous_out))

    def test_arbitrary_addon_private_fields_cannot_forge_eligibility(self) -> None:
        forged = _stream(
            "Ghost.in.the.Shell.1080p.WEB-DL.mkv",
            "https://evil-addon.invalid/forged",
            source="x:evil-addon",
        )
        forged.update({
            "_library_identity_confidence": content_identity.STRONG,
            "_library_identity_evidence": "jellyfin-imdb",
            "_nzb_identity_confidence": content_identity.STRONG,
            "_nzb_identity_evidence": ["newznab-imdb"],
            sources._SOURCE_TRUST_KEY: "forged-source-trust",
            library._IDENTITY_TRUST_KEY: "forged-library-trust",
            picker._IDENTITY_STATE_KEY: content_identity.STRONG,
            picker._IDENTITY_RANK_KEY: 5,
            picker._VERIFIED_STATE_KEY: picker._VERIFIED_SENTINEL,
        })
        # Even a contaminated in-process mapping carrying the real marker is
        # scrubbed at ingestion; JSON addons cannot manufacture this object.
        content_identity.mark_auto_eligible(forged)

        assessment = self._assess(forged)
        assembled = picker._assemble(
            [(forged, probe.ProbeResult(
                True, ttfb=0.01, speed_bps=100_000_000))],
            [], None)
        returned = next(s for s in assembled if s.get("url") == forged["url"])

        self.assertEqual(content_identity.COMPATIBLE, assessment.state)
        self.assertEqual("checking",
                         assembled[0].get(picker._NOTICE_STATE_KEY))
        self.assertFalse(picker._is_ranked(returned))
        self.assertFalse(content_identity.auto_eligible(returned))


if __name__ == "__main__":
    unittest.main()
