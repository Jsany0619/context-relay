"""Explicit, same-process phone connection and its small desktop dialog."""

import ipaddress
from datetime import datetime
from pathlib import Path
import socket
import tkinter as tk
from tkinter import ttk
import time
import webbrowser


def private_address(value):
    address = ipaddress.IPv4Address(value)
    if (address.is_unspecified or address.is_multicast or address.is_link_local
            or not (address.is_private or address.is_loopback
                    or address in ipaddress.IPv4Network("100.64.0.0/10"))):
        raise ValueError("请选择本机的局域网或私有网络 IPv4 地址，不开放公网监听。")
    return str(address)


def local_addresses():
    addresses = set()
    preferred = None
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            try:
                addresses.add(private_address(item[4][0]))
            except ValueError:
                pass
    except OSError:
        pass
    try:
        # UDP connect selects a local route without sending a packet.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            preferred = private_address(probe.getsockname()[0])
    except (OSError, ValueError):
        pass
    return ([preferred] if preferred and preferred != "127.0.0.1" else []) + sorted(
        addresses - {preferred, "127.0.0.1"}) + ["127.0.0.1"]


class PhoneHost:
    """Owned by CommandWorker; HTTP only enqueues operations and reads snapshots."""

    def __init__(self, manager, commands):
        self.manager, self.commands = manager, commands
        self.core = self.server = None
        self.endpoint = self.fingerprint = None

    def publish(self, tasks):
        if self.core is not None:
            self.core.publish_tasks(tasks, recovery_info=self.manager.recovery_info,
                                    connection_id=self.manager._connection_id)

    def enable(self, host, port=8765):
        if self.manager.recovery_info is not None:
            raise ValueError("恢复库仅供本地只读检视，不能开启手机控制。")
        if self.core is not None:
            raise ValueError("手机连接已经开启；更换地址前请先关闭连接。")
        host = private_address(host)
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
            raise ValueError("端口须为 1024 到 65535 的整数。")
        from .gateway import GatewayCore, RemoteGateway, ensure_certificate
        cert, key, fingerprint = ensure_certificate(self.manager.root)
        core = GatewayCore(self.manager.root, lambda request_id:
                           self.commands.put(("remote_command", (core, request_id), {})),
                           conversation_reader=getattr(self.manager, "read_chat", None))
        server = None
        try:
            core.publish_tasks(self.manager.list_tasks(), recovery_info=self.manager.recovery_info,
                               connection_id=self.manager._connection_id)
            server = RemoteGateway(core, host, port, cert, key)
            server.start()
        except Exception:
            if server is not None:
                server.close()
            core.close()
            raise
        self.core, self.server = core, server
        self.endpoint = f"https://{host}:{server.port}"
        self.fingerprint = fingerprint
        return self.status()

    def status(self):
        if self.core is None:
            return {"enabled": False, "devices": []}
        return {**self.core.local_status(), "enabled": True, "endpoint": self.endpoint}

    def network_status(self):
        from .network import diagnostics
        return diagnostics()

    def pair(self, task_ids=(), scope="read_only"):
        if self.core is None:
            raise ValueError("请先开启手机连接。")
        from .gateway import PAIRING_TTL_SECONDS
        created_at = time.time()
        pairing_uri = self.core.new_pairing(
            self.endpoint, self.fingerprint, list(task_ids), scope)
        return {**self.status(), "pairing_uri": pairing_uri,
                "pairing_expires_at": created_at + PAIRING_TTL_SECONDS}

    def revoke(self, device_id):
        if self.core is None:
            raise ValueError("手机连接未开启。")
        for device in self.core.local_status()["devices"]:
            if device.get("id") == device_id and device.get("revoked"):
                return self.status()
        self.core.revoke_device(device_id)
        return self.status()

    def gate_control(self, method, *args):
        """Block accepted work immediately; worker cleanup and UI replies remain serialized."""
        if self.core is None:
            raise ValueError("手机连接未开启。")
        if method == "remote_revoke":
            self.revoke(*args)
        elif method == "remote_disable":
            self.core.close()

    def execute(self, core, request_id):
        if core is not self.core or self.core is None:
            return  # A stopped/replaced host already marked unexecuted receipts unknown.
        self.core.execute(request_id, self.manager)

    def close(self):
        if self.server is not None:
            self.server.close()
        if self.core is not None:
            self.core.close()
        self.core = self.server = None
        self.endpoint = self.fingerprint = None
        return self.status()


class PhoneDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app, self.enabled, self.devices = app, False, []
        self._pairing_request = self._pairing_snapshot = None
        self._pairing_deadline = self._pairing_timer = None
        self._pairing_serial = 0
        self.title("手机连接 · Context Relay")
        self.transient(app.root)
        self.geometry(f"{min(720, self.winfo_screenwidth() - 80)}x{min(580, self.winfo_screenheight() - 120)}")
        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="在手机 App 里查看和控制这里的任务", font=("Segoe UI", 13, "bold")).pack(anchor="w")
        ttk.Label(body, text="电脑需保持开机且此管理器运行。先在电脑创建或导入任务，再在手机选择。\n"
                  "同一 Wi-Fi 使用局域网地址；异地使用已连接的私有网络地址。",
                  wraplength=650, justify="left").pack(fill="x", pady=8)
        address_row = ttk.Frame(body)
        address_row.pack(fill="x")
        ttk.Label(address_row, text="本机地址").pack(side="left")
        addresses = local_addresses()
        self.address = tk.StringVar(value=addresses[0])
        self.address_box = ttk.Combobox(address_row, values=addresses, textvariable=self.address, width=24)
        self.address_box.pack(side="left", padx=8)
        ttk.Label(address_row, text="端口").pack(side="left")
        self.port = tk.StringVar(value="8765")
        self.port_box = ttk.Entry(address_row, textvariable=self.port, width=8)
        self.port_box.pack(side="left", padx=8)
        self.info = tk.StringVar(value="连接未开启。配对信息只发给自己的手机，不要公开分享。")
        ttk.Label(body, textvariable=self.info, wraplength=650, justify="left").pack(fill="x", pady=10)
        permission_row = ttk.Frame(body)
        permission_row.pack(fill="x", pady=(0, 6))
        ttk.Label(permission_row, text="手机权限").pack(side="left")
        self.scope = tk.StringVar(value="仅查看")
        self.scope_box = ttk.Combobox(permission_row, textvariable=self.scope,
                                      values=("仅查看", "查看与控制"), state="readonly", width=14)
        self.scope_box.pack(side="left", padx=8)
        self.scope_box.bind("<<ComboboxSelected>>", self._pairing_choices_changed)
        ttk.Label(permission_row, text="控制权限可发送、暂停和审批任务。").pack(side="left")
        ttk.Label(body, text="选择这台手机可访问的任务（默认全不选）").pack(anchor="w")
        self.task_list = tk.Listbox(body, height=4, selectmode="extended", exportselection=False)
        self.task_list.pack(fill="x", pady=(3, 8))
        self.task_list.bind("<<ListboxSelect>>", self._pairing_choices_changed)
        self.task_ids = []
        self._sync_tasks()
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        self.enable_button = ttk.Button(buttons, text="开启手机连接", command=self.enable)
        self.enable_button.pack(side="left")
        self.pair_button = ttk.Button(buttons, text="按所选权限生成配对信息", command=self.pair)
        self.pair_button.pack(side="left", padx=6)
        self.stop_button = ttk.Button(buttons, text="关闭手机连接", command=lambda: self.submit("remote_disable"))
        self.stop_button.pack(side="left")
        self.pairing = tk.Text(body, height=4, wrap="char", state="disabled")
        self.pairing.pack(fill="x", pady=(10, 4))
        self.copy_button = ttk.Button(body, text="复制配对信息（5 分钟内有效，仅一次）", command=self.copy)
        self.copy_button.pack(anchor="w")
        ttk.Label(body, text="已配对设备").pack(anchor="w", pady=(12, 4))
        self.device_list = tk.Listbox(body, height=4, exportselection=False)
        self.device_list.pack(fill="both", expand=True)
        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(8, 0))
        self.refresh_button = ttk.Button(actions, text="刷新设备", command=lambda: self.submit("remote_status"))
        self.refresh_button.pack(side="left")
        self.network_button = ttk.Button(actions, text="异地连接检查", command=lambda: self.submit("remote_network"))
        self.network_button.pack(side="left", padx=8)
        self.revoke_button = ttk.Button(actions, text="撤销所选设备", command=self.revoke)
        self.revoke_button.pack(side="left")
        ttk.Button(actions, text="使用说明 / 费用", command=self.open_help).pack(side="left", padx=8)
        ttk.Button(actions, text="关闭此窗口", command=self.destroy).pack(side="right")
        self.controls()
        self.submit("remote_status")

    def submit(self, method, *args):
        return self.app.submit_remote(self, method, *args)

    def enable(self):
        try:
            host, port = private_address(self.address.get().strip()), int(self.port.get())
        except ValueError:
            self.info.set("请填写本机私有 IPv4 地址和整数端口。")
            return
        self.submit("remote_enable", host, port)

    def _sync_tasks(self):
        selected = {self.task_ids[index] for index in self.task_list.curselection()
                    if index < len(self.task_ids)}
        tasks = [task for task in self.app.tasks.values() if not task.get("archived")]
        ids = [task["id"] for task in tasks]
        if ids == self.task_ids and self.task_list.size():
            return
        self.task_ids = ids
        # A native Tk Listbox ignores content edits while disabled.
        self.task_list.configure(state="normal")
        self.task_list.delete(0, "end")
        if not tasks:
            self.task_list.insert("end", "暂无可授权任务；请先在主窗口新建或导入任务。")
        for index, task in enumerate(tasks):
            self.task_list.insert("end", f"{task.get('title', '未命名任务')} · {task['id'][:8]}")
            if task["id"] in selected:
                self.task_list.selection_set(index)
        if self._pairing_snapshot is not None and self._choice_snapshot() != self._pairing_snapshot:
            self._clear_pairing("任务列表已变化，请重新选择并生成配对信息。")

    def _choice_snapshot(self):
        task_ids = tuple(self.task_ids[index] for index in self.task_list.curselection()
                         if index < len(self.task_ids))
        scope = {"仅查看": "read_only", "查看与控制": "control"}.get(self.scope.get())
        return task_ids, scope

    def _pairing_choices_changed(self, _event=None):
        if self._pairing_snapshot is not None and self._choice_snapshot() != self._pairing_snapshot:
            self._clear_pairing("选择已变化，旧配对信息已清除；请重新生成。")
        self.controls()

    def _clear_pairing(self, message=None):
        self._pairing_serial += 1
        if self._pairing_timer is not None:
            try:
                self.after_cancel(self._pairing_timer)
            except tk.TclError:
                pass
        self._pairing_timer = self._pairing_snapshot = self._pairing_deadline = None
        self.pairing.configure(state="normal")
        self.pairing.delete("1.0", "end")
        self.pairing.configure(state="disabled")
        if message:
            self.info.set(message)

    def _show_pairing(self, value, snapshot, deadline):
        try:
            deadline = float(deadline)
        except (TypeError, ValueError):
            self._clear_pairing("无法确认配对信息有效期，请重新生成。")
            return
        if deadline <= time.time():
            self._clear_pairing("配对信息已过期，请重新生成。")
            return
        self._clear_pairing()
        self._pairing_snapshot, self._pairing_deadline = snapshot, deadline
        self.pairing.configure(state="normal")
        self.pairing.insert("1.0", value)
        self.pairing.configure(state="disabled")
        serial = self._pairing_serial
        delay = max(1, int((deadline - time.time()) * 1000))
        self._pairing_timer = self.after(delay, self._expire_pairing, serial)
        scope = "仅查看" if snapshot[1] == "read_only" else "查看与控制"
        self.info.set(f"配对信息已生成：{len(snapshot[0])} 个任务 · {scope}。请在 5 分钟内到手机完成配对。")

    def _expire_pairing(self, serial):
        if serial != self._pairing_serial or self._pairing_deadline is None:
            return
        if self._pairing_deadline > time.time():
            self._pairing_timer = self.after(
                max(1, int((self._pairing_deadline - time.time()) * 1000)),
                self._expire_pairing, serial)
            return
        self._clear_pairing("配对信息已过期，请重新生成。")
        self.controls()

    def pair(self):
        task_ids, scope = self._choice_snapshot()
        if not task_ids:
            self.info.set("请先选择至少一个允许手机访问的任务。")
            return
        self._clear_pairing()
        self._pairing_request = (task_ids, scope)
        self.info.set("正在按当前任务和权限生成配对信息…")
        if not self.submit("remote_pair", list(task_ids), scope):
            self._pairing_request = None
        self.controls()

    def copy(self):
        if self._pairing_snapshot is None or self._choice_snapshot() != self._pairing_snapshot:
            self._clear_pairing("选择已变化，旧配对信息已清除；请重新生成。")
            self.controls()
            return
        if self._pairing_deadline is None or self._pairing_deadline <= time.time():
            self._clear_pairing("配对信息已过期，请重新生成。")
            self.controls()
            return
        value = self.pairing.get("1.0", "end-1c")
        if value:
            self.clipboard_clear()
            self.clipboard_append(value)
            self.info.set("已复制。请在自己的手机 App 粘贴连接；配对内容包含短时秘密。")

    def revoke(self):
        selected = self.device_list.curselection()
        if selected:
            self.submit("remote_revoke", self.devices[selected[0]]["id"])

    def open_help(self):
        webbrowser.open((Path(__file__).resolve().parents[1] / "docs/mobile.md").as_uri())

    @staticmethod
    def _expiry_label(value):
        if not isinstance(value, str):
            return ""
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return ""
            return parsed.astimezone().strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return ""

    def deliver(self, result=None, error=None):
        if error:
            if self._pairing_request is not None:
                self._pairing_request = None
                self._clear_pairing()
            self.info.set(error)
        elif isinstance(result, dict) and result.get("kind") == "network_diagnostics":
            addresses = "\n可用私有地址：" + "、".join(result.get("local_ipv4", [])) if result.get("local_ipv4") else ""
            self.info.set(result.get("detail", "未获取到网络诊断。") + addresses)
        elif isinstance(result, dict):
            self.enabled = result.get("enabled", False)
            self.devices = result.get("devices", [])
            self.device_list.delete(0, "end")
            for device in self.devices:
                state = "已撤销" if device.get("revoked") else "已过期" if device.get("expired") else "已配对"
                scope = {"read_only": "仅查看", "control": "查看与控制"}.get(
                    device.get("scope"), "旧权限失效")
                expiry = self._expiry_label(device.get("expires_at"))
                self.device_list.insert("end", f"{device.get('name', '手机')} · {state} · "
                                               f"{scope} · "
                                               f"{len(device.get('task_ids', []))} 个任务"
                                               + (f" · 到期 {expiry}" if expiry else ""))
            self.info.set((f"连接已开启：{result['endpoint']}\n"
                           "若无法连接，核对同一网络及 Windows 防火墙。关闭窗口不会关闭连接。")
                          if self.enabled else "手机连接已关闭；不会再接收手机指令。")
            if "pairing_uri" in result:
                requested, self._pairing_request = self._pairing_request, None
                if requested is None or requested != self._choice_snapshot():
                    self._clear_pairing("选择已变化，返回的旧配对信息已丢弃；请重新生成。")
                else:
                    self._show_pairing(result["pairing_uri"], requested,
                                       result.get("pairing_expires_at"))
            elif not self.enabled:
                self._pairing_request = None
                self._clear_pairing()
        self.controls()

    def controls(self):
        self._sync_tasks()
        available = self.app.ready and not self.app.busy and not self.app.closing and self.app.recovery_info is None
        selected = bool(self._choice_snapshot()[0])
        for widget, enabled in ((self.enable_button, not self.enabled),
                                (self.pair_button, self.enabled and selected and self._pairing_request is None),
                                (self.stop_button, self.enabled), (self.refresh_button, True),
                                (self.network_button, True),
                                (self.revoke_button, self.enabled and bool(self.devices))):
            widget.configure(state="normal" if available and enabled else "disabled")
        self.copy_button.configure(state="normal" if self.enabled and self._pairing_snapshot is not None
                                   and self.pairing.get("1.0", "end-1c") else "disabled")
        self.address_box.configure(state="normal" if available and not self.enabled else "disabled")
        self.port_box.configure(state="normal" if available and not self.enabled else "disabled")
        self.scope_box.configure(state="readonly" if available else "disabled")
        self.task_list.configure(state="normal" if available and self.task_ids else "disabled")

    def destroy(self):
        if self._pairing_timer is not None:
            try:
                self.after_cancel(self._pairing_timer)
            except tk.TclError:
                pass
            self._pairing_timer = None
        super().destroy()
