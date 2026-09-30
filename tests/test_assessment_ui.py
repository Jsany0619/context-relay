"""Assessment UI contract checks with fake reports; no native client or real task library."""
from copy import deepcopy
import gc
import tempfile
import threading
import time
import tkinter as tk
import unittest

from relay.ui import RelayApp
from tests import test_import_ui as import_ui
from tests import test_ui as base_ui


def brief_record():
    return {"binding": {"revision": 1, "work_turns": 1, "requirements_hash": "known", "source_fingerprint": None, "files": {}},
            "report": {"goal": "建议目标", "decisions": ["沿用当前方案"], "completed": ["已有候选稿"],
                       "unknowns": ["尚待人工确认"], "approaches": ["核对现有文件"], "acceptance": ["满足已记录约束"],
                       "next_step": "读取候选稿", "evidence": ["本地报告引用"]},
            "status": "current", "decision": "pending"}


def review_record():
    return {"binding": {"revision": 1, "work_turns": 1, "requirements_hash": "known", "source_fingerprint": None, "files": {}},
            "report": {"verdict": "ready_for_user", "summary": "可交人工检查", "findings": [
                {"severity": "low", "category": "content", "finding": "存在表述待确认", "suggestion": "核对原文", "evidence": ["候选稿"]}],
                "checks": [{"category": "requirements", "result": "observed", "finding": "约束已列出", "evidence": ["任务要求"]}],
                "unknowns": ["未确认发布要求"], "test_evidence": ["引用已有报告，不代表重新验证"]},
            "status": "current", "decision": "pending", "machine_checks": "not_verified", "human_acceptance": "pending"}


class AssessmentManager(import_ui.ImportManager):
    def __init__(self, state_dir=None):
        super().__init__(state_dir)
        self.assessment_calls = []
        self.tasks[0].update(work_turns=1, revision=1, brief=None, review=None, brief_required=False)

    def action(self, method, task_id, *values):
        self.record(method)
        self.assessment_calls.append((method, task_id, values))
        time.sleep(self.delay)
        if self.fail_method == method:
            raise ValueError("文件已变化，请重新核验")
        return next(task for task in self.tasks if task["id"] == task_id)

    def analyze(self, task_id, kind="brief"):
        task = self.action("analyze", task_id, kind)
        task[kind] = brief_record() if kind == "brief" else review_record()
        return deepcopy(task)

    def adopt_brief(self, task_id, goal, acceptance):
        task = self.action("adopt_brief", task_id, goal, acceptance)
        task.update(goal=goal, acceptance=acceptance, brief_required=False)
        task["brief"].update(decision="adopted", adopted_goal=goal, adopted_acceptance=acceptance)
        return deepcopy(task)

    def accept_review(self, task_id):
        task = self.action("accept_review", task_id)
        task["review"].update(decision="accepted", human_acceptance="accepted")
        return deepcopy(task)

    def revise_from_review(self, task_id):
        task = self.action("revise_from_review", task_id)
        task["review"]["decision"] = "revise"
        return deepcopy(task)


