"""Analysis-session failure paths; every controller uses a fake native client."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from relay.backup import restore_backup
from relay.manager import Manager
from relay.transport import RequestTimeout
from tests.test_assessment_manager import AssessmentClient, brief_report, review_report


class AssessmentRecoveryTests(unittest.TestCase):
    def setUp(self):
        guard = patch("relay.transport.CodexClient", side_effect=AssertionError("Native access forbidden"))
        guard.start()
        self.addCleanup(guard.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "result.txt").write_text("candidate", encoding="utf-8")
        self.client = AssessmentClient()
        self.manager = Manager(self.root / "state", self.client)
        self.addCleanup(self.manager.close)

    def task(self, **settings):
        return self.manager.create_task("Assessment recovery", self.project, "Compare the existing candidate",
                                        mode="workspace-write", **settings)

    def complete(self, task, report=None):
        thread_id = task.get("analysis_thread_id") or task["thread_id"]
        items = [] if report is None else [{"id": "reply-" + task["turn_id"], "type": "agentMessage",
                                           "text": json.dumps(report)}]
        for item in items:
            self.client.push("item/completed", {"threadId": thread_id, "turnId": task["turn_id"], "item": item})
        self.client.push("turn/completed", {"threadId": thread_id,
                          "turn": {"id": task["turn_id"], "status": "completed", "items": items}})
        self.manager.poll()
        return self.manager.get_task(task["id"])

    def candidate(self, **settings):
        return self.complete(self.manager.start(self.task(**settings)["id"]))

    def receipt(self, task, *, turn_id=None, report=None):
        items = [] if report is None else [{"id": "recovered-reply", "type": "agentMessage",
                                           "text": json.dumps(report)}]
        return {"thread": {"id": task["analysis_thread_id"], "status": {"type": "idle"}, "turns": [
            {"id": turn_id or task["turn_id"], "status": "completed", "items": items}]}}

    def test_unknown_creation_is_not_replayed_and_late_receipt_never_becomes_owner(self):
        task = self.task()
        self.client.responses["thread/start"].append(RequestTimeout("thread/start", 31, .01))
        with self.assertRaises(RequestTimeout):
            self.manager.analyze(task["id"], "brief")
        for action in (lambda: self.manager.analyze(task["id"], "brief"),
                       lambda: self.manager.start(task["id"]), lambda: self.manager.reconcile(task["id"])):
            with self.assertRaises(ValueError):
                action()
        self.assertEqual(len(self.client.calls_for("thread/start")), 1)
        self.assertFalse(self.client.calls_for("turn/start"))
        self.client.push("transport/lateResponse", {"requestId": 31, "result": {
            "thread": {"id": "analysis-late"}, "cwd": str(self.project), "model": "test-model",
            "sandbox": {"type": "readOnly"}, "approvalPolicy": "on-request"}})
        self.manager.poll()
        received = self.manager.get_task(task["id"])
        self.assertEqual(received["analysis_thread_id"], "analysis-late")
        self.assertIsNone(received["thread_id"])
        self.assertIsNone(received["receiver_id"])
        self.assertNotIn("permission_receipt", received)
        self.assertEqual(received["state"], "needs_reconcile")
        recovered = self.manager.reconcile(task["id"])
        self.assertIsNone(recovered["thread_id"])
        self.assertIsNone(recovered["analysis_thread_id"])
        self.assertEqual(recovered["work_turns"], 0)
        self.assertNotIn("brief", recovered)
        self.assertFalse(self.client.calls_for("turn/start"))

    def test_unknown_analysis_turn_requires_its_receipt_and_never_auto_adopts(self):
        task = self.task()
        self.client.responses["turn/start"].append(RequestTimeout("turn/start", 41, .01))
        with self.assertRaises(RequestTimeout):
            self.manager.analyze(task["id"], "brief")
        for action in (lambda: self.manager.start(task["id"]),
                       lambda: self.manager.analyze(task["id"], "brief"), lambda: self.manager.reconcile(task["id"])):
            with self.assertRaises(ValueError):
                action()
        self.assertEqual(len(self.client.calls_for("thread/start")), 1)
        self.assertEqual(len(self.client.calls_for("turn/start")), 1)
        self.client.push("transport/lateResponse", {"requestId": 41, "result": {"turn": {"id": "late-turn"}}})
        self.manager.poll()
        received = self.manager.get_task(task["id"])
        self.assertEqual(received["turn_id"], "late-turn")
        self.assertIsNone(received["thread_id"])
        self.client.responses["thread/read"].append(self.receipt(received, report=brief_report()))
        recovered = self.manager.reconcile(task["id"])
        self.assertEqual(recovered["goal"], task["goal"])
        self.assertEqual(recovered["acceptance_criteria"], [])
        self.assertNotIn("brief", recovered)
        self.assertEqual(recovered["work_turns"], 0)
        self.assertEqual(len(self.client.calls_for("turn/start")), 1)

    def test_reused_request_id_on_new_connection_cannot_claim_old_analysis(self):
        task = self.task()
        self.client.responses["thread/start"].append(RequestTimeout("thread/start", 51, .01))
        with self.assertRaises(RequestTimeout):
            self.manager.analyze(task["id"], "brief")
        self.client.push("transport/closed", {"returncode": 1})
        self.manager.poll()
        fresh = AssessmentClient()
        self.manager.client = fresh
        fresh.push("transport/lateResponse", {"requestId": 51, "result": {
            "thread": {"id": "unrelated-analysis"}, "cwd": str(self.project), "model": "test-model",
            "sandbox": {"type": "readOnly"}, "approvalPolicy": "on-request"}})
        self.manager.poll()
        preserved = self.manager.get_task(task["id"])
        self.assertIsNone(preserved["analysis_thread_id"])
        self.assertIsNone(preserved["thread_id"])
        self.assertEqual(preserved["intent"]["kind"], "create_analysis")
        with self.assertRaises(ValueError):
            self.manager.reconcile(task["id"])
        self.assertEqual(fresh.calls, [])

    def test_immediate_pause_waits_for_analysis_start_and_interrupts_only_it_once(self):
        owner = self.candidate()
        task = self.manager.analyze(owner["id"], "review")
        self.manager.pause(task["id"])
        self.manager.pause(task["id"])
        self.assertFalse(self.client.calls_for("turn/interrupt"))
        self.client.push("turn/started", {"threadId": task["analysis_thread_id"],
                                         "turn": {"id": task["turn_id"], "status": "inProgress"}})
        self.manager.poll()
        self.assertEqual(self.client.calls_for("turn/interrupt"), [
            {"threadId": task["analysis_thread_id"], "turnId": task["turn_id"]}])
        self.manager.poll()
        paused = self.manager.get_task(task["id"])
        self.assertEqual(paused["state"], "paused")
        self.assertEqual(paused["thread_id"], owner["thread_id"])
        self.assertEqual(paused["work_turns"], owner["work_turns"])
        self.assertIsNone(paused["analysis_thread_id"])
        self.assertNotIn("review", paused)
        self.assertEqual(len(self.client.calls_for("turn/start")), 2)

    def test_restart_with_analysis_only_requires_matching_terminal_and_does_not_adopt(self):
        original = self.task()
        running = self.manager.analyze(original["id"], "brief")
        self.manager.poll()
        self.client.push("transport/closed", {"returncode": 1})
        self.manager.poll()
        self.manager.close()
        self.client = AssessmentClient()
        self.manager = Manager(self.root / "state", self.client)
        self.addCleanup(self.manager.close)
        self.assertEqual(self.client.calls, [])
        self.client.responses["thread/read"].append(self.receipt(running, turn_id="different-turn"))
        with self.assertRaises(ValueError):
            self.manager.reconcile(running["id"])
        self.assertEqual(self.manager.get_task(running["id"])["turn_id"], running["turn_id"])
        self.client.responses["thread/read"].append(self.receipt(running, report=brief_report()))
        recovered = self.manager.reconcile(running["id"])
        self.assertEqual(recovered["state"], "paused")
        self.assertIsNone(recovered["thread_id"])
        self.assertIsNone(recovered["analysis_thread_id"])
        self.assertEqual(recovered["goal"], original["goal"])
        self.assertEqual(recovered["work_turns"], 0)
        self.assertNotIn("brief", recovered)
        self.assertEqual({name for name, _ in self.client.calls}, {"config/read", "thread/read"})
        self.assertFalse(self.client.answers)

    def test_analysis_budget_interrupts_without_changing_owner_telemetry_or_permission(self):
        owner = self.candidate(max_tokens=100, auto_handoff=True)
        seeded = self.manager._task(owner["id"])
        seeded.update(context_estimate=.4, fresh_usage=True, compactions=1, usage_signature="owner-usage")
        self.manager._save(seeded)
        task = self.manager.analyze(owner["id"], "review")
        self.client.push("thread/tokenUsage/updated", {"threadId": task["analysis_thread_id"], "turnId": task["turn_id"],
            "tokenUsage": {"total": {"totalTokens": 120}, "last": {"totalTokens": 95}, "modelContextWindow": 100}})
        self.client.push("item/completed", {"threadId": task["analysis_thread_id"], "turnId": task["turn_id"],
                                           "item": {"id": "analysis-compact", "type": "contextCompaction"}})
        self.client.push("model/changed", {"threadId": task["analysis_thread_id"], "turnId": task["turn_id"],
                                          "model": "analysis-rerouted"})
        self.manager.poll()
        held = self.manager.get_task(task["id"])
        self.assertEqual(held["usage"], 120)
        self.assertEqual(held["state"], "pausing")
        self.assertEqual(self.client.calls_for("turn/interrupt")[-1]["threadId"], task["analysis_thread_id"])
        for key in ("thread_id", "permission_receipt", "context_estimate", "fresh_usage", "compactions",
                    "usage_signature", "telemetry_model_valid", "auto_handoff", "work_turns"):
            self.assertEqual(held[key], seeded[key], key)
        self.manager.poll()
        with self.assertRaises(ValueError):
            self.manager.analyze(task["id"], "review")
        self.assertEqual(len(self.client.calls_for("thread/start")), 2)
        self.assertNotIn("review", self.manager.get_task(task["id"]))

    def test_retired_analysis_events_cannot_overwrite_next_analysis_or_adopt_results(self):
        task = self.manager.analyze(self.task()["id"], "brief")
        first = self.complete(task, brief_report())
        second = self.manager.analyze(first["id"], "brief")
        self.manager.poll()
        before = copy.deepcopy(self.manager._task(first["id"]))
        forged = dict(brief_report(), goal="Unexpected old analysis result")
        self.client.push("item/completed", {"threadId": task["analysis_thread_id"], "turnId": task["turn_id"],
                 "item": {"id": "late-result", "type": "agentMessage", "text": json.dumps(forged)}})
        self.client.push("turn/completed", {"threadId": task["analysis_thread_id"],
                                          "turn": {"id": task["turn_id"], "status": "completed", "items": []}})
        self.manager.poll()
        self.assertEqual(self.manager._task(first["id"]), before)
        self.assertNotEqual(second["analysis_thread_id"], task["analysis_thread_id"])
        self.manager.pause(second["id"])
        self.manager.poll()

    def test_backup_and_inspection_keep_acceptance_levels_and_forbid_assessment_actions(self):
        task = self.complete(self.manager.analyze(self.task()["id"], "brief"), brief_report())
        task = self.manager.adopt_brief(task["id"], "Compare two candidate flows", ["User selects a preferred flow"])
        task = self.complete(self.manager.start(task["id"]))
        task = self.complete(self.manager.analyze(task["id"], "review"), review_report())
        task = self.manager.accept_review(task["id"])
        prepared = self.manager.prepare_snapshot(task["id"])
        packet = json.loads(Path(prepared["draft"]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(packet["task"]["acceptance_criteria"], task["acceptance_criteria"])
        self.assertEqual(packet["task"]["review"]["machine_checks"], "not_verified")
        self.assertEqual(packet["task"]["review"]["human_acceptance"], "accepted")
        archive = self.root / "assessment.zip"
        self.manager.backup_state(archive)
        destination = self.root / "inspection"
        restore_backup(archive, destination)
        client = AssessmentClient()
        recovered = Manager(destination, client)
        self.addCleanup(recovered.close)
        retained = recovered.get_task(task["id"])
        for key in ("acceptance_criteria", "brief", "review"):
            self.assertEqual(retained[key], task[key], key)
        for action in (lambda: recovered.analyze(task["id"], "brief"),
                       lambda: recovered.analyze(task["id"], "review"),
                       lambda: recovered.adopt_brief(task["id"], "Changed", ["New standard"]),
                       lambda: recovered.accept_review(task["id"]),
                       lambda: recovered.revise_from_review(task["id"])):
            with self.assertRaises(ValueError):
                action()
        recovered.poll()
        self.assertEqual(recovered.get_task(task["id"]), retained)
        self.assertEqual(client.calls, [])
        self.assertEqual(client.answers, [])


if __name__ == "__main__":
    unittest.main()
