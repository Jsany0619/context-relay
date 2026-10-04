"""Preliminary snapshots must not confer READY or reuse stale final evidence."""
import json
from pathlib import Path
import tempfile
import unittest

from relay.manager import Manager
from tests.test_manager import FakeClient


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.evidence = self.project / "adopted.txt"
        self.evidence.write_text("adopted version", encoding="utf-8")
        self.client = FakeClient()
        self.manager = Manager(self.root / "state", self.client)
        self.addCleanup(self.manager.close)

    def start(self, automatic=True):
        task = self.manager.create_task("Example", self.project, "Inspect adopted.txt", auto_handoff=automatic, direct=False)
        return self.manager.start(task["id"])

    def usage(self, task, last):
        self.client.push("thread/tokenUsage/updated", {
            "threadId": task["thread_id"], "turnId": task["turn_id"],
            "tokenUsage": {"last": {"totalTokens": last}, "total": {"totalTokens": 12345},
                           "modelContextWindow": 1000}})
        self.manager.poll()

    def complete(self, task):
        self.client.push("turn/completed", {"threadId": task["thread_id"],
            "turn": {"id": task["turn_id"], "status": "completed", "items": []}})
        self.manager.poll()
        return self.manager.get_task(task["id"])

    def test_seventy_percent_saves_provisional_packet_without_inference(self):
        task = self.start()
        self.usage(task, 700)
        self.assertIsNone(self.manager.get_task(task["id"]).get("draft"))
        saved = self.complete(task)
        self.assertIsNotNone(saved.get("draft"))
        packet = json.loads(Path(saved["draft"]["path"]).read_text(encoding="utf-8"))
        self.assertEqual("preparatory", packet["kind"])
        self.assertIs(False, packet["ready"])
        self.assertEqual(["Inspect adopted.txt"], packet["task"]["requirements"])
        self.assertIn("adopted.txt", packet["files"])
        self.assertTrue(packet["unverified"])
        self.assertIsNone(saved["checkpoint"])
        self.assertEqual(1, len(self.client.calls_for("turn/start")))
        self.assertEqual(1, len(self.client.calls_for("thread/start")))
        self.assertEqual(0, saved["generation"])

    def test_below_threshold_unknown_and_opt_out_do_not_auto_snapshot(self):
        for automatic, last in ((True, 699), (True, None), (False, 750)):
            with self.subTest(automatic=automatic, last=last):
                task = self.start(automatic)
                if last is not None:
                    self.usage(task, last)
                self.assertIsNone(self.complete(task).get("draft"))

    def test_compaction_invalidates_old_usage_before_snapshot(self):
        task = self.start()
        self.usage(task, 750)
        self.client.push("item/completed", {"threadId": task["thread_id"], "turnId": task["turn_id"],
            "item": {"id": "compact-1", "type": "contextCompaction"}})
        self.manager.poll()
        self.assertIsNone(self.complete(task).get("draft"))

    def test_manual_snapshot_requires_safe_state_and_keeps_latest_file(self):
        task = self.start(False)
        prepare = getattr(self.manager, "prepare_snapshot", None)
        self.assertTrue(callable(prepare), "Manual snapshot action is missing")
        with self.assertRaises(ValueError):
            prepare(task["id"])
        self.complete(task)
        first = prepare(task["id"])
        self.evidence.write_text("new adopted version", encoding="utf-8")
        second = prepare(task["id"])
        self.assertEqual(first["draft"]["path"], second["draft"]["path"])
        self.assertNotEqual(first["draft"]["hash"], second["draft"]["hash"])
        self.assertIsNone(second["checkpoint"])
        held = self.manager._task(task["id"])
        held["intent"] = {"kind": "turn"}
        self.manager._save(held)
        with self.assertRaises(ValueError):
            prepare(task["id"])

    def test_changed_files_after_draft_get_new_final_evidence(self):
        task = self.start()
        self.usage(task, 700)
        saved = self.complete(task)
        self.assertIsNotNone(saved.get("draft"))
        draft = json.loads(Path(saved["draft"]["path"]).read_text(encoding="utf-8"))
        self.evidence.write_text("changed after preparation", encoding="utf-8")
        self.manager.handoff(task["id"])
        summary_turn = self.client.calls_for("turn/start")[-1]
        summary = {"summary": "Reviewed current adopted version", "next_step": "Check the changed artifact",
            "decisions": [], "completed": ["Reviewed the document"],
            "evidence": [{"path": "adopted.txt", "description": "Current adopted artifact"}],
            "unknown_operations": [], "unrecoverable_resources": [], "task_complete": False}
        self.client.push("item/completed", {"threadId": task["thread_id"],
            "turnId": f"turn-{self.client.turn_count}", "item": {"id": "summary", "type": "agentMessage",
                                                                  "text": json.dumps(summary)}})
        self.client.push("turn/completed", {"threadId": summary_turn["threadId"],
            "turn": {"id": f"turn-{self.client.turn_count}", "status": "completed", "items": []}})
        self.manager.poll()
        verifying = self.manager.get_task(task["id"])
        self.assertEqual("verifying", verifying["state"])
        self.assertNotEqual(draft["files"], verifying["checkpoint"]["files"])
        self.assertEqual(0, verifying["generation"])
        self.assertEqual(task["thread_id"], verifying["thread_id"])

    def test_project_must_not_contain_or_live_inside_manager_state(self):
        nested = self.manager.root / "drafts"
        nested.mkdir()
        for directory in (self.root, nested):
            with self.subTest(directory=directory), self.assertRaises(ValueError):
                self.manager.create_task("Overlap", directory, "Read files only", direct=False)

    def test_legacy_overlap_cannot_write_snapshot_into_read_only_project(self):
        task = self.start(False)
        self.complete(task)
        nested = self.manager.root / "drafts"
        nested.mkdir()
        legacy = self.manager._task(task["id"])
        legacy["cwd"] = str(nested)
        self.manager._save(legacy)
        with self.assertRaises(ValueError):
            self.manager.prepare_snapshot(task["id"])
        self.assertEqual([], list(nested.iterdir()))


if __name__ == "__main__":
    unittest.main()
