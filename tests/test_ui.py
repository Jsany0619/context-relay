"""Tk integration smoke checks with a fake controller; no Codex sessions are started."""
from copy import deepcopy
import gc
import json
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest import mock

from relay.ui import CommandWorker, NewTaskDialog, RelayApp


class FakeManager:
    def __init__(self, state_dir=None):
        self.calls = []
        self.answers = []
        self.prepared_task_ids = []
        self.starts = []
        self.archives = []
        self.reopens = []
        self.exports = []
        self.diagnostic_exports = []
        self.diagnostic_error = None
        self.diagnostic_read_error = None
        self.settings = []
        self.backups = []
        self.backup_error = None
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

    def backup_state(self, destination):
        self.record("backup_state")
        if self.backup_error:
            raise OSError(self.backup_error)
        self.backups.append(destination)
        return {"path": destination, "task_count": len(self.tasks), "event_count": 4,
                "file_count": 2, "created_at": "2026-09-30T12:00:00Z"}

    def export_task(self, task_id, destination):
        self.record("export_task")
        self.exports.append((task_id, destination))
        return {"path": destination}

    def reopen_task(self, task_id):
        self.record("reopen_task")
        self.reopens.append(task_id)
        task = next(task for task in self.tasks if task["id"] == task_id)
        task["state"] = "paused"
        return deepcopy(task)

    def diagnostics(self):
        self.record("diagnostics")
        time.sleep(self.delay)
        if self.diagnostic_read_error:
            raise ValueError(self.diagnostic_read_error)
        return {"format": "context-relay-diagnostics", "version": 1,
                "captured_at": "2026-10-04T12:00:00Z", "runtime": {"platform": "Windows"},
                "inspection_only": bool(getattr(self, "recovery_info", None)),
                "tasks": {"total": len(self.tasks)}, "counts": {}, "boundaries": ["local metadata only"]}

    def export_diagnostics(self, destination):
        self.record("export_diagnostics")
        if self.diagnostic_error:
            raise OSError(self.diagnostic_error)
        self.diagnostic_exports.append(destination)
        return {"path": destination}

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

    def set_inspection(self):
        info = {"mode": "inspection", "source_state_dir": self.temp.name,
                "backup_created_at": "2026-09-30T12:00:00Z", "restored_at": "2026-09-30T13:00:00Z"}
        self.app.worker.events.put(("ready", info))
        self.wait_for(lambda: getattr(self.app, "recovery_info", None) == info)

    def test_global_backup_does_not_need_selected_task_and_reports_counts(self):
        self.app.search.set("no match")
        self.assertIsNone(self.app.selected_id)
        destination = str(Path(self.temp.name) / "manager.zip")
        with mock.patch("relay.ui.filedialog.asksaveasfilename", return_value=destination):
            self.app.backup_button.invoke()
        self.wait_for(lambda: self.fake.backups and not self.app.busy)
        self.assertEqual(self.fake.backups, [destination])
        self.assertIn(destination, self.app.status.get())
        self.assertIn("任务 1", self.app.status.get())
        self.assertIn("事件 4", self.app.status.get())
        self.assertIn("文件 2", self.app.status.get())
        self.assertIn("明文", self.app.backup_notice.cget("text"))
        self.assertIn("原生聊天", self.app.backup_notice.cget("text"))
        self.assertEqual({identifier for method, identifier in self.fake.calls if method == "backup_state"},
                         {self.app.worker.ident})

    def test_task_export_offers_markdown_default_and_json_on_worker(self):
        for suffix in (".md", ".json"):
            destination = str(Path(self.temp.name) / ("task" + suffix))
            with mock.patch("relay.ui.filedialog.asksaveasfilename", return_value=destination) as choose:
                self.app.buttons["export_task"].invoke()
            self.wait_for(lambda: not self.app.busy)
            self.assertEqual(choose.call_args.kwargs["defaultextension"], ".md")
            self.assertIn(("Markdown", "*.md"), choose.call_args.kwargs["filetypes"])
            self.assertIn(("JSON", "*.json"), choose.call_args.kwargs["filetypes"])
            self.assertEqual(self.fake.exports[-1], ("task-1", destination))
        with mock.patch("relay.ui.filedialog.asksaveasfilename", return_value=""):
            self.app.buttons["export_task"].invoke()
        self.assertEqual(len(self.fake.exports), 2)
        self.assertEqual({ident for name, ident in self.fake.calls if name == "export_task"}, {self.app.worker.ident})

    def test_reopen_requires_confirmation_and_does_not_start(self):
        self.fake.tasks[0]["state"] = "completed"
        self.wait_for(lambda: not self.app.buttons["reopen_task"].instate(["disabled"]))
        with mock.patch("relay.ui.messagebox.askyesno", return_value=False):
            self.app.buttons["reopen_task"].invoke()
        self.assertFalse(self.fake.reopens)
        with mock.patch("relay.ui.messagebox.askyesno", return_value=True) as confirm:
            self.app.buttons["reopen_task"].invoke()
        self.assertIn("不会自动", confirm.call_args.args[1])
        self.wait_for(lambda: not self.app.busy and self.app.tasks["task-1"]["state"] == "paused")
        self.assertEqual(self.fake.reopens, ["task-1"])
        self.assertFalse(self.fake.starts)
        self.assertIn("未启动", self.app.status.get())
        self.fake.tasks[0]["events"] = [{"at": "2026-10-04T12:00:00Z", "kind": "task_reopened", "data": {}}]
        self.app.buttons["get_task"].invoke()
        self.wait_for(lambda: not self.app.busy)
        window = next(child for child in self.root.winfo_children() if hasattr(child, "history_text"))
        self.assertIn("重新打开", window.history_text.get("1.0", "end-1c"))
        self.assertEqual({ident for name, ident in self.fake.calls if name == "reopen_task"}, {self.app.worker.ident})

    def test_reopen_disabled_when_not_completed_or_not_quiet_or_inspecting(self):
        for changes in ({"state": "idle"}, {"state": "completed", "archived": True},
                        {"inflight": {"item": {}}}, {"intent": {"kind": "turn"}},
                        {"pending": [{"id": "request"}]}, {"receiver_id": "receiver"}, {"run_started": 1},
                        {"analysis_thread_id": "analysis"}):
            with self.subTest(changes=changes):
                self.fake.tasks[0].update(state="completed", archived=False, inflight={}, intent=None,
                                          pending=[], receiver_id=None, run_started=None, analysis_thread_id=None)
                self.fake.tasks[0].update(changes)
                self.wait_for(lambda: all(self.app.tasks["task-1"].get(key) == value for key, value in changes.items()))
                self.assertTrue(self.app.buttons["reopen_task"].instate(["disabled"]))
                self.app.buttons["reopen_task"].invoke()
        self.assertFalse(self.fake.reopens)
        self.set_inspection()
        self.assertFalse(self.app.submit("reopen_task", "task-1"))

    def test_diagnostics_global_preview_and_explicit_save_use_worker(self):
        self.app.message_text.insert("1.0", "保留草稿")
        self.app.search.set("no matching task")
        self.assertIsNone(self.app.selected_id)
        self.app.diagnostics_button.invoke()
        self.wait_for(lambda: not self.app.busy)
        window = self.app.diagnostics_window
        preview = window.report_text.get("1.0", "end-1c")
        self.assertEqual(json.loads(preview)["format"], "context-relay-diagnostics")
        self.assertNotIn("测试任务", preview)
        self.assertEqual(str(window.report_text.cget("state")), "disabled")
        self.assertFalse(self.fake.diagnostic_exports)
        with mock.patch("relay.ui.filedialog.asksaveasfilename", return_value=""):
            window.save_button.invoke()
        self.assertFalse(self.fake.diagnostic_exports)
        destination = str(Path(self.temp.name) / "diagnostics.json")
        with mock.patch("relay.ui.filedialog.asksaveasfilename", return_value=destination) as choose:
            window.save_button.invoke()
        self.wait_for(lambda: not self.app.busy and self.fake.diagnostic_exports)
        self.assertEqual(self.fake.diagnostic_exports, [destination])
        self.assertFalse(choose.call_args.kwargs["confirmoverwrite"])
        self.assertIn("诊断", self.app.status.get())
        self.assertFalse(self.fake.starts)
        self.app.search.set("")
        self.assertEqual(self.app.message_text.get("1.0", "end-1c"), "保留草稿")
        self.assertEqual({ident for name, ident in self.fake.calls if name in ("diagnostics", "export_diagnostics")},
                         {self.app.worker.ident})

    def test_diagnostics_inspection_export_error_preserves_readonly_preview(self):
        self.set_inspection()
        self.app.diagnostics_button.invoke()
        self.wait_for(lambda: not self.app.busy)
        window = self.app.diagnostics_window
        previous = window.report_text.get("1.0", "end-1c")
        self.fake.diagnostic_error = "目标文件已存在，不能覆盖"
        with mock.patch("relay.ui.filedialog.asksaveasfilename", return_value="existing.json"):
            window.save_button.invoke()
        self.wait_for(lambda: not self.app.busy and "不能覆盖" in self.app.status.get())
        self.assertEqual(window.report_text.get("1.0", "end-1c"), previous)
        self.assertFalse(self.fake.diagnostic_exports)
        self.assertFalse(self.fake.starts)

    def test_diagnostics_late_result_does_not_reopen_closed_preview(self):
        self.fake.delay = 0.2
        self.app.diagnostics_button.invoke()
        window = self.app.diagnostics_window
        window.destroy()
        self.wait_for(lambda: not self.app.busy)
        self.assertFalse(window.winfo_exists())
        self.assertIs(self.app.diagnostics_window, window)
        self.assertFalse(self.fake.starts)

    def test_diagnostics_read_failure_stays_visible_and_cannot_save(self):
        self.fake.diagnostic_read_error = "诊断读取失败"
        self.app.diagnostics_button.invoke()
        self.wait_for(lambda: not self.app.busy)
        window = self.app.diagnostics_window
        self.assertIn("诊断读取失败", window.report_text.get("1.0", "end-1c"))
        self.assertTrue(window.save_button.instate(["disabled"]))
        self.assertFalse(self.fake.diagnostic_exports)

    def test_export_and_reopen_stay_bound_to_the_task_chosen_before_dialog(self):
        self.fake.tasks[0]["state"] = "completed"
        self.add_task(state="completed")
        self.wait_for(lambda: self.app.tasks["task-1"]["state"] == "completed")
        def choose():
            self.select_task("task-other")
            return "chosen-task.md"
        with mock.patch("relay.ui.filedialog.asksaveasfilename", side_effect=lambda **kwargs: choose()):
            self.app.buttons["export_task"].invoke()
        self.wait_for(lambda: not self.app.busy)
        self.assertEqual(self.fake.exports, [("task-1", "chosen-task.md")])
        self.select_task("task-1")
        with mock.patch("relay.ui.messagebox.askyesno", side_effect=lambda *args, **kwargs: bool(choose())):
            self.app.buttons["reopen_task"].invoke()
        self.wait_for(lambda: not self.app.busy)
        self.assertEqual(self.fake.reopens, ["task-1"])
        self.assertEqual(self.app.selected_id, "task-other")
        self.assertFalse(self.fake.starts)

    def test_close_during_diagnostics_does_not_show_late_preview(self):
        self.fake.delay = 0.2
        self.app.diagnostics_button.invoke()
        self.wait_for(lambda: any(name == "diagnostics" for name, _ in self.fake.calls))
        with mock.patch("relay.ui.messagebox.askokcancel", return_value=True):
            self.app.request_close()
        self.wait_for(lambda: self.app.closed)
        self.assertTrue(self.fake.closed)
        self.assertFalse(self.fake.diagnostic_exports)
        self.assertFalse(self.fake.starts)

    def test_backup_failure_keeps_message_draft_and_pending_or_active_tasks_disable_it(self):
        self.app.message_text.insert("1.0", "不能丢失的补充说明")
        self.fake.backup_error = "目标文件已存在"
        with mock.patch("relay.ui.filedialog.asksaveasfilename", return_value="already-exists.zip"):
            self.app.backup_button.invoke()
        self.wait_for(lambda: not self.app.busy and "目标文件已存在" in self.app.status.get())
        self.assertEqual(self.app.message_text.get("1.0", "end-1c"), "不能丢失的补充说明")
        self.add_task(state="running")
        self.app.search.set("测试任务")
        self.assertTrue(self.app.backup_button.instate(["disabled"]))
        self.fake.tasks[1].update(state="paused", pending=[{"id": "pending"}])
        self.wait_for(lambda: self.app.tasks["task-other"]["state"] == "paused")
        self.assertTrue(self.app.backup_button.instate(["disabled"]))
        self.fake.tasks[1]["pending"] = []
        self.wait_for(lambda: not self.app.backup_button.instate(["disabled"]))

    def test_inspection_blocks_mutations_and_questions_but_allows_local_inspection(self):
        self.set_inspection()
        self.assertIn("永久只读", self.app.recovery_banner.cget("text"))
        self.assertIn("并非当前", self.app.recovery_banner.cget("text"))
        self.assertIn("不能继续", self.app.recovery_banner.cget("text"))
        self.assertTrue(self.app.new_button.instate(["disabled"]))
        self.assertTrue(self.app.backup_button.instate(["disabled"]))
        for method in ("start", "pause", "handoff", "reconcile", "finish", "prepare_snapshot", "update_settings", "set_archived"):
            self.assertTrue(self.app.buttons[method].instate(["disabled"]), method)
            self.assertFalse(self.app.submit(method, "task-1"), method)
        self.assertFalse(self.app.submit("create_task", title="不可创建"))
        self.assertFalse(self.app.submit("backup_state", "backup.zip"))
        self.assertFalse(self.app.submit("answer", "task-1", 1, {"decision": "accept"}))
        self.set_pending({"id": 1, "method": "item/commandExecution/requestApproval", "params": {"command": "echo test"}})
        self.assertTrue(self.app.approval_allow.instate(["disabled"]))
        self.assertTrue(self.app.approval_decline.instate(["disabled"]))
        self.app._answer_approval("decline")
        self.assertFalse(self.fake.answers)
        self.set_pending({"id": 2, "method": "item/tool/requestUserInput", "params": {
            "questions": [{"id": "q", "question": "历史问题"}]}})
        self.assertFalse(self.app.question_values)
        self.assertFalse(any(child.winfo_manager() and child.winfo_class() in ("TButton", "TEntry", "TCombobox")
                             for child in self.app.answer_frame.winfo_children()))
        self.assertFalse(self.app.buttons["get_task"].instate(["disabled"]))
        self.assertTrue(self.app.submit("get_task", "task-1"))
        self.wait_for(lambda: not self.app.busy)
        self.assertTrue(self.app.submit("export_task", "task-1", "local-inspection.json"))
        self.wait_for(lambda: not self.app.busy)
        self.assertFalse(self.fake.starts)

    def test_inspection_close_does_not_interrupt_historical_running_task_or_tick_budget(self):
        self.set_inspection()
        self.fake.tasks[0].update(state="running", run_started=100, elapsed_seconds=60)
        self.wait_for(lambda: self.app.tasks["task-1"]["state"] == "running")
        with mock.patch("relay.budget.time.time", return_value=100000):
            self.app._controls()
            self.assertIn("1.00 /", self.app.budget_details.get())
        self.fake.close_delay = 0.1
        with mock.patch("relay.ui.messagebox.askokcancel") as confirm:
            self.app.request_close()
        confirm.assert_not_called()
        self.assertNotIn("中断", self.app.status.get())
        self.wait_for(lambda: self.app.closed)
        self.assertTrue(self.fake.closed)
        self.assertFalse(any(method in ("pause", "start", "answer") for method, _ in self.fake.calls))

    def test_settings_bound_to_opened_task_and_only_save(self):
        self.fake.tasks[0].update(max_tokens=500, max_minutes=2.5, auto_handoff=False)
        self.add_task()
        self.wait_for(lambda: self.app.tasks["task-1"].get("max_tokens") == 500)
        dialog = self.open_settings()
        self.assertEqual(dialog.fields["title"].get(), "测试任务")
        self.assertEqual(dialog.fields["max_minutes"].get(), "2.5")
        self.assertEqual(dialog.advanced_frame.winfo_manager(), "")
        dialog.advanced_open.set(True)
        dialog.toggle_advanced()
        self.root.update()
        self.assertEqual(dialog.advanced_frame.winfo_manager(), "grid")
        self.assertNotIn("task-1", " ".join(str(child.cget("text")) for child in dialog.winfo_children()
                                             if "text" in child.keys()))
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
        self.assertEqual(dialog.advanced_frame.winfo_manager(), "")
        dialog.advanced_open.set(True)
        dialog.toggle_advanced()
        self.root.update()
        self.assertEqual(dialog.advanced_frame.winfo_manager(), "grid")
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
        self.assertTrue(self.app.details_button.instate(["disabled"]))
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

    def test_attention_entry_recovers_filtered_error_without_control_calls(self):
        self.fake.tasks[0]["state"] = "running"
        self.wait_for(lambda: self.app.tasks["task-1"]["state"] == "running")
        self.app.state_filter.set("运行中")
        self.app.message_text.insert("1.0", "留给原任务的草稿")
        self.fake.tasks[0].update(state="needs_reconcile", error="连接已断开，需核对执行结果")
        self.wait_for(lambda: self.app.tasks["task-1"]["state"] == "needs_reconcile")
        self.assertEqual(self.app.state_filter.get(), "运行中")
        self.assertEqual(self.app.task_tree.get_children(), ())
        self.assertIsNone(self.app.selected_id)
        self.assertTrue(self.app.buttons["reconcile"].instate(["disabled"]))
        self.assertEqual(self.app.attention_count, 1)
        self.assertIn("待处理 1 项", self.app.attention_button.cget("text"))
        calls = [method for method, _ in self.fake.calls if method not in ("poll", "list_tasks")]
        with mock.patch.object(self.app, "submit", wraps=self.app.submit) as submit:
            self.app.attention_button.invoke()
            self.root.update()
        submit.assert_not_called()
        self.assertEqual(calls, [method for method, _ in self.fake.calls if method not in ("poll", "list_tasks")])
        self.assertEqual(self.app.state_filter.get(), "待处理")
        self.assertEqual(self.app.selected_id, "task-1")
        self.assertFalse(self.app.buttons["reconcile"].instate(["disabled"]))
        self.assertEqual(self.app.message_text.get("1.0", "end-1c"), "留给原任务的草稿")

    def test_attention_counts_tasks_ignores_search_and_archive_then_clears(self):
        self.fake.tasks[0]["pending"] = [{"id": 1, "method": "unsupported"}, {"id": 2, "method": "unsupported"}]
        self.add_task(state="blocked", archived=True)
        self.wait_for(lambda: len(self.app.tasks["task-1"]["pending"]) == 2)
        self.app.search.set("没有匹配项")
        self.assertEqual(self.app.task_tree.get_children(), ())
        self.assertEqual(self.app.attention_count, 1)
        self.app.attention_button.invoke()
        self.assertEqual(self.app.search.get(), "")
        self.assertEqual(self.app.task_tree.get_children(), ("task-1",))
        self.fake.tasks[0]["pending"] = []
        self.wait_for(lambda: self.app.tasks["task-1"]["pending"] == [])
        self.assertEqual(self.app.attention_count, 0)
        self.assertTrue(self.app.attention_button.instate(["disabled"]))

    def test_attention_browsing_allowed_while_busy_but_disabled_during_close(self):
        self.fake.tasks[0]["state"] = "blocked"
        self.wait_for(lambda: self.app.tasks["task-1"]["state"] == "blocked")
        self.app.search.set("hidden")
        try:
            self.app.busy = True
            self.app._controls()
            self.assertFalse(self.app.attention_button.instate(["disabled"]))
            self.app.attention_button.invoke()
            self.assertEqual(self.app.selected_id, "task-1")
            self.assertTrue(self.app.buttons["reconcile"].instate(["disabled"]))
            self.app.closing = True
            self.app._controls()
            self.assertTrue(self.app.attention_button.instate(["disabled"]))
        finally:
            self.app.busy = self.app.closing = False

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

    def test_send_preserves_whitespace_and_closed_original_window_stays_closed(self):
        original = "  请保留原话\n末尾空格  "
        self.app.message_text.insert("1.0", original)
        self.app.buttons["start"].invoke()
        self.wait_for(lambda: bool(self.fake.starts) and not self.app.busy)
        self.assertEqual(self.fake.starts[-1], ("task-1", original))
        self.assertEqual(self.app.message_text.get("1.0", "end-1c"), "")
        window = self.app.conversation_window = tk.Toplevel(self.root)
        window.destroy()
        self.app.busy = True
        self.app.worker.events.put(("command_done", ("read_chat", ("task-1",), {"entries": []})))
        self.wait_for(lambda: not self.app.busy)
        self.assertIs(self.app.conversation_window, window)
        self.assertFalse(window.winfo_exists())

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

    def test_pending_and_details_use_progressive_disclosure_without_stale_task_state(self):
        self.assertEqual(self.app.pending_frame.winfo_manager(), "")
        self.assertEqual(self.app.details_panel.winfo_manager(), "")
        self.app.details_button.invoke()
        self.root.update()
        self.assertEqual(self.app.details_panel.winfo_manager(), "pack")
        other = self.add_task(state="paused")
        self.select_task(other["id"])
        self.root.update()
        self.assertEqual(self.app.details_panel.winfo_manager(), "")
        self.assertEqual(self.app.details_button.cget("text"), "显示任务详情")
        self.select_task("task-1")
        self.set_pending({"id": 3, "method": "item/commandExecution/requestApproval",
                          "params": {"command": "echo synthetic"}})
        self.root.update()
        self.assertEqual(self.app.pending_frame.winfo_manager(), "pack")
        self.fake.tasks[0]["pending"] = []
        self.wait_for(lambda: not self.app.pending_requests)
        self.assertEqual(self.app.pending_frame.winfo_manager(), "")

    def test_more_menu_uses_existing_button_guards_and_keeps_primary_action_clear(self):
        self.root.deiconify()
        self.root.update()
        self.assertEqual(self.app.buttons["start"].cget("text"), "发送 / 继续")
        self.assertTrue(self.app.buttons["start"].winfo_ismapped())
        for widget in (self.app.phone_button, self.app.backup_button, self.app.diagnostics_button,
                       self.app.assessment_button, self.app.buttons["update_settings"],
                       self.app.buttons["prepare_snapshot"]):
            self.assertFalse(widget.winfo_ismapped())
        for key, widget in (("phone", self.app.phone_button), ("backup", self.app.backup_button),
                            ("diagnostics", self.app.diagnostics_button),
                            ("assessment", self.app.assessment_button),
                            ("update_settings", self.app.buttons["update_settings"]),
                            ("prepare_snapshot", self.app.buttons["prepare_snapshot"]),
                            ("reconcile", self.app.buttons["reconcile"])):
            expected = "disabled" if widget.instate(["disabled"]) else "normal"
            self.assertEqual(self.app.more_menu.entrycget(self.app.more_entries[key], "state"), expected)
        self.assertEqual(self.app.more_menu.entrycget(self.app.more_entries["reconcile"], "state"), "normal")
        self.fake.tasks[0].update(max_minutes=1, elapsed_seconds=60)
        self.wait_for(lambda: "已达到预算" in self.app.task_alert.get())
        self.assertTrue(self.app.task_alert_label.winfo_ismapped())
        self.assertEqual(self.app.more_menu.entrycget(self.app.more_entries["handoff"], "state"), "disabled")

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

    def test_tk_callback_exception_is_visible_without_changing_work_and_can_close(self):
        self.app.busy = True
        tasks = deepcopy(self.app.tasks)
        commands = self.app.worker.commands
        calls = [method for method, _ in self.fake.calls if method not in ("poll", "list_tasks")]

        def fail_callback():
            raise RuntimeError("测试回调失败")

        with mock.patch("relay.ui.messagebox.showerror") as showerror:
            self.root.after(0, fail_callback)
            self.root.update()
            showerror.assert_called_once_with("界面回调出错", "RuntimeError: 测试回调失败", parent=self.root)
        self.assertIn("RuntimeError: 测试回调失败", self.app.status.get())
        self.assertEqual(self.app.tasks, tasks)
        self.assertTrue(self.app.busy)
        self.assertIs(self.app.worker.commands, commands)
        self.assertTrue(commands.empty())
        self.assertEqual([method for method, _ in self.fake.calls if method not in ("poll", "list_tasks")], calls)
        self.assertTrue(self.root.winfo_exists())
        with mock.patch("relay.ui.messagebox.askokcancel", return_value=True):
            self.app.request_close()
        self.wait_for(lambda: self.app.closed)
        self.assertTrue(self.fake.closed)
        self.assertEqual([method for method, _ in self.fake.calls if method not in ("poll", "list_tasks")], calls + ["close"])

    def test_render_callback_error_keeps_event_pump_and_close_working(self):
        calls = [method for method, _ in self.fake.calls if method not in ("poll", "list_tasks")]
        with mock.patch.object(self.app, "_render_tasks", side_effect=RuntimeError("render failed")), \
                mock.patch("relay.ui.messagebox.showerror") as showerror:
            self.app.worker.events.put(("tasks", deepcopy(self.fake.tasks)))
            self.wait_for(lambda: showerror.called)
        self.app.request_close()
        self.wait_for(lambda: self.app.closed)
        self.assertTrue(self.fake.closed)
        self.assertEqual([method for method, _ in self.fake.calls if method not in ("poll", "list_tasks")],
                         calls + ["close"])

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


