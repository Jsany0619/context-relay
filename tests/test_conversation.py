"""Persisted phone conversation behavior; only a synthetic native client is used."""
import tempfile
from pathlib import Path
import unittest

from relay.manager import Manager
from tests.test_manager import FakeClient


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.client = FakeClient()
        self.manager = Manager(self.root / "state", self.client)
        self.addCleanup(self.manager.close)
        self.task = self.manager.create_task("Phone", self.project, "Inspect only")

    def start(self, message=None):
        task = self.manager.start(self.task["id"], message)
        self.manager.poll()
        return task

    def answer(self, task, text, item_id="answer"):
        self.client.push("item/completed", {"threadId": task["thread_id"], "turnId": task["turn_id"],
            "item": {"id": item_id, "type": "agentMessage", "text": text}})
        self.manager.poll()

    def complete(self, task):
        self.client.push("turn/completed", {"threadId": task["thread_id"],
            "turn": {"id": task["turn_id"], "status": "completed", "items": []}})
        self.manager.poll()

    def test_previous_result_remains_visible_during_next_turn_and_restart(self):
        first = self.start()
        self.answer(first, "First result")
        self.complete(first)
        second = self.start("Explain this result")
        texts = [m["text"] for m in second["messages"]]
        self.assertEqual(texts, ["Inspect only", "First result", "Explain this result"])
        self.manager.close()
        restored = Manager(self.root / "state", FakeClient())
        self.addCleanup(restored.close)
        self.assertEqual([m["text"] for m in restored.get_task(first["id"])["messages"]], texts)

    def test_duplicate_completed_notification_is_one_message(self):
        task = self.start()
        self.answer(task, "Answer")
        self.answer(task, "Answer")
        messages = self.manager.get_task(task["id"])["messages"]
        self.assertEqual([m["text"] for m in messages], ["Inspect only", "Answer"])

    def test_incomplete_secret_chunks_never_publish(self):
        task = self.start()
        for delta in ("Authorization: Bearer ", "private-value"):
            self.client.push("item/agentMessage/delta", {"threadId": task["thread_id"],
                "turnId": task["turn_id"], "delta": delta})
            self.manager.poll()
        messages = self.manager.list_tasks()[0]["messages"]
        self.assertEqual([m["text"] for m in messages], ["Inspect only"])

    def test_reconcile_deduplicates_completed_message(self):
        task = self.start()
        self.answer(task, "Recovered result")
        saved = self.manager._task(task["id"])
        saved["state"] = "needs_reconcile"
        self.manager._save(saved)
        self.client.responses["thread/read"].append({"thread": {"id": task["thread_id"],
            "status": {"type": "idle"}, "turns": [{"id": task["turn_id"], "status": "completed", "items": [
                {"id": "answer", "type": "agentMessage", "text": "Recovered result"}]}]}})
        restored = self.manager.reconcile(task["id"])
        self.assertEqual([m["text"] for m in restored["messages"]].count("Recovered result"), 1)

    def test_analysis_json_does_not_replace_work_conversation(self):
        task = self.start()
        saved = self.manager._task(task["id"])
        saved.update(purpose="brief", analysis_thread_id=task["thread_id"], state="briefing")
        self.manager._save(saved)
        self.answer(task, '{"internal":"brief candidate"}')
        self.assertEqual([m["text"] for m in self.manager.get_task(task["id"])["messages"]], ["Inspect only"])

    def test_import_history_is_visible_without_executing(self):
        task = self.manager._task(self.task["id"])
        task.pop("messages", None)
        task["source_snapshot"] = {"messages": [{"role": "assistant", "text": "Old draft", "item_id": "old", "turn_id": "prior"}],
            "omitted_messages": 3, "truncated_messages": 0}
        self.manager._save(task)
        visible = self.manager.get_task(task["id"])
        self.assertTrue(any(m["text"] == "Old draft" and m.get("historical") for m in visible["messages"]))
        self.assertTrue(visible["messages_truncated"])
        self.assertEqual(len(self.client.calls_for("turn/start")), 0)

    def test_legacy_projection_is_stable_so_phone_etag_can_be_used(self):
        task = self.manager._task(self.task["id"])
        task.pop("messages")
        task["last_message"] = "Legacy current result"
        self.manager._save(task)
        first = self.manager.list_tasks()[0]
        second = self.manager.list_tasks()[0]
        self.assertEqual(first, second)
        self.assertEqual([m["text"] for m in first["messages"]], ["Inspect only", "Legacy current result"])


if __name__ == "__main__":
    unittest.main()
