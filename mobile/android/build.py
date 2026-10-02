"""Offline native Java APK builder. Requires an existing JDK and SDK tools; never downloads.

Example: python build.py --jdk JDK --android-jar SDK/platforms/android-35/android.jar
                        --build-tools SDK/build-tools/35.0.0
Signing identity is sealed with Windows current-user DPAPI outside the repository.
"""
import argparse
import base64
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent.parent))
from relay import secrets as protected

SIGNING_ALIAS = "contextrelay-local"
SIGNING_PURPOSE = "android-release-signing"


def _keytool(jdk, arguments, password_environment):
    """Password values only travel via the child's environment, never argv/output."""
    environment = os.environ.copy()
    environment.update(password_environment)
    result = subprocess.run([str(Path(jdk) / "bin/keytool.exe"), *map(str, arguments)],
                            env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=60, check=False,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise ValueError("Signing keytool operation failed; no secret output is displayed.")
    return result.stdout


def _signing_certificate(jdk, key, password):
    return _keytool(jdk, ["-exportcert", "-keystore", key, "-alias", SIGNING_ALIAS,
                         "-storepass:env", "CR_SIGN_PASSWORD"], {"CR_SIGN_PASSWORD": password})


def _read_signing(vault):
    value = json.loads(protected.load(vault, SIGNING_PURPOSE))
    if (value.get("schema_version") != 1 or value.get("alias") != SIGNING_ALIAS
            or not isinstance(value.get("password"), str) or len(value["password"]) < 48
            or not isinstance(value.get("certificate_sha256"), str)
            or len(value["certificate_sha256"]) != 64):
        raise ValueError("Protected signing identity is invalid; refusing replacement.")
    base64.b64decode(value["keystore"], validate=True)
    return value


@contextmanager
def signing_material(vault):
    """Provide a private, temporary PKCS12 file only while signing."""
    vault = Path(vault)
    value = _read_signing(vault)
    if value.get("legacy_source") and Path(value["legacy_source"]).exists():
        raise ValueError("Legacy signing key still exists; finish its explicit migration before building.")
    with protected.private_temporary_directory(vault.parent) as temporary:
        key = temporary / "signing.p12"
        protected.write_private(key, base64.b64decode(value["keystore"], validate=True))
        yield key, value["password"], value["certificate_sha256"]


def prepare_signing(jdk, directory, migrate_from=None, legacy_password=None, *, initialize=False):
    """Migrate one explicit identity without rotating its signing certificate."""
    directory = Path(directory).absolute()
    repository = ROOT.parent.parent.resolve()
    if directory.is_relative_to(repository):
        raise ValueError("Signing identity must remain outside the repository.")
    directory = protected.private_directory(directory)
    vault = directory / "signing.dpapi"
    if migrate_from is None and vault.exists():
        with signing_material(vault) as (key, password, fingerprint):
            if hashlib.sha256(_signing_certificate(jdk, key, password)).hexdigest() != fingerprint:
                raise ValueError("Protected signing certificate does not match its recorded identity.")
        return vault
    if migrate_from is None and not initialize:
        raise ValueError("No protected signing identity. Explicitly migrate the existing key, or initialize only a first release.")
    source = Path(migrate_from).absolute() if migrate_from is not None else None
    if source is not None:
        if source.is_relative_to(repository) or not legacy_password:
            raise ValueError("Migration requires an out-of-repository key and its password environment variable.")
        protected.restrict_file(source)
        source_bytes = source.read_bytes()
        certificate = _signing_certificate(jdk, source, legacy_password)
    with protected.private_temporary_directory(directory) as temporary:
        key = temporary / "signing.p12"
        if vault.exists():
            value = _read_signing(vault)
            if source is None or value.get("legacy_source") != str(source):
                raise ValueError("A protected signing identity already exists; refusing replacement.")
            protected.write_private(key, base64.b64decode(value["keystore"], validate=True))
            password = value["password"]
        else:
            password = secrets.token_urlsafe(48)
            destination = ["-destkeystore", key, "-deststoretype", "PKCS12",
                           "-deststorepass:env", "CR_SIGN_NEW", "-destkeypass:env", "CR_SIGN_NEW",
                           "-destalias", SIGNING_ALIAS, "-noprompt"]
            if source is not None:
                _keytool(jdk, ["-importkeystore", "-srckeystore", source, "-srcstoretype", "PKCS12",
                              "-srcalias", SIGNING_ALIAS, "-srcstorepass:env", "CR_SIGN_OLD",
                              "-srckeypass:env", "CR_SIGN_OLD", *destination],
                         {"CR_SIGN_OLD": legacy_password, "CR_SIGN_NEW": password})
            else:
                _keytool(jdk, ["-genkeypair", "-keystore", key, "-storetype", "PKCS12",
                              "-storepass:env", "CR_SIGN_NEW", "-keypass:env", "CR_SIGN_NEW",
                              "-alias", SIGNING_ALIAS, "-keyalg", "RSA", "-keysize", "3072",
                              "-validity", "3650", "-dname", "CN=Context Relay"],
                         {"CR_SIGN_NEW": password})
            protected.restrict_file(key)
        actual = _signing_certificate(jdk, key, password)
        if source is not None and not hmac.compare_digest(actual, certificate):
            raise ValueError("Migration changed the certificate identity; original key retained.")
        if not vault.exists():
            value = {"schema_version": 1, "alias": SIGNING_ALIAS, "password": password,
                     "keystore": base64.b64encode(key.read_bytes()).decode("ascii"),
                     "certificate_sha256": hashlib.sha256(actual).hexdigest(),
                     "legacy_source": str(source) if source is not None else None}
            protected.save(vault, json.dumps(value, sort_keys=True).encode("utf-8"), SIGNING_PURPOSE)
        saved = _read_signing(vault)
        protected.write_private(key, base64.b64decode(saved["keystore"], validate=True))
        if not hmac.compare_digest(_signing_certificate(jdk, key, saved["password"]), actual):
            raise ValueError("Protected signing identity readback failed; original key retained.")
    # Cleanup must finish before removing the sole original source.
    if source is not None:
        if not hmac.compare_digest(source.read_bytes(), source_bytes):
            raise ValueError("Legacy signing key changed during migration; original retained.")
        try:
            source.unlink()
        except OSError as error:
            raise ValueError("Protected identity saved, but legacy key removal failed; retry explicit migration.") from error
    return vault


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jdk", type=Path, default=os.environ.get("JAVA_HOME"))
    parser.add_argument("--android-jar", type=Path)
    parser.add_argument("--build-tools", type=Path)
    parser.add_argument("--sdk-cache", type=Path, help="Existing extracted platform/build-tools cache (read-only)")
    parser.add_argument("--signing-dir", type=Path, help="Out-of-repository current-user DPAPI signing directory")
    parser.add_argument("--migrate-keystore", type=Path, help="Explicitly migrate and remove this legacy PKCS12 identity")
    parser.add_argument("--legacy-password-env", help="Environment variable holding the legacy PKCS12 password")
    parser.add_argument("--initialize-signing", action="store_true", help="Create identity only for a first release with no existing installations")
    parser.add_argument("--signing-only", action="store_true", help="Prepare/verify signing identity without compiling an APK")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This builder currently supports Windows only.")
    jdk = args.jdk
    if jdk is None:
        matches = sorted(Path(os.environ.get("ProgramFiles", r"C:\Program Files"), "Microsoft").glob("jdk-17*"))
        jdk = matches[-1] if matches else None
    if jdk is None or not (jdk / "bin/javac.exe").is_file():
        parser.error("Set JAVA_HOME or pass --jdk with an existing JDK 17+.")
    if args.migrate_keystore and args.initialize_signing:
        parser.error("Choose migration or first-release initialization, never both.")
    legacy_default = Path(os.environ.get("LOCALAPPDATA", str(Path.home())), "ContextRelayAndroid/signing/local-debug.p12")
    if args.initialize_signing and legacy_default.exists():
        parser.error("Existing legacy signing identity found; migrate it to preserve installed-app updates.")
    signing_directory = args.signing_dir or Path(os.environ.get("LOCALAPPDATA", str(Path.home())), "ContextRelayAndroid/release-signing")
    try:
        vault = prepare_signing(jdk, signing_directory, args.migrate_keystore,
                                os.environ.get(args.legacy_password_env) if args.legacy_password_env else None,
                                initialize=args.initialize_signing)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    if args.signing_only:
        print(json.dumps({"signing": "Windows current-user DPAPI", "certificate_sha256": _read_signing(vault)["certificate_sha256"]}))
        return
    android_jar, tools = args.android_jar, args.build_tools
    if args.sdk_cache:
        android_jar = android_jar or next(args.sdk_cache.glob("platform/**/android.jar"), None)
        executable = next(args.sdk_cache.glob("build-tools/**/aapt2.exe"), None)
        tools = tools or (executable.parent if executable else None)
    if android_jar is None or not android_jar.is_file() or tools is None or not (tools / "aapt2.exe").is_file():
        parser.error("Pass an existing API 35 android.jar and Build Tools 35 (or --sdk-cache); no auto-install is performed.")
    out = ROOT / "build"
    out.mkdir(exist_ok=True)
    java, javac = jdk / "bin/java.exe", jdk / "bin/javac.exe"

    with signing_material(vault) as (key, password, certificate_sha256), tempfile.TemporaryDirectory(prefix="compile-", dir=out) as temporary:
        staging = Path(temporary)
        def run(*parts, environment=None):
            argv = [str(parts[0])]
            for part in parts[1:]:
                if isinstance(part, Path) and part.is_relative_to(staging):
                    argv.append(str(part.relative_to(staging)))
                else:
                    argv.append(str(part))
            subprocess.run(argv, cwd=staging, check=True, env=environment)

        shutil.copy2(android_jar, staging / "android.jar")
        shutil.copy2(ROOT / "AndroidManifest.xml", staging / "AndroidManifest.xml")
        shutil.copytree(ROOT / "res", staging / "res")
        classes, checks = staging / "classes", staging / "checks"
        classes.mkdir(); checks.mkdir()
        (staging / "test-cert.der").write_bytes(_signing_certificate(jdk, key, password))
        source = ROOT / "src/app/contextrelay/mobile"
        run(javac, "--release", "8", "-encoding", "UTF-8", "-d", checks,
            source / "Protocol.java", source / "PinnedTrust.java", ROOT / "checks/ProtocolCheck.java")
        run(java, "-ea", "-cp", checks, "app.contextrelay.mobile.ProtocolCheck", staging / "test-cert.der")
        run(javac, "--release", "8", "-encoding", "UTF-8", "-cp", staging / "android.jar", "-d", classes, *source.glob("*.java"))
        run(java, "-cp", tools / "lib/d8.jar", "com.android.tools.r8.D8", "--min-api", "26", "--lib", staging / "android.jar",
            "--output", staging, *classes.rglob("*.class"))
        run(tools / "aapt2.exe", "compile", "--dir", staging / "res", "-o", staging / "resources.zip")
        run(tools / "aapt2.exe", "link", "-o", staging / "unsigned.apk", "--manifest", staging / "AndroidManifest.xml",
            "-I", staging / "android.jar", staging / "resources.zip")
        with zipfile.ZipFile(staging / "unsigned.apk", "a", zipfile.ZIP_DEFLATED) as apk:
            apk.write(staging / "classes.dex", "classes.dex")
        run(tools / "zipalign.exe", "-f", "4", staging / "unsigned.apk", staging / "aligned.apk")
        apk_path = out / "context-relay.apk"
        signing_environment = os.environ.copy()
        signing_environment["CR_APK_SIGN_PASSWORD"] = password
        run(java, "-jar", tools / "lib/apksigner.jar", "sign", "--ks", key, "--ks-key-alias", SIGNING_ALIAS,
            "--ks-pass", "env:CR_APK_SIGN_PASSWORD", "--key-pass", "env:CR_APK_SIGN_PASSWORD",
            "--out", staging / "context-relay.apk", staging / "aligned.apk", environment=signing_environment)
        run(java, "-jar", tools / "lib/apksigner.jar", "verify", "--verbose", staging / "context-relay.apk")
        run(tools / "aapt2.exe", "dump", "permissions", staging / "context-relay.apk")
        run(tools / "aapt2.exe", "dump", "badging", staging / "context-relay.apk")
        shutil.copy2(staging / "context-relay.apk", apk_path)
        digest = hashlib.sha256(apk_path.read_bytes()).hexdigest()
        (out / "context-relay.apk.sha256").write_text(digest + "  context-relay.apk\n", encoding="utf-8")
        report = {"apk": "context-relay.apk", "sha256": digest, "min_sdk": 26, "target_sdk": 35,
                  "signing": "current-user DPAPI protected identity; not store approval",
                  "signing_certificate_sha256": certificate_sha256,
                  "host_checks": "ProtocolCheck passed", "apk_signature": "verified", "device_test": "not performed by builder"}
        (out / "build-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    main()
