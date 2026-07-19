"""Settings dashboard: config store validation, secret hygiene, and routes.

The properties that matter: the save endpoint must not be a generic
'set any env var' primitive, secrets must never appear in rendered HTML or
test-failure details, and blank submits must mean 'keep' for secrets but
'revert' for knobs.
"""

import json
import base64
import os
import pathlib
import re
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="sp-settings-test-")
os.environ.setdefault("ADDON_SECRET", "test-secret")
os.environ["CONFIG_FILE"] = os.path.join(_TMP, "config.json")

from app import config, connections, envref, knobs, settings_ui


def _wipe_store():
    try:
        os.unlink(os.environ["CONFIG_FILE"])
    except FileNotFoundError:
        pass


class ConfigStoreTests(unittest.TestCase):
    def setUp(self):
        _wipe_store()

    def test_save_pending_running_roundtrip(self):
        os.environ.pop("BUFFER_CACHE_GB", None)
        res = config.save({"BUFFER_CACHE_GB": "150"})
        self.assertIn("BUFFER_CACHE_GB", res["changed"])
        self.assertTrue(res["restart_needed"])
        self.assertEqual("150", config.pending("BUFFER_CACHE_GB"))
        # the running process still has its boot-time value (code default)
        self.assertEqual("100", config.running("BUFFER_CACHE_GB"))
        mode = os.stat(os.environ["CONFIG_FILE"]).st_mode & 0o777
        self.assertEqual(0o600, mode)

    def test_bool_spellings_normalized(self):
        config.save({"PREFETCH_NEXT": "off"})
        self.assertEqual("0", config.pending("PREFETCH_NEXT"))
        config.save({"PREFETCH_NEXT": "Yes"})
        self.assertEqual("1", config.pending("PREFETCH_NEXT"))
        with self.assertRaises(ValueError):
            config.save({"PREFETCH_NEXT": "maybe"})

    def test_numbers_reject_out_of_range_nonfinite_and_fractional_ints(self):
        for value in ("9999", "-3", "lots", "nan", "inf"):
            with self.assertRaises(ValueError, msg=value):
                config.save({"BUFFER_AHEAD_GB": value})
        with self.assertRaises(ValueError):
            config.save({"MAX_PROBES": "4.5"})

    def test_whole_config_constraints_are_atomic(self):
        with self.assertRaisesRegex(ValueError, "BUFFER_AHEAD_GB"):
            config.save({"BUFFER_CACHE_GB": "10", "BUFFER_AHEAD_GB": "20"})
        self.assertEqual("100", config.pending("BUFFER_CACHE_GB"))
        self.assertEqual("8", config.pending("BUFFER_AHEAD_GB"))

    def test_corrupt_json_is_quarantined(self):
        path = pathlib.Path(os.environ["CONFIG_FILE"])
        path.write_text('{"env": {broken')
        self.assertEqual({}, config._read())
        self.assertFalse(path.exists())
        self.assertTrue(list(path.parent.glob(path.name + ".corrupt-*")))

    def test_choice_must_be_in_schema(self):
        config.save({"DV_REJECT": "all"})
        self.assertEqual("all", config.pending("DV_REJECT"))
        with self.assertRaises(ValueError):
            config.save({"DV_REJECT": "sometimes"})

    def test_unknown_keys_rejected(self):
        # the endpoint must never become a set-any-env-var primitive
        for key in ("PATH", "LD_PRELOAD", "ADDON_SECRET", "PYTHONPATH"):
            with self.assertRaises(ValueError):
                config.save({key: "x"})

    def test_blank_secret_keeps_stored_value(self):
        config.save({"TMDB_API_KEY": "abcd1234efgh5678"})
        res = config.save({"TMDB_API_KEY": ""})
        self.assertEqual([], res["changed"])
        self.assertEqual("abcd1234efgh5678", config.pending("TMDB_API_KEY"))

    def test_blank_knob_reverts_to_default(self):
        os.environ.pop("BUFFER_CACHE_GB", None)
        config.save({"BUFFER_CACHE_GB": "150"})
        config.save({"BUFFER_CACHE_GB": ""})
        self.assertEqual("100", config.pending("BUFFER_CACHE_GB"))
        with open(os.environ["CONFIG_FILE"]) as f:
            self.assertNotIn("BUFFER_CACHE_GB", json.load(f)["env"])

    def test_indexer_lines_joined_for_storage(self):
        config.save({"NZB_INDEXERS":
                     "abc|https://abc.example/api|k1\n"
                     "def|https://def.example/api|k2\n\n"})
        self.assertEqual("abc|https://abc.example/api|k1;"
                         "def|https://def.example/api|k2",
                         config.pending("NZB_INDEXERS"))

    def test_apply_env_overlays(self):
        config.save({"BUFFER_CACHE_GB": "220"})
        os.environ.pop("BUFFER_CACHE_GB", None)
        config.apply_env()
        self.assertEqual("220", os.environ["BUFFER_CACHE_GB"])
        os.environ.pop("BUFFER_CACHE_GB", None)

    def test_apply_env_canonicalizes_explicit_bool_spellings(self):
        os.environ["PROXY_PLAYBACK"] = "off"
        try:
            config.apply_env()
            self.assertEqual("0", os.environ["PROXY_PLAYBACK"])
        finally:
            os.environ.pop("PROXY_PLAYBACK", None)


