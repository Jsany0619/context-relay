import ctypes
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

from relay.display import enable_native_dpi


class DisplayTests(unittest.TestCase):
    def test_non_windows_does_not_load_windows_libraries(self):
        with mock.patch("relay.display.sys.platform", "linux"), mock.patch("ctypes.WinDLL", create=True) as loader:
            self.assertFalse(enable_native_dpi())
        loader.assert_not_called()

    def test_existing_awareness_is_preserved(self):
        user32 = SimpleNamespace(IsProcessDPIAware=mock.Mock(return_value=1), SetProcessDpiAwarenessContext=mock.Mock())
        with mock.patch("relay.display.sys.platform", "win32"), mock.patch("ctypes.WinDLL", return_value=user32, create=True):
            self.assertTrue(enable_native_dpi())
        user32.SetProcessDpiAwarenessContext.assert_not_called()

    def test_modern_api_uses_system_context_and_never_retries_a_locked_mode(self):
        for result in (0, 1):
            with self.subTest(result=result):
                user32 = SimpleNamespace(IsProcessDPIAware=mock.Mock(return_value=0),
                                         SetProcessDpiAwarenessContext=mock.Mock(return_value=result))
                with mock.patch("relay.display.sys.platform", "win32"), mock.patch("ctypes.WinDLL", return_value=user32, create=True) as loader:
                    self.assertEqual(enable_native_dpi(), bool(result))
                self.assertEqual(user32.SetProcessDpiAwarenessContext.call_args.args[0].value, ctypes.c_void_p(-2).value)
                self.assertEqual(loader.call_count, 1)

    def test_shcore_fallback_preserves_an_already_set_mode(self):
        for result in (0, -2147024891):  # S_OK, E_ACCESSDENIED
            with self.subTest(result=result):
                user32 = SimpleNamespace(IsProcessDPIAware=mock.Mock(return_value=0), SetProcessDPIAware=mock.Mock())
                shcore = SimpleNamespace(SetProcessDpiAwareness=mock.Mock(return_value=result))
                with mock.patch("relay.display.sys.platform", "win32"), mock.patch("ctypes.WinDLL", side_effect=[user32, shcore], create=True):
                    self.assertEqual(enable_native_dpi(), result == 0)
                shcore.SetProcessDpiAwareness.assert_called_once_with(1)
                user32.SetProcessDPIAware.assert_not_called()

    def test_legacy_fallback_and_unsupported_platform(self):
        for supported in (True, False):
            with self.subTest(supported=supported):
                user32 = SimpleNamespace(IsProcessDPIAware=mock.Mock(return_value=0))
                if supported:
                    user32.SetProcessDPIAware = mock.Mock(return_value=1)
                with mock.patch("relay.display.sys.platform", "win32"), mock.patch("ctypes.WinDLL", side_effect=[user32, OSError()], create=True):
                    self.assertEqual(enable_native_dpi(), supported)

    @unittest.skipUnless(sys.platform == "win32", "Native Windows DPI check")
    def test_fresh_process_initializes_dpi_before_tk(self):
        child = subprocess.run([sys.executable, "-m", "tests.test_display", "--native-probe"],
                               cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
                               timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True)
        report = json.loads(child.stdout)
        self.assertTrue(report["enabled"])
        self.assertIn(report["after"]["awareness"], (1, 2))
        if report["before"]["awareness"]:
            self.assertEqual(report["after"]["awareness"], report["before"]["awareness"])
        else:
            self.assertEqual(report["after"]["awareness"], 1)
        self.assertAlmostEqual(report["tk_dpi"], report["after"]["dpi"], delta=1)
        self.assertEqual(report["window_state"], "withdrawn")
        print(json.dumps(report))


def native_probe():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    def snapshot():
        return {"awareness": user32.GetAwarenessFromDpiAwarenessContext(user32.GetThreadDpiAwarenessContext()),
                "dpi": user32.GetDpiForSystem(), "screen": [user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)]}
    before = snapshot()
    enabled = enable_native_dpi()
    after = snapshot()
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    root.attributes("-alpha", 0.0)
    try:
        print(json.dumps({"before": before, "after": after, "enabled": enabled,
                          "tk_dpi": root.winfo_fpixels("1i"), "tk_scaling": float(root.tk.call("tk", "scaling")),
                          "tk_version": str(root.tk.call("info", "patchlevel")), "window_state": root.state()}))
    finally:
        root.destroy()


if __name__ == "__main__":
    if sys.argv[1:] == ["--native-probe"]:
        native_probe()
    else:
        unittest.main()
