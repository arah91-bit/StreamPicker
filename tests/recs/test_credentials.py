"""Where a missing credential is allowed to stop Daily Picks: at use, not import.

Reading TMDB_API_KEY and SETUP_SECRET at import time made `import
app.recs.anything` — every module in this directory — raise KeyError on a
checkout without a deployment's secrets, which silently took the whole Daily
Picks suite out of every test run. The fail-fast is worth keeping; it just
belongs one step later.
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app.recs import config, tmdb

REPO_ROOT = Path(__file__).resolve().parents[2]
CREDENTIALS = ("TMDB_API_KEY", "SETUP_SECRET")


def without_credentials() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in CREDENTIALS}


class ImportWithoutCredentialsTests(unittest.TestCase):
    def test_the_package_imports_with_no_credentials_in_the_environment(self):
        """A subprocess, because this one's environment is already primed."""
        result = subprocess.run(
            [sys.executable, "-c",
             "import app.recs.main, app.recs.dramas.tmdb"],
            cwd=REPO_ROOT,
            env={**without_credentials(), "PYTHONPATH": str(REPO_ROOT)},
            capture_output=True, text=True, timeout=60,
        )

        self.assertEqual(0, result.returncode, result.stderr)


class RequireTests(unittest.TestCase):
    def test_a_missing_credential_is_an_error_that_names_it(self):
        with patch.dict(os.environ, {}, clear=False):
            del os.environ["TMDB_API_KEY"]
            with self.assertRaises(RuntimeError) as raised:
                config.require("TMDB_API_KEY")

        self.assertIn("TMDB_API_KEY", str(raised.exception))

    def test_an_empty_credential_counts_as_missing(self):
        with patch.dict(os.environ, {"SETUP_SECRET": ""}):
            with self.assertRaises(RuntimeError):
                config.require("SETUP_SECRET")


class TmdbKeyTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_request_carries_the_api_key(self):
        sent = []

        async def handler(request: httpx.Request) -> httpx.Response:
            sent.append(request.url)
            return httpx.Response(200, json={})

        with patch.object(tmdb._client, "_transport",
                          httpx.MockTransport(handler)):
            await tmdb._get("/movie/1", {"language": "en-US"})

        self.assertEqual(os.environ["TMDB_API_KEY"], sent[0].params["api_key"])
        self.assertEqual("en-US", sent[0].params["language"])

    async def test_no_key_stops_the_request_before_it_is_sent(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("request sent without an API key")

        with patch.dict(os.environ, {"TMDB_API_KEY": ""}):
            with patch.object(tmdb._client, "_transport",
                              httpx.MockTransport(handler)):
                with self.assertRaises(RuntimeError):
                    await tmdb._get("/movie/1")


class SetupSecretTests(unittest.IsolatedAsyncioTestCase):
    """An unset SETUP_SECRET must not degrade into "the empty guess opens it"."""

    async def test_an_unconfigured_install_does_not_accept_an_empty_secret(self):
        from app.recs import main

        with patch.dict(os.environ, {"SETUP_SECRET": ""}):
            with self.assertRaises(RuntimeError):
                main._check_setup_secret("")


if __name__ == "__main__":
    unittest.main()
