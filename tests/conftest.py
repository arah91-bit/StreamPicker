"""Environment the test suite needs before collection starts.

`app.recs.config` reads its settings once, at import time, and deliberately
refuses to start without the credentials a real deployment must supply.  That
fail-fast is worth keeping, but it also makes a bare `pytest` unrunnable: every
module that imports the recs package dies during collection with a KeyError
unless the developer already knows which four names to export.  Filling them in
here keeps the production check strict while making a clean checkout testable.

The two on-disk locations are redirected as well.  Their defaults (/catalogs,
/data) are container paths that a dev machine has no business creating, and
sending them to a scratch directory also guarantees that running the suite can
never write into a live deployment's catalog pack or recommendation database.
"""

import os
import shutil
import tempfile
from pathlib import Path

# The values only have to be present and non-empty: tests that exercise an API
# mock the transport or assert on the request that would have been sent.
# setdefault rather than assignment, so an explicitly exported value still
# wins — that is how the suite has been run up to now.
for _name, _placeholder in (
    ("TMDB_API_KEY", "test-tmdb-key"),
    ("TRAKT_CLIENT_ID", "test-trakt-client-id"),
    ("TRAKT_CLIENT_SECRET", "test-trakt-client-secret"),
    ("SETUP_SECRET", "test-setup-secret"),
):
    os.environ.setdefault(_name, _placeholder)

# Unconditional, unlike the credentials above: whatever a shell happens to have
# exported, a test run writes to its own directory and nowhere else.
_SCRATCH = Path(tempfile.mkdtemp(prefix="stream-picker-tests-"))
os.environ["CATALOGS_DIR"] = str(_SCRATCH / "catalogs")
os.environ["DB_PATH"] = str(_SCRATCH / "recs.db")


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_SCRATCH, ignore_errors=True)
