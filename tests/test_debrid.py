"""Debrid registry integrity and the Comet/StremThru URL surgery: adding or
removing a provider must rewrite only the debrid list and leave every other
setting the operator has in those URLs untouched.
"""

from __future__ import annotations

import base64
import json
import unittest

from app import debrid


async def _ok(service, key):
    return {"ok": True, "detail": "key accepted"}


async def _reject(service, key):
    return {"ok": False, "detail": "the service rejected this API key"}


def _mk_comet(base: str, cfg: dict) -> str:
    b64 = base64.b64encode(json.dumps(cfg).encode()).decode()
    return f"{base.rstrip('/')}/{b64}"


class RegistryTests(unittest.TestCase):
    def test_every_provider_is_well_formed_and_unique(self):
        ids, codes, badges = set(), set(), set()
        for p in debrid.PROVIDERS:
            for field in ("id", "label", "badge", "code", "key_url"):
                self.assertTrue(p.get(field), f"{p.get('id')} missing {field}")
            ids.add(p["id"]); codes.add(p["code"]); badges.add(p["badge"])
        self.assertEqual(len(ids), len(debrid.PROVIDERS))
        self.assertEqual(len(codes), len(debrid.PROVIDERS))
        self.assertEqual(len(badges), len(debrid.PROVIDERS))

    def test_store_codes_match_stremthru(self):
        # The authoritative StremThru name->code map (from its source).
        expect = {"torbox": "tb", "realdebrid": "rd", "alldebrid": "ad",
                  "premiumize": "pm", "debridlink": "dl", "offcloud": "oc",
                  "easydebrid": "ed", "debrider": "dr", "pikpak": "pp"}
        self.assertEqual(expect, {p["id"]: p["code"] for p in debrid.PROVIDERS})

    def test_unsupported_services_absent(self):
        # Comet's VALID_DEBRID_SERVICES rejects these, so they must never appear.
        self.assertNotIn("putio", debrid.BY_ID)
        self.assertNotIn("seedr", debrid.BY_ID)

    def test_signup_url_carries_referral_only_when_present(self):
        self.assertIn("referral=9ca21adb", debrid.signup_url(debrid.BY_ID["torbox"]))
        self.assertNotIn("referral=", debrid.signup_url(debrid.BY_ID["realdebrid"]))


class CometSurgeryTests(unittest.TestCase):
    RICH = {"cachedOnly": True, "removeTrash": True, "maxSize": 0,
            "languages": ["en"], "resolutions": ["1080p", "2160p"],
            "debridServices": [{"service": "torbox", "apiKey": "TB-OLD"}]}

    def test_parse_roundtrips_and_reads_services(self):
        url = _mk_comet("https://comet.example.org", self.RICH)
        base, cfg = debrid.parse_comet(url)
        self.assertEqual("https://comet.example.org", base)
        self.assertEqual(self.RICH, cfg)
        self.assertEqual([{"service": "torbox", "key": "TB-OLD"}],
                         debrid.current(url))

    def test_build_preserves_other_settings_and_host(self):
        url = _mk_comet("https://comet.example.org", self.RICH)
        new = debrid.build_comet(url, [("torbox", "TB-NEW"),
                                       ("realdebrid", "RD-NEW")])
        base, cfg = debrid.parse_comet(new)
        self.assertEqual("https://comet.example.org", base)     # host kept
        self.assertEqual(["1080p", "2160p"], cfg["resolutions"])  # extras kept
        self.assertEqual(["en"], cfg["languages"])
        self.assertEqual([{"service": "torbox", "apiKey": "TB-NEW"},
                          {"service": "realdebrid", "apiKey": "RD-NEW"}],
                         cfg["debridServices"])

    def test_build_from_bare_host_uses_defaults(self):
        new = debrid.build_comet("https://comet.example.org", [("torbox", "K")])
        base, cfg = debrid.parse_comet(new)
        self.assertEqual("https://comet.example.org", base)
        self.assertTrue(cfg["cachedOnly"])
        self.assertEqual("torbox", cfg["debridServices"][0]["service"])

    def test_build_from_empty_falls_back_to_public(self):
        new = debrid.build_comet("", [("torbox", "K")])
        base, _ = debrid.parse_comet(new)
        self.assertEqual(debrid.wizard.COMET_PUBLIC, base)

    def test_current_drops_unknown_service(self):
        cfg = dict(self.RICH, debridServices=[
            {"service": "torbox", "apiKey": "k"},
            {"service": "putio", "apiKey": "x"}])
        url = _mk_comet("https://c.example", cfg)
        self.assertEqual([{"service": "torbox", "key": "k"}], debrid.current(url))


