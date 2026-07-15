"""Prepare persistent bind mounts, then permanently drop root privileges.

Docker creates a missing bind-mount directory as root, and older releases may
have written root-owned 0600 files.  A static ``USER`` therefore makes both a
fresh install and an upgrade fragile.  The container grants this bootstrap
process only CHOWN, DAC_OVERRIDE, SETGID, and SETUID; after the ownership pass,
the application is exec'd as the unprivileged stream-picker account.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys


APP_UID = 1000
APP_GID = 1000


def _prepare_tree(root: Path) -> int:
    """Create *root* and repair mixed ownership without following symlinks."""
    if root.is_symlink():
        raise RuntimeError(f"refusing symlinked persistent root: {root}")
    root.mkdir(parents=True, exist_ok=True)
    changed = 0
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            path = os.path.join(directory, name)
            try:
                st = os.lstat(path)
                if st.st_uid != APP_UID or st.st_gid != APP_GID:
                    os.chown(path, APP_UID, APP_GID, follow_symlinks=False)
                    changed += 1
            except FileNotFoundError:
                # Defensive against an external process touching a shared bind
                # during startup. The application itself is not running yet.
                continue
    st = root.stat()
    if st.st_uid != APP_UID or st.st_gid != APP_GID:
        os.chown(root, APP_UID, APP_GID)
        changed += 1
    return changed


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("stream-picker entrypoint: no command supplied")

    if os.geteuid() == 0:
        telemetry = Path(os.environ.get("TELEMETRY_DIR", "/data"))
        buffer_dir = Path(os.environ.get(
            "BUFFER_DIR", str(telemetry / "bufcache")))
        roots = [telemetry]
        # A normal buffer directory is nested under /data and is covered by the
        # first non-following walk. Do not revisit it as a walk root: doing so
        # could follow a user-created bufcache symlink on the next restart.
        try:
            nested = (os.path.commonpath((os.path.abspath(buffer_dir),
                                          os.path.abspath(telemetry)))
                      == os.path.abspath(telemetry))
        except ValueError:
            nested = False
        if not nested:
            roots.append(buffer_dir)
        changed = sum(_prepare_tree(path) for path in roots)
        if changed:
            print(f"entrypoint: repaired ownership on {changed} data paths",
                  flush=True)

        os.setgroups([])
        os.setgid(APP_GID)
        os.setuid(APP_UID)
        os.umask(0o027)

    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
