"""Unit tests for stream-picker.

Anything the suite needs in the environment before a test module is imported
belongs here, in the package every test module already imports through, and
not in a conftest.py: pytest reads conftest, `python -m unittest discover`
does not, and the suite is run both ways.
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

# Start from no settings at all. Every key in .env.reference is a setting the
# app reads at import, and an exported one — `docker compose run` loads the
# live env file, a shell may have sourced it — changes behaviour the tests
# assert on. Measured: ten production-like values in the environment produced
# 14 failures that looked exactly like regressions. Paths (CONFIG_FILE,
# TELEMETRY_DIR) are not settings and are left alone. Set
# STREAM_PICKER_TESTS_KEEP_ENV=1 to run against an environment on purpose.
if os.environ.get("STREAM_PICKER_TESTS_KEEP_ENV") != "1":
    _reference = Path(__file__).resolve().parents[1] / ".env.reference"
    for _line in _reference.read_text().splitlines():
        _key = _line.lstrip("#").split("=", 1)[0].strip()
        if _key.isupper() and "=" in _line and " " not in _key:
            os.environ.pop(_key, None)
    # Test modules assume this exact secret (several assert on its URLs).
    os.environ["ADDON_SECRET"] = "test-secret"

# app.recs refuses to talk to TMDB or Trakt without these, which is right,
# but tests never do — they mock the transport or assert on the request that
# would have been sent. The values only have to be present and non-empty.
# setdefault, so a deliberately kept environment (above) still wins.
for _name, _placeholder in (
    ("TMDB_API_KEY", "test-tmdb-key"),
    ("TRAKT_CLIENT_ID", "test-trakt-client-id"),
    ("TRAKT_CLIENT_SECRET", "test-trakt-client-secret"),
    ("SETUP_SECRET", "test-setup-secret"),
):
    os.environ.setdefault(_name, _placeholder)

# Unconditional, unlike the credentials above: whatever a shell happens to
# have exported, a test run writes to its own directory and nowhere else.
# The defaults (/catalogs, /data) are container paths that a dev machine has
# no business creating, and redirecting them also guarantees that running the
# suite can never touch a live deployment's catalog pack or recs database.
_SCRATCH = Path(tempfile.mkdtemp(prefix="stream-picker-tests-"))
os.environ["CATALOGS_DIR"] = str(_SCRATCH / "catalogs")
# The app's lifespan starts Daily Picks' background jobs, which do real
# outbound work; tests that only need the lifespan must not touch the network.
# (test_recs_mount re-enables them where it asserts they start.)
os.environ["RECS_BACKGROUND_JOBS"] = "0"
os.environ["DB_PATH"] = str(_SCRATCH / "recs.db")
atexit.register(shutil.rmtree, _SCRATCH, ignore_errors=True)
