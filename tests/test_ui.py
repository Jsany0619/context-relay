"""Tk integration smoke checks with a fake controller; no Codex sessions are started."""
from copy import deepcopy
import gc
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest import mock

from relay.ui import NewTaskDialog, RelayApp


class FakeManager:
    def __init__(self, state_dir=None):
        self.calls = []
        self.answers = []
        self.prepared_task_ids = []
        self.starts = []
        self.archives = []
        self.settings = []
        self.delay = 0
        self.close_delay = 0
        self.close_failures = 0
        self.close_entered = threading.Event()
        self.closed = False
        self.start_created_on_poll = False
        self.created_pending = False
        self.tasks = [{"id": "task-1", "title": "测试任务", "cwd": str(state_dir), "goal": "完成一个可验证任务",
                       "mode": "read-only", "state": "idle", "thread_id": "thread-1", "turn_id": None,
                       "generation": 0, "last_message": "准备就绪", "error": None, "pending": [],
                       "usage": 0, "context_estimate": 0.4, "compactions": 0}]
        self.record("init")

    def record(self, method):
        self.calls.append((method, threading.get_ident()))

    def poll(self):
        self.record("poll")
        if self.start_created_on_poll and self.created_pending:
            self.created_pending = False
            self.start("task-2")

    def list_tasks(self):
        self.record("list_tasks")
        return deepcopy(self.tasks)

    def create_task(self, **values):
        self.record("create_task")
        time.sleep(self.delay)
        task = dict(self.tasks[0], **values)
        task.update(id="task-2", state="queued")
        self.tasks.append(task)
        self.created_pending = True
        return deepcopy(task)

    def start(self, task_id, message=None):
        self.record("start")
        self.starts.append((task_id, message))
        time.sleep(self.delay)
        next(task for task in self.tasks if task["id"] == task_id).update(
            state="running", last_message=message or "正在运行")

    def get_task(self, task_id):
        self.record("get_task")
        time.sleep(self.delay)
        return deepcopy(next(task for task in self.tasks if task["id"] == task_id))

    def set_archived(self, task_id, archived):
        self.record("set_archived")
        self.archives.append((task_id, archived))
        task = next(task for task in self.tasks if task["id"] == task_id)
        task["archived"] = archived
        return deepcopy(task)

    def update_settings(self, task_id, **values):
        self.record("update_settings")
        self.settings.append((task_id, values))
        task = next(task for task in self.tasks if task["id"] == task_id)
        task.update(values)
        return deepcopy(task)

    def pause(self, task_id):
        self.record("pause")
        time.sleep(self.delay)
        self.tasks[0]["state"] = "paused"

    def prepare_snapshot(self, task_id):
        self.record("prepare_snapshot")
        self.prepared_task_ids.append(task_id)
        self.tasks[0]["draft"] = {"created_at": "2026-09-30T12:34:56Z"}
        return deepcopy(self.tasks[0])

    def answer(self, task_id, request_id, answer):
        self.record("answer")
        self.answers.append((task_id, request_id, answer))
        self.tasks[0]["pending"] = [item for item in self.tasks[0]["pending"] if item["id"] != request_id]

    def close(self):
        self.record("close")
        self.close_entered.set()
        time.sleep(self.close_delay)
        if self.close_failures:
            self.close_failures -= 1
            raise RuntimeError("Closing could not be confirmed")
        self.closed = True


class TkSmokeTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as error:
            self.skipTest(f"Tk display unavailable: {error}")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.main_thread = threading.get_ident()

        def factory(state_dir=None):
            self.fake = FakeManager(state_dir)
            return self.fake

        self.app = RelayApp(self.root, state_dir=self.temp.name, factory=factory)
        self.wait_for(lambda: self.app.ready and self.app.selected_id == "task-1")

    def wait_for(self, condition, timeout=4):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() >= deadline:
                self.fail("Timed out waiting for Tk/worker state")
            if not self.app.closed:
                self.root.update()
            time.sleep(0.005)
        if not self.app.closed:
            self.root.update()

    def tearDown(self):
        if hasattr(self, "app"):
            if not self.app.closed:
                with mock.patch("relay.ui.messagebox.askokcancel", return_value=True):
                    self.app.request_close()
                self.wait_for(lambda: self.app.closed)
            self.app.worker.join(timeout=2)
            self.assertFalse(self.app.worker.is_alive(), "The UI must not leave an invisible worker")
            self.app = None
            gc.collect()  # Dispose Tk reference cycles on the UI thread before another worker starts.

    def set_pending(self, request):
        self.fake.tasks[0]["pending"] = [request]
        self.wait_for(lambda: self.app.pending_requests == [request])

    def add_task(self, **values):
        task = dict(self.fake.tasks[0], id="task-other", title="另一任务", goal="Other goal", **values)
        self.fake.tasks.append(task)
        self.wait_for(lambda: task["id"] in self.app.tasks)
        return task

    def select_task(self, task_id):
        self.app.task_tree.selection_set(task_id)
        self.app._select_task()

    def open_settings(self):
        self.app.buttons["update_settings"].invoke()
        return next(child for child in self.root.winfo_children()
                    if getattr(child, "task_id", None) == self.app.selected_id and hasattr(child, "save"))

    def test_settings_bound_to_opened_task_and_only_save(self):
        self.fake.tasks[0].update(max_tokens=500, max_minutes=2.5, auto_handoff=False)
        self.add_task()
        self.wait_for(lambda: self.app.tasks["task-1"].get("max_tokens") == 500)
        dialog = self.open_settings()
        self.assertEqual(dialog.fields["title"].get(), "测试任务")
        self.assertEqual(dialog.fields["max_minutes"].get(), "2.5")
        self.assertEqual(self.root.grab_current(), dialog)
        self.select_task("task-other")
        dialog.fields["title"].set("新名称")
        dialog.fields["max_tokens"].set("1000")
        dialog.fields["max_minutes"].set("3.75")
        dialog.auto_handoff.set(True)
        dialog.save()
        self.wait_for(lambda: self.fake.settings and not self.app.busy)
        self.assertEqual(self.fake.settings, [("task-1", {
            "title": "新名称", "max_tokens": 1000, "max_minutes": 3.75, "auto_handoff": True})])
        self.assertEqual(self.app.tasks["task-other"]["title"], "另一任务")
        self.assertFalse(self.fake.starts)
        self.assertEqual({identifier for method, identifier in self.fake.calls if method == "update_settings"},
                         {self.app.worker.ident})

    def test_settings_reject_invalid_limits_without_dispatch(self):
        dialog = self.open_settings()
        for tokens, minutes in (("1.5", "0"), ("True", "0"), ("-1", "0"),
                                ("0", "nan"), ("0", "inf"), ("0", "-0.1")):
            with self.subTest(tokens=tokens, minutes=minutes), mock.patch("relay.ui.messagebox.showwarning") as warning:
                dialog.fields["max_tokens"].set(tokens)
                dialog.fields["max_minutes"].set(minutes)
                dialog.save()
                warning.assert_called_once()
                self.assertFalse(self.fake.settings)
        dialog.destroy()

    def test_settings_disabled_during_work_unknown_execution_and_archive(self):
        for changes in ({"state": "running"}, {"state": "needs_reconcile"}, {"state": "completed"},
                        {"state": "idle", "intent": {"kind": "turn"}},
                        {"state": "paused", "run_started": 0},
                        {"state": "queued", "inflight": {"op": {}}},
                        {"state": "idle", "pending": [{"id": 1, "method": "unsupported"}]},
                        {"state": "idle", "receiver_id": "receiver"}, {"state": "completed", "archived": True}):
            with self.subTest(changes=changes):
                self.fake.tasks[0].update(state="idle", intent=None, run_started=None, inflight={},
                                          pending=[], receiver_id=None, archived=False)
                self.fake.tasks[0].update(changes)
                if changes.get("archived"):
                    self.app.state_filter.set("已归档")
                self.wait_for(lambda: all(self.app.tasks["task-1"].get(key) == value for key, value in changes.items()))
                self.assertTrue(self.app.buttons["update_settings"].instate(["disabled"]))
                self.app.buttons["update_settings"].invoke()
                self.assertFalse(self.fake.settings)

    def test_budget_limit_disables_start_and_handoff_but_keeps_settings_and_reconcile(self):
        for values in ({"usage": 100, "max_tokens": 100, "max_minutes": 0, "elapsed_seconds": 60},
                       {"usage": 0, "max_tokens": 0, "max_minutes": 1.5, "elapsed_seconds": 90}):
            with self.subTest(values=values):
                self.fake.tasks[0].update(values, auto_handoff=True)
                self.wait_for(lambda: self.app.tasks["task-1"].get("elapsed_seconds") == values["elapsed_seconds"])
                self.assertTrue(self.app.buttons["start"].instate(["disabled"]))
                self.assertTrue(self.app.buttons["handoff"].instate(["disabled"]))
                self.assertFalse(self.app.buttons["update_settings"].instate(["disabled"]))
                self.assertFalse(self.app.buttons["reconcile"].instate(["disabled"]))
                self.assertIn("已达预算", self.app.budget_details.get())
                self.assertIn("先调整预算或核对结果", self.app.budget_details.get())
                self.assertIn("自动交接：已开启", self.app.budget_details.get())
        self.assertIn("1.50 / 1.5", self.app.budget_details.get())

    def test_budget_display_refreshes_elapsed_wait_without_new_task_event(self):
        self.fake.tasks[0].update(state="running", elapsed_seconds=30, run_started=100, max_minutes=10)
        self.wait_for(lambda: self.app.tasks["task-1"]["state"] == "running")
        with mock.patch("relay.budget.time.time", return_value=130) as clock:
            self.app._controls()
            self.assertIn("1.00 / 10", self.app.budget_details.get())
            clock.return_value = 160
            self.app._controls()
            self.assertIn("1.50 / 10", self.app.budget_details.get())

    def test_settings_disable_auto_only_for_invalid_model_basis(self):
        self.fake.tasks[0].update(telemetry_model_valid=False, context_estimate=None)
        self.wait_for(lambda: self.app.tasks["task-1"].get("telemetry_model_valid") is False)
        dialog = self.open_settings()
        self.assertTrue(dialog.auto_check.instate(["disabled"]))
        self.assertIn("模型口径", dialog.notice.cget("text"))
        dialog.auto_handoff.set(True)
        with mock.patch("relay.ui.messagebox.showwarning") as warning:
            dialog.save()
        warning.assert_called_once()
        self.assertFalse(self.fake.settings)
        dialog.destroy()
        self.fake.tasks[0]["telemetry_model_valid"] = True
        self.wait_for(lambda: self.app.tasks["task-1"].get("telemetry_model_valid") is True)
        dialog = self.open_settings()
        self.assertFalse(dialog.auto_check.instate(["disabled"]))
        dialog.auto_handoff.set(True)
        dialog.save()
        self.wait_for(lambda: self.fake.settings and not self.app.busy)
        self.assertTrue(self.fake.settings[0][1]["auto_handoff"])

    def test_new_task_accepts_fractional_minutes_and_rejects_nonfinite(self):
        submit = mock.Mock(return_value=True)
        dialog = NewTaskDialog(self.root, submit)
        dialog.fields["title"].set("小预算任务")
        dialog.fields["cwd"].set(self.temp.name)
        dialog.goal.insert("1.0", "只读核对")
        for value in ("nan", "inf"):
            dialog.fields["max_minutes"].set(value)
            with mock.patch("relay.ui.messagebox.showwarning") as warning:
                dialog.save()
            warning.assert_called_once()
            submit.assert_not_called()
        dialog.fields["max_minutes"].set("0.5")
        with mock.patch("relay.ui.messagebox.showwarning"):
            dialog.save()
        self.assertIsNotNone(submit.call_args)
        self.assertEqual(submit.call_args.kwargs["max_minutes"], 0.5)

    def test_search_filters_title_goal_directory_without_losing_all_tasks(self):
        self.add_task(cwd="C:\\sample\\Folder", state="completed")
        for query in ("另一任务", "OTHER GOAL", "folder"):
            self.app.search.set(query)
            self.assertEqual(self.app.task_tree.get_children(), ("task-other",))
            self.assertEqual(self.app.selected_id, "task-other")
            self.assertEqual(len(self.app.tasks), 2)
        self.app.search.set("no matches")
        self.assertIsNone(self.app.selected_id)
        self.assertEqual(self.app.goal_text.get("1.0", "end-1c"), "")
        self.assertTrue(self.app.buttons["start"].instate(["disabled"]))
        self.app._action("start")
        self.assertFalse(self.fake.starts)

    def test_status_filters_and_hidden_running_task_still_protect_close(self):
        self.fake.tasks[0]["state"] = "running"
        self.add_task(state="completed", archived=True)
        self.wait_for(lambda: self.app.tasks["task-1"]["state"] == "running")
        for label, expected in (("运行中", ("task-1",)), ("待处理", ()),
                                ("已完成", ()), ("已归档", ("task-other",))):
            self.app.state_filter.set(label)
            self.assertEqual(self.app.task_tree.get_children(), expected)
        self.assertEqual(len(self.app.tasks), 2)
        with mock.patch("relay.ui.messagebox.askokcancel", return_value=False) as confirm:
            self.app.request_close()
        confirm.assert_called_once()
        self.assertFalse(self.app.closing)

    def test_archive_unarchive_completed_task_and_legacy_visibility(self):
        self.assertNotIn("archived", self.app.tasks["task-1"])
        self.assertTrue(self.app.buttons["set_archived"].instate(["disabled"]))
        self.fake.tasks[0]["state"] = "completed"
        self.wait_for(lambda: not self.app.buttons["set_archived"].instate(["disabled"]))
        self.app.buttons["set_archived"].invoke()
        self.wait_for(lambda: not self.app.busy and self.app.tasks["task-1"].get("archived"))
        self.assertEqual(self.app.task_tree.get_children(), ())
        self.assertIsNone(self.app.selected_id)
        self.app.state_filter.set("已归档")
        self.assertEqual(self.app.buttons["set_archived"].cget("text"), "取消归档")
        self.app.buttons["set_archived"].invoke()
        self.wait_for(lambda: not self.app.busy and not self.app.tasks["task-1"].get("archived"))
        self.app.state_filter.set("全部未归档")
        self.assertEqual(self.app.tasks["task-1"]["state"], "completed")
        self.assertEqual(self.fake.archives, [("task-1", True), ("task-1", False)])
        self.assertFalse(self.fake.starts)

    def test_archive_disabled_with_unknown_execution(self):
        self.fake.tasks[0].update(state="completed", intent={"kind": "turn"})
        self.wait_for(lambda: self.app.tasks["task-1"]["state"] == "completed")
        self.assertTrue(self.app.buttons["set_archived"].instate(["disabled"]))
        self.app.buttons["set_archived"].invoke()
        self.assertFalse(self.fake.archives)

    def test_new_task_clears_filters_and_selects_created_task(self):
        self.app.search.set("invisible")
        self.app.state_filter.set("已归档")
        self.app.submit("create_task", title="新任务", cwd=self.temp.name, goal="新的目标")
        self.wait_for(lambda: "task-2" in self.app.tasks and not self.app.busy)
        self.assertEqual(self.app.search.get(), "")
        self.assertEqual(self.app.state_filter.get(), "全部未归档")
        self.assertEqual(self.app.selected_id, "task-2")
        self.assertIn("task-2", self.app.task_tree.get_children())

    def test_task_drafts_are_separate_and_start_completion_clears_only_sent_task(self):
        self.add_task()
        self.app.message_text.insert("1.0", "给第一个任务")
        self.select_task("task-other")
        self.assertEqual(self.app.message_text.get("1.0", "end-1c"), "")
        self.app.message_text.insert("1.0", "给另一个任务")
        self.select_task("task-1")
        self.assertEqual(self.app.message_text.get("1.0", "end-1c"), "给第一个任务")
        self.fake.delay = 0.3
        self.app.buttons["start"].invoke()
        self.wait_for(lambda: bool(self.fake.starts))
        self.select_task("task-other")
        self.wait_for(lambda: not self.app.busy)
        self.assertEqual(self.app.message_text.get("1.0", "end-1c"), "给另一个任务")
        self.select_task("task-1")
        self.assertEqual(self.app.message_text.get("1.0", "end-1c"), "")
        self.assertEqual(self.fake.starts, [("task-1", "给第一个任务")])

    def test_start_completion_preserves_newer_edit_on_same_task(self):
        self.fake.delay = 0.3
        self.app.message_text.insert("1.0", "已发送的要求")
        self.app.buttons["start"].invoke()
        self.wait_for(lambda: bool(self.fake.starts))
        self.app.message_text.delete("1.0", "end")
        self.app.message_text.insert("1.0", "下一次补充")
        self.wait_for(lambda: not self.app.busy)
        self.assertEqual(self.app.message_text.get("1.0", "end-1c"), "下一次补充")
        self.assertEqual(self.fake.starts, [("task-1", "已发送的要求")])

    def test_history_is_read_only_redacted_and_bound_to_requested_task(self):
        self.add_task()
        self.fake.tasks[0]["events"] = [
            {"at": "2026-09-30T12:00:00Z", "kind": "turn_started", "data": {"goal": "SECRET-GOAL"}},
            {"at": "2026-09-30T12:00:01Z", "kind": "native_turn_started", "data": {"command": "SECRET-COMMAND"}},
            {"at": "2026-09-30T12:00:02Z", "kind": "turn_completed", "data": {"status": "completed", "purpose": "work", "answer": "SECRET-ANSWER"}},
            {"at": "2026-09-30T12:00:03Z", "kind": "future_event", "data": {"status": "SECRET-STATUS"}},
            {"at": "2026-09-30T12:00:04Z", "kind": "task_settings_updated", "data": {
                "changes": {"title": {"before": "SECRET-OLD", "after": "SECRET-NEW"}}}},
        ]
        self.fake.delay = 0.3
        self.app.buttons["get_task"].invoke()
        self.wait_for(lambda: any(method == "get_task" for method, _ in self.fake.calls))
        self.select_task("task-other")
        self.wait_for(lambda: not self.app.busy)
        window = next(child for child in self.root.winfo_children() if getattr(child, "task_id", None) == "task-1")
        text = window.history_text.get("1.0", "end-1c")
        self.assertIn("测试任务", text)
        self.assertIn("task-1", text)
        self.assertIn("2026-09-30T12:00:00Z", text)
        self.assertIn("启动回执", text)
        self.assertIn("原生轮次已开始", text)
        self.assertIn("不是完整对话", text)
        self.assertIn("外部成功凭证", text)
        self.assertIn("其他本地记录", text)
        self.assertIn("已更新任务设置", text)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("future_event", text)
        self.assertEqual(str(window.history_text.cget("state")), "disabled")
        worker_ids = {identifier for method, identifier in self.fake.calls if method == "get_task"}
        self.assertEqual(worker_ids, {self.app.worker.ident})
        window.destroy()
        self.fake.tasks[0]["events"].append({"at": "later", "kind": "draft_saved", "data": {}})
        self.select_task("task-1")
        self.app.buttons["get_task"].invoke()
        self.wait_for(lambda: not self.app.busy)
        refreshed = next(child for child in self.root.winfo_children() if getattr(child, "task_id", None) == "task-1")
        self.assertIn("later", refreshed.history_text.get("1.0", "end-1c"))

    def test_manager_calls_use_one_worker_and_tasks_refresh(self):
        self.assertFalse(self.app.worker.daemon)
        self.assertEqual(self.app.goal_text.get("1.0", "end-1c"), "完成一个可验证任务")
        self.assertTrue(self.app.submit("create_task", title="第二项", cwd=self.temp.name, goal="测试创建",
                                        mode="read-only", auto_handoff=False, max_tokens=0, max_minutes=0))
        self.wait_for(lambda: "task-2" in self.app.tasks and not self.app.busy)
        self.assertEqual(self.app.selected_id, "task-2")
        threads = {identifier for _, identifier in self.fake.calls}
        self.assertEqual(len(threads), 1)
        self.assertNotIn(self.main_thread, threads)

    def test_approval_shows_action_and_accepts_only_once(self):
        request = {"id": 17, "method": "item/commandExecution/requestApproval",
                   "params": {"command": "python -m unittest", "cwd": self.temp.name}}
        self.set_pending(request)
        self.assertIn("python -m unittest", self.app.request_text.get("1.0", "end-1c"))
        self.app._answer_approval("accept")
        self.wait_for(lambda: len(self.fake.answers) == 1 and not self.app.busy)
        self.assertEqual(self.fake.answers[0], ("task-1", 17, {"decision": "accept"}))
        self.app._answer_approval("accept")
        self.assertEqual(len(self.fake.answers), 1)

    def test_prepare_snapshot_uses_worker_and_displays_only_a_draft(self):
        self.assertNotIn("draft", self.app.tasks["task-1"])
        self.assertNotIn("预备快照", self.app.details.get())
        self.app.buttons["prepare_snapshot"].invoke()
        self.wait_for(lambda: self.app.tasks["task-1"].get("draft") and not self.app.busy)
        self.assertEqual(self.fake.prepared_task_ids, ["task-1"])
        self.assertIn("2026-09-30T12:34:56Z", self.app.details.get())
        self.assertIn("仅预备，交接前须重验", self.app.details.get())
        self.assertNotIn("READY", self.app.details.get())
        self.assertNotIn("已接管", self.app.details.get())
        self.assertEqual(self.app.tasks["task-1"]["state"], "idle")
        self.assertEqual(self.app.tasks["task-1"]["thread_id"], "thread-1")
        worker_ids = {identifier for method, identifier in self.fake.calls if method == "prepare_snapshot"}
        self.assertEqual(worker_ids, {self.app.worker.ident})

    def test_prepare_snapshot_only_available_for_idle_or_paused(self):
        button = self.app.buttons["prepare_snapshot"]
        for state in ("idle", "paused", "working", "running", "queued", "needs_reconcile",
                      "creating", "pausing", "summarizing", "verifying", "blocked", "completed", "unknown"):
            with self.subTest(state=state):
                self.fake.tasks[0]["state"] = state
                self.wait_for(lambda: self.app.tasks["task-1"]["state"] == state)
                self.assertEqual(button.instate(["disabled"]), state not in ("idle", "paused"))
                if state not in ("idle", "paused"):
                    button.invoke()
                    self.assertFalse(self.fake.prepared_task_ids)
        self.fake.tasks.clear()
        self.wait_for(lambda: self.app.selected_id is None)
        self.assertTrue(button.instate(["disabled"]))

    def test_unsupported_permissions_cannot_be_approved(self):
        self.set_pending({"id": "permission", "method": "item/permissions/requestApproval", "params": {}})
        self.assertEqual(self.app.approval_allow.winfo_manager(), "")
        self.app._answer_approval("accept")
        self.assertFalse(self.fake.answers)

    def test_file_approval_waits_for_actual_changes(self):
        self.set_pending({"id": "file-approval", "method": "item/fileChange/requestApproval",
                          "params": {"itemId": "file-item", "reason": "Update the file"}})
        self.assertTrue(self.app.approval_allow.instate(["disabled"]))
        self.app._answer_approval("accept")
        self.assertFalse(self.fake.answers)
        self.fake.tasks[0]["inflight"] = {"file-item": {"type": "fileChange", "changes": [
            {"path": "sample.txt", "diff": "-old\n+new"}]}}
        self.wait_for(lambda: not self.app.approval_allow.instate(["disabled"]))
        self.assertIn("sample.txt", self.app.request_text.get("1.0", "end-1c"))
        self.app._answer_approval("decline")
        self.wait_for(lambda: bool(self.fake.answers))
        self.assertEqual(self.fake.answers[0][2], {"decision": "decline"})

    def test_question_answers_keep_native_ids_and_shape(self):
        self.set_pending({"id": "question-request", "method": "item/tool/requestUserInput", "params": {
            "questions": [{"id": "language", "question": "使用什么语言？", "options": [{"label": "Python"}]},
                          {"id": "detail", "question": "说明要求", "isOther": True}]}})
        self.app.question_values["language"].set("Python")
        self.app.question_values["detail"].set("仅本地运行")
        self.app._answer_questions()
        self.wait_for(lambda: bool(self.fake.answers) and not self.app.busy)
        self.assertEqual(self.fake.answers[0][2], {"answers": {
            "language": {"answers": ["Python"]}, "detail": {"answers": ["仅本地运行"]}}})

    def test_secret_question_never_collects_an_answer(self):
        self.set_pending({"id": "secret-request", "method": "item/tool/requestUserInput", "params": {
            "questions": [{"id": "credential", "question": "Enter a credential", "isSecret": True}]}})
        self.assertFalse(self.app.question_values)
        self.app._answer_questions()
        self.assertFalse(self.fake.answers)
        cancel = next(child for child in self.app.answer_frame.winfo_children()
                      if child.winfo_class() == "TButton" and child.cget("text") == "取消并暂停")
        cancel.invoke()
        self.wait_for(lambda: any(method == "pause" for method, _ in self.fake.calls))
        self.assertFalse(self.fake.answers)

    def test_slow_operation_does_not_block_tk(self):
        self.fake.delay = 0.3
        heartbeat = []
        self.app.submit("pause", "task-1")
        self.root.after(15, lambda: heartbeat.append(True))
        self.wait_for(lambda: heartbeat)
        self.assertTrue(self.app.busy)
        self.wait_for(lambda: not self.app.busy)
        self.assertEqual(self.app.tasks["task-1"]["state"], "paused")

    def test_active_close_can_be_cancelled_and_waits_for_cleanup(self):
        self.fake.tasks[0]["state"] = "running"
        self.wait_for(lambda: self.app.tasks["task-1"]["state"] == "running")
        with mock.patch("relay.ui.messagebox.askokcancel", return_value=False):
            self.app.request_close()
        self.assertFalse(self.app.closing)
        self.assertFalse(self.fake.close_entered.is_set())
        self.fake.close_delay = 0.2
        with mock.patch("relay.ui.messagebox.askokcancel", return_value=True):
            self.app.request_close()
        self.wait_for(lambda: self.fake.close_entered.is_set())
        self.assertFalse(self.app.closed)
        self.assertTrue(self.root.winfo_exists())
        self.wait_for(lambda: self.app.closed)
        self.assertTrue(self.fake.closed)

    def test_failed_close_keeps_window_for_retry(self):
        self.fake.close_failures = 1
        self.app.request_close()
        self.wait_for(lambda: self.fake.close_entered.is_set() and not self.app.closing)
        self.assertFalse(self.app.closed)
        self.assertTrue(self.root.winfo_exists())
        self.assertIn("Closing could not be confirmed", self.app.status.get())
        self.app.request_close()
        self.wait_for(lambda: self.app.closed)

    def test_close_during_slow_create_never_polls_to_start_work(self):
        self.fake.delay = 0.3
        self.fake.start_created_on_poll = True
        self.assertIs(self.fake.stop_requested, self.app.worker.stop_requested)
        self.app.submit("create_task", title="慢创建", cwd=self.temp.name, goal="不要在关闭后启动")
        self.wait_for(lambda: any(method == "create_task" for method, _ in self.fake.calls))
        with mock.patch("relay.ui.messagebox.askokcancel", return_value=True):
            self.app.request_close()
        self.wait_for(lambda: self.app.closed)
        self.assertTrue(self.fake.closed)
        self.assertFalse(any(method == "start" for method, _ in self.fake.calls))

    def test_new_task_defaults_and_invalid_budget(self):
        submit = mock.Mock(return_value=True)
        dialog = NewTaskDialog(self.root, submit)
        dialog.withdraw()
        self.assertEqual(dialog.mode.get(), "只读")
        self.assertFalse(dialog.auto_handoff.get())
        dialog.fields["title"].set("新任务")
        dialog.fields["cwd"].set(self.temp.name)
        dialog.goal.insert("1.0", "明确完成标准")
        dialog.fields["max_tokens"].set("-1")
        with mock.patch("relay.ui.messagebox.showwarning"):
            dialog.save()
        submit.assert_not_called()
        dialog.fields["max_tokens"].set("1000")
        dialog.save()
        self.assertEqual(submit.call_args.kwargs["mode"], "read-only")
        self.assertFalse(submit.call_args.kwargs["auto_handoff"])
        self.assertEqual(submit.call_args.kwargs["max_tokens"], 1000)


