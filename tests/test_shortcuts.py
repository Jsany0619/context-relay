"""Windows shortcut installer integration checks using disposable folders."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Shortcut installer requires Windows PowerShell")
class ShortcutInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="context-relay-shortcuts-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "源 code & shortcut test"
        self.source.mkdir()
        installer = ROOT / "install-shortcuts.ps1"
        wrapper = ROOT / "install-shortcuts.cmd"
        self.assertTrue(installer.is_file(), "install-shortcuts.ps1 is missing")
        self.assertTrue(wrapper.is_file(), "install-shortcuts.cmd is missing")
        self.installer = self.source / installer.name
        self.wrapper = self.source / wrapper.name
        shutil.copyfile(installer, self.installer)
        shutil.copyfile(wrapper, self.wrapper)
        (self.source / "start-manager.pyw").write_text("# test launcher\n", encoding="utf-8")
        self.desktop = self.base / "重定向 Desktop & files"
        self.programs = self.base / "重定向 Programs & files"
        self.desktop.mkdir()
        self.programs.mkdir()

    def command(self, *extra: str, wrapper: bool = False) -> list[str]:
        common = [
            "-PythonExe", sys.executable,
            "-DesktopDir", str(self.desktop),
            "-ProgramsDir", str(self.programs),
            *extra,
        ]
        if wrapper:
            return ["cmd.exe", "/d", "/c", "call", str(self.wrapper), *common]
        return [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                str(self.installer), *common]

    def run_installer(self, *extra: str, wrapper: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(self.command(*extra, wrapper=wrapper), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=30)

    def links(self) -> dict[str, Path]:
        menu = self.programs / "Context Relay"
        return {
            "desktop": self.desktop / "Context Relay.lnk",
            "menu": menu / "Context Relay.lnk",
            "check": menu / "Context Relay Check.lnk",
        }

    def shortcut(self, path: Path) -> dict:
        code = (
            "$w=New-Object -ComObject WScript.Shell;"
            "$s=$w.CreateShortcut($args[0]);"
            "[pscustomobject]@{TargetPath=$s.TargetPath;Arguments=$s.Arguments;"
            "WorkingDirectory=$s.WorkingDirectory;IconLocation=$s.IconLocation;"
            "Description=$s.Description}|ConvertTo-Json -Compress"
        )
        environment = dict(os.environ, CONTEXT_RELAY_TEST_LINK=str(path))
        code = code.replace("$args[0]", "$env:CONTEXT_RELAY_TEST_LINK")
        result = subprocess.run([POWERSHELL, "-NoProfile", "-Command", code], env=environment,
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def write_foreign_shortcut(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        code = (
            "$w=New-Object -ComObject WScript.Shell;"
            "$s=$w.CreateShortcut($args[0]);$s.TargetPath=$args[1];"
            "$s.Description='Unrelated shortcut';$s.Save()"
        )
        environment = dict(os.environ, CONTEXT_RELAY_TEST_LINK=str(path),
                           CONTEXT_RELAY_TEST_TARGET=str(Path(os.environ["WINDIR"]) / "notepad.exe"))
        code = code.replace("$args[0]", "$env:CONTEXT_RELAY_TEST_LINK")
        code = code.replace("$args[1]", "$env:CONTEXT_RELAY_TEST_TARGET")
        result = subprocess.run([POWERSHELL, "-NoProfile", "-Command", code], env=environment,
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def retarget_shortcut_without_changing_its_description(self, path: Path) -> None:
        code = (
            "$w=New-Object -ComObject WScript.Shell;"
            "$s=$w.CreateShortcut($env:CONTEXT_RELAY_TEST_LINK);"
            "$s.TargetPath=$env:CONTEXT_RELAY_TEST_TARGET;$s.Save()"
        )
        environment = dict(os.environ, CONTEXT_RELAY_TEST_LINK=str(path),
                           CONTEXT_RELAY_TEST_TARGET=str(Path(os.environ["WINDIR"]) / "notepad.exe"))
        result = subprocess.run([POWERSHELL, "-NoProfile", "-Command", code], env=environment,
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_installs_exact_shortcuts_and_second_run_is_idempotent(self):
        first = self.run_installer()
        self.assertEqual(first.returncode, 0, first.stderr)
        pythonw = Path(sys.executable).resolve().with_name("pythonw.exe")
        launcher = self.source / "start-manager.pyw"
        expected_arguments = f'-I "{launcher}"'
        descriptions = set()
        for name, path in self.links().items():
            with self.subTest(name=name):
                self.assertTrue(path.is_file())
                shortcut = self.shortcut(path)
                self.assertEqual(Path(shortcut["TargetPath"]), pythonw)
                self.assertEqual(shortcut["WorkingDirectory"], str(self.source))
                self.assertEqual(shortcut["Arguments"],
                                 expected_arguments + (" --check" if name == "check" else ""))
                self.assertEqual(shortcut["IconLocation"], f"{pythonw},0")
                self.assertIn(str(self.source), shortcut["Description"])
                self.assertIn("start-manager.pyw", shortcut["Description"])
                descriptions.add(shortcut["Description"])
        self.assertEqual(len(descriptions), 1)
        before = {name: self.shortcut(path) for name, path in self.links().items()}
        second = self.run_installer()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual({name: self.shortcut(path) for name, path in self.links().items()}, before)

    def test_cmd_wrapper_forwards_quoted_arguments_without_pausing(self):
        result = self.run_installer(wrapper=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(path.is_file() for path in self.links().values()))

    def test_foreign_name_collision_is_rejected_before_writing(self):
        links = self.links()
        self.write_foreign_shortcut(links["check"])
        original = links["check"].read_bytes()
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(links["check"].read_bytes(), original)
        self.assertFalse(links["desktop"].exists())
        self.assertFalse(links["menu"].exists())

    def test_uninstall_removes_only_shortcuts_owned_by_this_repo(self):
        installed = self.run_installer()
        self.assertEqual(installed.returncode, 0, installed.stderr)
        links = self.links()
        links["check"].unlink()
        self.write_foreign_shortcut(links["check"])
        unrelated = links["check"].parent / "keep.txt"
        unrelated.write_text("keep", encoding="utf-8")
        result = self.run_installer("-Uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(links["desktop"].exists())
        self.assertFalse(links["menu"].exists())
        self.assertTrue(links["check"].exists())
        self.assertEqual(self.shortcut(links["check"])["Description"], "Unrelated shortcut")
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")

    def test_modified_shortcut_is_neither_overwritten_nor_uninstalled(self):
        installed = self.run_installer()
        self.assertEqual(installed.returncode, 0, installed.stderr)
        links = self.links()
        self.retarget_shortcut_without_changing_its_description(links["desktop"])
        changed = links["desktop"].read_bytes()
        reinstall = self.run_installer()
        self.assertNotEqual(reinstall.returncode, 0)
        self.assertEqual(links["desktop"].read_bytes(), changed)
        removed = self.run_installer("-Uninstall")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertTrue(links["desktop"].exists())
        self.assertFalse(links["menu"].exists())
        self.assertFalse(links["check"].exists())
        self.assertTrue(links["menu"].parent.is_dir())

    def test_missing_launcher_or_python_is_rejected_without_writes(self):
        (self.source / "start-manager.pyw").unlink()
        missing_launcher = self.run_installer()
        self.assertNotEqual(missing_launcher.returncode, 0)
        self.assertFalse(any(path.exists() for path in self.links().values()))
        (self.source / "start-manager.pyw").write_text("# test launcher\n", encoding="utf-8")
        missing_python = subprocess.run([
            POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self.installer),
            "-PythonExe", str(self.base / "missing python.exe"),
            "-DesktopDir", str(self.desktop), "-ProgramsDir", str(self.programs),
        ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        self.assertNotEqual(missing_python.returncode, 0)
        self.assertFalse(any(path.exists() for path in self.links().values()))


if __name__ == "__main__":
    unittest.main()
