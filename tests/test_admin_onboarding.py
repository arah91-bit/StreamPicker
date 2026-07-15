"""First-run administrator enrollment and credential durability.

These tests intentionally use a fresh credential directory per case.  The
dashboard account is a one-time, local-only security boundary; ADDON_SECRET
must never become a usable dashboard password.
"""

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
import stat
import tempfile
import threading
import unittest
from unittest import mock


os.environ.setdefault("ADDON_SECRET", "test-secret")

from app import admin_auth, adminui, main
from fastapi.testclient import TestClient


def _basic(username: str, password: str) -> dict[str, str]:
    value = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"authorization": f"Basic {value}"}


class AdminOnboardingTests(unittest.TestCase):
    USERNAME = "firstadmin"
    PASSWORD = "correct horse battery staple"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sp-admin-test-")
        self._env = mock.patch.dict(os.environ, {
            "CONFIG_FILE": os.path.join(self._tmp.name, "config.json"),
            # Enrollment remains local even when an operator later elects to
            # expose the authenticated dashboard.
            "DASHBOARD_LOCAL_ONLY": "0",
            "TRUSTED_PROXIES": "127.0.0.0/8,::1/128",
        })
        self._env.start()
        os.environ.pop("ADMIN_USERNAME", None)
        os.environ.pop("ADMIN_PASSWORD", None)
        adminui._AUTH_CACHE.clear()
        self.local = TestClient(main.app, client=("127.0.0.1", 50000))

    def tearDown(self):
        self.local.close()
        adminui._AUTH_CACHE.clear()
        self._env.stop()
        self._tmp.cleanup()

    def _csrf(self) -> str:
        response = self.local.get("/")
        self.assertEqual(200, response.status_code)
        match = re.search(r'data-csrf="([^"]+)"', response.text)
        self.assertIsNotNone(match, "setup form did not contain its CSRF token")
        return match.group(1)

    def _payload(self, **changes) -> dict[str, str]:
        payload = {
            "username": self.USERNAME,
            "password": self.PASSWORD,
            "confirmation": self.PASSWORD,
        }
        payload.update(changes)
        return payload

    def _create(self, **changes):
        return self.local.post(
            "/api/admin/setup",
            headers={"x-csrf-token": self._csrf()},
            json=self._payload(**changes),
        )

    def test_first_run_form_creates_separate_admin_credentials(self):
        self.assertTrue(admin_auth.setup_required())
        response = self.local.get("/")
        self.assertEqual(200, response.status_code)
        self.assertNotIn("www-authenticate", response.headers)
        self.assertIn("Create your administrator account", response.text)
        self.assertNotIn("Addon setup key", response.text)
        self.assertNotIn(main.SECRET, response.text)
        self.assertEqual("no-store", response.headers.get("cache-control"))
        self.assertEqual("DENY", response.headers.get("x-frame-options"))
        self.assertIn("frame-ancestors 'none'",
                      response.headers.get("content-security-policy", ""))

        # Enrollment exposes only the setup page.  Existing admin pages point
        # back to it, while JSON APIs fail explicitly instead of leaking data.
        settings = self.local.get("/settings", follow_redirects=False)
        self.assertEqual(307, settings.status_code)
        self.assertEqual("/", settings.headers.get("location"))
        pending = self.local.get("/api/admin/csrf")
        self.assertEqual(428, pending.status_code)
        self.assertNotIn("www-authenticate", pending.headers)

        # First-run state never makes ADDON_SECRET a Basic-auth password.
        old = self.local.get("/", headers=_basic("admin", main.SECRET))
        self.assertEqual(200, old.status_code)
        self.assertIn("Create your administrator account", old.text)

    def test_enrollment_is_unconditionally_local_and_forwarding_fails_closed(self):
        token = self._csrf()
        body = self._payload()
        clients = [
            # Direct public caller.
            (TestClient(main.app, client=("8.8.8.8", 50000)), {}),
            # A trusted loopback reverse proxy forwarding a public caller.
            (self.local, {"x-forwarded-for": "8.8.8.8"}),
            # An untrusted LAN peer trying to forge a loopback client.
            (TestClient(main.app, client=("192.168.1.44", 50000)),
             {"x-forwarded-for": "127.0.0.1"}),
        ]
        try:
            for client, forwarding in clients:
                with self.subTest(headers=forwarding):
                    self.assertEqual(404, client.get(
                        "/", headers=forwarding).status_code)
                    headers = {**forwarding, "x-csrf-token": token}
                    self.assertEqual(404, client.post(
                        "/api/admin/setup", headers=headers,
                        json=body).status_code)
        finally:
            clients[0][0].close()
            clients[2][0].close()
        self.assertTrue(admin_auth.setup_required())

    def test_csrf_and_origin_are_required(self):
        token = self._csrf()
        body = self._payload()

        self.assertEqual(403, self.local.post(
            "/api/admin/setup", json=body).status_code)
        self.assertEqual(403, self.local.post(
            "/api/admin/setup", headers={"x-csrf-token": "wrong"},
            json=body).status_code)
        self.assertEqual(403, self.local.post(
            "/api/admin/setup",
            headers={"x-csrf-token": token,
                     "origin": "https://attacker.example"},
            json=body).status_code)
        self.assertEqual(403, self.local.post(
            "/api/admin/setup",
            headers={"x-csrf-token": token,
                     "sec-fetch-site": "cross-site"},
            json=body).status_code)
        self.assertTrue(admin_auth.setup_required())

        response = self.local.post(
            "/api/admin/setup",
            headers={"x-csrf-token": token, "origin": "http://testserver"},
            json=body,
        )
        self.assertEqual(201, response.status_code)

    def test_invalid_accounts_do_not_partially_initialize(self):
        invalid = [
            {"username": ""},
            {"username": "two words"},
            {"username": "bad:name"},
            {"username": "x" * 129},
            {"password": "elevenchars", "confirmation": "elevenchars"},
            {"password": " leading whitespace password",
             "confirmation": " leading whitespace password"},
            {"password": "control\x00password", "confirmation": "control\x00password"},
            {"password": "x" * 1025, "confirmation": "x" * 1025},
            {"confirmation": "a different long password"},
        ]
        token = self._csrf()
        for changes in invalid:
            with self.subTest(changes=tuple(changes)):
                response = self.local.post(
                    "/api/admin/setup", headers={"x-csrf-token": token},
                    json=self._payload(**changes))
                self.assertEqual(400, response.status_code, response.text)
                self.assertNotIn(str(changes.get("password", "not-present")),
                                 response.text)
                self.assertTrue(admin_auth.setup_required())
                self.assertFalse(admin_auth.account_path().exists())
                self.assertFalse(admin_auth.marker_path().exists())

    def test_account_is_hashed_owner_only_immediate_and_exactly_once(self):
        response = self._create()
        self.assertEqual(201, response.status_code, response.text)
        self.assertEqual(self.USERNAME, response.json()["username"])

        account = admin_auth.account_path()
        marker = admin_auth.marker_path()
        raw = account.read_bytes()
        record = json.loads(raw)
        self.assertEqual("scrypt", record["algorithm"])
        self.assertNotIn("password", record)
        self.assertNotIn(self.PASSWORD.encode(), raw)
        self.assertNotIn(main.SECRET.encode(), raw)
        self.assertTrue(stat.S_ISREG(account.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(account.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(marker.stat().st_mode))

        # The new account takes effect without a process restart.  Enrollment
        # closes before any subsequent request can replace it.
        unauthenticated = self.local.get("/")
        self.assertEqual(401, unauthenticated.status_code)
        self.assertIn("Basic", unauthenticated.headers.get(
            "www-authenticate", ""))
        self.assertEqual(200, self.local.get(
            "/", headers=_basic(self.USERNAME, self.PASSWORD)).status_code)
        self.assertEqual(401, self.local.get(
            "/", headers=_basic("admin", main.SECRET)).status_code)

        retry = self.local.post(
            "/api/admin/setup", headers={"x-csrf-token": adminui.csrf_token()},
            json=self._payload(username="replacement",
                               password="replacement password",
                               confirmation="replacement password"))
        self.assertEqual(409, retry.status_code)
        adminui._AUTH_CACHE.clear()
        self.assertEqual(200, self.local.get(
            "/", headers=_basic(self.USERNAME, self.PASSWORD)).status_code)
        self.assertEqual(401, self.local.get(
            "/", headers=_basic("replacement", "replacement password")).status_code)

    def test_corrupt_or_missing_verifier_with_marker_never_reopens_setup(self):
        self.assertEqual(201, self._create().status_code)
        account = admin_auth.account_path()

        account.write_text("{not valid JSON", encoding="utf-8")
        adminui._AUTH_CACHE.clear()
        self.assertTrue(admin_auth.initialized())
        self.assertFalse(admin_auth.setup_required())
        self.assertEqual(401, self.local.get(
            "/", headers=_basic(self.USERNAME, self.PASSWORD)).status_code)
        self.assertEqual(409, self.local.post(
            "/api/admin/setup", headers={"x-csrf-token": adminui.csrf_token()},
            json=self._payload()).status_code)

        account.unlink()
        adminui._AUTH_CACHE.clear()
        self.assertTrue(admin_auth.initialized())
        self.assertFalse(admin_auth.setup_required())
        self.assertEqual(401, self.local.get(
            "/", headers=_basic(self.USERNAME, self.PASSWORD)).status_code)
        self.assertEqual(409, self.local.post(
            "/api/admin/setup", headers={"x-csrf-token": adminui.csrf_token()},
            json=self._payload()).status_code)

    def test_explicit_legacy_password_skips_setup_and_migrates_to_hash(self):
        os.environ["ADMIN_USERNAME"] = "legacyadmin"
        os.environ["ADMIN_PASSWORD"] = "legacy password has length"
        self.assertFalse(admin_auth.setup_required())
        self.assertEqual(401, self.local.get("/").status_code)
        self.assertEqual(200, self.local.get(
            "/", headers=_basic("legacyadmin",
                                 "legacy password has length")).status_code)

        self.assertTrue(asyncio.run(adminui.migrate_legacy()))
        self.assertFalse(asyncio.run(adminui.migrate_legacy()))
        raw = admin_auth.account_path().read_bytes()
        self.assertNotIn(b"legacy password has length", raw)
        self.assertEqual(0o600, stat.S_IMODE(
            admin_auth.account_path().stat().st_mode))

        # Simulate the next start without the plaintext legacy environment.
        os.environ.pop("ADMIN_USERNAME", None)
        os.environ.pop("ADMIN_PASSWORD", None)
        adminui._AUTH_CACHE.clear()
        self.assertEqual(200, self.local.get(
            "/", headers=_basic("legacyadmin",
                                 "legacy password has length")).status_code)
        self.assertEqual(409, self.local.post(
            "/api/admin/setup", headers={"x-csrf-token": adminui.csrf_token()},
            json=self._payload()).status_code)

    def test_concurrent_creators_have_one_durable_winner(self):
        barrier = threading.Barrier(2)
        candidates = [
            ("alice", "alice has a sufficiently long password"),
            ("bob", "bob also has a sufficiently long password"),
        ]

        def attempt(candidate):
            username, password = candidate
            barrier.wait(timeout=5)
            try:
                admin_auth.create_account(username, password)
                return "created", username, password
            except admin_auth.AccountExistsError:
                return "exists", username, password

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, candidates))
        self.assertEqual(["created", "exists"],
                         sorted(result[0] for result in results))
        winner = next(result for result in results if result[0] == "created")
        loser = next(result for result in results if result[0] == "exists")
        self.assertTrue(admin_auth.verify_credentials(winner[1], winner[2]))
        self.assertFalse(admin_auth.verify_credentials(loser[1], loser[2]))
        self.assertTrue(admin_auth.marker_path().exists())


if __name__ == "__main__":
    unittest.main()