class TkLayoutTests(unittest.TestCase):
    def test_snapshot_and_existing_actions_visible_at_supported_scaling(self):
        for dpi in (95, 96, 144):
            with self.subTest(dpi=dpi):
                try:
                    root = tk.Tk()
                except tk.TclError as error:
                    self.skipTest(f"Tk display unavailable: {error}")
                app = None
                try:
                    root.tk.call("tk", "scaling", dpi / 72)
                    def factory(state_dir=None):
                        manager = FakeManager(state_dir)
                        manager.tasks[0].update(max_minutes=1, elapsed_seconds=60, telemetry_model_valid=False)
                        return manager
                    app = RelayApp(root, factory=factory)
                    deadline = time.monotonic() + 4
                    while (not app.ready or app.selected_id is None) and time.monotonic() < deadline:
                        root.update()
                        time.sleep(0.005)
                    self.assertTrue(app.ready)
                    root.update()
                    self.assertIn("prepare_snapshot", app.buttons)
                    self.assertIn("set_archived", app.buttons)
                    self.assertIn("get_task", app.buttons)
                    self.assertIn("update_settings", app.buttons)
                    self.assertIn("已达预算", app.budget_details.get())
                    for method, button in dict(app.buttons, budget_label=app.budget_label).items():
                        self.assertTrue(button.winfo_ismapped(), method)
                        parent = button.master
                        while parent is not None:
                            self.assertGreaterEqual(button.winfo_rootx(), parent.winfo_rootx(), method)
                            self.assertGreaterEqual(button.winfo_rooty(), parent.winfo_rooty(), method)
                            self.assertLessEqual(button.winfo_rootx() + button.winfo_width(),
                                                 parent.winfo_rootx() + parent.winfo_width(), method)
                            self.assertLessEqual(button.winfo_rooty() + button.winfo_height(),
                                                 parent.winfo_rooty() + parent.winfo_height(), method)
                            parent = parent.master
                finally:
                    if app:
                        with mock.patch("relay.ui.messagebox.askokcancel", return_value=True):
                            app.request_close()
                        deadline = time.monotonic() + 4
                        while not app.closed and time.monotonic() < deadline:
                            root.update()
                            time.sleep(0.005)
                        app.worker.join(timeout=2)
                        self.assertFalse(app.worker.is_alive())
                    if app is None or not app.closed:
                        root.destroy()
                    app = None
                    gc.collect()


if __name__ == "__main__":
    unittest.main()
