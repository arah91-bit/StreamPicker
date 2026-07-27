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

# app.recs refuses to talk to TMDB or Trakt without these, which is right,
# but tests never do — they mock the transport or assert on the request that
# would have been sent. The values only have to be present and non-empty.
# setdefault rather than assignment, so an explicitly exported value still
# wins; that is how the suite has been run up to now.
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
os.environ["DB_PATH"] = str(_SCRATCH / "recs.db")
atexit.register(shutil.rmtree, _SCRATCH, ignore_errors=True)
