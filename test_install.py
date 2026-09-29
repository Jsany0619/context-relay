"""Isolated installer checks: python -m unittest -v test_install.py."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import install


@unittest.skipUnless(os.name == "nt", "Installer intentionally supports Windows only")
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="context-relay-")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "用户 data" / "codex"

    def test_preview_does_not_write(self):
        with patch.object(install.os, "name", "posix"):
            with self.assertRaisesRegex(ValueError, "Only Windows"):
                install.install(self.home, apply=True)
        result = install.install(self.home)
        self.assertEqual(result["mode"], "preview")
        self.assertEqual(len(result["changed"]), 7)
        self.assertFalse(self.home.exists())

    def test_preserves_unrelated_is_idempotent_and_pins_installed_bytes(self):
        self.home.mkdir(parents=True)
        unrelated = {"type": "command", "command": "existing-plugin-check " + str(
            self.home / "skills/context-handoff/scripts/context_handoff.py"), "timeout": 9}
        original = {"plugin_setting": {"enabled": True}, "hooks": {
            "PostToolUse": [{"matcher": "Bash", "hooks": [unrelated]}],
            "Stop": [{"hooks": [unrelated]}]}}
        hooks = self.home / "hooks.json"
        original_bytes = json.dumps(original).encode()
        hooks.write_bytes(original_bytes)
        trust = self.home / "config.toml"
        trust.write_text("# Existing trust must stay unchanged\n", encoding="utf-8")
        result = install.install(self.home, apply=True)
        self.assertEqual(result["mode"], "applied")
        self.assertEqual((Path(result["backup"]) / "hooks.json").read_bytes(), original_bytes)
        installed = json.loads(hooks.read_text(encoding="utf-8"))
        self.assertEqual(installed["plugin_setting"], original["plugin_setting"])
        self.assertEqual(installed["hooks"]["Stop"], original["hooks"]["Stop"])
        self.assertEqual(installed["hooks"]["PostToolUse"][0], original["hooks"]["PostToolUse"][0])
        script = self.home / "skills/context-handoff/scripts/context_handoff.py"
        digest = hashlib.sha256(script.read_bytes()).hexdigest()
        command = installed["hooks"]["PostToolUse"][-1]["hooks"][0]["command"]
        self.assertTrue(command.endswith(" " + digest))
        self.assertIn(subprocess.list2cmdline([str(script)]), command)
        self.assertTrue(command.startswith(subprocess.list2cmdline([sys.executable])))
        # Run the exact generated command, using an unsupported event (no ledger writes).
        completed = subprocess.run(command, input='{"session_id":"installer-test","hook_event_name":"Stop"}',
                                   text=True, capture_output=True, shell=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        before = hooks.read_bytes()
        self.assertEqual(install.install(self.home, apply=True)["changed"], [])
        self.assertEqual(hooks.read_bytes(), before)
        self.assertEqual(len(list((self.home / "backups").iterdir())), 1)
        self.assertEqual(trust.read_text(encoding="utf-8"), "# Existing trust must stay unchanged\n")

    def test_existing_skill_files_are_backed_up(self):
        path = self.home / "skills/context-handoff/SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"previous local skill")
        result = install.install(self.home, apply=True)
        backup = Path(result["backup"]) / "skills/context-handoff/SKILL.md"
        self.assertEqual(backup.read_bytes(), b"previous local skill")
        self.assertEqual(path.read_bytes(), (install.SOURCE / "SKILL.md").read_bytes())

    def test_updates_in_place_preserve_other_trust_positions(self):
        install.install(self.home, apply=True)
        path = self.home / "hooks.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        owned = config["hooks"]["PostToolUse"][0]["hooks"][0]
        owned["command"] = owned["command"].rsplit(" ", 1)[0] + " " + "0" * 64
        owned["timeout"] = 12
        unrelated = {"type": "command", "command": "other-hook"}
        groups = [{"matcher": "custom", "hooks": [unrelated, owned, unrelated]},
                  {"hooks": [unrelated]}]
        config["hooks"]["PostToolUse"] = groups
        path.write_text(json.dumps(config), encoding="utf-8")
        install.install(self.home, apply=True)
        actual = json.loads(path.read_text(encoding="utf-8"))["hooks"]["PostToolUse"]
        self.assertEqual(actual[0]["matcher"], "custom")
        self.assertEqual(actual[0]["hooks"][1]["timeout"], 12)
        self.assertNotEqual(actual[0]["hooks"][1]["command"], owned["command"])
        self.assertEqual(actual[0]["hooks"][0], unrelated)
        self.assertEqual(actual[0]["hooks"][2], unrelated)
        self.assertEqual(actual[1], groups[1])
        # Existing duplicate ownership is ambiguous; never reshuffle other hooks.
        actual[1]["hooks"].append(actual[0]["hooks"][1])
        config["hooks"]["PostToolUse"] = actual
        path.write_text(json.dumps(config), encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "Duplicate Context Relay"):
            install.install(self.home, apply=True)
        self.assertEqual(path.read_bytes(), before)

    def test_malformed_config_is_rejected_before_any_write(self):
        self.home.mkdir(parents=True)
        path = self.home / "hooks.json"
        for content in (b"", b"not json", b"[]", b'{"hooks":[]}', b'{"hooks":{"PostToolUse":{}}}',
                        b'{"hooks":{"PostToolUse":[{"hooks":[{"type":"command"}]}]}}',
                        b'{"hooks":{},"hooks":{}}'):
            with self.subTest(content=content):
                path.write_bytes(content)
                with self.assertRaises(ValueError):
                    install.install(self.home, apply=True)
                self.assertEqual(path.read_bytes(), content)
                self.assertEqual(list(self.home.iterdir()), [path])

    def test_interrupted_skill_update_does_not_publish_new_hooks(self):
        install.install(self.home, apply=True)
        script = self.home / "skills/context-handoff/scripts/context_handoff.py"
        script.write_bytes(b"# old runtime\n")
        hooks = self.home / "hooks.json"
        config = json.loads(hooks.read_text(encoding="utf-8"))
        for groups in config["hooks"].values():
            hook = groups[-1]["hooks"][0]
            hook["command"] = hook["command"].rsplit(" ", 1)[0] + " " + hashlib.sha256(script.read_bytes()).hexdigest()
        hooks.write_text(json.dumps(config), encoding="utf-8")
        original = hooks.read_bytes()
        replace = install.replace_checked

        def interrupted(path, content, expected):
            replace(path, content, expected)
            raise OSError("simulated interruption")

        with patch.object(install, "replace_checked", interrupted):
            with self.assertRaises(OSError):
                install.install(self.home, apply=True)
        self.assertEqual(hooks.read_bytes(), original)
        self.assertEqual(script.read_bytes(), (install.SOURCE / "scripts/context_handoff.py").read_bytes())
        command = config["hooks"]["PostToolUse"][-1]["hooks"][0]["command"]
        result = subprocess.run(command, text=True, capture_output=True, shell=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("integrity mismatch", result.stderr)


if __name__ == "__main__":
    unittest.main()
