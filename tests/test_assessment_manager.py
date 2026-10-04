"""Brief/review integration checks. Native client construction is forbidden."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from relay.manager import Manager
from tests.test_manager import FakeClient

SOURCE = "019a0000-0000-7000-8000-000000000007"


def brief_report():
    return {"goal": "Explore a simpler task workflow", "decisions": ["Keep local storage"],
            "completed": ["Existing candidate inspected"], "unknowns": ["Which interaction feels clearest"],
            "approaches": ["Try a small guided flow", "Keep the current list with shortcuts"],
            "next_step": "Compare a small prototype", "acceptance": ["User can import without rewriting history"],
            "evidence": [{"source": "file", "reference": "result.txt", "finding": "Existing candidate"}]}


def review_report(verdict="ready_for_user"):
    return {"verdict": verdict, "summary": "Candidate needs human experience review", "findings": [],
            "checks": [{"category": category, "result": "unverified", "finding": "Inspect with the user",
                        "evidence": []} for category in ("requirements", "correctness", "usability", "creative")],
            "unknowns": ["Human visual preference"], "test_evidence": ["No independently verified test receipt"]}


class AssessmentClient(FakeClient):
    source = None

    def request(self, method, params=None, timeout=30):
        if method == "thread/read" and (params or {}).get("threadId") == SOURCE:
            self.responses[method].append({"thread": copy.deepcopy(self.source)})
        return super().request(method, params, timeout)


class AssessmentManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "result.txt").write_text("candidate", encoding="utf-8")
        self.block_native = patch("relay.transport.CodexClient", side_effect=AssertionError("Native access forbidden"))
        self.block_native.start()
        self.addCleanup(self.block_native.stop)
        self.client = AssessmentClient()
        self.client.source = {"id": SOURCE, "name": "Imported project", "preview": "Explore", "source": "vscode",
            "cwd": str(self.project), "updatedAt": 1, "status": {"type": "notLoaded"},
            "turns": [{"id": "old-turn", "status": "completed", "items": [
                {"id": "old-user", "type": "userMessage", "content": [{"type": "text", "text": "Explore options"}]}]}]}
        self.manager = Manager(self.root / "state", self.client)
        self.addCleanup(self.manager.close)

    def task(self):
        return self.manager.create_task("Project", self.project, "Improve the candidate", mode="workspace-write", direct=False)

    def imported(self):
        preview = self.manager.preview_import(SOURCE)
        return self.manager.import_thread(SOURCE, preview["fingerprint"], title="Import", goal="", source_stopped=True,
                                         mode="workspace-write", direct=False)

    def complete(self, task, report=None):
        thread_id = task.get("analysis_thread_id") or task["thread_id"]
        items = [] if report is None else [{"id": "result-" + task["turn_id"], "type": "agentMessage",
                                           "text": json.dumps(report)}]
        for item in items:
            self.client.push("item/completed", {"threadId": thread_id, "turnId": task["turn_id"], "item": item})
        self.client.push("turn/completed", {"threadId": thread_id,
                          "turn": {"id": task["turn_id"], "status": "completed", "items": items}})
        self.manager.poll()
        return self.manager.get_task(task["id"])

    def candidate(self):
        return self.complete(self.manager.start(self.task()["id"]))

    def test_empty_import_first_start_only_generates_readonly_brief(self):
        task = self.imported()
        self.assertTrue(task["brief_required"])
        self.assertFalse(self.client.calls_for("thread/start"))
        running = self.manager.start(task["id"])
        self.assertEqual(running["state"], "briefing")
        self.assertIsNone(running["thread_id"])
        self.assertIsNone(running["receiver_id"])
        self.assertEqual(self.client.calls_for("thread/start")[-1]["sandbox"], "read-only")
        self.assertEqual(self.client.calls_for("turn/start")[-1]["sandboxPolicy"], {"type": "readOnly"})
        result = self.complete(running, brief_report())
        self.assertEqual(result["brief"]["decision"], "pending")
        self.assertEqual(result["last_message"], "")
        self.assertEqual(result["last_message_kind"], "brief")
        self.assertEqual(result["work_turns"], 0)
        self.assertIsNone(result["thread_id"])
        self.assertTrue(result["brief_required"])
        with self.assertRaises(ValueError):
            self.manager.start(task["id"])
        self.assertEqual(len(self.client.calls_for("turn/start")), 1)

    def test_malformed_completed_assessment_is_rejected_without_an_automatic_retry(self):
        running = self.manager.analyze(self.task()["id"], "brief")
        item = {"id": "malformed-brief", "type": "agentMessage", "text": "{not-json"}
        self.client.push("item/completed", {"threadId": running["analysis_thread_id"],
                         "turnId": running["turn_id"], "item": item})
        self.client.push("turn/completed", {"threadId": running["analysis_thread_id"],
                         "turn": {"id": running["turn_id"], "status": "completed", "items": [item]}})
        self.manager.poll()
        rejected = self.manager.get_task(running["id"])
        self.assertEqual(rejected["state"], "idle")
        self.assertEqual(rejected["last_message"], "")
        self.assertEqual(rejected["last_message_kind"], "brief")
        self.assertNotIn("brief", rejected)
        self.assertEqual(len(self.client.calls_for("turn/start")), 1)

    def test_invalid_completed_brief_is_rejected_without_exposing_json_or_reconciliation(self):
        running = self.manager.start(self.imported()["id"])
        invalid = brief_report()
        invalid["evidence"] = [{"source": "chat", "reference": "missing-turn/missing-item",
                                "finding": "Not bound to the imported excerpt"}]
        raw = json.dumps(invalid)
        item = {"id": "invalid-brief", "type": "agentMessage", "text": raw}
        self.client.push("item/completed", {"threadId": running["analysis_thread_id"],
                         "turnId": running["turn_id"], "item": item})
        self.manager.poll()
        received = self.manager.get_task(running["id"])
        self.assertEqual(received["state"], "briefing")
        self.assertEqual(received["last_message_kind"], "brief")
        self.assertNotEqual(received["last_message"], raw)

        self.client.push("turn/completed", {"threadId": running["analysis_thread_id"],
                         "turn": {"id": running["turn_id"], "status": "completed", "items": [item]}})
        self.manager.poll()
        rejected = self.manager.get_task(running["id"])
        self.assertEqual(rejected["state"], "idle")
        self.assertTrue(rejected["brief_required"])
        self.assertEqual(rejected["work_turns"], 0)
        self.assertEqual(rejected["mode"], "workspace-write")
        self.assertEqual(rejected["last_message"], "")
        self.assertEqual(rejected["last_message_kind"], "brief")
        self.assertIsNone(rejected["analysis_thread_id"])
        self.assertIsNone(rejected["assessment"])
        self.assertIsNone(rejected["turn_id"])
        self.assertNotIn("brief", rejected)
        self.assertIn("未通过", rejected["error"])
        event = next(event for event in rejected["events"] if event["kind"] == "assessment_rejected")
        self.assertEqual(event["data"], {"kind": "brief", "reason": "invalid_candidate"})
        self.assertNotIn("missing-turn", json.dumps(rejected["events"]))
        self.assertEqual(len(self.client.calls_for("turn/start")), 1)

    def test_work_response_that_is_json_remains_a_visible_work_message(self):
        running = self.manager.start(self.task()["id"])
        text = json.dumps({"result": "normal work JSON"})
        item = {"id": "work-json", "type": "agentMessage", "text": text}
        self.client.push("item/completed", {"threadId": running["thread_id"],
                         "turnId": running["turn_id"], "item": item})
        self.client.push("turn/completed", {"threadId": running["thread_id"],
                         "turn": {"id": running["turn_id"], "status": "completed", "items": [item]}})
        self.manager.poll()
        result = self.manager.get_task(running["id"])
        self.assertEqual(result["last_message"], text)
        self.assertEqual(result["last_message_kind"], "work")
        self.assertEqual(result["work_turns"], 1)

    def test_adopting_brief_is_local_and_work_uses_current_permissions(self):
        task = self.complete(self.manager.start(self.imported()["id"]), brief_report())
        calls = len(self.client.calls_for("turn/start"))
        adopted = self.manager.adopt_brief(task["id"], "Compare two small prototypes", ["User chooses a direction"])
        self.assertFalse(adopted["brief_required"])
        self.assertEqual(adopted["goal"], "Compare two small prototypes")
        self.assertEqual(adopted["acceptance_criteria"], ["User chooses a direction"])
        self.assertEqual(adopted["brief"]["decision"], "adopted")
        self.assertEqual(len(self.client.calls_for("turn/start")), calls)
        started = self.manager.start(task["id"])
        self.assertNotEqual(started["thread_id"], SOURCE)
        self.assertEqual(self.client.calls_for("thread/start")[-1]["sandbox"], "workspace-write")
        self.assertIn("User chooses a direction", self.client.calls_for("turn/start")[-1]["input"][0]["text"])

    def test_brief_adoption_refuses_changed_artifacts_or_invalid_input(self):
        task = self.complete(self.manager.analyze(self.task()["id"], "brief"), brief_report())
        for goal, acceptance in (("", ["Check"]), ("Goal", []), ("Goal", "not-a-list")):
            with self.assertRaises(ValueError):
                self.manager.adopt_brief(task["id"], goal, acceptance)
        (self.project / "result.txt").write_text("changed", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.manager.adopt_brief(task["id"], "Goal", ["Check"])
        self.assertEqual(self.manager.get_task(task["id"])["brief"]["status"], "stale")

    def test_review_is_independent_and_cannot_accept_itself(self):
        task = self.candidate()
        running = self.manager.analyze(task["id"], "review")
        self.assertNotEqual(running["analysis_thread_id"], task["thread_id"])
        self.assertEqual(running["thread_id"], task["thread_id"])
        reviewed = self.complete(running, review_report())
        self.assertEqual(reviewed["review"]["machine_checks"], "not_verified")
        self.assertEqual(reviewed["review"]["human_acceptance"], "pending")
        self.assertEqual(reviewed["work_turns"], task["work_turns"])
        accepted = self.manager.accept_review(task["id"])
        self.assertEqual(accepted["review"]["human_acceptance"], "accepted")
        self.assertEqual(accepted["state"], "idle")
        self.assertEqual(len(self.client.calls_for("turn/start")), 2)

    def test_changed_files_or_requirements_invalidate_review_actions(self):
        task = self.complete(self.manager.analyze(self.candidate()["id"], "review"), review_report())
        (self.project / "result.txt").write_text("new version", encoding="utf-8")
        for action in (self.manager.accept_review, self.manager.revise_from_review):
            with self.assertRaises(ValueError):
                action(task["id"])
        self.assertEqual(len(self.client.calls_for("turn/start")), 2)
        self.assertEqual(self.manager.get_task(task["id"])["review"]["status"], "stale")

    def test_review_rework_requires_explicit_action_and_retains_evidence(self):
        report = review_report("changes_requested")
        report["findings"] = [{"severity": "medium", "category": "usability", "finding": "Too many clicks",
                                "suggestion": "Try one import action", "evidence": []}]
        task = self.complete(self.manager.analyze(self.candidate()["id"], "review"), report)
        self.assertEqual(len(self.client.calls_for("turn/start")), 2)
        with self.assertRaises(ValueError):
            self.manager.accept_review(task["id"])
        restarted = self.manager.revise_from_review(task["id"])
        self.assertEqual(restarted["state"], "running")
        self.assertEqual(restarted["review"]["decision"], "revise")
        self.assertEqual(restarted["review"]["status"], "stale")
        self.assertIn("Too many clicks", self.client.calls_for("turn/start")[-1]["input"][0]["text"])

    def test_handoff_cannot_turn_pending_review_into_work_instructions(self):
        task = self.complete(self.manager.analyze(self.candidate()["id"], "review"), review_report("changes_requested"))
        calls = len(self.client.calls_for("turn/start"))
        with self.assertRaises(ValueError):
            self.manager.handoff(task["id"])
        self.assertEqual(len(self.client.calls_for("turn/start")), calls)

    def test_external_owner_turn_invalidates_ready_review_without_file_changes(self):
        task = self.complete(self.manager.analyze(self.candidate()["id"], "review"), review_report())
        self.client.responses["thread/read"].append({"thread": {"id": task["thread_id"],
            "status": {"type": "idle"}, "turns": [{"id": "outside-turn", "status": "completed", "items": []}]}})
        with self.assertRaises(ValueError):
            self.manager.accept_review(task["id"])
        self.assertEqual(self.manager.get_task(task["id"])["review"]["status"], "stale")

    def test_external_owner_turn_during_analysis_prevents_report_acceptance(self):
        task = self.manager.analyze(self.candidate()["id"], "review")
        self.client.responses["thread/read"].append({"thread": {"id": task["thread_id"],
            "status": {"type": "idle"}, "turns": [{"id": "outside-turn", "status": "completed", "items": []}]}})
        result = self.complete(task, review_report())
        self.assertNotIn("review", result)
        self.assertEqual(result["state"], "needs_reconcile")

    def test_analysis_budget_counts_without_owner_pressure_or_work_progress(self):
        task = self.manager.analyze(self.task()["id"], "brief")
        self.client.push("thread/tokenUsage/updated", {"threadId": task["analysis_thread_id"], "turnId": task["turn_id"],
             "tokenUsage": {"total": {"totalTokens": 900}, "last": {"totalTokens": 850}, "modelContextWindow": 1000}})
        self.client.push("item/completed", {"threadId": task["analysis_thread_id"], "turnId": task["turn_id"],
                                            "item": {"id": "compact", "type": "contextCompaction"}})
        result = self.complete(task, brief_report())
        self.assertEqual(result["usage"], 900)
        self.assertEqual(result["compactions"], 0)
        self.assertIsNone(result["context_estimate"])
        self.assertEqual(result["work_turns"], 0)

    def test_analysis_answers_update_requirements_but_never_grant_write_approval(self):
        task = self.manager.analyze(self.task()["id"], "brief")
        common = {"threadId": task["analysis_thread_id"], "turnId": task["turn_id"]}
        self.client.push("item/commandExecution/requestApproval", common, request_id="write-denied")
        self.client.push("item/tool/requestUserInput", dict(common, questions=[
            {"id": "direction", "question": "Which direction?", "isSecret": False}]), request_id="clarify")
        self.manager.poll()
        self.assertEqual(self.client.answers[0], ("write-denied", {"decision": "decline"}))
        self.manager.answer(task["id"], "clarify", {"answers": {"direction": {"answers": ["Explore both"]}}})
        result = self.complete(task, brief_report())
        self.assertEqual(result["state"], "idle")
        self.assertEqual(result["brief"]["binding"]["revision"], result["revision"])
        self.assertEqual(result["brief"]["decision"], "pending")
        self.assertIsNone(result["thread_id"])
        self.manager.adopt_brief(task["id"], "Compare options", ["Keep options reversible"])
        self.manager.start(task["id"])
        self.assertIn("Explore both", self.client.calls_for("turn/start")[-1]["input"][0]["text"])

    def test_report_rejects_mid_analysis_changes_or_forged_evidence(self):
        task = self.manager.analyze(self.task()["id"], "brief")
        (self.project / "result.txt").write_text("concurrent change", encoding="utf-8")
        result = self.complete(task, brief_report())
        self.assertNotIn("brief", result)
        self.assertEqual(result["state"], "needs_reconcile")

    def test_refreshing_import_after_only_analysis_preserves_usage_and_history(self):
        task = self.complete(self.manager.start(self.imported()["id"]), brief_report())
        self.manager.db.execute("UPDATE tasks SET data=? WHERE id=?", (json.dumps(dict(task, usage=42,
            thread_usage={task["history"][-1]["thread_id"]: 42})), task["id"]))
        self.manager.db.commit()
        self.client.source["updatedAt"] += 1
        preview = self.manager.preview_import(SOURCE)
        refreshed = self.manager.import_thread(SOURCE, preview["fingerprint"], title="Refreshed", goal="",
                         source_stopped=True, existing_task_id=task["id"], direct=False)
        self.assertEqual(refreshed["usage"], 42)
        self.assertEqual(refreshed["history"], task["history"])
        self.assertTrue(refreshed["brief_required"])
        self.assertEqual(refreshed["brief"]["status"], "stale")


if __name__ == "__main__":
    unittest.main()