class AdvancedKnobTests(unittest.TestCase):
    def setUp(self):
        _wipe_store()

    def test_advanced_knob_saves_and_is_free_form(self):
        os.environ.pop("FAST_TIMEOUT", None)
        res = config.save({"FAST_TIMEOUT": "12"})
        self.assertIn("FAST_TIMEOUT", res["changed"])
        self.assertEqual("12", config.pending("FAST_TIMEOUT"))
        # no clamp on advanced knobs (unlike curated sliders)
        config.save({"FAST_TIMEOUT": "600"})
        self.assertEqual("600", config.pending("FAST_TIMEOUT"))

    def test_advanced_knob_rejects_junk_and_negatives(self):
        with self.assertRaises(ValueError):
            config.save({"FAST_TIMEOUT": "soon"})
        with self.assertRaises(ValueError):
            config.save({"FAST_TIMEOUT": "-5"})

    def test_setting_advanced_knob_to_default_stores_nothing(self):
        # keeps config.json to real overrides only
        res = config.save({"FAST_TIMEOUT": config.default("FAST_TIMEOUT")})
        self.assertEqual([], res["changed"])
        try:
            with open(os.environ["CONFIG_FILE"]) as f:
                stored = json.load(f).get("env", {})
        except FileNotFoundError:
            stored = {}                     # nothing written at all — even better
        self.assertNotIn("FAST_TIMEOUT", stored)

    def test_advanced_bool_knob(self):
        config.save({"TWIN_SPLICE": "off"})
        self.assertEqual("0", config.pending("TWIN_SPLICE"))

    def test_every_env_var_the_app_reads_is_reachable(self):
        # The load-bearing guarantee: nothing the code reads is unreachable from
        # the dashboard. A new os.environ knob fails this until it's cataloged
        # in app/knobs.py, added as a curated setting/connection, or EXCLUDED.
        read = set()
        pats = [re.compile(r'os\.environ\.get\(\s*"([A-Z0-9_]+)"'),
                re.compile(r'os\.environ\[\s*"([A-Z0-9_]+)"\s*\]'),
                re.compile(r'_env_bool\(\s*"([A-Z0-9_]+)"')]
        appdir = pathlib.Path(__file__).resolve().parent.parent / "app"
        for p in appdir.glob("*.py"):
            src = p.read_text()
            for pat in pats:
                read |= set(pat.findall(src))
        reachable = set(config._SPECS) | set(knobs.EXCLUDE)
        self.assertEqual(set(), read - reachable,
                         f"unreachable env vars: {sorted(read - reachable)}")

    def test_no_phantom_catalog_entries(self):
        read = set()
        appdir = pathlib.Path(__file__).resolve().parent.parent / "app"
        for p in appdir.glob("*.py"):
            src = p.read_text()
            read |= set(re.findall(r'os\.environ\.get\(\s*"([A-Z0-9_]+)"', src))
            read |= set(re.findall(r'_env_bool\(\s*"([A-Z0-9_]+)"', src))
        self.assertEqual(set(), set(knobs.keys()) - read,
                         "catalog lists knobs the app never reads")

    def test_excluded_keys_cannot_be_saved(self):
        for key in knobs.EXCLUDE:
            with self.assertRaises(ValueError):
                config.save({key: "x"})


