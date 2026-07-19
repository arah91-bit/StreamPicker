"""First-run setup wizard: URL minting dialects, gating, and the apply flow.

The load-bearing details: Comet URLs must be standard *padded* base64 (its
strict config check rejects urlsafe/unpadded — learned live), StremThru Torz
must be urlsafe unpadded; apply() saves only lanes that passed their live
test and never succeeds with zero working stream sources; the dashboard home
shows the wizard instead of an empty overview until any source exists.
"""

import asyncio
import base64
import json
import os
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="sp-wizard-test-")
os.environ.setdefault("ADDON_SECRET", "test-secret")
os.environ.setdefault("CONFIG_FILE", os.path.join(_TMP, "config.json"))

from app import config, wizard


def _wipe_store():
    try:
        os.unlink(os.environ["CONFIG_FILE"])
    except FileNotFoundError:
        pass


class MintingTests(unittest.TestCase):
    def test_comet_url_is_standard_padded_base64(self):
        url = wizard.comet_url([("torbox", "key-1"), ("realdebrid", "key-2")])
        base, b64 = url.rsplit("/", 1)
        self.assertEqual(wizard.COMET_PUBLIC, base)
        self.assertEqual(0, len(b64) % 4)            # padding intact
        cfg = json.loads(base64.b64decode(b64))      # strict standard decode
        self.assertIs(True, cfg["cachedOnly"])
        self.assertEqual(
            [{"service": "torbox", "apiKey": "key-1"},
             {"service": "realdebrid", "apiKey": "key-2"}],
            cfg["debridServices"])

    def test_stremthru_url_is_urlsafe_unpadded(self):
        url = wizard.stremthru_url([("torbox", "key-1")])
        self.assertTrue(url.startswith(
            wizard.STREMTHRU_PUBLIC + "/stremio/torz/"))
        b64 = url.rsplit("/", 1)[1]
        self.assertNotIn("=", b64)
        cfg = json.loads(base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)))
        self.assertEqual({"indexers": None,
                          "stores": [{"c": "tb", "t": "key-1"}],
                          "cached": True}, cfg)

    def test_custom_bases_and_trailing_slashes(self):
        url = wizard.comet_url([("torbox", "k")], "https://my.comet/")
        self.assertTrue(url.startswith("https://my.comet/ey"))

    def test_every_wizard_debrid_has_a_stremthru_store_code(self):
        for sid, _, code, key_url in wizard.DEBRIDS:
            self.assertTrue(code, sid)
            self.assertTrue(key_url.startswith("https://"), sid)


class NeededTests(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.pop(k, None) for k in wizard._SOURCE_KEYS}
        _wipe_store()

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _wipe_store()

    def test_fresh_install_needs_the_wizard(self):
        self.assertTrue(wizard.needed())

    def test_any_env_source_satisfies(self):
        os.environ["STREMTHRU_BASE_URL"] = "http://st:8080/stremio/torz/abc"
        self.assertFalse(wizard.needed())

    def test_a_saved_but_unrestarted_source_satisfies(self):
        config.save({"FAST_BASE_URL": "https://comet.example/eyJ4IjogMX0="})
        self.assertFalse(wizard.needed())


