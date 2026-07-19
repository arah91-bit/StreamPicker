"""Authenticated encryption for secrets persisted by :mod:`app.config`.

Configuration still lives in the human-readable ``config.json`` store, but
every field marked sensitive is sealed independently with AES-256-GCM.  The
master key is deliberately separate from that file.  Production deployments
should mount it read-only and set ``CONFIG_ENCRYPTION_KEY_FILE``; a private key
next to ``config.json`` is generated only as a convenient standalone default.

The setting name is authenticated as additional data.  Copying a ciphertext
from (say) ``TMDB_API_KEY`` into ``JELLYFIN_PASSWORD`` therefore fails closed
instead of silently decrypting to the wrong credential.
"""

from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


PREFIX = "enc:v1:"
KEY_BYTES = 32
NONCE_BYTES = 12
_AAD_PREFIX = b"stream-picker/config/v1/"


class SecretStoreError(RuntimeError):
    """A key or ciphertext cannot be used safely.

    This intentionally does not inherit from ``ValueError``: malformed JSON is
    quarantined by config.py, while a valid encrypted store paired with a
    missing/wrong key must be left exactly where it is for operator recovery.
    """


def is_encrypted(value: str) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def _default_key_path(config_path: str) -> Path:
    return Path(config_path).resolve().parent / ".config-encryption.key"


def key_path(config_path: str) -> Path:
    explicit = (os.environ.get("CONFIG_ENCRYPTION_KEY_FILE") or "").strip()
    return Path(explicit) if explicit else _default_key_path(config_path)


def _read_key(path: Path) -> bytes:
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        try:
            st = os.fstat(fd)
            if st.st_mode & 0o007:
                raise SecretStoreError(
                    "configuration encryption key must not be accessible to other users")
            raw = os.read(fd, KEY_BYTES + 1)
        finally:
            os.close(fd)
    except FileNotFoundError as exc:
        raise SecretStoreError(
            "configuration encryption key is missing; encrypted settings were not changed") from exc
    except OSError as exc:
        raise SecretStoreError(
            "configuration encryption key could not be read") from exc
    if len(raw) != KEY_BYTES:
        raise SecretStoreError(
            f"configuration encryption key must contain exactly {KEY_BYTES} bytes")
    return raw


def _create_default_key(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    key = os.urandom(KEY_BYTES)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return _read_key(path)
    try:
        written = 0
        while written < len(key):
            written += os.write(fd, key[written:])
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    return key


def load_key(config_path: str, *, create: bool = False) -> bytes:
    path = key_path(config_path)
    if create and not (os.environ.get("CONFIG_ENCRYPTION_KEY_FILE") or "").strip():
        try:
            return _read_key(path)
        except SecretStoreError:
            if not path.exists():
                return _create_default_key(path)
            raise
    return _read_key(path)


def encrypt(setting: str, value: str, config_path: str) -> str:
    if not value:
        return value
    key = load_key(config_path, create=True)
    nonce = os.urandom(NONCE_BYTES)
    aad = _AAD_PREFIX + setting.encode("utf-8")
    sealed = nonce + AESGCM(key).encrypt(nonce, value.encode("utf-8"), aad)
    token = base64.urlsafe_b64encode(sealed).decode("ascii")
    return PREFIX + token


def decrypt(setting: str, value: str, config_path: str) -> str:
    if not is_encrypted(value):
        return value
    try:
        sealed = base64.b64decode(
            value[len(PREFIX):].encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
        raise SecretStoreError("encrypted configuration value is malformed") from exc
    if len(sealed) < NONCE_BYTES + 16:
        raise SecretStoreError("encrypted configuration value is truncated")
    nonce, ciphertext = sealed[:NONCE_BYTES], sealed[NONCE_BYTES:]
    aad = _AAD_PREFIX + setting.encode("utf-8")
    try:
        raw = AESGCM(load_key(config_path)).decrypt(nonce, ciphertext, aad)
        return raw.decode("utf-8")
    except (InvalidTag, UnicodeDecodeError) as exc:
        raise SecretStoreError(
            "encrypted configuration could not be authenticated; check the mounted key") from exc
