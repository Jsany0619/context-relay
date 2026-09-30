"""Fake-only import UI checks; never connect to a native Codex session."""
from copy import deepcopy
import gc
import tempfile
import threading
import time
import tkinter as tk
import unittest

from relay.ui import RelayApp
from tests import test_ui as base_ui


class ImportManager(base_ui.FakeManager):
    def __init__(self, state_dir=None):
        super().__init__(state_dir)
        self.import_calls = []
        self.list_calls = []
        self.fail_method = None
        self.preview_delay = 0
        self.existing = None
        self.can_import = True

    def list_import_threads(self, search="", cursor=None, archived=False):
        self.record("list_import_threads")
        self.list_calls.append((search, cursor, archived))
        if self.fail_method == "list_import_threads":
            raise ValueError("列表读取失败")
        ids = ["source-b"] if cursor else ["source-a", "source-b"]
        return {"data": [{"id": key, "title": "来源 " + key, "cwd": self.tasks[0]["cwd"],
                          "updated_at": 1790812800, "status": "notLoaded", "source": "vscode",
                          "imported_task_id": None} for key in ids], "next_cursor": None if cursor else "page-2"}

    def preview_import(self, thread_id):
        self.record("preview_import")
        time.sleep(self.preview_delay)
        if self.fail_method == "preview_import":
            raise ValueError("预览读取失败")
        return {"thread_id": thread_id, "title": "预览 " + thread_id, "cwd": self.tasks[0]["cwd"],
                "fingerprint": "fp-" + thread_id, "read_at": "2026-10-01", "updated_at": "2026-10-01",
                "status": "notLoaded", "messages": [{"role": "user", "text": "旧目标，仅作资料", "turn_id": "old-turn", "item_id": "item"}],
                "omitted_messages": 2, "truncated_messages": 1, "non_text_items": 3,
                "can_import": self.can_import, "warnings": ["cross_client_activity_unknown", "synthetic_future_warning"],
                "existing_task": deepcopy(self.existing)}

    def import_thread(self, thread_id, fingerprint, **kwargs):
        self.record("import_thread")
        self.import_calls.append((thread_id, fingerprint, kwargs))
        if self.fail_method == "import_thread":
            raise ValueError("来源已改变，请重新预览")
        task = dict(self.tasks[0], **{key: kwargs[key] for key in ("title", "goal", "mode", "max_tokens", "max_minutes", "auto_handoff")})
        task.update(id=kwargs.get("existing_task_id") or "imported-task", state="queued", thread_id=None)
        task["source_snapshot"] = {"thread_id": thread_id}
        self.tasks = [entry for entry in self.tasks if entry["id"] != task["id"]] + [task]
        return deepcopy(task)