class EnvReferenceTests(unittest.TestCase):
    def setUp(self):
        _wipe_store()

    def test_committed_reference_is_current(self):
        # tools/gen_env_reference.py output must match the committed file.
        path = pathlib.Path(__file__).resolve().parent.parent / ".env.reference"
        self.assertTrue(path.exists(), "run: python -m tools.gen_env_reference")
        self.assertEqual(envref.reference_dotenv(), path.read_text(),
                         "stale .env.reference — regenerate it")

    def test_reference_lists_every_key(self):
        text = envref.reference_dotenv()
        for key in list(knobs.keys()) + ["ADDON_SECRET", "ADDON_PUBLIC_URL",
                                         "FAST_BASE_URL", "TMDB_API_KEY"]:
            self.assertIn(key, text, f"{key} missing from reference")

    def test_export_shows_values_but_redacts_secrets(self):
        os.environ["ADDON_PUBLIC_URL"] = "https://mine.example"
        os.environ["TMDB_API_KEY"] = "tmdb-live-secret-4242"
        os.environ["ADMIN_PASSWORD"] = "admin-live-secret-9898"
        os.environ["FAST_BASE_URL"] = "https://comet.example/config/embedded-token"
        os.environ["NZB_INDEXERS"] = "idx|https://idx.example/api|nzb-secret"
        try:
            text = envref.current_dotenv()
            self.assertIn("ADDON_PUBLIC_URL=https://mine.example", text)
            self.assertNotIn("tmdb-live-secret-4242", text)
            self.assertNotIn("admin-live-secret-9898", text)
            self.assertNotIn("embedded-token", text)
            self.assertNotIn("nzb-secret", text)
            self.assertIn("TMDB_API_KEY", text)   # key present, value not
        finally:
            os.environ.pop("ADDON_PUBLIC_URL", None)
            os.environ.pop("TMDB_API_KEY", None)
            os.environ.pop("ADMIN_PASSWORD", None)
            os.environ.pop("FAST_BASE_URL", None)
            os.environ.pop("NZB_INDEXERS", None)


class SecretHygieneTests(unittest.TestCase):
    def setUp(self):
        _wipe_store()

    def test_mask_reveals_at_most_a_tail(self):
        secret = "SUPERSECRETVALUE1234"
        m = config.mask(secret)
        self.assertNotIn(secret, m)
        self.assertIn("1234", m)
        self.assertEqual("kept", config.mask("shortkey"))
        self.assertEqual("", config.mask(""))

    def test_rendered_page_never_contains_secret_values(self):
        os.environ["TMDB_API_KEY"] = "tmdb-secret-value-98765"
        os.environ["NZBDAV_PASS"] = "davpass-secret-55555"
        os.environ["FAST_BASE_URL"] = "https://comet.example/secret-config-path"
        os.environ["JELLYFIN_PASSWORD"] = "jellyfin-secret-password-2468"
        os.environ["NZB_INDEXERS"] = "idx|https://idx.example|secret-indexer-key"
        try:
            page = settings_ui.render()
            self.assertNotIn("tmdb-secret-value-98765", page)
            self.assertNotIn("davpass-secret-55555", page)
            self.assertNotIn("secret-config-path", page)
            self.assertNotIn("jellyfin-secret-password-2468", page)
            self.assertNotIn("secret-indexer-key", page)
            # Sensitive URLs/multiline specs are fully hidden; ordinary secret
            # fields show only their harmless four-character tail.
            self.assertGreaterEqual(page.count("kept · hidden"), 2)
            self.assertGreaterEqual(page.count("kept"), 5)
            self.assertIn("TMDB_API_KEY", page)   # the key name is shown
            self.assertIn("data-service='tmdb'", page)
        finally:
            os.environ.pop("TMDB_API_KEY", None)
            os.environ.pop("NZBDAV_PASS", None)
            os.environ.pop("FAST_BASE_URL", None)
            os.environ.pop("JELLYFIN_PASSWORD", None)
            os.environ.pop("NZB_INDEXERS", None)

    def test_blank_sensitive_url_and_multiline_keep_values(self):
        config.save({"FAST_BASE_URL": "https://comet.example/private",
                     "NZB_INDEXERS": "idx|https://idx.example|key"})
        res = config.save({"FAST_BASE_URL": "", "NZB_INDEXERS": ""})
        self.assertEqual([], res["changed"])
        self.assertEqual("https://comet.example/private",
                         config.pending("FAST_BASE_URL"))
        self.assertIn("|key", config.pending("NZB_INDEXERS"))

    def test_failure_details_scrub_credentials(self):
        s = connections._scrub(
            "GET https://indexer.example/api?t=caps&apikey=verysecret123 "
            "and https://user:pw@nzbdav.example/nzbs/")
        self.assertNotIn("verysecret123", s)
        self.assertNotIn("user:pw", s)

    def test_debrid_bearing_base_urls_are_treated_as_secret(self):
        # A StremThru Torz URL carries the debrid key in its path, and a
        # configured MediaFusion URL can encode credentials the same way the
        # Comet URL does — so both must be classified secret and never appear
        # in the export or the rendered page, not just Comet's FAST_BASE_URL.
        for key in ("STREMTHRU_BASE_URL", "MEDIAFUSION_BASE_URL"):
            self.assertTrue(config.is_secret(key), key)
        os.environ["STREMTHRU_BASE_URL"] = (
            "https://st.example/stremio/torz/torz-debrid-key-abcd")
        os.environ["MEDIAFUSION_BASE_URL"] = (
            "https://mf.example/D-mf-debrid-blob-wxyz/manifest.json")
        try:
            export = envref.current_dotenv()
            self.assertNotIn("torz-debrid-key-abcd", export)
            self.assertNotIn("mf-debrid-blob-wxyz", export)
            page = settings_ui.render()
            self.assertNotIn("torz-debrid-key-abcd", page)
            self.assertNotIn("mf-debrid-blob-wxyz", page)
        finally:
            os.environ.pop("STREMTHRU_BASE_URL", None)
            os.environ.pop("MEDIAFUSION_BASE_URL", None)