class AssessmentUiTests(unittest.TestCase):
    wait_for = base_ui.TkSmokeTests.wait_for
    tearDown = base_ui.TkSmokeTests.tearDown

    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as error:
            self.skipTest(f"Tk display unavailable: {error}")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.main_thread = threading.get_ident()
        self.make_app()

    def make_app(self):
        def factory(state_dir=None):
            self.fake = AssessmentManager(state_dir)
            return self.fake
        self.app = RelayApp(self.root, state_dir=self.temp.name, factory=factory)
        self.wait_for(lambda: self.app.ready and self.app.selected_id == "task-1")

    def open_assessment(self):
        self.app.assessment_button.invoke()
        return self.app.assessment_dialogs[self.app.selected_id]

    def seed_reports(self):
        self.fake.tasks[0].update(brief=brief_record(), review=review_record())
        self.wait_for(lambda: bool(self.app.tasks["task-1"].get("review")))

    def test_brief_generation_and_adoption_are_explicit_and_bound_to_task(self):
        dialog = self.open_assessment()
        self.assertFalse(self.fake.assessment_calls)
        dialog.generate_brief.invoke()
        self.wait_for(lambda: not self.app.busy and bool(self.app.tasks["task-1"].get("brief")))
        self.assertEqual(self.fake.assessment_calls[0], ("analyze", "task-1", ("brief",)))
        self.assertIn("沿用当前方案", dialog.brief_text.get("1.0", "end-1c"))
        dialog.goal_text.delete("1.0", "end")
        dialog.goal_text.insert("1.0", "人工修订目标")
        dialog.acceptance_text.delete("1.0", "end")
        dialog.acceptance_text.insert("1.0", "检查项一\n检查项二")
        dialog.adopt_button.invoke()
        self.wait_for(lambda: not self.app.busy)
        self.assertEqual(self.fake.assessment_calls[-1], ("adopt_brief", "task-1", ("人工修订目标", ["检查项一", "检查项二"])))
        self.assertFalse(self.fake.starts)
        self.assertTrue(all(thread != self.main_thread for method, thread in self.fake.calls if method == "analyze"))
        dialog.destroy()
        reopened = self.open_assessment()
        self.assertEqual(reopened.goal_text.get("1.0", "end-1c"), "人工修订目标")
        self.assertIn("检查项二", reopened.acceptance_text.get("1.0", "end-1c"))
        self.assertTrue(reopened.adopt_button.instate(["disabled"]))

    def test_report_refresh_preserves_edits_and_separates_review_acceptance(self):
        self.seed_reports()
        dialog = self.open_assessment()
        dialog.goal_text.insert("end", "，保留我的编辑")
        self.fake.tasks[0]["last_message"] = "无关进度刷新"
        self.wait_for(lambda: self.app.tasks["task-1"]["last_message"] == "无关进度刷新")
        self.assertIn("保留我的编辑", dialog.goal_text.get("1.0", "end-1c"))
        text = dialog.review_text.get("1.0", "end-1c")
        self.assertIn("AI 审查意见", text)
        self.assertIn("未独立验证", text)
        self.assertIn("人工未认可", text)
        self.assertIn("存在表述待确认", text)
        dialog.accept_button.invoke()
        self.wait_for(lambda: not self.app.busy and self.app.tasks["task-1"]["review"]["human_acceptance"] == "accepted")
        self.assertIn("人工已认可", dialog.review_text.get("1.0", "end-1c"))
        self.assertNotIn("发布通过", dialog.review_text.get("1.0", "end-1c"))
        self.assertFalse(self.fake.starts)

    def test_stale_active_pending_and_inspection_disable_mutations(self):
        self.seed_reports()
        dialog = self.open_assessment()
        for key in ("brief", "review"):
            self.fake.tasks[0][key]["status"] = "stale"
        self.wait_for(lambda: self.app.tasks["task-1"]["brief"]["status"] == "stale")
        self.assertTrue(dialog.adopt_button.instate(["disabled"]))
        self.assertTrue(dialog.accept_button.instate(["disabled"]))
        self.assertTrue(dialog.revise_button.instate(["disabled"]))
        for state in ("briefing", "reviewing", "needs_reconcile"):
            self.fake.tasks[0]["state"] = state
            self.wait_for(lambda: self.app.tasks["task-1"]["state"] == state)
            self.assertTrue(dialog.generate_brief.instate(["disabled"]))
            self.assertTrue(dialog.generate_review.instate(["disabled"]))
        self.fake.tasks[0].update(state="idle", pending=[{"id": "pending"}])
        self.wait_for(lambda: bool(self.app.tasks["task-1"]["pending"]))
        self.assertTrue(dialog.generate_brief.instate(["disabled"]))
        self.app.recovery_info = {"mode": "inspection"}
        self.app._controls()
        self.assertTrue(dialog.generate_brief.instate(["disabled"]))
        self.assertFalse(self.app.submit("analyze", "task-1", "brief"))

    def test_review_requires_work_and_revise_dispatches_once_without_extra_start(self):
        self.fake.tasks[0]["work_turns"] = 0
        self.wait_for(lambda: self.app.tasks["task-1"].get("work_turns") == 0)
        dialog = self.open_assessment()
        self.assertTrue(dialog.generate_review.instate(["disabled"]))
        self.fake.tasks[0].update(work_turns=1, review=review_record())
        self.wait_for(lambda: bool(self.app.tasks["task-1"].get("review")))
        self.assertIn("开始返工", dialog.revise_button.cget("text"))
        dialog.revise_button.invoke()
        self.wait_for(lambda: not self.app.busy)
        self.assertEqual(self.fake.assessment_calls[-1], ("revise_from_review", "task-1", ()))
        self.assertFalse(self.fake.starts)

    def test_pending_required_brief_blocks_repeat_main_start_and_analysis_history_can_refresh_import(self):
        self.fake.tasks[0].update(brief_required=True, brief=brief_record())
        self.wait_for(lambda: bool(self.app.tasks["task-1"].get("brief_required")))
        self.assertTrue(self.app.buttons["start"].instate(["disabled"]))
        self.fake.existing = dict(self.fake.tasks[0], thread_id=None, work_turns=0, state="idle", usage=900,
                                 history=[{"thread_id": "analysis-old", "role": "analysis"}])
        self.app.import_button.invoke()
        dialog = self.app.import_dialog
        self.wait_for(lambda: not self.app.busy)
        dialog.tree.selection_set("source-a")
        dialog.select_source()
        dialog.preview_button.invoke()
        self.wait_for(lambda: not self.app.busy)
        self.assertTrue(dialog.can_save())
        self.assertIn("更新待启动任务", dialog.save_button.cget("text"))
        self.assertEqual(dialog.goal_text.get("1.0", "end-1c"), "")
        dialog.source_stopped.set(True)
        dialog.controls()
        dialog.save_button.invoke()
        self.wait_for(lambda: not self.app.busy)
        self.assertEqual(self.fake.import_calls[-1][2]["goal"], "")
        self.assertTrue(self.app.tasks["task-1"]["brief_required"])

    def test_late_result_after_closed_window_does_not_rebind_other_task(self):
        self.fake.tasks.append(dict(self.fake.tasks[0], id="task-other", title="另一任务"))
        self.wait_for(lambda: "task-other" in self.app.tasks)
        old = self.open_assessment()
        self.fake.delay = .2
        old.generate_brief.invoke()
        old.destroy()
        self.app.task_tree.selection_set("task-other")
        self.app._select_task()
        current = self.open_assessment()
        self.wait_for(lambda: not self.app.busy)
        self.assertEqual(current.task_id, "task-other")
        self.assertNotIn("建议目标", current.goal_text.get("1.0", "end-1c"))
        self.assertIsNone(self.app.tasks["task-other"].get("brief"))
        self.assertEqual(self.fake.assessment_calls[0][1], "task-1")

    def test_backend_error_stays_in_origin_and_does_not_retry(self):
        dialog = self.open_assessment()
        self.fake.fail_method = "analyze"
        dialog.generate_brief.invoke()
        self.wait_for(lambda: not self.app.busy)
        self.assertIn("文件已变化", dialog.info.get())
        self.assertEqual(len(self.fake.assessment_calls), 1)
        self.assertFalse(self.fake.starts)

    def test_empty_import_goal_queues_brief_without_starting(self):
        self.app.import_button.invoke()
        dialog = self.app.import_dialog
        self.wait_for(lambda: not self.app.busy)
        dialog.tree.selection_set("source-a")
        dialog.select_source()
        dialog.preview_button.invoke()
        self.wait_for(lambda: not self.app.busy)
        dialog.source_stopped.set(True)
        dialog.controls()
        dialog.save_button.invoke()
        self.wait_for(lambda: not self.app.busy)
        self.assertEqual(self.fake.import_calls[0][2]["goal"], "")
        task = self.app.tasks[self.app.selected_id]
        self.assertTrue(task["brief_required"])
        self.assertEqual(task["state"], "queued")
        self.assertIn("先整理", self.app.buttons["start"].cget("text"))
        self.assertIn("模型用量", self.app.details.get())
        self.assertFalse(self.fake.starts)

    def test_assessment_actions_visible_at_96_and_144_dpi(self):
        for dpi in (96, 144):
            with self.subTest(dpi=dpi):
                self.app.request_close()
                self.wait_for(lambda: self.app.closed)
                self.app.worker.join(timeout=2)
                self.app = None
                gc.collect()
                self.root = tk.Tk()
                self.root.tk.call("tk", "scaling", dpi / 72)
                self.make_app()
                dialog = self.open_assessment()
                for tab in (0, 1):
                    dialog.tabs.select(tab)
                    self.root.update()
                    buttons = (dialog.generate_brief, dialog.adopt_button) if tab == 0 else (
                        dialog.generate_review, dialog.accept_button, dialog.revise_button)
                    for button in buttons:
                        self.assertTrue(button.winfo_ismapped())
                        self.assertLessEqual(button.winfo_rooty() + button.winfo_height(), dialog.winfo_rooty() + dialog.winfo_height())
                        self.assertLessEqual(button.winfo_rootx() + button.winfo_width(), dialog.winfo_rootx() + dialog.winfo_width())
                dialog.destroy()


if __name__ == "__main__":
    unittest.main()
