"""Backup and inspection-mode integration; no native Codex calls."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from relay.__main__ import main
from relay.manager import Manager
from tests.test_manager import FakeClient


class BackupManagerTests(unittest.TestCase):
    def setUp(self):
        native_guard = patch("relay.transport.CodexClient", side_effect=AssertionError("Native calls are forbidden in this suite"))
        native_guard.start()
        self.addCleanup(native_guard.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.client = FakeClient()
        self.manager = Manager(self.root / "state", self.client)
        self.addCleanup(self.manager.close)
        self.task = self.manager.create_task("Example", self.project, "Inspect original requirements", direct=False)

    def inspection_copy(self):
        destination = self.root / "inspection"
        destination.mkdir()
        with contextlib.closing(sqlite3.connect(destination / "tasks.sqlite3")) as db, db:
            self.manager.db.backup(db)
            db.execute("CREATE TABLE recovery (data TEXT NOT NULL)")
            db.execute("INSERT INTO recovery VALUES (?)", (json.dumps({
                "mode": "inspection", "source_state_dir": str(self.manager.root),
                "backup_created_at": "2026-09-30T00:00:00Z", "restored_at": "2026-09-30T01:00:00Z"}),))
        return destination

    def test_inspection_preserves_historical_running_and_unknown_state_without_calls(self):
        task = self.manager._task(self.task["id"])
        task.update(state="running", thread_id="old-source", turn_id="old-turn", receiver_id="old-receiver",
                    generation=2, intent={"kind": "turn", "request_id": 8}, run_started=0,
                    pending=[{"id": 7}], inflight={"operation": "unknown"},
                    checkpoint={"ready": True}, checkpoint_hash="old", checkpoint_path="historical-only.json")
        self.manager._save(task, "sample_history")
        before = self.manager.get_task(task["id"])
        destination = self.inspection_copy()
        with patch("relay.transport.CodexClient") as native:
            recovered = Manager(destination)
            try:
                self.assertEqual(recovered.recovery_info["mode"], "inspection")
                self.assertEqual(recovered.get_task(task["id"]), before)
                recovered.poll()
                for operation in (
                    lambda: recovered.start(task["id"]), lambda: recovered.pause(task["id"]),
                    lambda: recovered.reconcile(task["id"]), lambda: recovered.handoff(task["id"]),
                    lambda: recovered.prepare_snapshot(task["id"]), lambda: recovered.finish(task["id"]),
                    lambda: recovered.answer(task["id"], 7, {"decision": "accept"}),
                    lambda: recovered.set_archived(task["id"], False),
                    lambda: recovered.update_settings(task["id"], title="changed", max_tokens=0,
                                                       max_minutes=0, auto_handoff=False),
                    lambda: recovered.create_task("New", self.project, "Must not run", direct=False),
                    lambda: recovered.backup_state(self.root / "nested.zip"),
                ):
                    with self.assertRaisesRegex(ValueError, "只读"):
                        operation()
                with self.assertRaises(sqlite3.OperationalError):
                    recovered.db.execute("DELETE FROM tasks")
                exported = self.root / "export.json"
                recovered.export_task(task["id"], exported)
                self.assertEqual(json.loads(exported.read_text(encoding="utf-8")), before)
                markdown = self.root / "historical.md"
                recovered.export_task(task["id"], markdown)
                self.assertIn("备份历史快照", markdown.read_text(encoding="utf-8"))
                diagnostic = recovered.diagnostics()
                self.assertTrue(diagnostic["inspection_only"])
                self.assertEqual("inspection_only", diagnostic["tasks"][0]["next_action"])
                self.assertEqual(0, diagnostic["tasks"][0]["budget"]["elapsed_minutes"])
                for folder in (destination, self.manager.root, self.project):
                    with self.assertRaisesRegex(ValueError, "只读"):
                        recovered.export_task(task["id"], folder / "export.json")
                    with self.assertRaisesRegex(ValueError, "只读"):
                        recovered.export_task(task["id"], folder / "historical.md")
                    with self.assertRaisesRegex(ValueError, "只读"):
                        recovered.export_diagnostics(folder / "diagnostics.json")
                self.assertEqual(recovered.get_task(task["id"]), before)
            finally:
                recovered.close()
            reopened = Manager(destination)
            try:
                self.assertEqual(reopened.get_task(task["id"]), before)
            finally:
                reopened.close()
            native.assert_not_called()

    def test_inspection_does_not_prevent_original_owner_from_working(self):
        destination = self.inspection_copy()
        inspection_client = FakeClient()
        recovered = Manager(destination, inspection_client)
        self.addCleanup(recovered.close)
        with self.assertRaisesRegex(ValueError, "只读"):
            recovered.start(self.task["id"])
        self.manager.start(self.task["id"])
        self.assertEqual(1, len(self.client.calls_for("thread/start")))
        self.assertEqual(1, len(self.client.calls_for("turn/start")))
        self.assertEqual(recovered.get_task(self.task["id"])["state"], "queued")
        recovered.close()
        self.assertTrue(inspection_client.closed)
        self.assertEqual(inspection_client.calls, [])

    def test_backup_refuses_busy_pending_and_project_or_state_paths(self):
        original = self.manager._task(self.task["id"])
        for changes in ({"state": "running"}, {"pending": [{"id": 1}]}):
            self.manager._save(dict(original, **changes))
            with self.assertRaises(ValueError):
                self.manager.backup_state(self.root / "backup.zip")
        self.manager._save(original)
        for destination in (self.project / "backup.zip", self.manager.root / "backup.zip"):
            with self.assertRaises(ValueError):
                self.manager.backup_state(destination)
            self.assertFalse(destination.exists())
        self.assertEqual([], self.client.calls)

    def test_backup_restore_cli_roundtrip_is_offline_and_requires_new_explicit_destination(self):
        backup = self.root / "backup.zip"
        report = self.manager.backup_state(backup)
        self.assertEqual(report["task_count"], 1)
        self.assertEqual([], self.client.calls)
        with patch("relay.transport.CodexClient") as native, patch("relay.ui.main") as launch:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["--inspect-backup", str(backup)]), 0)
            self.assertEqual(json.loads(output.getvalue())["task_count"], 1)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(["--restore-backup", str(backup)])
            target = self.root / "recovered's copy #1"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["--restore-backup", str(backup), "--state-dir", str(target)]), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["mode"], "inspection")
            self.assertTrue(result["open_command"].startswith("& '"))
            self.assertIn("recovered''s copy", result["open_command"])
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["--restore-backup", str(backup), "--state-dir", str(target)]), 1)
            launch.assert_not_called()
            native.assert_not_called()
        recovered = Manager(target)
        self.addCleanup(recovered.close)
        self.assertEqual(recovered.get_task(self.task["id"]), self.manager.get_task(self.task["id"]))

    def test_restore_publication_failure_cannot_leave_partial_or_overwrite_racing_target(self):
        from relay.backup import restore_backup
        backup = self.root / "backup.zip"
        self.manager.backup_state(backup)
        target = self.root / "restore-target"
        with patch("relay.backup.os.rename", side_effect=OSError("simulated publish failure")):
            with self.assertRaises(ValueError):
                restore_backup(backup, target)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.glob(".restore-target.restore-*")), [])

        rename = os.rename
        def competing_directory(stage, destination):
            destination.mkdir()
            (destination / "keep.txt").write_text("Keep competing data", encoding="utf-8")
            rename(stage, destination)

        with patch("relay.backup.os.rename", side_effect=competing_directory):
            with self.assertRaises(ValueError):
                restore_backup(backup, target)
        self.assertEqual((target / "keep.txt").read_text(encoding="utf-8"), "Keep competing data")
        self.assertFalse((target / "tasks.sqlite3").exists())
        self.assertEqual(list(self.root.glob(".restore-target.restore-*")), [])
        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
