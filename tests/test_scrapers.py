"""The unified scraper catalog: engine registry integrity, per-engine URL
minting from the central debrid key, and apply() authoring the runtime source
keys (FAST_BASE_URL / STREMTHRU_BASE_URL / MEDIAFUSION_BASE_URL / EXTRA_ADDONS +
SCRAPERS) while preserving every other setting in the debrid lane URLs.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app import config, debrid, scrapers


async def _ok(service, key):
    return {"ok": True, "detail": "key accepted"}


async def _reject(service, key):
    return {"ok": False, "detail": "the service rejected this API key"}


class CatalogTests(unittest.TestCase):
    def test_engines_well_formed_and_unique(self):
        ids, badges = set(), set()
        for e in scrapers.ENGINES:
            for f in ("id", "label", "badge", "blurb", "docs"):
                self.assertTrue(e.get(f), f"{e.get('id')} missing {f}")
            ids.add(e["id"]); badges.add(e["badge"])
        self.assertEqual(len(ids), len(scrapers.ENGINES))
        self.assertEqual(len(badges), len(scrapers.ENGINES))

    def test_builtins_own_the_expected_runtime_keys(self):
        self.assertEqual("FAST_BASE_URL", scrapers.BY_ID["comet"]["key"])
        self.assertEqual("STREMTHRU_BASE_URL", scrapers.BY_ID["stremthru"]["key"])
        self.assertEqual("MEDIAFUSION_BASE_URL",
                         scrapers.BY_ID["mediafusion"]["key"])
        self.assertIsNone(scrapers.BY_ID["torrentio"]["key"])

    def test_jackettio_is_custom_only_and_needs_no_debrid(self):
        self.assertTrue(scrapers.BY_ID["jackettio"]["custom_only"])
        self.assertFalse(scrapers.BY_ID["jackettio"]["needs_debrid"])

    def test_engine_meta_leaks_no_internals(self):
        for m in scrapers.engine_meta():
            self.assertEqual({"id", "label", "badge", "blurb", "needs_debrid",
                              "custom_only", "needs_prowlarr", "internal",
                              "docs"}, set(m))


class TorrentioBuilderTests(unittest.TestCase):
    def test_uses_primary_supported_debrid_keyed_by_id(self):
        url = scrapers.build_torrentio([("torbox", "TB"), ("realdebrid", "RD")])
        self.assertTrue(url.startswith("https://torrentio.strem.fun/"))
        self.assertIn("|torbox=TB", url)          # first supported wins
        self.assertNotIn("realdebrid", url)       # only one debrid
        self.assertNotIn("/manifest.json", url)   # stored as a base
        self.assertIn("sort=qualitysize", url)

    def test_skips_unsupported_primary_and_finds_next(self):
        url = scrapers.build_torrentio([("pikpak", "P"), ("torbox", "TB")])
        self.assertIn("|torbox=TB", url)
        self.assertNotIn("pikpak", url)

    def test_no_supported_debrid_mints_bare_options(self):
        url = scrapers.build_torrentio([("pikpak", "P")])
        self.assertNotIn("=P", url)
        self.assertTrue(url.endswith("unknown"))

    def test_knightcrawler_honours_custom_base(self):
        url = scrapers.build_knightcrawler([("realdebrid", "RD")],
                                           base="https://kc.example/")
        self.assertTrue(url.startswith("https://kc.example/"))
        self.assertIn("|realdebrid=RD", url)


class MintForTestTests(unittest.IsolatedAsyncioTestCase):
    FAST = debrid.build_comet("https://comet.example",
                              [("torbox", "TB-STORED")])

    async def test_builtins_delegate_to_debrid_builders(self):
        comet = await scrapers.mint_for_test(
            "comet", "", [{"service": "torbox", "key": ""}], self.FAST, "", "")
        self.assertEqual([{"service": "torbox", "key": "TB-STORED"}],
                         debrid.current(comet))
        st = await scrapers.mint_for_test(
            "stremthru", "", [{"service": "torbox", "key": "K"}], "", "", "")
        self.assertIn("/stremio/torz/", st)

    async def test_custom_url_passthrough_strips_manifest(self):
        base = await scrapers.mint_for_test(
            "custom-x", "https://a.b/manifest.json", None, "", "", "")
        self.assertEqual("https://a.b", base)

    async def test_custom_only_and_unknown_require_or_reject(self):
        with self.assertRaises(ValueError):
            await scrapers.mint_for_test("jackettio", "", None, "", "", "")
        with self.assertRaises(ValueError):
            await scrapers.mint_for_test("nope", "", None, "", "", "")


class CurrentTests(unittest.TestCase):
    def test_scrapers_json_is_authoritative(self):
        scr = json.dumps([{"id": "comet"},
                          {"id": "jackettio", "url": "https://jk/x"}])
        self.assertEqual([{"id": "comet"},
                          {"id": "jackettio", "url": "https://jk/x"}],
                         scrapers.current("", "", "", "", scr))

    def test_reconstructs_from_runtime_keys_when_unset(self):
        extra = json.dumps([
            {"name": "Torrentio", "url": "https://torrentio.strem.fun/sort/x"},
            {"name": "My", "url": "https://my.addon"}])
        got = scrapers.current("https://comet/x", "", "https://mf/y", extra, "")
        self.assertEqual({"comet", "mediafusion", "torrentio", "custom-my"},
                         {r["id"] for r in got})
        custom = next(r for r in got if r["id"] == "custom-my")
        self.assertEqual("https://my.addon", custom["url"])

    def test_prowlarr_source_flag_reconstructs_engine(self):
        got = scrapers.current("https://comet/x", "", "", "", "", "1")
        self.assertIn("prowlarr", {r["id"] for r in got})
        self.assertNotIn("prowlarr",
                         {r["id"] for r in scrapers.current(
                             "https://comet/x", "", "", "", "", "0")})


class ApplyTests(unittest.IsolatedAsyncioTestCase):
    FAST = debrid.build_comet("https://comet.example",
                              [("torbox", "TB-STORED")])

    async def test_enable_comet_and_torrentio_mints_and_clears_the_rest(self):
        with patch.object(debrid, "validate_key", _ok):
            res = await scrapers.apply(
                self.FAST, "", "", "",
                [{"service": "torbox", "key": ""}],
                [{"id": "comet"}, {"id": "torrentio"}])
        self.assertTrue(res["ok"])
        v = res["values"]
        # Comet minted (keeps the stored key), StremThru/MediaFusion cleared.
        self.assertEqual([{"service": "torbox", "key": "TB-STORED"}],
                         debrid.current(v["FAST_BASE_URL"]))
        self.assertEqual({"STREMTHRU_BASE_URL", "MEDIAFUSION_BASE_URL",
                          "PROWLARR_SOURCE"}, set(res["clears"]))
        extras = json.loads(v["EXTRA_ADDONS"])
        self.assertEqual("Torrentio", extras[0]["name"])
        self.assertIn("torbox=TB-STORED", extras[0]["url"])
        self.assertEqual([{"id": "comet"}, {"id": "torrentio"}],
                         json.loads(v["SCRAPERS"]))

    async def test_custom_only_engine_needs_no_debrid(self):
        res = await scrapers.apply(
            "", "", "", "", [],
            [{"id": "jackettio", "url": "https://jk.example/manifest.json"}])
        self.assertTrue(res["ok"])
        extras = json.loads(res["values"]["EXTRA_ADDONS"])
        self.assertEqual([{"name": "Jackettio", "url": "https://jk.example"}],
                         extras)
        self.assertEqual(["FAST_BASE_URL", "STREMTHRU_BASE_URL",
                          "MEDIAFUSION_BASE_URL", "PROWLARR_SOURCE"],
                         res["clears"])

    async def test_disabling_everything_clears_all_lanes(self):
        res = await scrapers.apply(self.FAST, "", "", "", [], [])
        self.assertEqual({"FAST_BASE_URL", "STREMTHRU_BASE_URL",
                          "MEDIAFUSION_BASE_URL", "PROWLARR_SOURCE"},
                         set(res["clears"]))
        self.assertEqual("", res["values"]["EXTRA_ADDONS"])

    async def test_dry_run_reports_checks_without_values(self):
        with patch.object(debrid, "validate_key", _ok):
            res = await scrapers.apply(
                "", "", "", "", [{"service": "torbox", "key": "TB"}],
                [{"id": "comet"}], dry_run=True)
        self.assertTrue(res["ok"])
        self.assertNotIn("values", res)
        self.assertIn("torbox", res["results"])

    async def test_rejected_new_key_aborts(self):
        with patch.object(debrid, "validate_key", _reject):
            res = await scrapers.apply(
                "", "", "", "", [{"service": "torbox", "key": "BAD"}],
                [{"id": "comet"}])
        self.assertFalse(res["ok"])
        self.assertNotIn("values", res)


class MediaFusionMintTests(unittest.IsolatedAsyncioTestCase):
    class _Resp:
        def __init__(self, data): self._data = data
        def raise_for_status(self): pass
        def json(self): return self._data

    class _Client:
        def __init__(self, resp): self._resp = resp
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, timeout=None, headers=None):
            MediaFusionMintTests.last = {"url": url, "body": json,
                                         "headers": headers}
            return self._resp

    async def test_encrypt_endpoint_mints_secret_path(self):
        resp = self._Resp({"encrypted_str": "SEALED"})
        with patch.object(scrapers.httpx, "AsyncClient",
                          lambda *a, **k: self._Client(resp)):
            url = await scrapers.build_mediafusion(
                "https://mf.example/old", [("torbox", "TB")])
        self.assertEqual("https://mf.example/SEALED", url)
        self.assertTrue(self.last["url"].endswith("/encrypt-user-data"))
        self.assertEqual("torbox",
                         self.last["body"]["streaming_provider"]["service"])

    async def test_prowlarr_and_api_password_are_injected(self):
        resp = self._Resp({"encrypted_str": "SEALED"})
        with patch.object(scrapers.httpx, "AsyncClient",
                          lambda *a, **k: self._Client(resp)):
            url = await scrapers.build_mediafusion(
                "https://mf.example/old", [("torbox", "TB")],
                prowlarr={"url": "http://pr:9696", "api_key": "PK"},
                api_password="secretpw")
        self.assertEqual("https://mf.example/SEALED", url)
        body = self.last["body"]
        self.assertEqual("secretpw", body["api_password"])
        self.assertEqual({"enabled": True, "use_global": False,
                          "url": "http://pr:9696", "api_key": "PK"},
                         body["indexer_config"]["prowlarr"])
        self.assertEqual({"X-API-Key": "secretpw"}, self.last["headers"])

    async def test_no_prowlarr_leaves_config_minimal(self):
        resp = self._Resp({"encrypted_str": "S"})
        with patch.object(scrapers.httpx, "AsyncClient",
                          lambda *a, **k: self._Client(resp)):
            await scrapers.build_mediafusion("https://mf.example/old",
                                             [("torbox", "TB")])
        self.assertNotIn("indexer_config", self.last["body"])
        self.assertNotIn("api_password", self.last["body"])
        self.assertEqual({}, self.last["headers"])

    async def test_custom_url_wins_over_minting(self):
        got = await scrapers._mediafusion_url(
            "", [], "https://mine.example/cfg/manifest.json")
        self.assertEqual("https://mine.example/cfg", got)

    async def test_falls_back_to_existing_when_mint_fails(self):
        with patch.object(scrapers, "build_mediafusion",
                          side_effect=Exception("unreachable")):
            got = await scrapers._mediafusion_url("https://mf.example/old", [], "")
        self.assertEqual("https://mf.example/old", got)

    async def test_no_existing_and_failed_mint_raises(self):
        with patch.object(scrapers, "build_mediafusion",
                          side_effect=Exception("unreachable")):
            with self.assertRaises(ValueError):
                await scrapers._mediafusion_url("", [], "")


class ProwlarrEngineTests(unittest.IsolatedAsyncioTestCase):
    FAST = debrid.build_comet("https://comet.example",
                              [("torbox", "TB-STORED")])

    async def test_engine_registered_internal_and_prowlarr_gated(self):
        eng = scrapers.BY_ID["prowlarr"]
        self.assertTrue(eng.get("internal"))
        self.assertTrue(eng.get("needs_prowlarr"))
        self.assertIsNone(eng["key"])
        meta = next(m for m in scrapers.engine_meta() if m["id"] == "prowlarr")
        self.assertTrue(meta["needs_prowlarr"] and meta["internal"])

    async def test_enabling_persists_backend_and_flag(self):
        with patch.object(debrid, "validate_key", _ok):
            res = await scrapers.apply(
                self.FAST, "", "", "", [{"service": "torbox", "key": ""}],
                [{"id": "prowlarr"}],
                prowlarr_submitted={"url": "http://pr:9696", "api_key": "PK"})
        v = res["values"]
        self.assertEqual("http://pr:9696", v["PROWLARR_URL"])
        self.assertEqual("PK", v["PROWLARR_API_KEY"])
        self.assertEqual("1", v["PROWLARR_SOURCE"])
        self.assertIn({"id": "prowlarr"}, json.loads(v["SCRAPERS"]))

    async def test_blank_key_keeps_stored_and_stays_enabled(self):
        with patch.object(debrid, "validate_key", _ok):
            res = await scrapers.apply(
                self.FAST, "", "", "", [{"service": "torbox", "key": ""}],
                [{"id": "prowlarr"}],
                prowlarr_submitted={"url": "http://pr:9696", "api_key": ""},
                prowlarr_key="STORED-PK")
        v = res["values"]
        self.assertEqual("http://pr:9696", v["PROWLARR_URL"])
        self.assertNotIn("PROWLARR_API_KEY", v)     # blank submit keeps stored
        self.assertEqual("1", v["PROWLARR_SOURCE"])

    async def test_engine_on_without_backend_is_cleared(self):
        with patch.object(debrid, "validate_key", _ok):
            res = await scrapers.apply(
                self.FAST, "", "", "", [{"service": "torbox", "key": ""}],
                [{"id": "prowlarr"}],
                prowlarr_submitted={"url": "", "api_key": ""})
        self.assertNotIn("PROWLARR_SOURCE", res["values"])
        self.assertIn("PROWLARR_SOURCE", res["clears"])

    async def test_removing_backend_clears_all_prowlarr_keys(self):
        res = await scrapers.apply(
            self.FAST, "", "", "", [], [],
            prowlarr_submitted={"url": "", "api_key": ""})
        for key in ("PROWLARR_URL", "PROWLARR_API_KEY", "PROWLARR_SOURCE"):
            self.assertIn(key, res["clears"])

    async def test_internal_engine_has_no_mintable_url(self):
        with self.assertRaises(ValueError):
            await scrapers.mint_for_test("prowlarr", "", None, "", "", "")


class ConfigSchemaTests(unittest.TestCase):
    def test_scrapers_normalizes_and_strips_manifest(self):
        spec = config._SPECS["SCRAPERS"]
        raw = json.dumps([{"id": "comet"},
                          {"id": "custom-x", "url": "https://a.b/manifest.json",
                           "name": "X"}])
        out = json.loads(config._normalize(spec, raw))
        self.assertEqual([{"id": "comet"},
                          {"id": "custom-x", "url": "https://a.b", "name": "X"}],
                         out)

    def test_scrapers_rejects_non_list(self):
        with self.assertRaises(ValueError):
            config._normalize(config._SPECS["SCRAPERS"], '{"id":"comet"}')

    def test_builtin_source_keys_are_secret(self):
        for key in scrapers.BUILTIN_KEYS:
            self.assertTrue(config.is_secret(key), key)


if __name__ == "__main__":
    unittest.main()
