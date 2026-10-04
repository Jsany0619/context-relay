"""Budget/settings boundaries; no real Codex session or external operation."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from relay.manager import Manager
from tests.test_manager import FakeClient


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.client = FakeClient()
        self.manager = Manager(self.root / "state", self.client)
        self.addCleanup(lambda: self.manager.close())
        self.task = self.manager.create_task("Example", self.project, "Inspect only", max_tokens=100, max_minutes=2, direct=False)

    def update(self, **values):
        options = dict(title="Renamed", max_tokens=200, max_minutes=3.5, auto_handoff=True)
        options.update(values)
        return self.manager.update_settings(self.task["id"], **options)

    def test_settings_persist_without_restarting_or_resetting_execution(self):
        task = self.manager._task(self.task["id"])
        task.update(state="paused", usage=100, elapsed_seconds=120, thread_id="source", generation=2,
                    checkpoint={"revision": task["revision"]}, checkpoint_hash="old",
                    checkpoint_path="old-checkpoint.json", draft={"revision": task["revision"]})
        self.manager._save(task)
        artifact = self.project / "adopted.txt"
        artifact.write_text("Keep", encoding="utf-8")
        self.assertTrue(self.manager._budget(task))
        result = self.update()
        for key in ("goal", "requirements", "cwd", "mode", "thread_id", "generation", "state",
                    "usage", "thread_usage", "elapsed_seconds", "work_turns", "user_inputs"):
            self.assertEqual(result[key], task[key], key)
        self.assertEqual(result["revision"], task["revision"] + 1)
        self.assertEqual(result["max_minutes"], 3.5)
        self.assertFalse(self.manager._budget(result))
        for key in ("checkpoint", "checkpoint_hash", "checkpoint_path", "draft"):
            self.assertIsNone(result[key])
        self.manager.close()
        self.manager = Manager(self.root / "state", self.client)
        saved = self.manager.get_task(task["id"])
        self.assertEqual(saved["title"], "Renamed")
        self.assertEqual(saved["state"], "paused")
        self.assertEqual(saved["events"][0]["kind"], "task_settings_updated")
        self.assertEqual(saved["events"][0]["data"]["changes"]["auto_handoff"], {"before": False, "after": True})
        self.assertEqual(artifact.read_text(encoding="utf-8"), "Keep")
        self.assertEqual(self.client.calls, [])

    def test_repeated_settings_do_not_invalidate_checkpoint_or_duplicate_events(self):
        self.update()
        before = self.manager.get_task(self.task["id"])
        self.update()
        self.assertEqual(self.manager.get_task(self.task["id"]), before)

    def test_updates_refuse_active_unknown_completed_archived_or_unsettled_state(self):
        base = self.manager._task(self.task["id"])
        cases = [dict(state=state) for state in ("creating", "running", "pausing", "summarizing", "verifying",
                                               "needs_reconcile", "blocked", "completed")]
        cases += [{"archived": True}, {"pending": [{"id": 2}]}, {"inflight": {"op": "pending"}},
                  {"intent": {"kind": "turn"}}, {"receiver_id": "unverified"}, {"run_started": 0}]
        for override in cases:
            with self.subTest(override=override):
                task = dict(base, **override)
                self.manager._save(task)
                before = self.manager.get_task(task["id"])
                with self.assertRaises(ValueError):
                    self.update()
                self.assertEqual(self.manager.get_task(task["id"]), before)
        self.manager._save(base)
        self.assertEqual(self.client.calls, [])

    def test_invalid_input_cannot_disable_limits_in_create_or_update(self):
        invalid = [dict(max_tokens=value) for value in (-1, 1.9, True, "1.5", "inf")]
        invalid += [dict(max_minutes=value) for value in (-1, float("nan"), float("inf"), "NaN", True)]
        invalid += [dict(auto_handoff=value) for value in ("false", 0, 1, None)]
        invalid += [dict(title="  "), dict(title="Bearer " + "A" * 50)]
        for override in invalid:
            with self.subTest(override=override):
                before = self.manager.get_task(self.task["id"])
                with self.assertRaises(ValueError):
                    self.update(**override)
                self.assertEqual(self.manager.get_task(self.task["id"]), before)
                options = dict(title="New", cwd=self.project, goal="Inspect only", max_tokens=100,
                               max_minutes=2, auto_handoff=False)
                options.update(override)
                with self.assertRaises(ValueError):
                    self.manager.create_task(**options, direct=False)
                self.assertEqual(len(self.manager.list_tasks()), 1)

    def test_invalid_model_scope_cannot_reenable_automatic_handoff(self):
        task = self.manager._task(self.task["id"])
        task.update(telemetry_model_valid=False, auto_handoff=False)
        self.manager._save(task)
        with self.assertRaisesRegex(ValueError, "模型"):
            self.update(auto_handoff=True)
        result = self.update(auto_handoff=False)
        self.assertFalse(result["telemetry_model_valid"])
        self.assertFalse(result["auto_handoff"])
        self.assertEqual(self.client.calls, [])

    def test_explicit_zero_limits_and_disable_handoff_do_not_start_work(self):
        result = self.update(max_tokens="0", max_minutes="0", auto_handoff=False)
        self.assertEqual(result["max_tokens"], 0)
        self.assertEqual(result["max_minutes"], 0)
        self.assertFalse(result["auto_handoff"])
        self.assertFalse(self.manager._budget(result))
        self.assertEqual(result["state"], "queued")
        self.assertEqual(self.client.calls, [])

    def test_shared_budget_status_matches_boundary_and_does_not_mutate_task(self):
        from relay.budget import budget_status
        task = dict(self.task, usage=100, elapsed_seconds=30, run_started=1000)
        before = copy.deepcopy(task)
        status = budget_status(task, now=1090)
        self.assertEqual(status, {"elapsed_seconds": 120, "token_reached": True,
                                  "minutes_reached": True, "reached": True})
        self.assertEqual(task, before)
        task.update(run_started=None, usage=99, elapsed_seconds=119)
        self.assertFalse(budget_status(task, now=999999)["reached"])
        self.assertEqual(budget_status(task, now=999999)["elapsed_seconds"], 119)
        task.update(run_started=2000, elapsed_seconds=0)
        self.assertEqual(budget_status(task, now=1999)["elapsed_seconds"], 0)

    def test_lower_limit_still_blocks_until_explicit_increase_and_start(self):
        task = self.manager._task(self.task["id"])
        task.update(usage=100, elapsed_seconds=120)
        self.manager._save(task)
        self.update(max_tokens=50, max_minutes=1, auto_handoff=False)
        with self.assertRaisesRegex(ValueError, "预算"):
            self.manager.start(task["id"])
        self.assertEqual(self.client.calls, [])
        self.update(max_tokens=200, max_minutes=3, auto_handoff=False)
        self.assertEqual(self.client.calls, [])
        self.manager.start(task["id"])
        self.assertEqual(len(self.client.calls_for("turn/start")), 1)

    def complete(self, task):
        self.client.push("turn/completed", {"threadId": task["thread_id"],
            "turn": {"id": task["turn_id"], "status": "completed", "items": []}})
        self.manager.poll()
        return self.manager.get_task(task["id"])

    def test_disabled_handoff_stays_disabled_at_next_high_pressure_completion(self):
        self.update(max_tokens=0, max_minutes=0)
        task = self.manager.start(self.task["id"])
        self.complete(task)
        task = self.manager._task(task["id"])
        task.update(context_estimate=.9, fresh_usage=True, compactions=2)
        self.manager._save(task)
        changed = self.update(max_tokens=0, max_minutes=0, auto_handoff=False)
        self.assertEqual(changed["context_estimate"], .9)
        self.assertTrue(changed["fresh_usage"])
        self.assertEqual(changed["compactions"], 2)
        next_turn = self.manager.start(task["id"])
        result = self.complete(next_turn)
        self.assertEqual(result["state"], "idle")
        self.assertIsNone(result["draft"])
        self.assertEqual(len(self.client.calls_for("thread/start")), 1)
        self.assertEqual(len(self.client.calls_for("turn/start")), 2)

    def test_updated_settings_are_in_fresh_draft_and_formal_handoff(self):
        self.update(max_tokens=5000, max_minutes=5, auto_handoff=False)
        task = self.manager.start(self.task["id"])
        self.complete(task)
        saved = self.manager.prepare_snapshot(task["id"])
        draft = json.loads(Path(saved["draft"]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(draft["task"]["max_tokens"], 5000)
        summary_turn = self.manager.handoff(task["id"])
        summary = {"summary": "Reviewed current project", "next_step": "Inspect remaining requirements",
                   "decisions": [], "completed": [], "evidence": [], "unknown_operations": [],
                   "unrecoverable_resources": [], "task_complete": False}
        self.client.push("item/completed", {"threadId": task["thread_id"], "turnId": summary_turn["turn_id"],
            "item": {"id": "summary", "type": "agentMessage", "text": json.dumps(summary)}})
        receiver = self.complete(summary_turn)
        self.assertEqual(receiver["state"], "verifying")
        self.assertEqual(receiver["checkpoint"]["settings"], {
            "title": "Renamed", "max_tokens": 5000, "max_minutes": 5, "auto_handoff": False})
        self.assertEqual(receiver["checkpoint"]["revision"], saved["revision"])


if __name__ == "__main__":
    unittest.main()