class ApplyTests(unittest.TestCase):
    def setUp(self):
        from app import connections
        self._connections = connections
        self._orig_test = connections.test
        self._env = {k: os.environ.pop(k, None)
                     for k in (*wizard._SOURCE_KEYS,
                               "ADDON_PUBLIC_URL", "TMDB_API_KEY")}
        _wipe_store()
        self.outcomes = {"comet": True, "stremthru": True, "tmdb": True}
        self.tested = {}
        self._orig_lane = wizard._lane_check

        async def fake_test(service, overrides):
            self.tested[service] = dict(overrides)
            return {"ok": self.outcomes.get(service, False),
                    "detail": "ok" if self.outcomes.get(service) else "nope"}
        connections.test = fake_test

        async def fake_lane(base):
            which = "comet" if "/stremio/torz/" not in base else "stremthru"
            self.tested[which] = base
            ok = self.outcomes.get(which, False)
            return {"ok": ok, "detail": "9 streams found" if ok else "nope"}
        wizard._lane_check = fake_lane
        self._orig_key = wizard._key_check

        async def fake_key(sid, key):
            ok = self.outcomes.get(f"key:{sid}", True)
            return {"ok": ok,
                    "detail": "key accepted" if ok else "rejected"}
        wizard._key_check = fake_key

    def tearDown(self):
        self._connections.test = self._orig_test
        wizard._lane_check = self._orig_lane
        wizard._key_check = self._orig_key
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _wipe_store()

    def _apply(self, **kw):
        body = {"debrids": [{"service": "torbox", "key": "k1"}], **kw}
        return asyncio.run(wizard.apply(body))

    def test_happy_path_saves_both_lanes_and_extras(self):
        res = self._apply(tmdb="tk", public_url="https://s.example.com")
        self.assertTrue(res["ok"])
        self.assertEqual(["ADDON_PUBLIC_URL", "FAST_BASE_URL",
                          "STREMTHRU_BASE_URL", "TMDB_API_KEY"], res["saved"])
        self.assertTrue(config.pending("FAST_BASE_URL")
                        .startswith(wizard.COMET_PUBLIC))
        self.assertEqual("tk", config.pending("TMDB_API_KEY"))
        # what was live-tested is exactly what was saved
        self.assertEqual(config.pending("STREMTHRU_BASE_URL"),
                         self.tested["stremthru"])

    def test_one_dead_lane_still_succeeds_with_the_other(self):
        self.outcomes["comet"] = False
        res = self._apply()
        self.assertTrue(res["ok"])
        self.assertEqual(["STREMTHRU_BASE_URL"], res["saved"])
        self.assertEqual("", config.pending("FAST_BASE_URL"))

    def test_no_working_lane_saves_nothing(self):
        self.outcomes.update(comet=False, stremthru=False)
        res = self._apply(tmdb="tk", public_url="https://s.example.com")
        self.assertFalse(res["ok"])
        self.assertEqual([], res["saved"])
        self.assertEqual("", config.stored("ADDON_PUBLIC_URL"))
        self.assertEqual("", config.stored("TMDB_API_KEY"))

    def test_failed_tmdb_key_is_not_saved(self):
        self.outcomes["tmdb"] = False
        res = self._apply(tmdb="bad-key")
        self.assertTrue(res["ok"])
        self.assertNotIn("TMDB_API_KEY", res["saved"])

    def test_validation_rejects_bad_input(self):
        for body in ({}, {"debrids": []},
                     {"debrids": [{"service": "torbox", "key": " "}]},
                     {"debrids": [{"service": "evil", "key": "k"}]}):
            with self.assertRaises(ValueError, msg=body):
                asyncio.run(wizard.apply(body))
        with self.assertRaises(ValueError):   # non-http base
            self._apply(comet_base="ftp://x")

    def test_invalid_debrid_key_fails_before_any_lane_is_trusted(self):
        # Learned live: Comet lists cached streams without using the key, so
        # the key must be proven against the debrid's own API first.
        self.outcomes["key:torbox"] = False
        res = self._apply(tmdb="tk")
        self.assertFalse(res["ok"])
        self.assertEqual([], res["saved"])
        self.assertFalse(res["results"]["torbox"]["ok"])
        self.assertNotIn("comet", res["results"])       # lanes never checked
        self.assertEqual("", config.stored("FAST_BASE_URL"))

    def test_valid_keys_appear_in_results(self):
        res = self._apply()
        self.assertTrue(res["results"]["torbox"]["ok"])

    def test_results_never_echo_the_key(self):
        res = self._apply()
        self.assertNotIn("k1", json.dumps(res))


class RouteTests(unittest.TestCase):
    AUTH = "Basic " + base64.b64encode(b"admin:test-secret").decode()
    LOCAL = {"authorization": AUTH}

    @classmethod
    def setUpClass(cls):
        cls._admin_env = {k: os.environ.get(k)
                          for k in ("ADMIN_USERNAME", "ADMIN_PASSWORD")}
        os.environ["ADMIN_USERNAME"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "test-secret"
        cls._src_env = {k: os.environ.pop(k, None)
                        for k in wizard._SOURCE_KEYS}
        _wipe_store()
        from fastapi.testclient import TestClient
        from app import main
        cls._context = TestClient(main.app, client=("127.0.0.1", 50001))
        cls.client = cls._context.__enter__()
        token = cls.client.get("/api/admin/csrf", headers=cls.LOCAL).json()
        cls.MUTATE = {**cls.LOCAL, "x-csrf-token": token["csrf_token"]}

    @classmethod
    def tearDownClass(cls):
        cls._context.__exit__(None, None, None)
        for k, v in {**cls._admin_env, **cls._src_env}.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_home_shows_wizard_until_a_source_exists(self):
        r = self.client.get("/", headers=self.LOCAL)
        self.assertEqual(200, r.status_code)
        self.assertIn("Set up your streams", r.text)
        self.assertIn("adminnav", r.text)
        os.environ["FAST_BASE_URL"] = "https://comet.example/abc"
        try:
            r = self.client.get("/", headers=self.LOCAL)
            self.assertNotIn("Set up your streams", r.text)
        finally:
            os.environ.pop("FAST_BASE_URL", None)

    def test_setup_page_is_admin_gated_and_revisitable(self):
        self.assertEqual(401, self.client.get("/setup").status_code)
        r = self.client.get("/setup", headers=self.LOCAL)
        self.assertEqual(200, r.status_code)
        self.assertIn("TorBox", r.text)

    def test_apply_requires_csrf(self):
        r = self.client.post("/api/setup/apply", headers=self.LOCAL,
                             json={"debrids": []})
        self.assertEqual(403, r.status_code)

    def test_apply_validates(self):
        r = self.client.post("/api/setup/apply", headers=self.MUTATE,
                             json={"debrids": []})
        self.assertEqual(400, r.status_code)


if __name__ == "__main__":
    unittest.main()