class AdminGuardTests(unittest.TestCase):
    def test_local_client_ips_allowed(self):
        from app import adminui

        class Req:
            def __init__(self, xff=None, host="127.0.0.1"):
                self.headers = {"x-forwarded-for": xff} if xff else {}
                self.client = type("C", (), {"host": host})()
        self.assertTrue(adminui.is_local(Req(host="127.0.0.1")))
        self.assertTrue(adminui.is_local(Req(host="192.168.1.10")))
        self.assertTrue(adminui.is_local(Req(host="172.17.0.1")))   # docker
        # A LAN caller cannot forge XFF to choose its own apparent address.
        self.assertFalse(adminui.is_local(Req(xff="10.0.0.5",
                                              host="192.168.1.10")))
        self.assertFalse(adminui.is_local(Req(xff="127.0.0.1",
                                              host="192.168.1.10")))
        # Loopback is a trusted proxy by default; its forwarded public client
        # is evaluated as public, while a forwarded LAN client remains local.
        self.assertTrue(adminui.is_local(Req(xff="10.0.0.5",
                                             host="127.0.0.1")))
        self.assertFalse(adminui.is_local(Req(xff="8.8.8.8",
                                              host="127.0.0.1")))
        self.assertFalse(adminui.is_local(Req(host="testclient")))  # non-IP


