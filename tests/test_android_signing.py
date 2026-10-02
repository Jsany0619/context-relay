"""Real keytool + Windows DPAPI checks using generated, disposable identities."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from relay import secrets as protected

BUILD = Path(__file__).resolve().parents[1] / "mobile/android/build.py"
spec = importlib.util.spec_from_file_location("android_build", BUILD)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
JDK = Path(os.environ.get("JAVA_HOME", r"C:\Program Files\Microsoft\jdk-17.0.9.8-hotspot"))


@unittest.skipUnless(os.name == "nt" and (JDK / "bin/keytool.exe").is_file(), "Windows/JDK required")
class AndroidSigningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.legacy = self.root / "old.p12"
        self.old_password = "synthetic-old-password"
        builder._keytool(JDK, ["-genkeypair", "-keystore", self.legacy, "-storetype", "PKCS12",
                            "-alias", builder.SIGNING_ALIAS, "-keyalg", "RSA", "-keysize", "2048",
                            "-validity", "1", "-dname", "CN=Disposable signing test",
                            "-storepass:env", "CR_TEST_OLD", "-keypass:env", "CR_TEST_OLD"],
                         {"CR_TEST_OLD": self.old_password})
        self.certificate = builder._signing_certificate(JDK, self.legacy, self.old_password)

    def test_migration_preserves_certificate_removes_weak_source_and_cleans_plaintext(self):
        vault = builder.prepare_signing(JDK, self.root / "protected", self.legacy, self.old_password)
        self.assertFalse(self.legacy.exists())
        self.assertNotIn(self.old_password.encode(), vault.read_bytes())
        protected.assert_private_acl(vault)
        with builder.signing_material(vault) as (key, password, fingerprint):
            self.assertGreaterEqual(len(password), 48)
            self.assertEqual(builder._signing_certificate(JDK, key, password), self.certificate)
            self.assertEqual(fingerprint, builder.hashlib.sha256(self.certificate).hexdigest())
            temporary_key = key
        self.assertFalse(temporary_key.exists())
        self.assertFalse(list(vault.parent.glob(".tmp-*")))
        self.assertEqual(builder.prepare_signing(JDK, vault.parent), vault)

    def test_failure_preserves_original_and_never_passes_password_values_in_argv(self):
        original = self.legacy.read_bytes()
        with mock.patch.object(protected, "save", side_effect=protected.SecretError("write failed")):
            with self.assertRaises(protected.SecretError):
                builder.prepare_signing(JDK, self.root / "protected", self.legacy, self.old_password)
        self.assertEqual(self.legacy.read_bytes(), original)
        self.assertFalse(list((self.root / "protected").glob(".tmp-*")))
        with mock.patch.object(builder.subprocess, "run", wraps=builder.subprocess.run) as run:
            builder._signing_certificate(JDK, self.legacy, self.old_password)
        self.assertNotIn(self.old_password, " ".join(map(str, run.call_args.args[0])))
        self.assertIn(self.old_password, run.call_args.kwargs["env"].values())

    def test_wrong_password_and_tampered_vault_refuse_without_replacing_identity(self):
        original = self.legacy.read_bytes()
        with self.assertRaises(ValueError):
            builder.prepare_signing(JDK, self.root / "protected", self.legacy, "wrong-password")
        self.assertEqual(self.legacy.read_bytes(), original)
        vault = builder.prepare_signing(JDK, self.root / "protected", self.legacy, self.old_password)
        blob = vault.read_bytes()
        vault.write_bytes(blob[:-1] + bytes([blob[-1] ^ 1]))
        with self.assertRaises(ValueError):
            builder.prepare_signing(JDK, vault.parent)

    def test_initialization_is_explicit_and_existing_vault_cannot_change_identity(self):
        target = self.root / "protected"
        with self.assertRaises(ValueError):
            builder.prepare_signing(JDK, target)
        vault = builder.prepare_signing(JDK, target, initialize=True)
        with self.assertRaises(ValueError):
            builder.prepare_signing(JDK, target, self.legacy, self.old_password)
        self.assertTrue(self.legacy.exists())
        self.assertTrue(vault.exists())

    def test_failed_old_key_removal_blocks_build_until_explicit_verified_retry(self):
        original_unlink = Path.unlink
        def refuse_old(path, *args, **kwargs):
            if path == self.legacy:
                raise PermissionError("synthetic file lock")
            return original_unlink(path, *args, **kwargs)
        target = self.root / "protected"
        with mock.patch.object(Path, "unlink", refuse_old):
            with self.assertRaisesRegex(ValueError, "removal failed"):
                builder.prepare_signing(JDK, target, self.legacy, self.old_password)
        self.assertTrue(self.legacy.exists())
        with self.assertRaisesRegex(ValueError, "Legacy signing key still exists"):
            builder.prepare_signing(JDK, target)
        vault = builder.prepare_signing(JDK, target, self.legacy, self.old_password)
        self.assertFalse(self.legacy.exists())
        with builder.signing_material(vault) as (key, password, _):
            self.assertEqual(builder._signing_certificate(JDK, key, password), self.certificate)


if __name__ == "__main__":
    unittest.main()
