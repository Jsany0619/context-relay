"""Offline native Java APK builder. Requires an existing JDK and SDK tools; never downloads.

Example: python build.py --jdk JDK --android-jar SDK/platforms/android-35/android.jar
                        --build-tools SDK/build-tools/35.0.0
The local test-signing key is stored outside the repository, never bundled or published.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jdk", type=Path, default=os.environ.get("JAVA_HOME"))
    parser.add_argument("--android-jar", type=Path)
    parser.add_argument("--build-tools", type=Path)
    parser.add_argument("--sdk-cache", type=Path, help="Existing extracted platform/build-tools cache (read-only)")
    parser.add_argument("--keystore", type=Path, help="Out-of-repository local test key; created if absent")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This builder currently supports Windows only.")
    jdk = args.jdk
    if jdk is None:
        matches = sorted(Path(os.environ.get("ProgramFiles", r"C:\Program Files"), "Microsoft").glob("jdk-17*"))
        jdk = matches[-1] if matches else None
    if jdk is None or not (jdk / "bin/javac.exe").is_file():
        parser.error("Set JAVA_HOME or pass --jdk with an existing JDK 17+.")
    android_jar, tools = args.android_jar, args.build_tools
    if args.sdk_cache:
        android_jar = android_jar or next(args.sdk_cache.glob("platform/**/android.jar"), None)
        executable = next(args.sdk_cache.glob("build-tools/**/aapt2.exe"), None)
        tools = tools or (executable.parent if executable else None)
    if android_jar is None or not android_jar.is_file() or tools is None or not (tools / "aapt2.exe").is_file():
        parser.error("Pass an existing API 35 android.jar and Build Tools 35 (or --sdk-cache); no auto-install is performed.")
    repository = ROOT.parent.parent.resolve()
    key = (args.keystore or Path(os.environ.get("LOCALAPPDATA", str(Path.home())), "ContextRelayAndroid/signing/local-debug.p12")).resolve()
    if key.is_relative_to(repository):
        parser.error("The signing key must remain outside the repository.")
    key.parent.mkdir(parents=True, exist_ok=True)
    out = ROOT / "build"
    out.mkdir(exist_ok=True)
    java, javac = jdk / "bin/java.exe", jdk / "bin/javac.exe"

    with tempfile.TemporaryDirectory(prefix="compile-", dir=out) as temporary:
        staging = Path(temporary)
        def run(*parts):
            argv = [str(parts[0])]
            for part in parts[1:]:
                if isinstance(part, Path) and part.is_relative_to(staging):
                    argv.append(str(part.relative_to(staging)))
                else:
                    argv.append(str(part))
            subprocess.run(argv, cwd=staging, check=True)

        shutil.copy2(android_jar, staging / "android.jar")
        shutil.copy2(ROOT / "AndroidManifest.xml", staging / "AndroidManifest.xml")
        shutil.copytree(ROOT / "res", staging / "res")
        classes, checks = staging / "classes", staging / "checks"
        classes.mkdir(); checks.mkdir()
        if not key.exists():
            run(jdk / "bin/keytool.exe", "-genkeypair", "-keystore", key, "-storetype", "PKCS12",
                "-storepass", "android", "-keypass", "android", "-alias", "contextrelay-local",
                "-keyalg", "RSA", "-keysize", "3072", "-validity", "3650", "-dname", "CN=Context Relay Local Test")
        run(jdk / "bin/keytool.exe", "-exportcert", "-keystore", key, "-storepass", "android",
            "-alias", "contextrelay-local", "-file", staging / "test-cert.der")
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
        run(java, "-jar", tools / "lib/apksigner.jar", "sign", "--ks", key, "--ks-key-alias", "contextrelay-local",
            "--ks-pass", "pass:android", "--key-pass", "pass:android", "--out", staging / "context-relay.apk", staging / "aligned.apk")
        run(java, "-jar", tools / "lib/apksigner.jar", "verify", "--verbose", staging / "context-relay.apk")
        run(tools / "aapt2.exe", "dump", "permissions", staging / "context-relay.apk")
        run(tools / "aapt2.exe", "dump", "badging", staging / "context-relay.apk")
        shutil.copy2(staging / "context-relay.apk", apk_path)
        digest = hashlib.sha256(apk_path.read_bytes()).hexdigest()
        (out / "context-relay.apk.sha256").write_text(digest + "  context-relay.apk\n", encoding="utf-8")
        report = {"apk": "context-relay.apk", "sha256": digest, "min_sdk": 26, "target_sdk": 35,
                  "signing": "local test key outside repository; not a store/release identity",
                  "host_checks": "ProtocolCheck passed", "apk_signature": "verified", "device_test": "not performed by builder"}
        (out / "build-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    main()
