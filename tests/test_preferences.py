import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from relay.preferences import DEFAULTS, load_preferences, save_preferences


class PreferencesTests(unittest.TestCase):
    def test_only_display_choices_roundtrip_and_invalid_files_fall_back(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ui/preferences.json"
            self.assertEqual(load_preferences(path), DEFAULTS)
            self.assertFalse(path.parent.exists())
            chosen = {"theme": "mint", "font_size": "large", "density": "compact"}
            self.assertEqual(save_preferences(chosen, path), chosen)
            self.assertEqual(load_preferences(path), chosen)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), chosen)
            for invalid in (b"{", b"\xff", b"[]", b" " * 4097, b"[" * 1500 + b"]" * 1500):
                path.write_bytes(invalid)
                self.assertEqual(load_preferences(path), DEFAULTS)
            path.write_text(json.dumps({"theme": "mint", "font_size": "huge", "density": False,
                                        "auto_approve": True}), encoding="utf-8")
            self.assertEqual(load_preferences(path), {**DEFAULTS, "theme": "mint"})

    def test_invalid_save_or_failed_replace_preserves_previous_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            save_preferences(DEFAULTS, path)
            before = path.read_bytes()
            for invalid in ({}, {**DEFAULTS, "theme": "unknown"},
                            {**DEFAULTS, "auto_approve": True}, None):
                with self.assertRaises(ValueError):
                    save_preferences(invalid, path)
                self.assertEqual(path.read_bytes(), before)
            with mock.patch("relay.preferences.os.replace", side_effect=PermissionError("unwritable")):
                with self.assertRaises(PermissionError):
                    save_preferences({**DEFAULTS, "theme": "mint"}, path)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