class RouteTests(unittest.TestCase):
    AUTH = "Basic " + base64.b64encode(b"admin:test-secret").decode()
    LOCAL = {"authorization": AUTH}

    @classmethod
    def setUpClass(cls):
        cls._old_admin_username = os.environ.get("ADMIN_USERNAME")
        cls._old_admin_password = os.environ.get("ADMIN_PASSWORD")
        os.environ["ADMIN_USERNAME"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "test-secret"
        _wipe_store()
        from fastapi.testclient import TestClient
        from app import main
        cls._context = TestClient(main.app, client=("127.0.0.1", 50000))
        cls.client = cls._context.__enter__()
        token = cls.client.get("/api/admin/csrf", headers=cls.LOCAL).json()
        cls.MUTATE = {**cls.LOCAL, "x-csrf-token": token["csrf_token"]}

    @classmethod
    def tearDownClass(cls):
        cls._context.__exit__(None, None, None)
        if cls._old_admin_username is None:
            os.environ.pop("ADMIN_USERNAME", None)
        else:
            os.environ["ADMIN_USERNAME"] = cls._old_admin_username
        if cls._old_admin_password is None:
            os.environ.pop("ADMIN_PASSWORD", None)
        else:
            os.environ["ADMIN_PASSWORD"] = cls._old_admin_password

    def test_local_dashboard_requires_authentication(self):
        r = self.client.get("/")
        self.assertEqual(401, r.status_code)
        self.assertIn("Basic", r.headers.get("www-authenticate", ""))

    def test_dashboard_is_clean_local_paths(self):
        for path in ("/", "/settings", "/stats"):
            r = self.client.get(path, headers=self.LOCAL)
            self.assertEqual(200, r.status_code, path)
        # one site: every admin page carries the shared tab nav
        self.assertIn("adminnav", self.client.get("/", headers=self.LOCAL).text)

    def test_public_client_is_blocked(self):
        # a request forwarded from the public internet must not see the dashboard
        pub = {**self.LOCAL, "x-forwarded-for": "8.8.8.8"}
        for path in ("/", "/settings", "/stats", "/api/settings/status.json"):
            self.assertEqual(404, self.client.get(path, headers=pub).status_code,
                             path)

    def test_settings_page_renders(self):
        r = self.client.get("/settings", headers=self.LOCAL)
        self.assertEqual(200, r.status_code)
        self.assertEqual("no-store", r.headers.get("cache-control"))
        self.assertEqual("DENY", r.headers.get("x-frame-options"))
        self.assertIn("frame-ancestors 'none'",
                      r.headers.get("content-security-policy", ""))
        self.assertIn("Stream path", r.text)
        self.assertIn("Connections", r.text)
        self.assertIn("Advanced tuning", r.text)
        self.assertIn("FAST_TIMEOUT", r.text)

    def test_export_env_route(self):
        r = self.client.get("/api/settings/export.env", headers=self.LOCAL)
        self.assertEqual(200, r.status_code)
        self.assertIn("text/plain", r.headers["content-type"])
        self.assertIn("attachment", r.headers.get("content-disposition", ""))
        self.assertIn("ADDON_PUBLIC_URL=", r.text)

    def test_save_and_status_roundtrip(self):
        r = self.client.post("/api/settings/save", headers=self.LOCAL,
                             json={"values": {"SLOW_MAX_PROBES": "24"}})
        self.assertEqual(403, r.status_code)  # authenticated is not CSRF-safe
        r = self.client.post("/api/settings/save", headers=self.MUTATE,
                             json={"values": {"SLOW_MAX_PROBES": "24"}})
        self.assertEqual(200, r.status_code)
        self.assertIn("SLOW_MAX_PROBES", r.json()["changed"])
        st = self.client.get("/api/settings/status.json",
                             headers=self.LOCAL).json()
        self.assertTrue(st["restart_pending"])
        self.assertIsInstance(st["playing"], int)

    def test_bad_save_is_400_not_500(self):
        r = self.client.post("/api/settings/save", headers=self.LOCAL,
                             json={"values": {"PATH": "/evil"}})
        self.assertEqual(403, r.status_code)
        r = self.client.post("/api/settings/save", headers=self.MUTATE,
                             json={"values": {"PATH": "/evil"}})
        self.assertEqual(400, r.status_code)

    def test_unknown_test_service_is_400(self):
        r = self.client.post("/api/settings/test/nope", headers=self.MUTATE,
                             json={"values": {}})
        self.assertEqual(400, r.status_code)

    def test_addon_endpoints_still_secret_gated(self):
        self.assertEqual(200, self.client.get(
            "/test-secret/manifest.json").status_code)
        self.assertEqual(404, self.client.get(
            "/wrong-secret/manifest.json").status_code)

    def test_cross_origin_mutation_is_denied_even_with_token(self):
        r = self.client.post("/api/settings/save",
                             headers={**self.MUTATE,
                                      "origin": "https://attacker.example"},
                             json={"values": {"PREFETCH_NEXT": "0"}})
        self.assertEqual(403, r.status_code)

    def test_liveness_and_readiness_are_distinct_and_healthy(self):
        self.assertEqual(200, self.client.get("/health/live").status_code)
        ready = self.client.get("/health/ready")
        self.assertEqual(200, ready.status_code)
        self.assertTrue(ready.json()["ok"])


if __name__ == "__main__":
    unittest.main()
