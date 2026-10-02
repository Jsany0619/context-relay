"""Isolated Windows DPAPI, private ACL and TLS-key migration checks."""
import os
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest import mock

from relay import secrets as protected
from relay.gateway import ensure_certificate


@unittest.skipUnless(os.name == "nt", "Windows DPAPI and DACL required")
class WindowsSecretsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_current_user_roundtrip_tamper_and_wrong_purpose_are_rejected(self):
        raw = b"isolated-secret-fixture"
        blob = protected.protect(raw, "unit-test")
        self.assertNotIn(raw, blob)
        self.assertEqual(protected.unprotect(blob, "unit-test"), raw)
        with self.assertRaises(protected.SecretError):
            protected.unprotect(blob[:-1] + bytes([blob[-1] ^ 1]), "unit-test")
        with self.assertRaises(protected.SecretError):
            protected.unprotect(blob, "different-purpose")
        with self.assertRaises(protected.SecretError):
            protected.unprotect(b"plaintext", "unit-test")

    def test_manager_rejects_unsafe_directory_before_lock_or_database_io(self):
        from relay.manager import Manager
        state = self.root / "unsafe-state"
        state.mkdir()
        with mock.patch.object(protected, "private_directory", side_effect=protected.SecretError("unsafe path")), \
                mock.patch("relay.manager.sqlite3.connect") as connect:
            with self.assertRaises(protected.SecretError):
                Manager(state)
            connect.assert_not_called()
        self.assertFalse((state / "manager.lock").exists())
        self.assertFalse((state / "tasks.sqlite3").exists())

    def test_private_files_have_protected_acl_and_temporary_plaintext_is_removed(self):
        directory = protected.private_directory(self.root / "private")
        target = directory / "secret.dpapi"
        protected.save(target, b"fixture-value", "test")
        protected.assert_private_acl(directory)
        protected.assert_private_acl(target)
        self.assertEqual(protected.load(target, "test"), b"fixture-value")
        with self.assertRaisesRegex(RuntimeError, "consumer failed"):
            with protected.plaintext_file(target, "test") as path:
                protected.assert_private_acl(path)
                plain_path = path
                self.assertEqual(path.read_bytes(), b"fixture-value")
                raise RuntimeError("consumer failed")
        self.assertFalse(plain_path.exists())
        self.assertFalse(list(directory.glob(".tmp-*")))

    def test_failed_plaintext_cleanup_is_an_error_not_silent_success(self):
        target = self.root / "private" / "secret.dpapi"
        protected.save(target, b"fixture", "test")
        original = protected._remove_private_tree
        with mock.patch.object(protected, "_remove_private_tree", side_effect=OSError("locked")):
            with self.assertRaisesRegex(protected.SecretError, "清理"):
                with protected.plaintext_file(target, "test") as path:
                    leftover = path.parent
        original(leftover, target.parent)

    def test_tls_generation_returns_encrypted_key_and_load_leaves_no_pem(self):
        certificate, key, fingerprint = ensure_certificate(self.root)
        self.assertEqual(key.suffix, ".dpapi")
        self.assertFalse((self.root / "gateway-key.pem").exists())
        self.assertNotIn(b"PRIVATE KEY", key.read_bytes())
        with protected.plaintext_file(key, "gateway-tls-key") as temporary_key:
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(str(certificate), str(temporary_key))
        self.assertFalse(list(key.parent.glob(".tmp-*")))
        self.assertEqual(ensure_certificate(self.root), (certificate, key, fingerprint))

    def legacy_fixture(self):
        certificate, key, fingerprint = ensure_certificate(self.root)
        raw = protected.load(key, "gateway-tls-key")
        key.unlink()
        legacy = self.root / "gateway-key.pem"
        legacy.write_bytes(raw)
        return certificate, legacy, fingerprint, raw

    def test_legacy_migration_preserves_identity_then_removes_exact_plaintext(self):
        certificate, legacy, fingerprint, raw = self.legacy_fixture()
        unrelated = self.root / "another-key.pem"
        unrelated.write_bytes(b"do-not-touch")
        migrated_cert, encrypted, migrated_fingerprint = ensure_certificate(self.root)
        self.assertEqual((migrated_cert, migrated_fingerprint), (certificate, fingerprint))
        self.assertEqual(protected.load(encrypted, "gateway-tls-key"), raw)
        self.assertFalse(legacy.exists())
        self.assertEqual(unrelated.read_bytes(), b"do-not-touch")

    def test_failed_migration_preserves_legacy_and_refuses_replacement(self):
        _, legacy, _, raw = self.legacy_fixture()
        with mock.patch.object(protected, "save", side_effect=protected.SecretError("failed")):
            with self.assertRaises(protected.SecretError):
                ensure_certificate(self.root)
        self.assertEqual(legacy.read_bytes(), raw)

    def test_conflicting_plaintext_is_not_deleted(self):
        _, encrypted, _ = ensure_certificate(self.root)
        legacy = self.root / "gateway-key.pem"
        legacy.write_bytes(b"different-legacy-material")
        with self.assertRaises(ValueError):
            ensure_certificate(self.root)
        self.assertEqual(legacy.read_bytes(), b"different-legacy-material")
        self.assertTrue(encrypted.exists())
