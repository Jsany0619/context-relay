"""Recovery regressions using local fake sessions, never real inference."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from relay.manager import Manager
from relay.transport import RequestTimeout
from tests.test_manager import FakeClient


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.client = FakeClient()
        self.manager = Manager(self.root / "state", self.client)
        self.addCleanup(self.manager.close)

    def started_task(self, cwd=None, **options):
        task = self.manager.create_task("Recovery", str(cwd or self.project),
                                        "Inspect the existing sample without repeating completed actions.",
                                        mode="workspace-write", **options)
        self.manager.start(task["id"])
        self.manager.poll()
        return self.manager.get_task(task["id"])

    def interrupted_without_receipt(self, task):
        self.client.responses["turn/interrupt"].append(RequestTimeout("turn/interrupt", 90, 0.01))
        with self.assertRaises(RequestTimeout):
            self.manager.pause(task["id"])

    def host_turn(self, task, status="completed", items=None, thread_id=None):
        self.client.responses["thread/read"].append({"thread": {
            "id": thread_id or task["thread_id"], "status": {"type": "idle"},
            "turns": [{"id": task["turn_id"], "status": status, "items": items or []}],
        }})

    def operation_started(self, task, operation_id="op-1"):
        self.client.push("item/started", {
            "threadId": task["thread_id"], "turnId": task["turn_id"],
            "item": {"id": operation_id, "type": "commandExecution", "status": "inProgress",
                     "command": "simulated operation only"},
        })
        self.manager.poll()

    def finish_turn(self, task, payload=None):
        items = []
        if payload is not None:
            items = [{"id": "message-" + task["turn_id"], "type": "agentMessage", "text": json.dumps(payload)}]
            self.client.push("item/completed", {
                "threadId": task["thread_id"], "turnId": task["turn_id"], "item": items[0],
            })
        self.client.push("turn/completed", {
            "threadId": task["thread_id"],
            "turn": {"id": task["turn_id"], "status": "completed", "items": items},
        })
        self.manager.poll()

    def test_missing_known_turn_cannot_be_reconciled_from_idle_alone(self):
        task = self.started_task()
        self.interrupted_without_receipt(task)
        before = len(self.client.calls_for("turn/start"))
        with self.assertRaises((ValueError, RuntimeError)):
            self.manager.reconcile(task["id"])
        held = self.manager.get_task(task["id"])
        self.assertEqual(task["turn_id"], held["turn_id"])
        self.assertEqual(task["thread_id"], held["thread_id"])
        self.assertEqual(before, len(self.client.calls_for("turn/start")))

    def test_matching_known_turn_must_have_a_recognized_terminal_status(self):
        task = self.started_task()
        self.interrupted_without_receipt(task)
        for status in ("inProgress", "unknown"):
            with self.subTest(status=status):
                self.host_turn(task, status=status)
                with self.assertRaises((ValueError, RuntimeError)):
                    self.manager.reconcile(task["id"])
                self.assertEqual(task["turn_id"], self.manager.get_task(task["id"])["turn_id"])

    def test_inflight_clears_only_after_same_operation_has_terminal_receipt(self):
        task = self.started_task()
        self.operation_started(task)
        self.interrupted_without_receipt(task)
        self.host_turn(task, items=[{"id": "other-operation", "type": "commandExecution", "status": "completed"}])
        with self.assertRaises((ValueError, RuntimeError)):
            self.manager.reconcile(task["id"])
        self.assertIn("op-1", self.manager.get_task(task["id"])["inflight"])
        self.host_turn(task, items=[{"id": "op-1", "type": "commandExecution", "status": "completed"}])
        recovered = self.manager.reconcile(task["id"])
        self.assertEqual("paused", recovered["state"])
        self.assertEqual({}, recovered["inflight"])
        self.assertEqual(task["thread_id"], recovered["thread_id"])

    def test_failed_or_interrupted_terminal_turn_recovers_without_replay(self):
        for status in ("failed", "interrupted"):
            with self.subTest(status=status):
                directory = self.project / status
                directory.mkdir()
                task = self.started_task(cwd=directory)
                self.operation_started(task)
                self.interrupted_without_receipt(task)
                self.host_turn(task, status=status,
                               items=[{"id": "op-1", "type": "commandExecution", "status": "completed"}])
                before = len(self.client.calls_for("turn/start"))
                recovered = self.manager.reconcile(task["id"])
                self.assertEqual("paused", recovered["state"])
                self.assertEqual(task["thread_id"], recovered["thread_id"])
                self.assertEqual(task["generation"], recovered["generation"])
                self.assertEqual(0, recovered["work_turns"])
                self.assertIn("执行失败" if status == "failed" else "已中断", recovered["error"])
                self.assertIn("没有自动继续", recovered["error"])
                self.assertEqual(before, len(self.client.calls_for("turn/start")))
                self.manager.start(task["id"])
                self.assertEqual(before + 1, len(self.client.calls_for("turn/start")))
                self.assertEqual(task["thread_id"], self.client.calls_for("turn/start")[-1]["threadId"])

    def test_terminal_turn_with_unfinished_reported_tool_remains_blocked(self):
        task = self.started_task()
        self.interrupted_without_receipt(task)
        self.host_turn(task, items=[{"id": "unseen-operation", "type": "commandExecution", "status": "inProgress"}])
        with self.assertRaises((ValueError, RuntimeError)):
            self.manager.reconcile(task["id"])
        self.assertEqual(task["turn_id"], self.manager.get_task(task["id"])["turn_id"])

    def test_recovery_settles_elapsed_once_and_paused_time_does_not_grow(self):
        with patch("relay.manager.time.time", return_value=1000) as clock:
            task = self.started_task(max_minutes=1)
            clock.return_value = 1020
            self.interrupted_without_receipt(task)
            clock.return_value = 1030
            self.host_turn(task)
            recovered = self.manager.reconcile(task["id"])
            self.assertIsNone(recovered["run_started"])
            self.assertEqual(30, recovered["elapsed_seconds"])
            clock.return_value = 1050
            again = self.manager.reconcile(task["id"])
            self.assertEqual(30, again["elapsed_seconds"])
            clock.return_value = 1300
            self.manager.start(task["id"])
            resumed = self.manager.get_task(task["id"])
            clock.return_value = 1320
            self.finish_turn(resumed)
            self.assertEqual(50, self.manager.get_task(task["id"])["elapsed_seconds"])

    def test_unsent_user_correction_is_in_actual_input_after_recovery(self):
        task = self.started_task()
        self.finish_turn(task)
        correction = "Never modify protected.txt."
        self.client.responses["thread/read"].append(RequestTimeout("thread/read", 91, 0.01))
        with self.assertRaises(RequestTimeout):
            self.manager.start(task["id"], correction)
        self.assertIn(correction, self.manager.get_task(task["id"])["requirements"])
        self.manager.reconcile(task["id"])
        self.manager.start(task["id"])
        sent = "\n".join(item.get("text", "") for item in self.client.calls_for("turn/start")[-1]["input"])
        self.assertIn(correction, sent)
        self.assertEqual(task["thread_id"], self.client.calls_for("turn/start")[-1]["threadId"])

    def test_receiver_terminal_recovery_does_not_adopt_ready_or_transfer_owner(self):
        evidence = self.project / "evidence.txt"
        evidence.write_text("Sample", encoding="utf-8")
        task = self.started_task()
        self.finish_turn(task)
        self.manager.handoff(task["id"])
        summary_turn = self.manager.get_task(task["id"])
        self.finish_turn(summary_turn, {
            "summary": "The sample has been inspected", "decisions": [], "completed": ["Read sample"],
            "next_step": "Inspect the sample once more", "evidence": [{"path": "evidence.txt", "description": "Sample"}],
            "unknown_operations": [], "unrecoverable_resources": [], "task_complete": False,
        })
        verifying = self.manager.get_task(task["id"])
        self.manager.poll()
        self.assertNotEqual(task["thread_id"], verifying["receiver_id"])
        self.interrupted_without_receipt(verifying)
        self.client.responses["thread/read"].append({"thread": {
            "id": task["thread_id"], "status": {"type": "idle"}, "turns": [],
        }})
        self.host_turn(verifying, status="interrupted", thread_id=verifying["receiver_id"],
                       items=[{"id": "unaccepted-ready", "type": "agentMessage", "text": '{"ready":true}'}])
        before = len(self.client.calls_for("turn/start"))
        recovered = self.manager.reconcile(task["id"])
        self.assertEqual("paused", recovered["state"])
        self.assertEqual(task["thread_id"], recovered["thread_id"])
        self.assertEqual(task["generation"], recovered["generation"])
        self.assertEqual(before, len(self.client.calls_for("turn/start")))
        self.manager.start(task["id"])
        self.assertEqual(task["thread_id"], self.client.calls_for("turn/start")[-1]["threadId"])

    def test_automatic_summary_timeout_preserves_unknown_execution_intent(self):
        task = self.started_task(auto_handoff=True)
        for name in ("compact-1", "compact-2"):
            self.client.push("item/completed", {
                "threadId": task["thread_id"], "turnId": task["turn_id"],
                "item": {"id": name, "type": "contextCompaction"},
            })
        self.client.push("thread/tokenUsage/updated", {
            "threadId": task["thread_id"], "turnId": task["turn_id"],
            "tokenUsage": {"last": {"totalTokens": 800}, "total": {"totalTokens": 800},
                           "modelContextWindow": 1000},
        })
        self.client.responses["turn/start"].append(RequestTimeout("turn/start", 92, 0.01))
        self.finish_turn(task)
        held = self.manager.get_task(task["id"])
        self.assertEqual("needs_reconcile", held["state"])
        self.assertEqual("summary", held["purpose"])
        self.assertIsNotNone(held["intent"])
        self.assertEqual("turn", held["intent"]["kind"])
        self.assertEqual(2, len(self.client.calls_for("turn/start")))
        with self.assertRaises((ValueError, RuntimeError)):
            self.manager.reconcile(task["id"])
        self.assertEqual(2, len(self.client.calls_for("turn/start")))

    def test_immediate_pause_waits_for_matching_start_and_interrupts_only_once(self):
        self.client.responses["turn/start"].append({
            "turn": {"id": "delayed-turn", "status": "inProgress", "items": []}})
        task = self.started_task()
        self.client.interrupt_completes = False
        self.manager.pause(task["id"])
        self.manager.pause(task["id"])
        self.assertEqual("pausing", self.manager.get_task(task["id"])["state"])
        self.assertEqual([], self.client.calls_for("turn/interrupt"))
        self.client.push("turn/started", {
            "threadId": task["thread_id"], "turn": {"id": "unrelated-turn", "status": "inProgress"}})
        self.manager.poll()
        self.assertEqual([], self.client.calls_for("turn/interrupt"))
        for _ in range(2):
            self.client.push("turn/started", {
                "threadId": task["thread_id"], "turn": {"id": task["turn_id"], "status": "inProgress"}})
        self.manager.poll()
        self.manager.pause(task["id"])
        self.assertEqual([{"threadId": task["thread_id"], "turnId": task["turn_id"]}],
                         self.client.calls_for("turn/interrupt"))
        self.client.push("turn/completed", {
            "threadId": task["thread_id"],
            "turn": {"id": task["turn_id"], "status": "interrupted", "items": []}})
        self.manager.poll()
        self.assertEqual("paused", self.manager.get_task(task["id"])["state"])

    def test_completed_before_started_preserves_work_count_without_auto_continuation(self):
        self.client.responses["turn/start"].append({
            "turn": {"id": "fast-turn", "status": "inProgress", "items": []}})
        task = self.started_task(auto_handoff=True)
        self.client.interrupt_completes = False
        for name in ("compact-1", "compact-2"):
            self.client.push("item/completed", {
                "threadId": task["thread_id"], "turnId": task["turn_id"],
                "item": {"id": name, "type": "contextCompaction"}})
        self.client.push("thread/tokenUsage/updated", {
            "threadId": task["thread_id"], "turnId": task["turn_id"],
            "tokenUsage": {"last": {"totalTokens": 800}, "total": {"totalTokens": 800},
                           "modelContextWindow": 1000}})
        self.manager.poll()
        self.manager.pause(task["id"])
        self.finish_turn(task)
        self.finish_turn(task)
        self.client.push("turn/started", {
            "threadId": task["thread_id"], "turn": {"id": task["turn_id"], "status": "inProgress"}})
        self.manager.poll()
        paused = self.manager.get_task(task["id"])
        self.assertEqual("paused", paused["state"])
        self.assertEqual(1, paused["work_turns"])
        self.assertIsNone(paused["receiver_id"])
        self.assertEqual(1, len(self.client.calls_for("turn/start")))
        self.assertEqual([], self.client.calls_for("turn/interrupt"))

    def test_paused_summary_completed_before_started_does_not_create_receiver(self):
        task = self.started_task()
        self.finish_turn(task)
        self.client.responses["turn/start"].append({
            "turn": {"id": "fast-summary", "status": "inProgress", "items": []}})
        self.manager.handoff(task["id"])
        summary = self.manager.get_task(task["id"])
        self.client.interrupt_completes = False
        self.manager.pause(task["id"])
        self.finish_turn(summary, {
            "summary": "The sample has been inspected", "decisions": [], "completed": ["Read sample"],
            "next_step": "Inspect the sample once more", "evidence": [], "unknown_operations": [],
            "unrecoverable_resources": [], "task_complete": False})
        paused = self.manager.get_task(task["id"])
        self.assertEqual("paused", paused["state"])
        self.assertEqual(1, paused["work_turns"])
        self.assertEqual(task["thread_id"], paused["thread_id"])
        self.assertIsNone(paused["receiver_id"])
        self.assertEqual(1, len(self.client.calls_for("thread/start")))
        self.assertEqual(2, len(self.client.calls_for("turn/start")))
        self.assertEqual([], self.client.calls_for("turn/interrupt"))


if __name__ == "__main__":
    unittest.main()
