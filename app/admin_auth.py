"""Durable, fail-closed administrator credentials.

Dashboard passwords do not belong in the generic settings JSON.  This module
stores a versioned scrypt verifier in a separate owner-only file and leaves a
second initialized marker behind.  If the verifier is ever lost or corrupted,
the marker keeps first-run enrollment closed instead of reopening a LAN account
takeover window.
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile


VERSION = 1
ALGORITHM = "scrypt"
SCRYPT_N = 1 << 15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 64 * 1024 * 1024
SALT_BYTES = 16
KEY_BYTES = 32
MIN_PASSWORD_CHARS = 12
MAX_PASSWORD_BYTES = 1024
MAX_RECORD_BYTES = 16 * 1024


class AccountExistsError(RuntimeError):
    pass


def _directory() -> Path:
    config_file = (os.environ.get("CONFIG_FILE") or "").strip()
    if config_file:
        return Path(config_file).parent
    return Path(os.environ.get("TELEMETRY_DIR", "/data"))


def account_path() -> Path:
    return _directory() / "admin-auth.json"


def marker_path() -> Path:
    return _directory() / "admin-auth.initialized"


def _lock_path() -> Path:
    return _directory() / ".admin-auth.lock"


def account_file_exists() -> bool:
    return os.path.lexists(account_path())


def initialized() -> bool:
    """True for valid, corrupt, or lost-after-initialization credentials."""
    return account_file_exists() or os.path.lexists(marker_path())


def legacy_configured() -> bool:
    """An explicit legacy password skips onboarding; ADDON_SECRET does not."""
    return bool(os.environ.get("ADMIN_PASSWORD", ""))


def account_configured() -> bool:
    return initialized() or legacy_configured()


def setup_required() -> bool:
    return not account_configured()


def validate_username(username: str) -> str:
    if not isinstance(username, str):
        raise ValueError("username is required")
    username = username.strip()
    if (not username or len(username) > 128 or ":" in username
            or any(ord(c) < 33 or ord(c) == 127 for c in username)):
        raise ValueError(
            "username must be 1-128 visible characters without spaces or ':'")
    return username


def validate_password(password: str) -> str:
    if not isinstance(password, str):
        raise ValueError("password is required")
    if password != password.strip():
        raise ValueError("password cannot start or end with whitespace")
    if len(password) < MIN_PASSWORD_CHARS:
        raise ValueError(
            f"password must be at least {MIN_PASSWORD_CHARS} characters")
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise ValueError("password is too long")
    if any(ord(c) < 32 or ord(c) == 127 for c in password):
        raise ValueError("password cannot contain control characters")
    return password


def _derive(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R,
        p=SCRYPT_P, maxmem=SCRYPT_MAXMEM, dklen=KEY_BYTES)


def _record(username: str, password: str) -> bytes:
    username = validate_username(username)
    password = validate_password(password)
    salt = secrets.token_bytes(SALT_BYTES)
    verifier = _derive(password, salt)
    value = {
        "version": VERSION,
        "algorithm": ALGORITHM,
        "username": username,
        "salt": base64.b64encode(salt).decode("ascii"),
        "verifier": base64.b64encode(verifier).decode("ascii"),
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
    }
    return (json.dumps(value, sort_keys=True, separators=(",", ":"))
            + "\n").encode("utf-8")


def _atomic_create(path: Path, payload: bytes) -> None:
    """Create *path* exactly once using a fully-fsynced same-dir temp file."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        try:
            os.link(tmp, path, follow_symlinks=False)
        except FileExistsError:
            raise AccountExistsError("administrator account is already initialized")
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _mark_initialized() -> None:
    try:
        fd = os.open(marker_path(), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    try:
        os.write(fd, b"initialized\n")
        os.fsync(fd)
    finally:
        os.close(fd)
    dfd = os.open(marker_path().parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _create(username: str, password: str, *, allow_legacy: bool) -> str:
    username = validate_username(username)
    password = validate_password(password)
    payload = _record(username, password)
    directory = _directory()
    directory.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(
        _lock_path(), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if initialized() or (legacy_configured() and not allow_legacy):
            raise AccountExistsError("administrator account is already initialized")
        _atomic_create(account_path(), payload)
        _mark_initialized()
        return username
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def create_account(username: str, password: str) -> str:
    return _create(username, password, allow_legacy=False)


def migrate_legacy() -> bool:
    """Hash an explicitly preseeded ADMIN_PASSWORD once, without downtime."""
    if initialized() or not legacy_configured():
        return False
    username = os.environ.get("ADMIN_USERNAME", "admin") or "admin"
    try:
        _create(username, os.environ["ADMIN_PASSWORD"], allow_legacy=True)
        return True
    except (AccountExistsError, ValueError):
        # Preserve compatibility with old explicitly configured passwords that
        # predate the new-account length policy. They remain usable as legacy
        # credentials instead of preventing startup.
        return False


def _load() -> tuple[str, bytes, bytes] | None:
    path = account_path()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RECORD_BYTES:
            return None
        with os.fdopen(fd, "rb", closefd=False) as f:
            raw = f.read(MAX_RECORD_BYTES + 1)
        if len(raw) > MAX_RECORD_BYTES:
            return None
        data = json.loads(raw)
        expected = {"version", "algorithm", "username", "salt", "verifier",
                    "n", "r", "p"}
        if (not isinstance(data, dict) or set(data) != expected
                or data["version"] != VERSION or data["algorithm"] != ALGORITHM
                or data["n"] != SCRYPT_N or data["r"] != SCRYPT_R
                or data["p"] != SCRYPT_P):
            return None
        username = validate_username(data["username"])
        salt = base64.b64decode(data["salt"], validate=True)
        verifier = base64.b64decode(data["verifier"], validate=True)
        if len(salt) != SALT_BYTES or len(verifier) != KEY_BYTES:
            return None
        return username, salt, verifier
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error):
        return None
    finally:
        os.close(fd)


def verify_credentials(username: str, password: str) -> bool:
    if initialized():
        record = _load()
        if record is None:
            return False
        expected_user, salt, verifier = record
        try:
            candidate = _derive(password, salt)
        except (ValueError, TypeError, UnicodeError):
            return False
        user_ok = secrets.compare_digest(
            username.encode("utf-8"), expected_user.encode("utf-8"))
        password_ok = secrets.compare_digest(candidate, verifier)
        return user_ok and password_ok

    expected_password = os.environ.get("ADMIN_PASSWORD", "")
    expected_user = os.environ.get("ADMIN_USERNAME", "admin") or "admin"
    if not expected_password:
        return False
    try:
        return (secrets.compare_digest(username.encode(), expected_user.encode())
                and secrets.compare_digest(password.encode(),
                                           expected_password.encode()))
    except UnicodeError:
        return False


def generation() -> tuple:
    """Stable cache key that changes whenever the credential record changes."""
    try:
        info = os.lstat(account_path())
        return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
    except OSError:
        return ("legacy", bool(os.environ.get("ADMIN_PASSWORD", "")))
