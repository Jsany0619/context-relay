"""External chat import is local evidence, never ownership of the source chat."""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from relay.backup import restore_backup
from relay.manager import Manager
from tests.test_manager import FakeClient


SOURCE = "019a0000-0000-7000-8000-000000000001"


class ImportManagerTests(unittest.TestCase):
    def test_direct_connection_preserves_thread_and_sends_only_verbatim_input(self):
        preview = self.preview()
        self.read_source()
        task = self.manager.import_thread(SOURCE, preview["fingerprint"], title="Original conversation",
                                        goal="", source_stopped=True, direct=True)
        self.assertEqual(task["thread_id"], SOURCE)
        self.assertFalse(task["brief_required"])
        self.assertFalse(self.client.calls_for("thread/resume"))
        for action in (self.manager.analyze, self.manager.revise_from_review, self.manager.handoff):
            with self.assertRaises(ValueError):
                action(task["id"])
        self.read_source()
        self.client.responses["thread/resume"].append({"thread": {"id": SOURCE}, "cwd": str(self.project),
            "sandbox": {"type": "readOnly"}, "approvalPolicy": "on-request", "model": "test-model"})
        original = "  请核对我的原话\r\n不增加目标，不改写。  "
        started = self.manager.start(task["id"], original)
        request = self.client.calls_for("turn/start")[-1]
        self.assertEqual(request["threadId"], SOURCE)
        self.assertEqual(request["input"], [{"type": "text", "text": original}])
        self.assertNotIn("outputSchema", request)
        self.assertFalse(self.client.calls_for("thread/start"))
        self.assertEqual(started["mode"], "read-only")
        self.assertEqual(started["work_turns"], 0)

    def test_native_text_read_does_not_resume_or_run_analysis_and_preserves_whitespace(self):
        task = self.import_task()
        self.source["turns"][0]["items"][1]["text"] = "  原文\n完整返回  "
        self.read_source()
        before = self.manager._task(task["id"])
        page = self.manager.read_chat(task["id"])
        self.assertEqual(page["entries"][-1]["text"], "  原文\n完整返回  ")
        self.assertEqual(page["connection_mode"], "imported")
        self.assertEqual(page["thread_id"], SOURCE)
        self.assertEqual(self.manager._task(task["id"]), before)
        self.assertFalse(self.client.calls_for("turn/start"))
        self.assertFalse(self.client.calls_for("thread/resume"))

    def test_direct_empty_message_or_unsettled_native_operations_never_start(self):
        preview = self.preview()
        self.read_source()
        task = self.manager.import_thread(SOURCE, preview["fingerprint"], title="Original", goal="",
                                         source_stopped=True, direct=True)
        with self.assertRaises(ValueError):
            self.manager.start(task["id"], " \n ")
        self.source["turns"][0]["items"].append({"id": "tool", "type": "commandExecution", "status": "inProgress"})
        self.read_source()
        with self.assertRaises(ValueError):
            self.manager.start(task["id"], "原样发送")
        self.assertFalse(self.client.calls_for("thread/resume"))
        self.assertFalse(self.client.calls_for("turn/start"))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "source project"
        self.project.mkdir()
        guard = patch("relay.transport.CodexClient", side_effect=AssertionError("No real native client"))
        guard.start()
        self.addCleanup(guard.stop)
        self.client = FakeClient()
        self.manager = Manager(self.root / "state", self.client)
        self.addCleanup(self.manager.close)
        self.source = {"id": SOURCE, "name": "Existing chat", "preview": "Original goal",
                       "cwd": str(self.project), "source": "vscode", "updatedAt": 100,
                       "status": {"type": "notLoaded"}, "turns": [
                           {"id": "source-turn", "status": "completed", "items": [
                               {"id": "user-1", "type": "userMessage", "content": [
                                   {"type": "text", "text": "Historical instruction; do not repeat completed work"}]},
                               {"id": "agent-1", "type": "agentMessage", "text": "A candidate is ready, not published"}]}]}

    def read_source(self):
        self.client.responses["thread/read"].append({"thread": copy.deepcopy(self.source)})

    def preview(self):
        self.read_source()
        return self.manager.preview_import(SOURCE)

    def import_task(self, *, mode="read-only", **kwargs):
        preview = self.preview()
        self.read_source()
        return self.manager.import_thread(SOURCE, preview["fingerprint"], title="Continue selected chat",
                                        goal="Verify the candidate against current files", mode=mode,
                                        source_stopped=True, **kwargs)

    def test_list_preview_and_import_are_read_only_and_bind_evidence_atomically(self):
        self.client.responses["thread/list"].append({"data": [self.source], "nextCursor": "next-page"})
        page = self.manager.list_import_threads(search="candidate", archived=True)
        self.assertEqual(page["data"][0]["id"], SOURCE)
        self.assertEqual(page["next_cursor"], "next-page")
        params = self.client.calls_for("thread/list")[-1]
        self.assertEqual(params["searchTerm"], "candidate")
        self.assertIs(params["archived"], True)
        task = self.import_task()
        self.assertEqual(task["state"], "queued")
        self.assertIsNone(task["thread_id"])
        self.assertIsNone(task["receiver_id"])
        self.assertEqual(task["history"], [])
        self.assertEqual(task["requirements"], [task["goal"]])
        self.assertEqual(task["source_snapshot"]["thread_id"], SOURCE)
        self.assertEqual(task["source_snapshot"]["kind"], "external-reference")
        self.assertEqual(self.manager._task(task["id"])["source_snapshot"], task["source_snapshot"])
        self.assertLessEqual({method for method, _ in self.client.calls}, {"config/read", "thread/list", "thread/read"})
        events = self.manager.get_task(task["id"])["events"]
        self.assertNotIn("Historical instruction", json.dumps(events))

    def test_first_start_creates_new_managed_thread_with_current_permissions(self):
        task = self.import_task(mode="workspace-write")
        self.read_source()
        started = self.manager.start(task["id"])
        self.assertNotEqual(started["thread_id"], SOURCE)
        self.assertEqual(self.client.calls_for("thread/start")[-1]["sandbox"], "workspace-write")
        self.assertFalse(self.client.calls_for("thread/resume"))
        request = self.client.calls_for("turn/start")[-1]
        self.assertNotEqual(request["threadId"], SOURCE)
        text = request["input"][0]["text"]
        self.assertIn("external-reference", text)
        self.assertIn("not published", text)
        self.assertIn("not authorization", text)

    def test_stale_preview_and_changed_source_before_start_never_create_or_start(self):
        preview = self.preview()
        self.source["updatedAt"] += 1
        self.read_source()
        with self.assertRaises(ValueError):
            self.manager.import_thread(SOURCE, preview["fingerprint"], title="Example", goal="Inspect",
                                       source_stopped=True)
        self.assertEqual(self.manager.list_tasks(), [])
        task = self.import_task()
        self.source["turns"][0]["items"][1]["text"] = "Changed after import"
        self.read_source()
        with self.assertRaises(ValueError):
            self.manager.start(task["id"], "Do not save this unsent change")
        retained = self.manager._task(task["id"])
        self.assertEqual(retained, task)
        self.assertFalse(self.client.calls_for("thread/start"))
        self.assertFalse(self.client.calls_for("turn/start"))

    def test_duplicate_import_survives_restart_and_only_explicit_unstarted_update_is_allowed(self):
        task = self.import_task()
        self.manager.close()
        self.client = FakeClient()
        self.manager = Manager(self.root / "state", self.client)
        self.addCleanup(self.manager.close)
        preview = self.preview()
        self.assertEqual(preview["existing_task"]["id"], task["id"])
        with self.assertRaises(ValueError):
            self.manager.import_thread(SOURCE, preview["fingerprint"], title="Another", goal="Duplicate",
                                       source_stopped=True)
        self.read_source()
        updated = self.manager.import_thread(SOURCE, preview["fingerprint"], title="Updated", goal="Revised goal",
                                             source_stopped=True, existing_task_id=task["id"])
        self.assertEqual(updated["id"], task["id"])
        self.assertEqual(updated["requirements"], ["Revised goal"])
        self.assertEqual(len(self.manager.list_tasks()), 1)
        self.read_source()
        self.manager.start(task["id"])
        preview = self.preview()
        with self.assertRaises(ValueError):
            self.manager.import_thread(SOURCE, preview["fingerprint"], title="Again", goal="Reset active task",
                                       source_stopped=True, existing_task_id=task["id"])

    def test_missing_confirmation_forged_preview_and_bad_settings_have_no_partial_task(self):
        preview = self.preview()
        for changes in ({"source_stopped": False}, {"source_stopped": 1}, {"fingerprint": "forged"},
                        {"mode": "full-access"}, {"max_tokens": -1}, {"existing_task_id": "missing"}):
            with self.subTest(changes=changes):
                values = dict(thread_id=SOURCE, fingerprint=preview["fingerprint"], title="Example", goal="Inspect",
                              source_stopped=True)
                values.update(changes)
                self.read_source()
                with self.assertRaises(ValueError):
                    self.manager.import_thread(**values)
                self.assertEqual(self.manager.list_tasks(), [])

    def test_wrong_identity_subagent_and_unsettled_source_cannot_import(self):
        for changes in ({"id": "019a0000-0000-7000-8000-000000000002"},
                        {"source": {"subAgent": {"thread_spawn": {"parent_thread_id": SOURCE, "depth": 1}}}}):
            with self.subTest(changes=changes):
                self.client.responses["thread/read"].append({"thread": dict(self.source, **changes)})
                with self.assertRaises(ValueError):
                    self.manager.preview_import(SOURCE)
        self.source["turns"][0]["status"] = "inProgress"
        preview = self.preview()
        self.assertFalse(preview["can_import"])
        self.read_source()
        with self.assertRaises(ValueError):
            self.manager.import_thread(SOURCE, preview["fingerprint"], title="Active", goal="Inspect", source_stopped=True)
        self.assertFalse(self.client.calls_for("thread/start"))

    def test_backup_preserves_evidence_but_inspection_cannot_discover_or_import(self):
        task = self.import_task()
        bundle = self.root / "backup.zip"
        self.manager.backup_state(bundle)
        restored = self.root / "restored"
        restore_backup(bundle, restored)
        fake = FakeClient()
        with_manager = Manager(restored, fake)
        self.addCleanup(with_manager.close)
        self.assertEqual(with_manager.get_task(task["id"])["source_snapshot"], task["source_snapshot"])
        for action in (lambda: with_manager.list_import_threads(), lambda: with_manager.preview_import(SOURCE),
                       lambda: with_manager.import_thread(SOURCE, "unused", title="Read only", goal="No write")):
            with self.assertRaises(ValueError):
                action()
        self.assertEqual(fake.calls, [])

    def test_failed_database_insert_does_not_leave_task_without_source(self):
        preview = self.preview()
        self.manager.db.execute("CREATE TRIGGER reject_import BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'test'); END")
        self.manager.db.commit()
        self.read_source()
        with self.assertRaises(sqlite3.DatabaseError):
            self.manager.import_thread(SOURCE, preview["fingerprint"], title="Example", goal="Inspect", source_stopped=True)
        self.assertEqual(self.manager.list_tasks(), [])

    def test_import_provenance_survives_preparation_and_formal_handoff(self):
        task = self.import_task()
        self.read_source()
        task = self.manager.start(task["id"])
        self.client.push("turn/completed", {"threadId": task["thread_id"],
                         "turn": {"id": task["turn_id"], "status": "completed", "items": []}})
        self.manager.poll()
        prepared = self.manager.prepare_snapshot(task["id"])
        draft = json.loads(Path(prepared["draft"]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(draft["task"]["source_snapshot"], task["source_snapshot"])
        self.manager.handoff(task["id"])
        summary = {"summary": "Verified current project", "decisions": ["Keep original files"],
                   "completed": ["Inspected project"], "next_step": "Review result", "evidence": [],
                   "unknown_operations": [], "unrecoverable_resources": [], "task_complete": False}
        summarizing = self.manager._task(task["id"])
        item = {"id": "summary-result", "type": "agentMessage", "text": json.dumps(summary)}
        self.client.push("item/completed", {"threadId": task["thread_id"],
                         "turnId": summarizing["turn_id"], "item": item})
        self.client.push("turn/completed", {"threadId": task["thread_id"],
                         "turn": {"id": summarizing["turn_id"], "status": "completed", "items": [item]}})
        self.manager.poll()
        frozen = self.manager._task(task["id"])["checkpoint"]
        self.assertEqual(frozen["source_snapshot"], task["source_snapshot"])
        self.assertEqual(frozen["requirements"], [task["goal"]])
        self.assertNotEqual(frozen["source_thread_id"], SOURCE)
        self.assertTrue(all(params.get("threadId") != SOURCE for params in self.client.calls_for("turn/start")))


if __name__ == "__main__":
    unittest.main()