class ImportUiTests(unittest.TestCase):
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

        def factory(state_dir=None):
            self.fake = ImportManager(state_dir)
            return self.fake

        self.app = RelayApp(self.root, state_dir=self.temp.name, factory=factory)
        self.wait_for(lambda: self.app.ready and self.app.selected_id == "task-1")

    def open_import(self):
        self.app.import_button.invoke()
        dialog = self.app.import_dialog
        self.wait_for(lambda: not self.app.busy)
        return dialog

    def select(self, dialog, source="source-a"):
        dialog.tree.selection_set(source)
        dialog.select_source()

    def preview(self, dialog, source="source-a"):
        self.select(dialog, source)
        dialog.preview_button.invoke()
        self.wait_for(lambda: not self.app.busy)

    def fill(self, dialog):
        dialog.goal_text.insert("1.0", "本轮明确目标")
        dialog.source_stopped.set(True)
        dialog.controls()

    def test_listing_search_archive_and_pagination_use_worker(self):
        dialog = self.open_import()
        self.assertEqual(self.fake.list_calls, [("", None, False)])
        values = dialog.tree.item("source-a", "values")
        self.assertEqual(values[1], "桌面 / IDE")
        self.assertNotEqual(values[0], "1790812800")
        self.assertIn("2026-", values[0])
        self.assertFalse(self.fake.import_calls)
        dialog.search.set("关键字")
        dialog.archived.set(True)
        dialog.load_button.invoke()
        self.wait_for(lambda: not self.app.busy)
        dialog.next_button.invoke()
        self.wait_for(lambda: not self.app.busy)
        self.assertEqual(self.fake.list_calls[-2:], [("关键字", None, True), ("关键字", "page-2", True)])
        self.assertEqual(dialog.tree.get_children(), ("source-b",))
        self.assertTrue(all(thread != self.main_thread for method, thread in self.fake.calls if method == "list_import_threads"))

    def test_preview_is_explicit_and_import_requires_fresh_goal_confirmation(self):
        dialog = self.open_import()
        self.select(dialog)
        self.assertFalse(any(method == "preview_import" for method, _ in self.fake.calls))
        self.preview(dialog)
        text = dialog.preview_text.get("1.0", "end-1c")
        self.assertIn(self.temp.name, text)
        self.assertIn("旧目标，仅作资料", text)
        self.assertIn("无法确认原客户端", text)
        self.assertIn("未识别的来源提示", text)
        self.assertNotIn("synthetic_future_warning", text)
        self.assertIn("2", text)
        self.assertEqual(dialog.goal_text.get("1.0", "end-1c"), "")
        self.assertEqual(dialog.mode.get(), "read-only")
        self.assertFalse(dialog.source_stopped.get())
        dialog.save()
        self.assertFalse(self.fake.import_calls)
        dialog.source_stopped.set(True)
        dialog.save()
        self.assertIn("目标不能为空", dialog.info.get())
        self.assertFalse(self.fake.import_calls)
        self.fill(dialog)
        dialog.tokens.set("1200")
        dialog.minutes.set("2.5")
        dialog.save_button.invoke()
        self.wait_for(lambda: not self.app.busy)
        source, fingerprint, values = self.fake.import_calls[0]
        self.assertEqual((source, fingerprint), ("source-a", "fp-source-a"))
        self.assertEqual(values["goal"], "本轮明确目标")
        self.assertEqual((values["mode"], values["max_tokens"], values["max_minutes"]), ("read-only", 1200, 2.5))
        self.assertTrue(values["source_stopped"])
        self.assertFalse(values["auto_handoff"])
        self.assertEqual(self.app.selected_id, "imported-task")
        self.assertIn("历史摘录来源：source-a", self.app.details.get())
        self.assertFalse(self.fake.starts)

    def test_preview_selection_clears_authorization_goal_and_late_result(self):
        dialog = self.open_import()
        self.preview(dialog)
        self.fill(dialog)
        dialog.mode.set("workspace-write")
        dialog.auto.set(True)
        self.fake.preview_delay = .2
        dialog.preview_button.invoke()
        self.select(dialog, "source-b")
        self.wait_for(lambda: not self.app.busy)
        self.assertIsNone(dialog.preview)
        self.assertFalse(dialog.source_stopped.get())
        self.assertFalse(dialog.auto.get())
        self.assertEqual(dialog.mode.get(), "read-only")
        self.assertEqual(dialog.goal_text.get("1.0", "end-1c"), "")
        self.assertTrue(dialog.save_button.instate(["disabled"]))

    def test_closed_dialog_late_preview_does_not_fill_reopened_dialog(self):
        old = self.open_import()
        self.fake.preview_delay = .2
        self.select(old)
        old.preview_button.invoke()
        old.destroy()
        self.wait_for(lambda: not self.app.busy)
        current = self.open_import()
        self.assertIsNot(current, old)
        self.assertIsNone(current.preview)
        self.assertEqual(current.goal_text.get("1.0", "end-1c"), "")
        self.assertFalse(self.fake.import_calls)

    def test_confirmation_must_follow_arrival_of_preview(self):
        dialog = self.open_import()
        self.fake.preview_delay = .2
        self.select(dialog)
        dialog.preview_button.invoke()
        dialog.source_stopped.set(True)
        dialog.mode.set("workspace-write")
        dialog.auto.set(True)
        self.wait_for(lambda: not self.app.busy)
        self.assertFalse(dialog.source_stopped.get())
        self.assertEqual(dialog.mode.get(), "read-only")
        self.assertFalse(dialog.auto.get())
        self.assertTrue(dialog.save_button.instate(["disabled"]))

    def test_errors_remain_in_original_dialog_and_retry_is_explicit(self):
        self.fake.fail_method = "list_import_threads"
        dialog = self.open_import()
        self.assertIn("列表读取失败", dialog.info.get())
        self.fake.fail_method = None
        dialog.load_button.invoke()
        self.wait_for(lambda: not self.app.busy)
        self.fake.fail_method = "preview_import"
        self.preview(dialog)
        self.assertIn("预览读取失败", dialog.info.get())
        self.fake.fail_method = None
        self.preview(dialog)
        self.fill(dialog)
        self.fake.fail_method = "import_thread"
        dialog.save_button.invoke()
        self.wait_for(lambda: not self.app.busy)
        self.assertIn("来源已改变", dialog.info.get())
        self.assertEqual(len(self.fake.import_calls), 1)
        self.assertTrue(dialog.winfo_exists())

    def test_existing_unstarted_task_is_explicit_update_started_task_blocked(self):
        self.fake.existing = dict(self.fake.tasks[0], id="existing", state="paused", thread_id=None, goal="本地待办",
                                 max_tokens=1200, max_minutes=2.5, mode="workspace-write", auto_handoff=True)
        dialog = self.open_import()
        self.preview(dialog)
        self.assertEqual(dialog.goal_text.get("1.0", "end-1c"), "本地待办")
        self.assertIn("更新待启动任务", dialog.save_button.cget("text"))
        self.assertFalse(dialog.source_stopped.get())
        dialog.source_stopped.set(True)
        dialog.controls()
        dialog.save_button.invoke()
        self.wait_for(lambda: not self.app.busy)
        self.assertEqual(self.fake.import_calls[0][2]["existing_task_id"], "existing")
        values = self.fake.import_calls[0][2]
        self.assertEqual((values["max_tokens"], values["max_minutes"], values["mode"], values["auto_handoff"]),
                         (1200, 2.5, "workspace-write", True))
        self.fake.existing["thread_id"] = "already-started"
        dialog = self.open_import()
        self.preview(dialog)
        self.fill(dialog)
        self.assertTrue(dialog.save_button.instate(["disabled"]))
        self.assertIn("已有", dialog.info.get())
        self.fake.existing.update(thread_id=None, archived=True)
        self.preview(dialog)
        self.fill(dialog)
        self.assertTrue(dialog.save_button.instate(["disabled"]))

    def test_source_not_importable_displays_warning_and_never_dispatches(self):
        self.fake.can_import = False
        dialog = self.open_import()
        self.preview(dialog)
        self.fill(dialog)
        self.assertIn("无法确认原客户端", dialog.preview_text.get("1.0", "end-1c"))
        self.assertIn("不证明原聊天已停止", dialog.preview_text.get("1.0", "end-1c"))
        self.assertIn("未在本连接加载（原端状态未知）", dialog.preview_text.get("1.0", "end-1c"))
        self.assertTrue(dialog.save_button.instate(["disabled"]))
        dialog.save()
        self.assertFalse(self.fake.import_calls)

    def test_invalid_budget_and_recovery_library_do_not_dispatch_import(self):
        dialog = self.open_import()
        self.preview(dialog)
        self.fill(dialog)
        for token, minute in (("1.5", "0"), ("0", "nan"), ("0", "inf"), ("-1", "0")):
            dialog.tokens.set(token)
            dialog.minutes.set(minute)
            dialog.save()
            self.assertIn("输入", dialog.info.get())
        self.assertFalse(self.fake.import_calls)
        dialog.destroy()
        self.app.recovery_info = {"mode": "inspection"}
        self.app._controls()
        self.assertTrue(self.app.import_button.instate(["disabled"]))
        self.assertFalse(self.app.submit("preview_import", "source-a"))

    def test_import_dialog_footer_visible_at_supported_scaling(self):
        for dpi in (96, 144):
            with self.subTest(dpi=dpi):
                self.app.request_close()
                self.wait_for(lambda: self.app.closed)
                self.app.worker.join(timeout=2)
                self.app = None
                gc.collect()
                self.root = tk.Tk()
                self.root.tk.call("tk", "scaling", dpi / 72)

                def factory(state_dir=None):
                    self.fake = ImportManager(state_dir)
                    return self.fake

                self.app = RelayApp(self.root, state_dir=self.temp.name, factory=factory)
                self.wait_for(lambda: self.app.ready and self.app.selected_id == "task-1")
                dialog = self.open_import()
                self.root.update()
                for widget in (dialog.load_button, dialog.preview_button, dialog.save_button, dialog.close_button):
                    self.assertTrue(widget.winfo_ismapped())
                    self.assertGreaterEqual(widget.winfo_rooty(), dialog.winfo_rooty())
                    self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), dialog.winfo_rooty() + dialog.winfo_height())
                    self.assertGreaterEqual(widget.winfo_rootx(), dialog.winfo_rootx())
                    self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), dialog.winfo_rootx() + dialog.winfo_width())
                dialog.destroy()


if __name__ == "__main__":
    unittest.main()
