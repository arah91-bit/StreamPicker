"""Authenticated-at-rest coverage for the dashboard configuration store.

These tests use only synthetic values.  In particular they must never depend
on the operator's real /data directory or CONFIG_ENCRYPTION_KEY_FILE.

The public contract is deliberately tested through config.save/pending/read:
callers continue to see plaintext while every field classified by
config.is_secret() is an AES-GCM envelope on disk.  The field name is AAD, so
moving a valid envelope to a different setting must not decrypt.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
import base64

from app import config, secret_store


class EncryptedConfigStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sp-encrypted-config-")
        self.root = Path(self._tmp.name)
        self.config_path = self.root / "config.json"
        self.key_path = self.root / "config.key"
        # Raw 32-byte Docker-secret files avoid relying on a textual key
        # encoding.  They also include non-UTF-8 bytes on purpose.
        self.key_path.write_bytes(bytes(range(32)))
        os.chmod(self.key_path, 0o600)
        self.sensitive_keys = sorted(
            key for key in config._SPECS if config.is_secret(key))
        tracked = {"CONFIG_FILE", "CONFIG_ENCRYPTION_KEY_FILE",
                   "BUFFER_CACHE_GB", *self.sensitive_keys}
        self._old_env = {
            key: os.environ.get(key)
            for key in tracked
        }
        os.environ["CONFIG_FILE"] = str(self.config_path)
        os.environ["CONFIG_ENCRYPTION_KEY_FILE"] = str(self.key_path)
        for key in self.sensitive_keys + ["BUFFER_CACHE_GB"]:
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def _disk_env(self) -> dict[str, str]:
        return json.loads(self.config_path.read_text(encoding="utf-8"))["env"]

    def _assert_envelope(self, value: str) -> None:
        self.assertIsInstance(value, str)
        self.assertTrue(value.startswith("enc:v1:"), value[:24])

    def test_all_secret_and_sensitive_fields_are_encrypted_but_round_trip(self):
        self.assertIn("JELLYFIN_PASSWORD", self.sensitive_keys)
        secrets = {}
        for key in self.sensitive_keys:
            spec = config._SPECS[key]
            if spec.get("kind") == "url":
                value = f"https://service.invalid/config/{key.lower()}-token"
            elif spec.get("kind") == "multiline":
                value = (f"synthetic|https://indexer.invalid/{key.lower()}|"
                         "synthetic-indexer-credential")
            else:
                value = f"synthetic-{key.lower()}-secret-9371"
            secrets[key] = value

        config.save({**secrets, "BUFFER_CACHE_GB": "150"})
        raw = self.config_path.read_bytes()
        disk = self._disk_env()

        for key, plaintext in secrets.items():
            with self.subTest(key=key):
                self.assertTrue(config.is_secret(key))
                self.assertNotIn(plaintext.encode(), raw)
                self._assert_envelope(disk[key])
                self.assertEqual(plaintext, config.pending(key))

        # Encryption is field-selective, not an opaque whole-file format: an
        # operator can still inspect harmless behavior knobs during recovery.
        self.assertEqual("150", disk["BUFFER_CACHE_GB"])
        self.assertEqual("150", config.pending("BUFFER_CACHE_GB"))
        self.assertEqual(0o600, stat.S_IMODE(self.config_path.stat().st_mode))

    def test_apply_env_exposes_plaintext_not_the_envelope(self):
        value = "synthetic-api-key-for-apply-env"
        config.save({"TMDB_API_KEY": value})
        envelope = self._disk_env()["TMDB_API_KEY"]
        config.apply_env()
        self.assertEqual(value, os.environ["TMDB_API_KEY"])
        self.assertNotEqual(envelope, os.environ["TMDB_API_KEY"])

    def test_blank_secret_keeps_existing_ciphertext_and_plaintext_value(self):
        value = "synthetic-secret-kept-on-blank"
        config.save({"TMDB_API_KEY": value})
        before = self.config_path.read_bytes()
        result = config.save({"TMDB_API_KEY": ""})
        self.assertEqual([], result["changed"])
        self.assertEqual(before, self.config_path.read_bytes())
        self.assertEqual(value, config.pending("TMDB_API_KEY"))

    def test_legacy_plaintext_store_is_read_then_migrated_on_next_write(self):
        legacy = "legacy-synthetic-secret-should-disappear"
        self.config_path.write_text(json.dumps({"env": {
            "TMDB_API_KEY": legacy,
            "BUFFER_CACHE_GB": "120",
        }}), encoding="utf-8")
        os.chmod(self.config_path, 0o600)

        self.assertEqual(legacy, config.pending("TMDB_API_KEY"))
        config.save({"BUFFER_CACHE_GB": "130"})

        raw = self.config_path.read_bytes()
        disk = self._disk_env()
        self.assertNotIn(legacy.encode(), raw)
        self._assert_envelope(disk["TMDB_API_KEY"])
        self.assertEqual(legacy, config.pending("TMDB_API_KEY"))
        self.assertEqual("130", config.pending("BUFFER_CACHE_GB"))

    def test_wrong_explicit_key_fails_closed_without_moving_valid_store(self):
        config.save({"TMDB_API_KEY": "synthetic-wrong-key-test"})
        original = self.config_path.read_bytes()
        self.key_path.write_bytes(bytes(reversed(range(32))))

        with self.assertRaises(secret_store.SecretStoreError):
            config._read()

        self.assertEqual(original, self.config_path.read_bytes())
        self.assertFalse(list(self.root.glob("config.json.corrupt-*")))

    def test_missing_explicit_key_fails_closed_without_moving_valid_store(self):
        config.save({"TMDB_API_KEY": "synthetic-missing-key-test"})
        original = self.config_path.read_bytes()
        self.key_path.unlink()

        with self.assertRaises(secret_store.SecretStoreError):
            config._read()

        self.assertEqual(original, self.config_path.read_bytes())
        self.assertFalse(list(self.root.glob("config.json.corrupt-*")))

    def test_ciphertext_is_bound_to_its_field_name_as_aad(self):
        config.save({
            "TMDB_API_KEY": "synthetic-tmdb-value",
            "OMDB_API_KEY": "synthetic-omdb-value",
        })
        document = json.loads(self.config_path.read_text(encoding="utf-8"))
        env = document["env"]
        env["TMDB_API_KEY"], env["OMDB_API_KEY"] = (
            env["OMDB_API_KEY"], env["TMDB_API_KEY"])
        self.config_path.write_text(json.dumps(document), encoding="utf-8")
        os.chmod(self.config_path, 0o600)

        with self.assertRaises(secret_store.SecretStoreError):
            config._read()

    def test_one_bit_ciphertext_tamper_never_falls_back_to_envelope_text(self):
        config.save({"TMDB_API_KEY": "synthetic-tamper-test"})
        document = json.loads(self.config_path.read_text(encoding="utf-8"))
        envelope = document["env"]["TMDB_API_KEY"]
        # Flip a ciphertext bit, then create another syntactically valid
        # base64url envelope.  This avoids accidentally changing only unused
        # padding bits.
        sealed = bytearray(base64.urlsafe_b64decode(
            envelope.removeprefix("enc:v1:").encode("ascii")))
        sealed[-1] ^= 1
        tampered = "enc:v1:" + base64.urlsafe_b64encode(sealed).decode("ascii")
        document["env"]["TMDB_API_KEY"] = tampered
        self.config_path.write_text(json.dumps(document), encoding="utf-8")
        os.chmod(self.config_path, 0o600)

        with self.assertRaises(secret_store.SecretStoreError):
            config._read()
        self.assertEqual(tampered, self._disk_env()["TMDB_API_KEY"])
        self.assertFalse(list(self.root.glob("config.json.corrupt-*")))
        self.assertNotEqual(tampered, os.environ.get("TMDB_API_KEY"))

    def test_default_key_is_generated_owner_only_beside_config(self):
        os.environ.pop("CONFIG_ENCRYPTION_KEY_FILE", None)
        config.save({"TMDB_API_KEY": "synthetic-default-key-test"})
        default_key = self.root / ".config-encryption.key"
        self.assertEqual(32, len(default_key.read_bytes()))
        self.assertEqual(0o600, stat.S_IMODE(default_key.stat().st_mode))
        self.assertNotIn(b"synthetic-default-key-test",
                         self.config_path.read_bytes())

    def test_explicit_key_readable_by_other_users_is_rejected(self):
        os.chmod(self.key_path, 0o604)
        with self.assertRaises(secret_store.SecretStoreError):
            config.save({"TMDB_API_KEY": "synthetic-permission-test"})
        self.assertFalse(self.config_path.exists())


if __name__ == "__main__":
    unittest.main()
