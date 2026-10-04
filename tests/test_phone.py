"""Desktop/phone ownership checks without native clients or real task state."""

from pathlib import Path
from contextlib import ExitStack
import threading
import time
import unittest
from unittest import mock

from relay.phone import PhoneHost, local_addresses, private_address
from tests import test_ui as base_ui


class PhoneUiTests(unittest.TestCase):
    def setUp(self):
        base_ui.TkSmokeTests.setUp(self)

    tearDown = base_ui.TkSmokeTests.tearDown
    wait_for = base_ui.TkSmokeTests.wait_for

    def test_plain_clicks_toggle_multiple_tasks_without_modifier_keys(self):
        self.app.tasks["task-two"] = dict(self.app.tasks["task-1"], id="task-two", title="第二个合成任务")
        with mock.patch("relay.phone.local_addresses", return_value=["127.0.0.1"]):
            self.app.phone_button.invoke()
        dialog = self.app.phone_dialog
        self.wait_for(lambda: not self.app.busy)
        dialog.deliver({"enabled": True, "endpoint": "https://192.0.2.1:8765", "devices": []})
        self.root.deiconify()
        dialog.deiconify()
        self.root.update()

        def click(index):
            bounds = dialog.task_list.bbox(index)
            self.assertIsNotNone(bounds)
            x, y, width, height = bounds
            dialog.task_list.event_generate("<Button-1>", x=x + 3, y=y + height // 2, state=0)
            dialog.task_list.event_generate("<ButtonRelease-1>", x=x + 3, y=y + height // 2, state=0)
            self.root.update()

        click(0)
        click(1)
        self.assertEqual(dialog.task_list.curselection(), (0, 1))
        self.assertEqual(dialog.selection_count.get(), "已选 2 / 共 2")
        dialog._show_pairing("contextrelay://pair#synthetic", dialog._choice_snapshot(), time.time() + 300)
        click(0)
        self.assertEqual(dialog.task_list.curselection(), (1,))
        self.assertEqual(dialog.selection_count.get(), "已选 1 / 共 2")
        self.assertEqual(dialog.pairing.get("1.0", "end-1c"), "")
        self.app.busy = True
        try:
            dialog.controls()
            click(1)
            self.assertEqual(dialog.task_list.curselection(), (1,))
        finally:
            self.app.busy = False
        self.assertFalse(self.fake.starts)

    def test_task_selection_shortcuts_count_and_invalidate_pairing_without_granting(self):
        original = self.app.tasks["task-1"]
        self.app.tasks["task-two"] = dict(original, id="task-two", title="第二个合成任务")
        self.app.tasks["task-three"] = dict(original, id="task-three", title="第三个合成任务")
        self.app.tasks["archived"] = dict(original, id="archived", archived=True)
        with mock.patch("relay.phone.local_addresses", return_value=["127.0.0.1"]):
            self.app.phone_button.invoke()
        dialog = self.app.phone_dialog
        self.wait_for(lambda: not self.app.busy)
        devices = [{"id": "synthetic-device", "task_ids": ["task-1"], "scope": "read_only"}]
        dialog.deliver({"enabled": True, "endpoint": "https://192.0.2.1:8765", "devices": devices})
        self.assertEqual(dialog.selection_count.get(), "已选 0 / 共 3")
        with mock.patch.object(dialog, "submit", return_value=True) as submit:
            dialog.select_all_button.invoke()
            self.assertEqual(dialog.selection_count.get(), "已选 3 / 共 3")
            self.assertEqual(dialog._choice_snapshot(), (("task-1", "task-two", "task-three"), "read_only"))
            submit.assert_not_called()
            dialog.pair()
            submit.assert_called_once_with("remote_pair", ["task-1", "task-two", "task-three"], "read_only")
        dialog._pairing_request = None
        dialog._show_pairing("contextrelay://pair#synthetic", dialog._choice_snapshot(), time.time() + 300)
        with mock.patch.object(dialog, "submit") as submit:
            dialog.clear_selection_button.invoke()
            submit.assert_not_called()
        self.assertEqual(dialog.selection_count.get(), "已选 0 / 共 3")
        self.assertEqual(dialog.pairing.get("1.0", "end-1c"), "")
        self.assertIsNone(dialog._pairing_snapshot)
        self.assertTrue(dialog.copy_button.instate(["disabled"]))
        self.assertEqual(dialog.devices, devices)
        self.assertEqual(dialog.devices[0]["task_ids"], ["task-1"])
        self.assertFalse(self.fake.starts)

    def test_new_tasks_remain_unselected_and_selection_count_does_not_flicker(self):
        with mock.patch("relay.phone.local_addresses", return_value=["127.0.0.1"]):
            self.app.phone_button.invoke()
        dialog = self.app.phone_dialog
        self.wait_for(lambda: not self.app.busy)
        dialog.select_all_button.invoke()
        self.app.tasks["task-two"] = dict(self.app.tasks["task-1"], id="task-two", title="新的合成任务")
        dialog.controls()
        self.assertEqual(dialog.selection_count.get(), "已选 1 / 共 2")
        self.assertEqual(dialog._choice_snapshot()[0], ("task-1",))
        self.app.tasks = dict(reversed(list(self.app.tasks.items())))
        dialog.controls()
        self.assertEqual(dialog.task_list.curselection(), (1,))
        self.assertEqual(dialog._choice_snapshot()[0], ("task-1",))
        with mock.patch.object(dialog.selection_count, "set", wraps=dialog.selection_count.set) as write:
            for _ in range(25):
                dialog.controls()
            write.assert_not_called()

    def test_task_selection_shortcuts_obey_busy_recovery_and_closing_gates(self):
        with mock.patch("relay.phone.local_addresses", return_value=["127.0.0.1"]):
            self.app.phone_button.invoke()
        dialog = self.app.phone_dialog
        self.wait_for(lambda: not self.app.busy)
        for field, blocked in (("busy", True), ("ready", False), ("closing", True), ("recovery_info", {})):
            previous = getattr(self.app, field)
            try:
                setattr(self.app, field, blocked)
                dialog.controls()
                self.assertTrue(dialog.select_all_button.instate(["disabled"]))
                self.assertTrue(dialog.clear_selection_button.instate(["disabled"]))
                dialog._select_tasks(True)
                self.assertEqual(dialog.task_list.curselection(), ())
            finally:
                setattr(self.app, field, previous)
        self.app.tasks.clear()
        dialog.controls()
        self.assertEqual(dialog.selection_count.get(), "已选 0 / 共 0")
        self.assertTrue(dialog.select_all_button.instate(["disabled"]))
        self.assertTrue(dialog.clear_selection_button.instate(["disabled"]))

    def test_unchanged_controls_do_not_repack_or_reconfigure(self):
        with mock.patch("relay.phone.local_addresses", return_value=["127.0.0.1"]):
            self.app.phone_button.invoke()
        dialog = self.app.phone_dialog
        self.wait_for(lambda: not self.app.busy)
        self.app.message_text.insert("1.0", "尚未发送的电脑草稿")
        dialog.task_list.selection_set(0)
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                dialog.deliver({"enabled": enabled, "endpoint": "https://127.0.0.1:8765", "devices": []})
                if enabled:
                    dialog._show_pairing("synthetic-pairing", dialog._choice_snapshot(), time.time() + 300)
                dialog.controls()
                widgets = (dialog.enable_button, dialog.pair_button, dialog.stop_button,
                           dialog.refresh_button, dialog.network_button, dialog.revoke_button,
                           dialog.copy_button, dialog.address_box, dialog.port_box,
                           dialog.scope_box, dialog.task_list)
                with ExitStack() as stack:
                    changes = [stack.enter_context(mock.patch.object(widget, "configure", wraps=widget.configure))
                               for widget in widgets]
                    for widget in (dialog.enable_button, dialog.pair_button):
                        changes.extend(stack.enter_context(mock.patch.object(widget, name, wraps=getattr(widget, name)))
                                       for name in ("pack", "pack_forget"))
                    for _ in range(25):
                        dialog.controls()
                    self.assertEqual(sum(change.call_count for change in changes), 0,
                                     "unchanged controls must not redraw or rewrite widget state")
                self.assertEqual(dialog.task_list.curselection(), (0,))
                self.assertEqual(dialog.scope.get(), "仅查看")
                self.assertEqual(dialog.copy_button.instate(["disabled"]), not enabled)
                self.assertEqual(self.app.message_text.get("1.0", "end-1c"), "尚未发送的电脑草稿")

    def test_unchanged_status_retains_selected_device_without_redraw(self):
        with mock.patch("relay.phone.local_addresses", return_value=["127.0.0.1"]):
            self.app.phone_button.invoke()
        dialog = self.app.phone_dialog
        self.wait_for(lambda: not self.app.busy)
        devices = [{"id": name, "name": name, "scope": "read_only", "task_ids": ["task-1"]}
                   for name in ("one", "two")]
        connected = {"enabled": True, "endpoint": "https://127.0.0.1:8765", "devices": devices}
        dialog.deliver(connected)
        dialog.device_list.selection_set(1)
        with ExitStack() as stack:
            changes = [stack.enter_context(mock.patch.object(dialog.device_list, name, wraps=getattr(dialog.device_list, name)))
                       for name in ("delete", "insert")]
            changes.append(stack.enter_context(mock.patch.object(dialog.info, "set", wraps=dialog.info.set)))
            for _ in range(25):
                dialog.deliver(connected)
            self.assertEqual(sum(change.call_count for change in changes), 0)
        self.assertEqual(dialog.device_list.curselection(), (1,))
        dialog.deliver(dict(connected, devices=[devices[1], dict(devices[0], revoked=True)]))
        self.assertEqual(dialog.device_list.curselection(), (0,))
        self.assertIn("已撤销", dialog.device_list.get(1))

    def test_repeated_disabled_status_does_not_reclear_empty_pairing(self):
        with mock.patch("relay.phone.local_addresses", return_value=["127.0.0.1"]):
            self.app.phone_button.invoke()
        dialog = self.app.phone_dialog
        self.wait_for(lambda: not self.app.busy)
        with ExitStack() as stack:
            changes = [stack.enter_context(mock.patch.object(dialog.pairing, name, wraps=getattr(dialog.pairing, name)))
                       for name in ("configure", "delete")]
            changes.append(stack.enter_context(mock.patch.object(dialog.pairing_panel, "pack_forget", wraps=dialog.pairing_panel.pack_forget)))
            for _ in range(25):
                dialog.deliver({"enabled": False, "devices": []})
            self.assertEqual(sum(change.call_count for change in changes), 0)

    def test_pairing_only_shows_current_step_and_settings_remain_reachable(self):
        with mock.patch("relay.phone.local_addresses", return_value=["127.0.0.1"]):
            self.app.phone_button.invoke()
        dialog = self.app.phone_dialog
        dialog.attributes("-alpha", 0.0)
        self.wait_for(lambda: not self.app.busy)
        self.assertEqual(dialog.sections.select(), str(dialog.pair_page))
        self.assertEqual(dialog.enable_button.winfo_manager(), "pack")
        self.assertEqual(dialog.pair_button.winfo_manager(), "")
        self.assertEqual(dialog.pairing_panel.winfo_manager(), "")
        dialog.sections.select(dialog.device_page)
        self.assertEqual(dialog.sections.select(), str(dialog.device_page))
        self.assertEqual(dialog.network_button.winfo_manager(), "pack")
        self.assertEqual(dialog.port_box.winfo_manager(), "pack")
        dialog.sections.select(dialog.pair_page)
        dialog.deliver({"enabled": True, "endpoint": "https://127.0.0.1:8765", "devices": []})
        dialog.task_list.selection_set(0)
        with mock.patch.object(dialog, "submit", return_value=True):
            dialog.pair()
        dialog.deliver({"enabled": True, "endpoint": "https://127.0.0.1:8765", "devices": [],
                        "pairing_uri": "contextrelay://pair#synthetic",
                        "pairing_expires_at": time.time() + 300})
        self.assertEqual(dialog.enable_button.winfo_manager(), "")
        self.assertEqual(dialog.pair_button.winfo_manager(), "pack")
        self.assertEqual(dialog.pairing_panel.winfo_manager(), "pack")
        self.assertEqual(dialog.copy_button.winfo_manager(), "pack")
        dialog.scope.set("查看与控制")
        dialog._pairing_choices_changed()
        self.assertEqual(dialog.pairing_panel.winfo_manager(), "")
        self.assertIn("选择已变化", dialog.info.get())
        self.assertFalse(self.fake.starts)

    def test_phone_reply_cannot_clear_desktop_busy_or_unsent_draft(self):
        self.fake.root = Path(self.temp.name)
        self.fake.recovery_info = None
        self.fake._connection_id = "connection-one"
        calls = []

        class Core:
            def execute(inner, request_id, manager):
                calls.append((request_id, threading.get_ident()))
                manager.start("task-1", "来自手机的明确指令")

            def publish_tasks(inner, *args, **kwargs):
                pass

            def close(inner):
                pass

        core = Core()
        self.app.worker.phone.core = core
        self.app.message_text.insert("1.0", "电脑上尚未发送的草稿")
        self.app.busy = True
        self.app.worker.commands.put(("remote_command", (core, "phone-command"), {}))
        self.wait_for(lambda: self.app.tasks["task-1"]["state"] == "running")
        self.assertEqual(calls, [("phone-command", self.app.worker.ident)])
        self.assertTrue(self.app.busy)
        self.assertEqual(self.app.message_text.get("1.0", "end-1c"), "电脑上尚未发送的草稿")
        self.assertEqual(self.fake.starts, [("task-1", "来自手机的明确指令")])
        self.app.busy = False

    def test_connection_dialog_needs_no_selected_task_and_invalid_input_does_not_enable(self):
        self.app.search.set("no-visible-task")
        with mock.patch("relay.phone.local_addresses", return_value=["127.0.0.1"]):
            self.app.phone_button.invoke()
        dialog = self.app.phone_dialog
        self.wait_for(lambda: not self.app.busy)
        self.assertFalse(dialog.enabled)
        self.assertTrue(dialog.stop_button.instate(["disabled"]))
        dialog.address.set("8.8.8.8")
        dialog.enable_button.invoke()
        self.assertIn("私有", dialog.info.get())
        self.assertIsNone(self.app.worker.phone.core)
        self.assertFalse(self.fake.starts)
        dialog.pair()
        self.assertIn("至少一个", dialog.info.get())

    def test_empty_task_list_explains_next_step_and_disables_pairing(self):
        self.app.tasks.clear()
        with mock.patch("relay.phone.local_addresses", return_value=["127.0.0.1"]):
            self.app.phone_button.invoke()
        dialog = self.app.phone_dialog
        self.wait_for(lambda: not self.app.busy)
        dialog.deliver({"enabled": True, "endpoint": "https://127.0.0.1:8765", "devices": []})
        self.assertEqual(dialog.task_ids, [])
        self.assertIn("新建或导入任务", dialog.task_list.get(0))
        self.assertEqual(dialog.task_list.cget("state"), "disabled")
        self.assertTrue(dialog.pair_button.instate(["disabled"]))

    def test_task_import_replaces_disabled_empty_state(self):
        task = dict(self.app.tasks["task-1"])
        self.app.tasks.clear()
        with mock.patch("relay.phone.local_addresses", return_value=["127.0.0.1"]):
            self.app.phone_button.invoke()
        dialog = self.app.phone_dialog
        self.wait_for(lambda: not self.app.busy)
        self.assertEqual(dialog.task_list.cget("state"), "disabled")
        self.app.tasks[task["id"]] = task
        dialog.controls()
        self.assertEqual(dialog.task_ids, ["task-1"])
        self.assertIn(task["title"], dialog.task_list.get(0))
        self.assertEqual(dialog.task_list.cget("state"), "normal")
        self.assertNotIn("task-1", dialog.task_list.get(0))
        dialog.task_list.selection_set(0)
        self.app.tasks["task-two"] = dict(task, id="task-two")
        dialog.controls()
        self.assertIn("task-1", dialog.task_list.get(0))
        self.assertIn("task-two", dialog.task_list.get(1))
        self.app.tasks["task-two"]["title"] = "不同名称"
        dialog.controls()
        self.assertEqual((task["title"], "不同名称"), dialog.task_list.get(0, "end"))
        self.assertEqual((0,), dialog.task_list.curselection())

    def test_changed_choices_discard_late_pairing_reply(self):
        with mock.patch("relay.phone.local_addresses", return_value=["127.0.0.1"]):
            self.app.phone_button.invoke()
        dialog = self.app.phone_dialog
        self.wait_for(lambda: not self.app.busy)
        dialog.deliver({"enabled": True, "endpoint": "https://127.0.0.1:8765", "devices": []})
        dialog.task_list.selection_set(0)
        with mock.patch.object(dialog, "submit", return_value=True):
            dialog.pair()
        dialog.scope.set("查看与控制")
        dialog.scope_box.event_generate("<<ComboboxSelected>>")
        dialog.deliver({"enabled": True, "endpoint": "https://127.0.0.1:8765", "devices": [],
                        "pairing_uri": "contextrelay://pair#old",
                        "pairing_expires_at": time.time() + 300})
        self.assertEqual(dialog.pairing.get("1.0", "end-1c"), "")
        self.assertTrue(dialog.copy_button.instate(["disabled"]))
        self.assertIn("选择已变化", dialog.info.get())

    def test_pairing_deadline_clears_secret_and_device_shows_expiry(self):
        with mock.patch("relay.phone.local_addresses", return_value=["127.0.0.1"]):
            self.app.phone_button.invoke()
        dialog = self.app.phone_dialog
        self.wait_for(lambda: not self.app.busy)
        dialog.deliver({"enabled": True, "endpoint": "https://127.0.0.1:8765", "devices": []})
        dialog.task_list.selection_set(0)
        with mock.patch.object(dialog, "submit", return_value=True):
            dialog.pair()
        dialog.deliver({"enabled": True, "endpoint": "https://127.0.0.1:8765",
                        "devices": [{"id": "one", "name": "Android", "revoked": False,
                                     "expired": False, "scope": "read_only", "task_ids": ["task-1"],
                                     "expires_at": "2030-01-02T03:04:00+00:00"}],
                        "pairing_uri": "contextrelay://pair#expired",
                        "pairing_expires_at": time.time() - 1})
        self.assertEqual(dialog.pairing.get("1.0", "end-1c"), "")
        self.assertTrue(dialog.copy_button.instate(["disabled"]))
        self.assertIn("已过期", dialog.info.get())
        self.assertIn("到期", dialog.device_list.get(0))

    def test_live_deadline_expires_and_old_timer_cannot_clear_new_pairing(self):
        with mock.patch("relay.phone.local_addresses", return_value=["127.0.0.1"]):
            self.app.phone_button.invoke()
        dialog = self.app.phone_dialog
        self.wait_for(lambda: not self.app.busy)
        dialog.deliver({"enabled": True, "endpoint": "https://127.0.0.1:8765", "devices": []})
        dialog.task_list.selection_set(0)
        with mock.patch.object(dialog, "submit", return_value=True):
            dialog.pair()
        dialog.deliver({"enabled": True, "endpoint": "https://127.0.0.1:8765", "devices": [],
                        "pairing_uri": "contextrelay://pair#short",
                        "pairing_expires_at": time.time() + 0.06})
        self.assertIn("#short", dialog.pairing.get("1.0", "end-1c"))
        self.wait_for(lambda: not dialog.pairing.get("1.0", "end-1c"))
        self.assertIn("已过期", dialog.info.get())

        with mock.patch.object(dialog, "submit", return_value=True):
            dialog.pair()
        dialog.deliver({"enabled": True, "endpoint": "https://127.0.0.1:8765", "devices": [],
                        "pairing_uri": "contextrelay://pair#first",
                        "pairing_expires_at": time.time() + 300})
        old_serial = dialog._pairing_serial
        with mock.patch.object(dialog, "submit", return_value=True):
            dialog.pair()
        dialog.deliver({"enabled": True, "endpoint": "https://127.0.0.1:8765", "devices": [],
                        "pairing_uri": "contextrelay://pair#second",
                        "pairing_expires_at": time.time() + 300})
        dialog._expire_pairing(old_serial)
        self.assertIn("#second", dialog.pairing.get("1.0", "end-1c"))

    def test_copy_rechecks_choices_even_without_selection_event(self):
        with mock.patch("relay.phone.local_addresses", return_value=["127.0.0.1"]):
            self.app.phone_button.invoke()
        dialog = self.app.phone_dialog
        self.wait_for(lambda: not self.app.busy)
        dialog.deliver({"enabled": True, "endpoint": "https://127.0.0.1:8765", "devices": []})
        dialog.task_list.selection_set(0)
        with mock.patch.object(dialog, "submit", return_value=True):
            dialog.pair()
        dialog.deliver({"enabled": True, "endpoint": "https://127.0.0.1:8765", "devices": [],
                        "pairing_uri": "contextrelay://pair#current",
                        "pairing_expires_at": time.time() + 300})
        dialog.scope.set("查看与控制")  # Programmatic change deliberately emits no Tk event.
        with mock.patch.object(dialog, "clipboard_append") as copied:
            dialog.copy()
        copied.assert_not_called()
        self.assertEqual(dialog.pairing.get("1.0", "end-1c"), "")
        self.assertIn("选择已变化", dialog.info.get())

    def test_network_diagnostics_do_not_replace_connection_or_devices(self):
        with mock.patch("relay.phone.local_addresses", return_value=["127.0.0.1"]):
            self.app.phone_button.invoke()
        dialog = self.app.phone_dialog
        self.wait_for(lambda: not self.app.busy)
        connected = {"enabled": True, "endpoint": "https://127.0.0.1:8765",
                     "devices": [{"id": "one", "name": "Android", "revoked": False,
                                  "expired": False, "scope": "read_only", "task_ids": ["task-1"],
                                  "expires_at": "2030-01-02T03:04:00+00:00"}]}
        dialog.deliver(connected)
        self.assertIn("仅查看", dialog.device_list.get(0))
        self.assertIn("到期", dialog.device_list.get(0))
        self.assertNotIn("read_only", dialog.device_list.get(0))
        dialog.deliver({"kind": "network_diagnostics", "state": "connected",
                        "local_ipv4": ["100.64.1.2"], "online_peers": 1, "detail": "Connected"})
        self.assertTrue(dialog.enabled)
        self.assertEqual(dialog.devices, connected["devices"])
        self.assertIn("100.64.1.2", dialog.info.get())

    def test_phone_host_pair_forwards_only_explicit_tasks_and_scope(self):
        host = PhoneHost(self.fake, self.app.worker.commands)
        core = mock.Mock()
        core.local_status.return_value = {"devices": []}
        core.new_pairing.return_value = "contextrelay://pair#secret"
        host.core, host.endpoint, host.fingerprint = core, "https://127.0.0.1:8765", "a" * 64
        before = time.time()
        result = host.pair(["task-1"], "control")
        self.assertEqual(result["pairing_uri"], "contextrelay://pair#secret")
        self.assertGreaterEqual(result["pairing_expires_at"], before + 299)
        self.assertLessEqual(result["pairing_expires_at"], time.time() + 300)
        core.new_pairing.assert_called_once_with(host.endpoint, host.fingerprint, ["task-1"], "control")

    def test_phone_dialog_maps_chinese_scope_to_protocol_value(self):
        with mock.patch("relay.phone.local_addresses", return_value=["127.0.0.1"]):
            self.app.phone_button.invoke()
        dialog = self.app.phone_dialog
        self.wait_for(lambda: not self.app.busy)
        dialog.enabled = True
        dialog.task_list.selection_set(0)
        dialog.scope.set("查看与控制")
        with mock.patch.object(dialog, "submit") as submit:
            dialog.pair()
        submit.assert_called_once_with("remote_pair", [dialog.task_ids[0]], "control")

    def test_recovery_inspection_disables_phone_control(self):
        base_ui.TkSmokeTests.set_inspection(self)
        self.assertTrue(self.app.phone_button.instate(["disabled"]))
        self.app.phone_button.invoke()
        self.assertIsNone(self.app.phone_dialog)
        self.assertIsNone(self.app.worker.phone.core)

    def test_late_phone_dialog_reply_never_updates_replacement_window(self):
        with mock.patch("relay.phone.local_addresses", return_value=["127.0.0.1"]):
            self.app.phone_button.invoke()
        old = self.app.phone_dialog
        self.wait_for(lambda: not self.app.busy)
        old.destroy()
        with mock.patch("relay.phone.local_addresses", return_value=["127.0.0.1"]):
            self.app.phone_button.invoke()
        new = self.app.phone_dialog
        self.wait_for(lambda: not self.app.busy)
        self.app.phone_request = (old, "remote_pair")
        self.app._phone_result("remote_pair", {"enabled": True, "endpoint": "https://127.0.0.1:8765",
                                                "pairing_uri": "old-secret", "devices": []})
        self.assertFalse(new.enabled)
        self.assertEqual(new.pairing.get("1.0", "end-1c"), "")

    def test_disabled_host_never_dispatches_old_queued_command(self):
        host = PhoneHost(self.fake, self.app.worker.commands)
        old = mock.Mock()
        host.execute(old, "already-queued")
        old.execute.assert_not_called()

    def _assert_local_control_gates_queued_command(self, method):
        entered, release = threading.Event(), threading.Event()

        class Core:
            def __init__(inner):
                inner.accepted = {"queued"}
                inner.executed = []
                inner.closed = inner.revoked = False

            def execute(inner, request_id, manager):
                if request_id == "block-worker":
                    entered.set()
                    release.wait(2)
                elif request_id in inner.accepted:
                    inner.executed.append(request_id)

            def revoke_device(inner, device_id):
                inner.revoked = True
                inner.accepted.clear()

            def local_status(inner):
                return {"devices": [{"id": "phone", "name": "phone", "revoked": inner.revoked}]}

            def publish_tasks(inner, *args, **kwargs):
                pass

            def close(inner):
                inner.closed = True
                inner.accepted.clear()

        core = Core()
        self.app.worker.phone.core = core
        self.app.worker.phone.endpoint = "https://127.0.0.1:8765"
        self.app.worker.commands.put(("remote_command", (core, "block-worker"), {}))
        self.assertTrue(entered.wait(1))
        self.app.worker.commands.put(("remote_command", (core, "queued"), {}))
        dialog = mock.Mock()
        dialog.winfo_exists.return_value = False
        args = ("phone",) if method == "remote_revoke" else ()
        submitted = self.app.submit_remote(dialog, method, *args)
        gated_before_dispatch = core.revoked if method == "remote_revoke" else core.closed
        release.set()
        self.wait_for(lambda: not self.app.busy)
        self.assertTrue(submitted)
        self.assertTrue(gated_before_dispatch)
        self.assertEqual(core.executed, [])

    def test_local_revoke_gates_commands_while_worker_is_blocked(self):
        self._assert_local_control_gates_queued_command("remote_revoke")

    def test_local_disable_gates_commands_while_worker_is_blocked(self):
        self._assert_local_control_gates_queued_command("remote_disable")


class PrivateAddressTests(unittest.TestCase):
    def test_default_route_precedes_unreachable_virtual_adapter(self):
        addresses = [(None, None, None, None, (host, 0)) for host in ("198.51.100.10", "192.0.2.10")]
        with mock.patch("relay.phone.socket.getaddrinfo", return_value=addresses), mock.patch("relay.phone.socket.socket") as probe:
            probe.return_value.__enter__.return_value.getsockname.return_value = ("192.0.2.10", 40000)
            self.assertEqual(local_addresses(), ["192.0.2.10", "198.51.100.10", "127.0.0.1"])

    def test_only_explicit_local_or_private_network_addresses(self):
        for address in ("192.0.2.10", "10.0.0.8", "127.0.0.1", "100.64.1.2"):
            self.assertEqual(private_address(address), address)
        for address in ("0.0.0.0", "8.8.8.8", "224.0.0.1", "169.254.1.2", "example.com", "::1"):
            with self.assertRaises(ValueError):
                private_address(address)


if __name__ == "__main__":
    unittest.main()
