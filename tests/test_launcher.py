"""The windowed entry point must never silently lose startup/check errors."""
import importlib.machinery
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class LauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "start-manager.pyw"
        loader = importlib.machinery.SourceFileLoader("relay_windowed_launcher", str(path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cls.launcher = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.launcher)

    def test_launch_passes_state_directory_without_creating_native_client(self):
        with tempfile.TemporaryDirectory() as directory, patch("relay.ui.main") as launch, \
                patch("relay.transport.CodexClient", side_effect=AssertionError("No native calls")):
            self.assertEqual(self.launcher.main(["--state-dir", directory]), 0)
            launch.assert_called_once_with(state_dir=directory)

    def test_check_shows_success_or_failure_without_manager_or_gui_start(self):
        for ok in (True, False):
            with self.subTest(ok=ok), patch("relay.__main__.check_environment", return_value={
                    "ok": ok, "python": "3.13.2", "tk_version": "8.6.15", "errors": [] if ok else ["Codex unavailable"]}), \
                    patch("relay.manager.Manager", side_effect=AssertionError("Must not open state")), \
                    patch("relay.ui.main") as launch, patch.object(self.launcher, "show_message") as show:
                self.assertEqual(self.launcher.main(["--check"]), 0 if ok else 1)
                show.assert_called_once()
                self.assertEqual(show.call_args.kwargs["error"], not ok)
                self.assertIn("3.13.2", show.call_args.args[0])
                launch.assert_not_called()

    def test_startup_failure_and_invalid_arguments_are_visible(self):
        with patch("relay.ui.main", side_effect=RuntimeError("Tk unavailable")), \
                patch.object(self.launcher, "show_message") as show:
            self.assertEqual(self.launcher.main([]), 1)
            self.assertIn("Tk unavailable", show.call_args.args[0])
            self.assertTrue(show.call_args.kwargs["error"])
        with patch.object(self.launcher, "show_message") as show, patch("relay.ui.main") as launch:
            self.assertEqual(self.launcher.main(["--unknown"]), 1)
            show.assert_called_once()
            launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