class StremthruSurgeryTests(unittest.TestCase):
    def test_roundtrip_preserves_config_and_uses_store_codes(self):
        base_url = debrid.build_stremthru(
            "http://stremthru:8080", [("torbox", "K1"), ("debridlink", "K2")])
        base, cfg = debrid.parse_stremthru(base_url)
        self.assertEqual("http://stremthru:8080", base)
        self.assertEqual([{"c": "tb", "t": "K1"}, {"c": "dl", "t": "K2"}],
                         cfg["stores"])
        self.assertIn("/stremio/torz/", base_url)

    def test_build_preserves_existing_torz_settings(self):
        first = debrid.build_stremthru("https://st.example",
                                       [("torbox", "K1")])
        second = debrid.build_stremthru(first, [("realdebrid", "K2")])
        _, cfg = debrid.parse_stremthru(second)
        self.assertEqual([{"c": "rd", "t": "K2"}], cfg["stores"])
        self.assertIn("cached", cfg)

    def test_stremthru_current_maps_codes_back_to_ids(self):
        url = debrid.build_stremthru("https://st.example",
                                     [("torbox", "K1"), ("offcloud", "K2")])
        self.assertEqual([{"service": "torbox", "key": "K1"},
                          {"service": "offcloud", "key": "K2"}],
                         debrid.stremthru_current(url))


class ResolveTests(unittest.TestCase):
    FAST = _mk_comet("https://comet.example", {
        "cachedOnly": True, "resolutions": ["1080p"],
        "debridServices": [{"service": "torbox", "apiKey": "TB-STORED"}]})

    def test_blank_key_keeps_stored_and_marks_kept(self):
        resolved, kept = debrid._resolve(
            self.FAST, "", [{"service": "torbox", "key": ""}])
        self.assertEqual([("torbox", "TB-STORED")], resolved)
        self.assertEqual({"torbox"}, kept)

    def test_new_key_overrides_and_is_not_kept(self):
        resolved, kept = debrid._resolve(
            self.FAST, "", [{"service": "torbox", "key": "TB-NEW"}])
        self.assertEqual([("torbox", "TB-NEW")], resolved)
        self.assertEqual(set(), kept)

    def test_key_can_be_kept_from_stremthru_when_absent_from_comet(self):
        torz = debrid.build_stremthru("https://st.example",
                                      [("offcloud", "OC-STORED")])
        resolved, kept = debrid._resolve(
            "", torz, [{"service": "offcloud", "key": ""}])
        self.assertEqual([("offcloud", "OC-STORED")], resolved)
        self.assertEqual({"offcloud"}, kept)

    def test_blank_key_with_nothing_stored_is_rejected(self):
        with self.assertRaises(ValueError):
            debrid._resolve("", "", [{"service": "realdebrid", "key": ""}])

    def test_duplicate_and_unknown_and_empty_are_rejected(self):
        with self.assertRaises(ValueError):
            debrid._resolve(self.FAST, "", [{"service": "torbox", "key": "a"},
                                            {"service": "torbox", "key": "b"}])
        with self.assertRaises(ValueError):
            debrid._resolve("", "", [{"service": "nope", "key": "a"}])
        with self.assertRaises(ValueError):
            debrid._resolve(self.FAST, "", [])


class ApplyTests(unittest.IsolatedAsyncioTestCase):
    FAST = _mk_comet("https://comet.example", {
        "cachedOnly": True, "resolutions": ["1080p", "2160p"],
        "languages": ["en"],
        "debridServices": [{"service": "torbox", "apiKey": "TB-STORED"}]})

    async def test_save_keeps_kept_key_adds_new_and_preserves_settings(self):
        # torbox blank => keep TB-STORED (no network); offcloud uncheckable.
        res = await debrid.apply(
            self.FAST, "",
            [{"service": "torbox", "key": ""},
             {"service": "offcloud", "key": "OC-NEW"}])
        self.assertTrue(res["ok"])
        base, cfg = debrid.parse_comet(res["values"]["FAST_BASE_URL"])
        self.assertEqual("https://comet.example", base)
        self.assertEqual(["1080p", "2160p"], cfg["resolutions"])  # preserved
        self.assertEqual([{"service": "torbox", "apiKey": "TB-STORED"},
                          {"service": "offcloud", "apiKey": "OC-NEW"}],
                         cfg["debridServices"])
        _, st = debrid.parse_stremthru(res["values"]["STREMTHRU_BASE_URL"])
        self.assertEqual([{"c": "tb", "t": "TB-STORED"},
                          {"c": "oc", "t": "OC-NEW"}], st["stores"])

    async def test_rejected_new_checkable_key_aborts_save(self):
        debrid.validate_key = _reject
        try:
            res = await debrid.apply(
                "", "", [{"service": "realdebrid", "key": "RD-BAD"}])
        finally:
            debrid.validate_key = ApplyTests._orig_validate
        self.assertFalse(res["ok"])
        self.assertNotIn("values", res)
        self.assertIs(res["results"]["realdebrid"]["ok"], False)

    async def test_dry_run_reports_without_saving(self):
        debrid.validate_key = _ok
        try:
            res = await debrid.apply(
                self.FAST, "", [{"service": "torbox", "key": ""}],
                dry_run=True)
        finally:
            debrid.validate_key = ApplyTests._orig_validate
        self.assertTrue(res["ok"])
        self.assertNotIn("values", res)
        self.assertIn("torbox", res["results"])

    @classmethod
    def setUpClass(cls):
        cls._orig_validate = debrid.validate_key


if __name__ == "__main__":
    unittest.main()