class WorkerTests(unittest.TestCase):
    def test_ready_carries_optional_recovery_metadata(self):
        manager = FakeManager()
        manager.recovery_info = {"mode": "inspection", "backup_created_at": "known time"}
        worker = CommandWorker(lambda state_dir: manager, None)
        worker.start()
        try:
            self.assertEqual(worker.events.get(timeout=2), ("ready", manager.recovery_info))
        finally:
            worker.stop_requested.set()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())


class TkLayoutTests(unittest.TestCase):
    def test_snapshot_and_existing_actions_visible_at_supported_scaling(self):
        for dpi, inspection in ((95, False), (96, False), (144, False), (96, True), (144, True)):
            with self.subTest(dpi=dpi, inspection=inspection):
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
                        if inspection:
                            manager.recovery_info = {"mode": "inspection", "backup_created_at": "2026-09-30T12:00:00Z",
                                                     "restored_at": "2026-09-30T13:00:00Z"}
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
                    widgets = {"new": app.new_button, "import": app.import_button, "more": app.more_button,
                               "tree": app.task_tree, "details": app.details_button,
                               "latest": app.latest_text, "message": app.message_text,
                               "attention": app.attention_button, "primary": app.buttons["start"]}
                    if inspection:
                        widgets["recovery_banner"] = app.recovery_banner
                    for method, button in widgets.items():
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
                    self.assertFalse(app.details_panel.winfo_ismapped())
                    self.assertTrue(app.task_alert_label.winfo_ismapped())
                    for widget in (app.diagnostics_button, app.backup_button, app.backup_notice,
                                   app.buttons["get_task"], app.buttons["prepare_snapshot"]):
                        self.assertFalse(widget.winfo_ismapped())
                    self.assertEqual(app.more_menu.entrycget(app.more_entries["diagnostics"], "state"), "normal")
                    app.diagnostics_button.invoke()
                    deadline = time.monotonic() + 4
                    while app.busy and time.monotonic() < deadline:
                        root.update()
                        time.sleep(0.005)
                    self.assertFalse(app.busy)
                    window = app.diagnostics_window
                    root.update()
                    self.assertEqual(str(window.report_text.cget("state")), "disabled")
                    self.assertFalse(window.save_button.instate(["disabled"]))
                    for widget in (window.report_text, window.save_button):
                        self.assertTrue(widget.winfo_ismapped())
                        self.assertGreaterEqual(widget.winfo_rooty(), window.winfo_rooty())
                        self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                                             window.winfo_rooty() + window.winfo_height())
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
