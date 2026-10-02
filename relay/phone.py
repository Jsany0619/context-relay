"""Explicit, same-process phone connection and its small desktop dialog."""

import ipaddress
from pathlib import Path
import socket
import tkinter as tk
from tkinter import ttk
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
                           self.commands.put(("remote_command", (core, request_id), {})))
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
        return {**self.status(), "pairing_uri": self.core.new_pairing(
            self.endpoint, self.fingerprint, list(task_ids), scope)}

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
        ttk.Label(permission_row, text="控制权限可发送、暂停和审批任务。").pack(side="left")
        ttk.Label(body, text="选择这台手机可访问的任务（默认全不选）").pack(anchor="w")
        self.task_list = tk.Listbox(body, height=4, selectmode="extended", exportselection=False)
        self.task_list.pack(fill="x", pady=(3, 8))
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
        if ids == self.task_ids:
            return
        self.task_ids = ids
        self.task_list.delete(0, "end")
        for index, task in enumerate(tasks):
            self.task_list.insert("end", f"{task.get('title', '未命名任务')} · {task['id'][:8]}")
            if task["id"] in selected:
                self.task_list.selection_set(index)

    def pair(self):
        task_ids = [self.task_ids[index] for index in self.task_list.curselection()
                    if index < len(self.task_ids)]
        if not task_ids:
            self.info.set("请先选择至少一个允许手机访问的任务。")
            return
        scope = {"仅查看": "read_only", "查看与控制": "control"}[self.scope.get()]
        self.submit("remote_pair", task_ids, scope)

    def copy(self):
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

    def deliver(self, result=None, error=None):
        if error:
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
                self.device_list.insert("end", f"{device.get('name', '手机')} · {state} · "
                                               f"{scope} · "
                                               f"{len(device.get('task_ids', []))} 个任务")
            self.info.set((f"连接已开启：{result['endpoint']}\n"
                           "若无法连接，核对同一网络及 Windows 防火墙。关闭窗口不会关闭连接。")
                          if self.enabled else "手机连接已关闭；不会再接收手机指令。")
            if "pairing_uri" in result or not self.enabled:
                self.pairing.configure(state="normal")
                self.pairing.delete("1.0", "end")
                self.pairing.insert("1.0", result.get("pairing_uri", ""))
                self.pairing.configure(state="disabled")
        self.controls()

    def controls(self):
        self._sync_tasks()
        available = self.app.ready and not self.app.busy and not self.app.closing and self.app.recovery_info is None
        for widget, enabled in ((self.enable_button, not self.enabled), (self.pair_button, self.enabled),
                                (self.stop_button, self.enabled), (self.refresh_button, True),
                                (self.network_button, True),
                                (self.revoke_button, self.enabled and bool(self.devices))):
            widget.configure(state="normal" if available and enabled else "disabled")
        self.copy_button.configure(state="normal" if self.enabled and self.pairing.get("1.0", "end-1c") else "disabled")
        self.address_box.configure(state="normal" if available and not self.enabled else "disabled")
        self.port_box.configure(state="normal" if available and not self.enabled else "disabled")
        self.scope_box.configure(state="readonly" if available else "disabled")
        self.task_list.configure(state="normal" if available else "disabled")
