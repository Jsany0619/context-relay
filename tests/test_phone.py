"""Desktop/phone ownership checks without native clients or real task state."""

from pathlib import Path
import threading
import time
import unittest
from unittest import mock

from relay.phone import PhoneHost, local_addresses, private_address
from tests import test_ui as base_ui


class PhoneUiTests(unittest.TestCase):
    setUp = base_ui.TkSmokeTests.setUp
    tearDown = base_ui.TkSmokeTests.tearDown
    wait_for = base_ui.TkSmokeTests.wait_for

    def test_pairing_only_shows_current_step_and_settings_remain_reachable(self):
        self.root.deiconify()
        with mock.patch("relay.phone.local_addresses", return_value=["127.0.0.1"]):
            self.app.phone_button.invoke()
        dialog = self.app.phone_dialog
        self.wait_for(lambda: not self.app.busy)
        self.root.update()
        self.assertTrue(dialog.enable_button.winfo_ismapped())
        self.assertFalse(dialog.pair_button.winfo_ismapped())
        self.assertFalse(dialog.pairing.winfo_ismapped())
        self.assertFalse(dialog.network_button.winfo_ismapped())
        self.assertFalse(dialog.port_box.winfo_ismapped())
        dialog.sections.select(dialog.device_page)
        self.root.update()
        self.assertTrue(dialog.network_button.winfo_ismapped())
        self.assertTrue(dialog.port_box.winfo_ismapped())
        dialog.sections.select(dialog.pair_page)
        dialog.deliver({"enabled": True, "endpoint": "https://127.0.0.1:8765", "devices": []})
        dialog.task_list.selection_set(0)
        with mock.patch.object(dialog, "submit", return_value=True):
            dialog.pair()
        dialog.deliver({"enabled": True, "endpoint": "https://127.0.0.1:8765", "devices": [],
                        "pairing_uri": "contextrelay://pair#synthetic",
                        "pairing_expires_at": time.time() + 300})
        self.root.update()
        self.assertFalse(dialog.enable_button.winfo_ismapped())
        self.assertTrue(dialog.pair_button.winfo_ismapped())
        self.assertTrue(dialog.pairing.winfo_ismapped())
        self.assertTrue(dialog.copy_button.winfo_ismapped())
        dialog.scope.set("查看与控制")
        dialog._pairing_choices_changed()
        self.root.update()
        self.assertFalse(dialog.pairing.winfo_ismapped())
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
