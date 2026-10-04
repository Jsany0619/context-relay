"""Local organization preserves execution state and never contacts Codex."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from relay.manager import Manager
from tests.test_manager import FakeClient


class OrganizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.client = FakeClient()
        self.manager = Manager(self.root / "state", self.client)
        self.addCleanup(lambda: self.manager.close())
        self.task = self.manager.create_task("Example", str(self.project), "Inspect only")

    def test_archive_restore_preserves_execution_and_artifacts_across_restart(self):
        artifact = self.project / "adopted.txt"
        artifact.write_text("Keep this adopted version", encoding="utf-8")
        task = self.manager._task(self.task["id"])
        task.update(thread_id="source", generation=3, checkpoint="retained-checkpoint",
                    checkpoint_hash="unchanged", last_message="Accepted result")
        self.manager._save(task)
        completed = self.manager.finish(task["id"])
        archived = self.manager.set_archived(task["id"], True)
        for key in completed.keys() - {"archived", "updated_at"}:
            self.assertEqual(archived[key], completed[key], key)
        self.assertTrue(archived["archived"])
        self.assertEqual(len(self.manager.list_tasks()), 1)
        self.manager.close()
        self.manager = Manager(self.root / "state", self.client)
        self.assertTrue(self.manager.get_task(task["id"])["archived"])
        restored = self.manager.set_archived(task["id"], False)
        self.assertFalse(restored["archived"])
        self.assertEqual(restored["state"], "completed")
        self.assertEqual(restored["thread_id"], "source")
        self.assertEqual(restored["generation"], 3)
        self.assertEqual(artifact.read_text(encoding="utf-8"), "Keep this adopted version")
        self.assertEqual(self.client.calls, [])

    def test_archive_refuses_unfinished_or_unsettled_tasks(self):
        base = self.manager._task(self.task["id"])
        for state in ("queued", "idle", "paused", "running", "pausing", "creating",
                      "summarizing", "verifying", "needs_reconcile", "blocked"):
            with self.subTest(state=state):
                task = dict(base, state=state)
                self.manager._save(task)
                with self.assertRaises(ValueError):
                    self.manager.set_archived(task["id"], True)
                self.assertFalse(self.manager.get_task(task["id"]).get("archived"))
        for key, value in (("inflight", {"tool": "commandExecution"}),
                           ("intent", {"kind": "turn"}), ("pending", [{"id": 9}]),
                           ("receiver_id", "unverified"), ("run_started", 1)):
            with self.subTest(unsettled=key):
                task = dict(base, state="completed", **{key: value})
                self.manager._save(task)
                with self.assertRaises(ValueError):
                    self.manager.set_archived(task["id"], True)
        self.manager._save(base)
        self.assertEqual(self.client.calls, [])

    def test_archived_task_cannot_reconcile_or_start_but_can_be_read_and_exported(self):
        task_id = self.task["id"]
        self.manager.finish(task_id)
        self.manager.set_archived(task_id, True)
        before = self.manager.get_task(task_id)
        for method in (self.manager.start, self.manager.reconcile):
            with self.assertRaises(ValueError):
                method(task_id)
        self.assertEqual(self.manager.get_task(task_id), before)
        destination = self.root / "export.json"
        self.manager.export_task(task_id, destination)
        self.assertEqual(json.loads(destination.read_text(encoding="utf-8")), before)
        self.assertEqual(self.client.calls, [])

    def test_archive_is_idempotent_and_rejects_non_boolean_values(self):
        task_id = self.task["id"]
        self.manager.finish(task_id)
        for value in ("false", 0, 1, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.manager.set_archived(task_id, value)
        self.manager.set_archived(task_id, True)
        once = self.manager.get_task(task_id)
        self.manager.set_archived(task_id, True)
        self.assertEqual(self.manager.get_task(task_id), once)
        self.manager.set_archived(task_id, False)
        restored = self.manager.get_task(task_id)
        self.manager.set_archived(task_id, False)
        self.assertEqual(self.manager.get_task(task_id), restored)
        kinds = [event["kind"] for event in restored["events"]]
        self.assertEqual(kinds[:2], ["task_unarchived", "task_archived"])

    def test_legacy_task_can_be_organized_without_changing_permissions(self):
        task = self.manager._task(self.task["id"])
        task.pop("archived", None)
        self.manager._save(task)
        self.manager.set_archived(task["id"], False)
        self.assertFalse(self.manager.get_task(task["id"]).get("archived"))
        self.manager.finish(task["id"])
        self.manager.set_archived(task["id"], True)
        self.assertEqual(self.manager.get_task(task["id"])["mode"], "read-only")

    def test_existing_history_remains_bounded_ordered_and_read_only(self):
        task = self.manager._task(self.task["id"])
        for index in range(105):
            self.manager._save(task, "turn_completed", {"sequence": index, "status": "completed"})
        before = copy.deepcopy(self.manager.list_tasks())
        events = self.manager.get_task(task["id"])["events"]
        self.assertEqual(len(events), 100)
        self.assertEqual([event["data"]["sequence"] for event in events], list(range(104, 4, -1)))
        self.assertEqual(self.manager.list_tasks(), before)
        self.assertEqual(self.client.calls, [])

    def test_reopen_completed_task_preserves_execution_identity_without_starting_work(self):
        task = self.manager._task(self.task["id"])
        task.update(thread_id="source", generation=4, mode="workspace-write", usage=321,
                    history=[{"thread_id": "old", "generation": 3}],
                    permission_receipt={"sandbox": {"type": "workspaceWrite"}})
        self.manager._save(task)
        completed = self.manager.finish(task["id"])
        before_calls = copy.deepcopy(self.client.calls)

        reopened = self.manager.reopen_task(task["id"])

        self.assertEqual("paused", reopened["state"])
        self.assertIn("等待明确", reopened["error"])
        for key in ("thread_id", "generation", "mode", "usage", "history", "permission_receipt"):
            self.assertEqual(completed[key], reopened[key], key)
        self.assertEqual(before_calls, self.client.calls)
        saved = self.manager.get_task(task["id"])
        self.assertEqual("task_reopened", saved["events"][0]["kind"])

    def test_reopen_refuses_noncompleted_archived_inspection_or_unsettled_task(self):
        base = self.manager._task(self.task["id"])
        for state in ("queued", "idle", "paused", "running", "needs_reconcile"):
            with self.subTest(state=state):
                self.manager._save(dict(base, state=state))
                with self.assertRaises(ValueError):
                    self.manager.reopen_task(base["id"])
        for key, value in (("pending", [{"id": 1}]), ("inflight", {"op": {}}),
                           ("intent", {"kind": "turn"}), ("receiver_id", "receiver"),
                           ("analysis_thread_id", "analysis"), ("run_started", 0)):
            with self.subTest(unsettled=key):
                self.manager._save(dict(base, state="completed", **{key: value}))
                with self.assertRaises(ValueError):
                    self.manager.reopen_task(base["id"])
        self.manager._save(dict(base, state="completed", archived=True))
        with self.assertRaises(ValueError):
            self.manager.reopen_task(base["id"])
        self.manager._save(dict(base, state="completed"))
        self.manager.recovery_info = {"mode": "inspection"}
        with self.assertRaises(ValueError):
            self.manager.reopen_task(base["id"])
        self.manager.recovery_info = None
        self.assertEqual([], self.client.calls)


if __name__ == "__main__":
    unittest.main()
