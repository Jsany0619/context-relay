"""Tk integration smoke checks with a fake controller; no Codex sessions are started."""
from copy import deepcopy
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
                       "usage": {}, "context_estimate": 0.4, "compactions": 0}]
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
        self.tasks[0].update(state="running", last_message=message or "正在运行")

    def pause(self, task_id):
        self.record("pause")
        time.sleep(self.delay)
        self.tasks[0]["state"] = "paused"

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

    def set_pending(self, request):
        self.fake.tasks[0]["pending"] = [request]
        self.wait_for(lambda: self.app.pending_requests == [request])

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


if __name__ == "__main__":
    unittest.main()
