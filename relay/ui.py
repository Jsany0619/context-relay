"""Tk interface; task mutations belong to the single command worker."""
import base64
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import queue
import struct
import threading
import time
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk
import zlib

from .budget import budget_status, validate_limits
from .chat_widgets import ConversationView, TaskCardList
from .display import enable_native_dpi
from .preferences import DEFAULTS, load_preferences, save_preferences


THEMES = {
    "blue": {"bg": "#f7f8fa", "surface": "#ffffff", "ink": "#17212b",
             "muted": "#606d7d", "accent": "#2457d6", "border": "#dce2e9",
             "select": "#e4ebfb", "control": "#edf0f4", "control_active": "#e3e7ed"},
    "mint": {"bg": "#f7f9f8", "surface": "#ffffff", "ink": "#202927",
             "muted": "#626e6a", "accent": "#226356", "border": "#dbe3df",
             "select": "#e8eeea", "control": "#eef2f0", "control_active": "#e2e9e5"},
}
WARNING_BG = "#fff6e8"
WARNING = "#8a4b08"


def _paint_rounded(image, fill, border):
    def rgb(value):
        return tuple(int(value[index:index + 2], 16) for index in (1, 3, 5))

    rows = []
    size = image.width()
    for y in range(size):
        row = bytearray((0,))
        for x in range(size):
            color = None
            for inset, value, radius in ((0, border, 4), (1, fill, 3)):
                low, high = inset, size - 1 - inset
                if low <= x <= high and low <= y <= high:
                    dx = max(low + radius - x, 0, x - (high - radius))
                    dy = max(low + radius - y, 0, y - (high - radius))
                    if dx * dx + dy * dy <= radius * radius:
                        color = value
            row.extend((*rgb(color), 255) if color else (0, 0, 0, 0))
        rows.append(bytes(row))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))
    image.configure(data=base64.b64encode(png), format="png")


def _rounded_elements(root, style, colors):
    images = getattr(root, "_context_relay_theme_images", None)
    if images is None:
        images = {name: tk.PhotoImage(master=root, width=12, height=12) for name in (
            "primary", "primary_active", "primary_pressed", "primary_disabled", "primary_focus",
            "secondary", "secondary_active", "secondary_pressed", "secondary_disabled", "secondary_focus",
            "entry", "entry_focus", "entry_disabled")}
        root._context_relay_theme_images = images
        style.element_create("ContextRelay.Primary.background", "image", images["primary"],
                             ("disabled", images["primary_disabled"]),
                             ("pressed", images["primary_pressed"]), ("active", images["primary_active"]),
                             ("focus", images["primary_focus"]),
                             border=4, sticky="nsew")
        style.element_create("ContextRelay.Entry.background", "image", images["entry"],
                             ("disabled", images["entry_disabled"]), ("focus", images["entry_focus"]),
                             border=4, sticky="nsew")
        style.element_create("ContextRelay.Secondary.background", "image", images["secondary"],
                             ("disabled", images["secondary_disabled"]),
                             ("pressed", images["secondary_pressed"]),
                             ("active", images["secondary_active"]), ("focus", images["secondary_focus"]),
                             border=4, sticky="nsew")
    for name, fill, border in (
            ("primary", colors["accent"], colors["accent"]),
            ("primary_active", "#1f4bbb" if colors is THEMES["blue"] else "#106b5c", colors["accent"]),
            ("primary_pressed", "#193f9e" if colors is THEMES["blue"] else "#0c594d", colors["accent"]),
            ("primary_disabled", "#b8c5e3" if colors is THEMES["blue"] else "#a9c9c1", colors["border"]),
            ("primary_focus", colors["accent"], colors["ink"]),
            ("secondary", colors["control"], colors["border"]),
            ("secondary_active", colors["control_active"], colors["border"]),
            ("secondary_pressed", colors["select"], colors["accent"]),
            ("secondary_disabled", colors["control"], colors["border"]),
            ("secondary_focus", colors["control"], colors["accent"]),
            ("entry", colors["surface"], colors["border"]),
            ("entry_focus", colors["surface"], colors["accent"]),
            ("entry_disabled", colors["bg"], colors["border"])):
        _paint_rounded(images[name], fill, border)
    style.layout("Primary.TButton", [
        ("ContextRelay.Primary.background", {"sticky": "nswe"}),
        ("Button.padding", {"sticky": "nswe", "children": [("Button.label", {"sticky": "nswe"})]})])
    style.layout("Secondary.TButton", [
        ("ContextRelay.Secondary.background", {"sticky": "nswe"}),
        ("Button.padding", {"sticky": "nswe", "children": [("Button.label", {"sticky": "nswe"})]})])
    style.layout("Rounded.TMenubutton", [
        ("ContextRelay.Secondary.background", {"sticky": "nswe"}),
        ("Menubutton.focus", {"sticky": "nswe", "children": [
            ("Menubutton.indicator", {"side": "right"}),
            ("Menubutton.padding", {"sticky": "we", "children": [
                ("Menubutton.label", {"side": "left"})]})]})])
    style.layout("TEntry", [("ContextRelay.Entry.background", {
        "sticky": "nswe", "children": [("Entry.padding", {
            "sticky": "nswe", "children": [("Entry.textarea", {"sticky": "nswe"})]})]})])
    style.layout("RoundedEntry.TFrame", [("ContextRelay.Entry.background", {
        "sticky": "nswe"})])


def _recolor_widgets(widget, colors):
    try:
        if isinstance(widget, (tk.Tk, tk.Toplevel)):
            widget.configure(background=colors["bg"])
        elif isinstance(widget, tk.Text):
            widget.configure(background=colors["surface"], foreground=colors["ink"],
                             insertbackground=colors["ink"], selectbackground=colors["accent"],
                             highlightbackground=colors["border"], highlightcolor=colors["accent"],
                             font="TkTextFont")
        elif isinstance(widget, tk.Listbox):
            widget.configure(background=colors["surface"], foreground=colors["ink"],
                             selectbackground=colors["accent"], selectforeground=colors["surface"],
                             highlightbackground=colors["border"], highlightcolor=colors["accent"],
                             font="TkTextFont")
        elif isinstance(widget, tk.Menu):
            widget.configure(background=colors["surface"], foreground=colors["ink"],
                             activebackground=colors["select"], font="TkMenuFont")
        elif isinstance(widget, tk.Canvas):
            widget.configure(background=colors["bg"])
    except tk.TclError:
        pass
    for child in widget.winfo_children():
        _recolor_widgets(child, colors)


def apply_theme(root, preferences=None):
    preferences = dict(DEFAULTS if preferences is None else preferences)
    colors = THEMES[preferences["theme"]]
    base = 12 if preferences["font_size"] == "large" else 10
    compact = preferences["density"] == "compact"
    root._context_relay_palette = colors
    root.configure(background=colors["bg"])
    for name, offset, weight in (("TkDefaultFont", 0, "normal"), ("TkTextFont", 1, "normal"),
                                 ("TkMenuFont", 0, "normal"), ("TkHeadingFont", 1, "bold")):
        try:
            tkfont.nametofont(name, root=root).configure(
                family="Segoe UI", size=base + offset, weight=weight)
        except tk.TclError:
            pass
    root.option_add("*Text.background", colors["surface"])
    root.option_add("*Text.foreground", colors["ink"])
    root.option_add("*Text.insertBackground", colors["ink"])
    root.option_add("*Text.selectBackground", colors["accent"])
    root.option_add("*Listbox.background", colors["surface"])
    root.option_add("*Listbox.foreground", colors["ink"])
    root.option_add("*Listbox.selectBackground", colors["accent"])
    root.option_add("*Listbox.selectForeground", colors["surface"])
    root.option_add("*Menu.background", colors["surface"])
    root.option_add("*Menu.foreground", colors["ink"])
    root.option_add("*Menu.activeBackground", colors["select"])
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    _rounded_elements(root, style, colors)
    button_pad = (10, 5) if compact else (12, 7)
    primary_pad = (12, 6) if compact else (14, 8)
    style.configure(".", font=("Segoe UI", base), foreground=colors["ink"])
    style.configure("TFrame", background=colors["bg"])
    style.configure("Sidebar.TFrame", background=colors["bg"])
    style.configure("Surface.TFrame", background=colors["surface"])
    style.configure("RoundedEntry.TFrame", background=colors["surface"])
    style.configure("TLabel", background=colors["bg"], foreground=colors["ink"])
    style.configure("Surface.TLabel", background=colors["surface"], foreground=colors["ink"])
    style.configure("Muted.TLabel", background=colors["bg"], foreground=colors["muted"])
    style.configure("Surface.Muted.TLabel", background=colors["surface"], foreground=colors["muted"])
    style.configure("Title.TLabel", background=colors["bg"], foreground=colors["ink"],
                    font=("Segoe UI", base + 5, "bold"))
    style.configure("Surface.Title.TLabel", background=colors["surface"], foreground=colors["ink"],
                    font=("Segoe UI", base + 3, "bold"))
    style.configure("Section.TLabel", background=colors["surface"], foreground=colors["ink"],
                    font=("Segoe UI", base + 1, "bold"))
    style.configure("Alert.TLabel", background=WARNING_BG, foreground=WARNING, padding=(10, 8))
    style.configure("TButton", background=colors["control"], foreground=colors["ink"],
                    bordercolor=colors["border"], padding=button_pad, relief="flat")
    style.map("TButton", background=[("active", colors["control_active"]), ("pressed", colors["select"])],
              foreground=[("disabled", "#98a3af")])
    style.configure("Secondary.TButton", background=colors["surface"], foreground=colors["ink"],
                    bordercolor=colors["border"], padding=button_pad, relief="flat")
    style.map("Secondary.TButton", foreground=[("disabled", "#98a3af")])
    style.configure("Primary.TButton", background=colors["surface"], foreground=colors["surface"],
                    bordercolor=colors["accent"], padding=primary_pad, relief="flat")
    style.map("Primary.TButton", foreground=[("disabled", "#f5f7fb")])
    style.configure("TMenubutton", background=colors["control"], foreground=colors["ink"],
                    bordercolor=colors["border"], padding=button_pad)
    style.configure("Rounded.TMenubutton", background=colors["control"], foreground=colors["ink"],
                    bordercolor=colors["border"], padding=button_pad)
    style.configure("TEntry", fieldbackground=colors["surface"], foreground=colors["ink"],
                    bordercolor=colors["border"], padding=4 if compact else 6)
    style.configure("TCombobox", fieldbackground=colors["surface"], foreground=colors["ink"],
                    bordercolor=colors["border"], padding=4 if compact else 5)
    style.map("TCombobox", fieldbackground=[("readonly", colors["surface"])],
              selectbackground=[("readonly", colors["surface"])],
              selectforeground=[("readonly", colors["ink"])])
    rowheight = (34 if base == 12 else 30) if compact else (38 if base == 12 else 34)
    style.configure("Task.Treeview", background=colors["bg"], fieldbackground=colors["bg"],
                    foreground=colors["ink"], borderwidth=0, relief="flat", rowheight=rowheight,
                    bordercolor=colors["bg"], lightcolor=colors["bg"], darkcolor=colors["bg"])
    style.configure("Task.Treeview.Heading", background=colors["bg"], foreground=colors["muted"],
                    relief="flat", padding=(6, 5 if compact else 7))
    style.map("Task.Treeview", background=[("selected", colors["select"])],
              foreground=[("selected", colors["ink"])])
    card_pad = (12, 8) if compact else (14, 11)
    style.configure("TaskCard.TFrame", background=colors["bg"], padding=card_pad)
    style.configure("Selected.TaskCard.TFrame", background=colors["select"], padding=card_pad)
    style.configure("Focused.TaskCard.TFrame", background=colors["control"], padding=card_pad,
                    relief="solid", borderwidth=1, bordercolor=colors["accent"])
    style.configure("Focused.SelectedCard.TFrame", background=colors["select"], padding=card_pad,
                    relief="solid", borderwidth=1, bordercolor=colors["accent"])
    for selected, background in (("", colors["bg"]), ("Selected.", colors["select"])):
        style.configure(f"{selected}TaskCardTitle.TLabel", background=background, foreground=colors["ink"],
                        font=("Segoe UI", base, "bold"))
        style.configure(f"{selected}TaskCardPreview.TLabel", background=background, foreground=colors["muted"],
                        font=("Segoe UI", max(9, base - 1)))
        style.configure(f"{selected}TaskCardMeta.TLabel", background=background, foreground=colors["muted"],
                        font=("Segoe UI", max(8, base - 2)))
    style.configure("Vertical.TScrollbar", background="#cfd7e1", troughcolor="#f1f3f6",
                    bordercolor="#f1f3f6", lightcolor="#cfd7e1", darkcolor="#cfd7e1",
                    arrowcolor=colors["muted"], relief="flat", borderwidth=0)
    style.map("Vertical.TScrollbar", background=[("active", "#bbc6d2")])
    style.configure("TNotebook", background=colors["bg"], borderwidth=0)
    style.configure("TNotebook.Tab", background=colors["control"], foreground=colors["muted"],
                    padding=(12, 6) if compact else (14, 8))
    style.map("TNotebook.Tab", background=[("selected", colors["surface"])],
              foreground=[("selected", colors["ink"])])
    style.configure("Surface.TLabelframe", background=colors["surface"],
                    bordercolor=colors["border"], relief="solid")
    style.configure("Surface.TLabelframe.Label", background=colors["surface"], foreground=colors["ink"],
                    font=("Segoe UI", base, "bold"))
    style.configure("Attention.TLabelframe", background=WARNING_BG, bordercolor="#efd7ae", relief="solid")
    style.configure("Attention.TLabelframe.Label", background=WARNING_BG, foreground=WARNING,
                    font=("Segoe UI", base, "bold"))
    _recolor_widgets(root, colors)
    return style


STATES = {
    "queued": "待启动", "creating": "正在创建", "running": "运行中",
    "pausing": "正在暂停", "paused": "已暂停", "idle": "等待继续",
    "summarizing": "准备交接", "verifying": "核验接管", "needs_reconcile": "待核对恢复",
    "blocked": "需要处理", "completed": "已完成",
    "briefing": "正在整理简报", "reviewing": "正在审核成果",
}
ACTIVE = {"creating", "running", "pausing", "summarizing", "verifying", "briefing", "reviewing"}
APPROVALS = {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}
USER_INPUT = "item/tool/requestUserInput"
FILTERS = ("全部未归档", "运行中", "待处理", "等待继续", "已完成", "已归档")
IMPORT_WARNINGS = {
    "cross_client_activity_unknown": "无法确认原客户端是否仍在执行；请回原聊天核对。",
    "stop_original_thread_before_import": "导入前须停止原聊天及同项目其他操作。",
    "source_thread_active": "来源聊天仍有活动状态，暂不能导入。",
    "source_thread_system_error": "来源聊天报告系统错误，请先在原聊天核对。",
    "title_truncated": "聊天名称过长，当前显示已截短。",
    "history_contains_incomplete_turns": "历史中存在未完整收束的轮次，不能当作已完成操作。",
    "latest_turn_not_completed": "最近轮次尚未完成，请先核对结果。",
    "unfinished_action": "仍有未收束的工具操作，请先查明结果。",
    "metadata_only": "当前只取得聊天信息，未取得可供导入的文字历史。",
    "no_completed_turn": "尚无已完成轮次，不能假定历史操作已完成。",
    "unfinished_turn": "存在仍在进行的轮次，请先收束原聊天。",
    "incomplete_or_unknown_history": "历史资料不完整或含未知状态，须先核对原始结果。",
    "active_subagent": "仍有等待或运行中的子代理，暂不能导入。",
    "subagent_activity_unknown": "子代理状态缺失或未知，须先核对，暂不能导入。",
    "subagent_incomplete": "子代理已中断、出错或关闭，历史结果仍有缺口。",
}
EVENT_LABELS = {
    "task_created": "已记录任务", "create_requested": "已请求创建会话", "thread_created": "已收到会话创建回执",
    "turn_requested": "已请求启动轮次", "turn_started": "已收到启动回执（不代表已实际开始）",
    "native_turn_started": "原生轮次已开始", "turn_completed": "已收到轮次终态", "work_finished": "工作轮次结束",
    "interrupt_requested": "已记录暂停请求", "paused": "已暂停", "draft_saved": "已保存预备快照",
    "budget_pause_requested": "已因预算请求暂停（以任务状态确认是否停止）",
    "requires_reconciliation": "需要核对恢复", "restart_requires_reconciliation": "重启后需要核对恢复",
    "reconciled_read_only": "已完成只读恢复核对", "handoff_requested": "已请求交接",
    "handoff_unnecessary": "无需交接", "checkpoint_frozen": "已冻结检查点", "ownership_transferred": "已移交执行权",
    "receiver_abandoned": "已放弃原接收会话", "user_instruction": "已记录用户指令", "user_answer": "已记录用户回答",
    "awaiting_user": "等待用户处理", "approval_denied_by_guard": "执行检查拒绝审批",
    "permission_expansion_denied": "已拒绝扩大权限", "secret_input_refused": "已拒绝采集秘密信息",
    "late_receipt_recorded": "已记录迟到回执", "tool_state": "已记录工具状态",
    "user_marked_complete": "用户已标记完成", "task_archived": "已归档", "task_unarchived": "已取消归档",
    "task_settings_updated": "已更新任务设置", "task_reopened": "已重新打开任务（尚未启动）",
    "source_imported": "已导入历史资料（尚未启动）",
    "source_import_updated": "已更新待启动任务的来源资料",
    "assessment_requested": "已请求只读分析", "assessment_ready": "已收到 AI 分析意见（待人工处理）",
    "assessment_rejected": "分析候选未通过核验（未采用，可明确重新整理）",
    "assessment_stale": "分析记录已过期", "brief_adopted": "用户已采用目标与验收标准",
    "review_accepted": "用户已认可本阶段审核结果", "review_rework_requested": "已记录用户返工请求",
}


def manager_factory(state_dir=None):
    from .manager import Manager
    return Manager(state_dir=state_dir)


def needs_attention(task):
    return not task.get("archived", False) and bool(
        task.get("state") in ("blocked", "needs_reconcile") or task.get("pending"))


class CommandWorker(threading.Thread):
    def __init__(self, factory, state_dir):
        super().__init__(name="context-relay-manager", daemon=False)
        self.factory, self.state_dir = factory, state_dir
        self.commands = queue.Queue()
        self.events = queue.Queue()
        self.stop_requested = threading.Event()
        self.phone = None

    def run(self):
        try:
            manager = self.factory(state_dir=self.state_dir)
            manager.stop_requested = self.stop_requested
        except Exception as error:
            self.events.put(("startup_error", str(error)))
            return
        previous, last_poll_error = None, None
        from .phone import PhoneHost
        self.phone = PhoneHost(manager, self.commands)
        self.events.put(("ready", deepcopy(getattr(manager, "recovery_info", None))))
        while True:
            if self.stop_requested.is_set():
                while True:
                    try:
                        self.commands.get_nowait()
                    except queue.Empty:
                        break
                try:
                    self.phone.close()
                    manager.close()
                except Exception as error:
                    self.stop_requested.clear()
                    self.events.put(("close_error", str(error)))
                else:
                    self.events.put(("closed", None))
                    return
            try:
                command = self.commands.get(timeout=0.2)
            except queue.Empty:
                command = None
            if self.stop_requested.is_set():
                continue
            if command is not None:
                method, args, kwargs = command
                if method == "remote_command":
                    try:
                        self.phone.execute(*args)
                    except Exception as error:
                        self.events.put(("remote_error", str(error)))
                else:
                    phone_actions = {"remote_enable": self.phone.enable, "remote_disable": self.phone.close,
                                     "remote_status": self.phone.status, "remote_pair": self.phone.pair,
                                     "remote_revoke": self.phone.revoke, "remote_network": self.phone.network_status}
                    try:
                        action = phone_actions[method] if method in phone_actions else getattr(manager, method)
                        result = action(*args, **kwargs)
                    except Exception as error:
                        self.events.put(("command_error", (method, str(error))))
                    else:
                        self.events.put(("command_done", (method, args, deepcopy(result))))
                if self.stop_requested.is_set():
                    continue
            try:
                manager.poll()
                tasks = deepcopy(manager.list_tasks())
                self.phone.publish(tasks)
                if tasks != previous:
                    previous = tasks
                    self.events.put(("tasks", tasks))
                last_poll_error = None
            except Exception as error:
                if str(error) != last_poll_error:
                    last_poll_error = str(error)
                    self.events.put(("poll_error", last_poll_error))


def set_text(widget, value, follow=False):
    value = value or ""
    if widget.get("1.0", "end-1c") == value:
        return
    view = widget.yview()
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("1.0", value)
    widget.configure(state="disabled")
    if follow and view[1] >= 0.98:
        widget.see("end")
    else:
        widget.yview_moveto(view[0] if follow else 0)


def configure_changed(widget, **values):
    changed = {key: value for key, value in values.items() if str(widget.cget(key)) != str(value)}
    if changed:
        widget.configure(**changed)


def set_changed(variable, value):
    if variable.get() != value:
        variable.set(value)


def menu_changed(menu, index, **values):
    changed = {key: value for key, value in values.items() if str(menu.entrycget(index, key)) != str(value)}
    if changed:
        menu.entryconfigure(index, **changed)


def text_area(parent, height):
    colors = getattr(parent._root(), "_context_relay_palette", THEMES["blue"])
    frame = ttk.Frame(parent, style="Surface.TFrame")
    widget = tk.Text(frame, height=height, wrap="word", state="disabled", relief="flat",
                     background=colors["surface"], foreground=colors["ink"],
                     insertbackground=colors["ink"], selectbackground=colors["accent"],
                     borderwidth=0, highlightthickness=0, padx=10, pady=8, font="TkTextFont")
    scrollbar = ttk.Scrollbar(frame, orient="vertical", command=widget.yview)
    widget.configure(yscrollcommand=scrollbar.set)
    widget.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")
    frame.pack(fill="both", expand=True, padx=6, pady=6)
    return widget


class HistoryWindow(tk.Toplevel):
    def __init__(self, parent, task):
        super().__init__(parent)
        self.task_id = task["id"]
        self.title(f"操作记录 · {task['title']}")
        self.transient(parent)
        self.geometry(f"{min(760, parent.winfo_screenwidth() - 80)}x{min(540, parent.winfo_screenheight() - 120)}")
        self.history_text = text_area(self, 18)
        lines = [f"任务：{task['title']}\n任务 ID：{task['id']}",
                 "最近最多 100 条本地操作记录；不是完整对话，也不是外部成功凭证。关闭后重新打开可刷新。", ""]
        status_labels = dict(STATES, inProgress="进行中", failed="失败", interrupted="已中断", declined="已拒绝")
        purpose_labels = {"work": "任务执行", "summary": "交接摘要", "verify": "接收核验"}
        for event in task.get("events", []):
            line = f"{event.get('at', '时间未知')} · {EVENT_LABELS.get(event.get('kind'), '其他本地记录')}"
            data = event.get("data") or {}
            for key, label, labels in (("status", "状态", status_labels), ("purpose", "阶段", purpose_labels)):
                value = data.get(key)
                if isinstance(value, str):
                    line += f" · {label}：{labels.get(value, '未知')}"
            lines.append(line)
        if not task.get("events"):
            lines.append("暂无本地操作记录。")
        set_text(self.history_text, "\n".join(lines))
        ttk.Button(self, text="关闭", command=self.destroy).pack(pady=(0, 10))


class DiagnosticsWindow(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.report_ready = False
        self.title("诊断信息 · 只读预览")
        self.transient(app.root)
        self.geometry(f"{min(760, app.root.winfo_screenwidth() - 80)}x{min(540, app.root.winfo_screenheight() - 120)}")
        ttk.Label(self, text="本机运行与任务数量摘要；不含任务标题、路径、编号、对话正文或凭据。\n"
                            "保存时重新采集最新状态，请选择一个新 JSON 文件。", wraplength=700,
                  justify="left").pack(fill="x", padx=12, pady=(10, 0))
        self.report_text = text_area(self, 18)
        set_text(self.report_text, "正在读取本机诊断信息…")
        actions = ttk.Frame(self)
        actions.pack(side="bottom", fill="x", padx=12, pady=(0, 10), before=self.report_text.master)
        self.save_button = ttk.Button(actions, text="保存 JSON…", command=self.save, state="disabled")
        self.save_button.pack(side="right")
        ttk.Button(actions, text="关闭", command=self.destroy).pack(side="right", padx=8)

    def save(self):
        if not self.report_ready or self.app.busy or self.app.closing or self.app.closed:
            return
        destination = filedialog.asksaveasfilename(parent=self, title="保存新的诊断信息 JSON",
                                                  defaultextension=".json", initialfile="context-relay-diagnostics.json",
                                                  filetypes=(("JSON", "*.json"),), confirmoverwrite=False)
        if destination and not self.app.closing and not self.app.closed and self.winfo_exists():
            self.app.submit("export_diagnostics", destination)


class NewTaskDialog(tk.Toplevel):
    def __init__(self, parent, submit):
        super().__init__(parent)
        self.title("新建任务")
        self.transient(parent)
        self.resizable(True, False)
        self.submit = submit
        self.fields = {}
        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        for row, (key, label, value) in enumerate((
                ("title", "任务名称", ""), ("cwd", "工作目录", str(Path.cwd())))):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=5)
            variable = tk.StringVar(value=value)
            self.fields[key] = variable
            ttk.Entry(body, textvariable=variable, width=54).grid(row=row, column=1, sticky="ew", pady=5)
        ttk.Button(body, text="选择…", command=self.choose_directory).grid(row=1, column=2, padx=(8, 0))
        ttk.Label(body, text="权限上限").grid(row=2, column=0, sticky="w", pady=5)
        self.mode = tk.StringVar(value="只读")
        ttk.Combobox(body, textvariable=self.mode, values=("只读", "允许修改工作区"),
                     state="readonly").grid(row=2, column=1, sticky="w", pady=5)
        ttk.Label(body, text="任务目标与完成标准").grid(row=3, column=0, columnspan=3, sticky="w")
        self.goal = tk.Text(body, height=7, width=68, wrap="word")
        self.goal.grid(row=4, column=0, columnspan=3, sticky="ew", pady=6)
        self.advanced_open = tk.BooleanVar(value=False)
        ttk.Checkbutton(body, text="显示预算与自动交接", variable=self.advanced_open,
                        command=self.toggle_advanced).grid(row=5, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.advanced_frame = ttk.Frame(body)
        self.advanced_frame.grid(row=6, column=0, columnspan=3, sticky="ew")
        self.advanced_frame.columnconfigure(1, weight=1)
        for row, (key, label) in enumerate((("max_tokens", "Token 上限（留空不限）"),
                                            ("max_minutes", "分钟上限（留空不限）"))):
            variable = tk.StringVar()
            self.fields[key] = variable
            ttk.Label(self.advanced_frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=4)
            ttk.Entry(self.advanced_frame, textvariable=variable, width=32).grid(row=row, column=1, sticky="ew", pady=4)
        self.auto_handoff = tk.BooleanVar(value=False)
        ttk.Checkbutton(self.advanced_frame, text="允许上下文风险达到条件时自动交接此任务",
                        variable=self.auto_handoff).grid(row=2, column=0, columnspan=2, sticky="w", pady=6)
        ttk.Label(self.advanced_frame, text="预算是软上限，当前调用可能超出；已记录用时包含等待。",
                  foreground="#555555").grid(row=3, column=0, columnspan=2, sticky="w")
        self.advanced_frame.grid_remove()
        buttons = ttk.Frame(body)
        buttons.grid(row=7, column=0, columnspan=3, sticky="e", pady=(8, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="left", padx=5)
        ttk.Button(buttons, text="创建", command=self.save).pack(side="left")
        self.grab_set()

    def toggle_advanced(self):
        (self.advanced_frame.grid if self.advanced_open.get() else self.advanced_frame.grid_remove)()

    def choose_directory(self):
        directory = filedialog.askdirectory(parent=self, initialdir=self.fields["cwd"].get())
        if directory:
            self.fields["cwd"].set(directory)

    def save(self):
        values = {key: value.get().strip() for key, value in self.fields.items()}
        values["goal"] = self.goal.get("1.0", "end").strip()
        if not values["title"] or not values["goal"] or not values["cwd"]:
            messagebox.showwarning("请补全任务", "任务名称、工作目录和目标不能为空。", parent=self)
            return
        try:
            directory = Path(values["cwd"]).expanduser().resolve()
            if not directory.is_dir():
                raise ValueError("工作目录不存在。")
            values["cwd"] = str(directory)
            values["max_tokens"], values["max_minutes"] = validate_limits(
                values["max_tokens"] or "0", values["max_minutes"] or "0")
        except (OSError, ValueError) as error:
            messagebox.showwarning("输入有误", str(error), parent=self)
            return
        values.update(mode="read-only" if self.mode.get() == "只读" else "workspace-write",
                      auto_handoff=self.auto_handoff.get())
        if self.submit("create_task", **values):
            self.destroy()


class TaskSettingsDialog(tk.Toplevel):
    def __init__(self, parent, task, submit):
        super().__init__(parent)
        self.task_id = task["id"]
        self.submit = submit
        self.auto_allowed = task.get("telemetry_model_valid", True) is not False
        self.title(f"任务设置 · {task['title']}")
        self.transient(parent)
        self.resizable(True, False)
        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        self.fields = {}
        self.fields["title"] = tk.StringVar(value=str(task.get("title") or ""))
        ttk.Label(body, text="任务名称").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=5)
        ttk.Entry(body, textvariable=self.fields["title"], width=44).grid(row=0, column=1, sticky="ew", pady=5)
        self.advanced_open = tk.BooleanVar(value=False)
        ttk.Checkbutton(body, text="显示预算与自动交接", variable=self.advanced_open,
                        command=self.toggle_advanced).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.advanced_frame = ttk.Frame(body)
        self.advanced_frame.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.advanced_frame.columnconfigure(1, weight=1)
        for row, (key, label) in enumerate((("max_tokens", "Token 上限（留空或 0 不限）"),
                                            ("max_minutes", "分钟上限（可小数，0 不限）"))):
            self.fields[key] = tk.StringVar(value=str(task.get(key) or ""))
            ttk.Label(self.advanced_frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=5)
            ttk.Entry(self.advanced_frame, textvariable=self.fields[key], width=36).grid(row=row, column=1, sticky="ew", pady=5)
        self.auto_handoff = tk.BooleanVar(value=bool(task.get("auto_handoff")) and self.auto_allowed)
        self.auto_check = ttk.Checkbutton(self.advanced_frame, text="允许按上下文风险自动预备与交接此任务", variable=self.auto_handoff,
                                         state="normal" if self.auto_allowed else "disabled")
        self.auto_check.grid(row=2, column=0, columnspan=2, sticky="w", pady=8)
        ttk.Label(self.advanced_frame, text="预算是软上限，当前调用可能超出；已记录用时包含等待。",
                  foreground="#555555").grid(row=3, column=0, columnspan=2, sticky="w")
        self.advanced_frame.grid_remove()
        text = "保存会使原预备快照与检查点失效；不会启动任务。权限、目录和原始目标保持不变。"
        if not self.auto_allowed:
            text += "\n模型口径无效，不能开启自动交接，请先核对遥测。"
        self.notice = ttk.Label(body, text=text, wraplength=580, justify="left")
        self.notice.grid(row=3, column=0, columnspan=2, sticky="w", pady=6)
        buttons = ttk.Frame(body)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="left", padx=5)
        self.save_button = ttk.Button(buttons, text="保存设置", command=self.save)
        self.save_button.pack(side="left")
        self.grab_set()

    def toggle_advanced(self):
        (self.advanced_frame.grid if self.advanced_open.get() else self.advanced_frame.grid_remove)()

    def save(self):
        try:
            title = self.fields["title"].get().strip()
            if not title:
                raise ValueError("任务名称不能为空。")
            tokens, minutes = validate_limits(self.fields["max_tokens"].get().strip() or "0",
                                              self.fields["max_minutes"].get().strip() or "0")
            auto = self.auto_handoff.get()
            if auto and not self.auto_allowed:
                raise ValueError("模型口径无效，不能开启自动交接，请先核对遥测。")
        except ValueError as error:
            messagebox.showwarning("输入有误", str(error), parent=self)
            return
        if self.submit("update_settings", self.task_id, title=title, max_tokens=tokens,
                       max_minutes=minutes, auto_handoff=auto):
            self.destroy()


def import_time(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OverflowError, OSError):
            return "未知"
    return str(value or "未知")


class ImportDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.generation = 0
        self.selected_source = None
        self.preview = None
        self.next_cursor = None
        self.search, self.archived = tk.StringVar(), tk.BooleanVar(value=False)
        self.info = tk.StringVar(value="选择来源后，点击预览。")
        self.title_value, self.mode = tk.StringVar(), tk.StringVar(value="read-only")
        self.tokens, self.minutes = tk.StringVar(value="0"), tk.StringVar(value="0")
        self.auto, self.source_stopped = tk.BooleanVar(value=False), tk.BooleanVar(value=False)
        self.title("连接或导入已有 Codex 聊天")
        self.geometry(f"{min(900, self.winfo_screenwidth() - 80)}x{min(820, self.winfo_screenheight() - 140)}")
        self.transient(app.root)
        footer = ttk.Frame(self, padding=10)
        footer.pack(side="bottom", fill="x")
        ttk.Label(footer, textvariable=self.info, wraplength=820, justify="left").pack(fill="x", pady=(0, 6))
        self.close_button = ttk.Button(footer, text="关闭", command=self.destroy)
        self.close_button.pack(side="right")
        self.save_button = ttk.Button(footer, text="导入为待启动任务", command=self.save)
        self.save_button.pack(side="right", padx=8)
        self.connect_button = ttk.Button(footer, text="连接原聊天（原话接续）", command=lambda: self.save(direct=True))
        self.connect_button.pack(side="right", padx=8)
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="连接原聊天会原话接续，但原桌面窗口未必实时同步；导入会另建任务。请先停止原聊天操作并重新选择权限，两者都不会自动开始。",
                  wraplength=820, justify="left").pack(fill="x", pady=(0, 8))
        search = ttk.Frame(top)
        search.pack(fill="x")
        ttk.Entry(search, textvariable=self.search).pack(side="left", fill="x", expand=True)
        ttk.Checkbutton(search, text="已归档", variable=self.archived).pack(side="left", padx=6)
        self.load_button = ttk.Button(search, text="搜索 / 首页", command=self.load_page)
        self.load_button.pack(side="left")
        self.next_button = ttk.Button(search, text="下一页", command=lambda: self.load_page(self.next_cursor))
        self.next_button.pack(side="left", padx=(6, 0))
        listing = ttk.Frame(top)
        listing.pack(fill="x", pady=8)
        self.tree = ttk.Treeview(listing, columns=("updated", "source"), show="tree headings", height=4, selectmode="browse")
        for column, label, width in (("#0", "来源聊天", 380), ("updated", "更新时间（本机）", 170), ("source", "来源类型", 100)):
            self.tree.heading(column, text=label)
            self.tree.column(column, width=width, minwidth=60)
        self.tree.pack(side="left", fill="x", expand=True)
        scroll = ttk.Scrollbar(listing, command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self.select_source)
        self.preview_button = ttk.Button(top, text="预览选定聊天", command=self.preview_selected)
        self.preview_button.pack(anchor="w")
        scrolling = ttk.Frame(self)
        scrolling.pack(fill="both", expand=True, padx=10)
        self.canvas = tk.Canvas(scrolling, highlightthickness=0)
        scrollbar = ttk.Scrollbar(scrolling, command=self.canvas.yview)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        body = ttk.Frame(self.canvas, padding=(0, 0, 8, 8))
        body_id = self.canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(body_id, width=event.width))
        ttk.Label(body, text="来源预览（只读摘录，不是完整历史或权限证明）").pack(anchor="w")
        self.preview_text = text_area(body, 6)
        ttk.Label(body, text="任务名称").pack(anchor="w")
        ttk.Entry(body, textvariable=self.title_value).pack(fill="x", pady=(2, 6))
        ttk.Label(body, text="补充想法（可留空；留空时首次启动先整理简报，消耗模型用量）").pack(anchor="w")
        self.goal_text = tk.Text(body, height=3, wrap="word")
        self.goal_text.pack(fill="x", pady=(2, 6))
        options = ttk.Frame(body)
        options.pack(fill="x", pady=4)
        ttk.Label(options, text="权限").grid(row=0, column=0, sticky="w")
        ttk.Combobox(options, textvariable=self.mode, values=("read-only", "workspace-write"), state="readonly", width=18).grid(row=0, column=1, padx=6)
        self.advanced_open = tk.BooleanVar(value=False)
        ttk.Checkbutton(body, text="显示预算与自动交接", variable=self.advanced_open,
                        command=self.toggle_advanced).pack(anchor="w", pady=(4, 0))
        self.advanced_frame = ttk.Frame(body)
        self.advanced_frame.pack(fill="x")
        ttk.Label(self.advanced_frame, text="Token 上限").grid(row=0, column=0)
        ttk.Entry(self.advanced_frame, textvariable=self.tokens, width=10).grid(row=0, column=1, padx=6)
        ttk.Label(self.advanced_frame, text="分钟上限").grid(row=0, column=2)
        ttk.Entry(self.advanced_frame, textvariable=self.minutes, width=8).grid(row=0, column=3, padx=6)
        ttk.Label(self.advanced_frame, text="0 表示不限；预算是软边界，当前调用可能超出。",
                  foreground="#555555").grid(row=1, column=0, columnspan=4, sticky="w")
        ttk.Checkbutton(self.advanced_frame, text="允许满足阈值后自动交接（可选）",
                        variable=self.auto).grid(row=2, column=0, columnspan=4, sticky="w", pady=6)
        self.advanced_frame.pack_forget()
        self.source_confirm = ttk.Checkbutton(body, text="我确认原聊天及同项目的其他操作已停止",
                                              variable=self.source_stopped, command=self.controls)
        self.source_confirm.pack(anchor="w", pady=6)
        self.controls()

    def toggle_advanced(self):
        if self.advanced_open.get():
            self.advanced_frame.pack(fill="x", before=self.source_confirm)
        else:
            self.advanced_frame.pack_forget()

    def reset_preview(self):
        self.generation += 1
        self.preview = None
        self.title_value.set("")
        self.goal_text.delete("1.0", "end")
        self.mode.set("read-only")
        self.tokens.set("0")
        self.minutes.set("0")
        self.auto.set(False)
        self.source_stopped.set(False)
        set_text(self.preview_text, "")
        self.save_button.configure(text="导入为待启动任务")

    def request(self, method, *args, **kwargs):
        token = (self.generation, self.selected_source)
        if self.app.submit_import(self, token, method, *args, **kwargs):
            self.info.set("正在读取…" if method != "import_thread" else "正在保存待启动任务…")
            self.controls()

    def load_page(self, cursor=None):
        if self.app.busy or self.app.closing:
            return
        self.reset_preview()
        self.selected_source = None
        self.next_cursor = None
        self.tree.delete(*self.tree.get_children())
        self.request("list_import_threads", search=self.search.get().strip(), cursor=cursor, archived=self.archived.get())

    def select_source(self, event=None):
        selected = self.tree.selection()
        source = selected[0] if selected else None
        if source != self.selected_source:
            self.selected_source = source
            self.reset_preview()
            self.info.set("点击预览核对来源，可补充本轮想法。")
        self.controls()

    def preview_selected(self):
        if not self.selected_source or self.app.busy:
            return
        self.reset_preview()
        self.request("preview_import", self.selected_source)

    def can_save(self):
        if not self.preview or not self.preview.get("can_import"):
            return False
        existing = self.preview.get("existing_task")
        return not existing or (existing.get("state") in ("queued", "paused", "idle") and existing.get("thread_id") is None
                                and not existing.get("archived", False) and not existing.get("work_turns", 0)
                                and not any(existing.get(key) for key in ("receiver_id", "analysis_thread_id", "intent", "pending", "inflight", "assessment"))
                                and all(entry.get("role") == "analysis" for entry in existing.get("history", []))
                                and existing.get("run_started") is None)

    def controls(self):
        available = self.app.ready and not self.app.busy and not self.app.closing and self.app.recovery_info is None
        configure_changed(self.load_button, state="normal" if available else "disabled")
        configure_changed(self.next_button, state="normal" if available and self.next_cursor else "disabled")
        configure_changed(self.preview_button,
                          state="normal" if available and self.selected_source else "disabled")
        enabled = available and self.can_save() and self.source_stopped.get()
        configure_changed(self.save_button, state="normal" if enabled else "disabled")
        configure_changed(self.connect_button, state="normal" if enabled else "disabled")

    def deliver(self, method, token, result=None, error=None):
        if token != (self.generation, self.selected_source):
            return
        if error:
            self.info.set(f"需要处理：{error}")
        elif method == "list_import_threads":
            for item in result["data"]:
                title = item.get("title") or item["id"]
                if item.get("imported_task_id"):
                    title += "（已导入）"
                self.tree.insert("", "end", iid=item["id"], text=title,
                                 values=(import_time(item.get("updated_at")),
                                         "桌面 / IDE" if item.get("source") == "vscode" else item.get("source", "未知")))
            self.next_cursor = result.get("next_cursor")
            self.info.set(f"本页 {len(result['data'])} 个聊天；选择后显式预览。")
        elif method == "preview_import":
            if result.get("thread_id") != self.selected_source:
                self.info.set("来源标识不匹配，请重新预览。")
                return
            self.reset_preview()
            self.preview = result
            existing = result.get("existing_task")
            self.title_value.set((existing or {}).get("title") or result.get("title") or self.selected_source)
            if existing and self.can_save():
                self.goal_text.insert("1.0", "" if existing.get("brief_required") else existing.get("goal", ""))
                self.mode.set(existing.get("mode", "read-only"))
                self.tokens.set(str(existing.get("max_tokens", 0)))
                self.minutes.set(str(existing.get("max_minutes", 0)))
                self.auto.set(existing.get("auto_handoff", False))
                configure_changed(self.save_button, text="更新待启动任务")
            status = result.get("status", "未知")
            if status == "notLoaded":
                status = "未在本连接加载（原端状态未知）"
            lines = [f"来源：{result['thread_id']}", f"目录：{result.get('cwd', '未知')}",
                     f"原生状态：{status}（不证明原聊天已停止）",
                     f"读取时间：{import_time(result.get('read_at'))} · 更新时间：{import_time(result.get('updated_at'))}",
                     f"未展示消息 {result.get('omitted_messages', 0)} · 截短消息 {result.get('truncated_messages', 0)} · 非文字项目 {result.get('non_text_items', 0)}"]
            lines.extend(IMPORT_WARNINGS.get(warning, "存在未识别的来源提示，请先回原聊天核对。")
                         for warning in result.get("warnings", []))
            lines.extend(f"\n[{item.get('role', '未知')}] {item.get('text', '')}" for item in result.get("messages", []))
            set_text(self.preview_text, "\n".join(lines))
            self.info.set("确认原操作已停止；想法可留空，导入仅保存待办。首次整理简报会消耗模型用量。" if self.can_save() else
                          "不能导入：已有任务已启动、已归档，或来源不满足条件；请核对预览提示。")
        elif method == "import_thread":
            self.destroy()
            return
        self.controls()

    def save(self, direct=False):
        if self.app.busy or not self.can_save() or not self.source_stopped.get():
            self.info.set("请先预览可导入来源，并确认原聊天及同项目操作已停止。")
            return
        try:
            title, goal = self.title_value.get().strip(), self.goal_text.get("1.0", "end-1c").strip()
            if not title:
                raise ValueError("任务名称不能为空。")
            tokens, minutes = validate_limits(self.tokens.get().strip(), self.minutes.get().strip())
        except ValueError as error:
            self.info.set(f"输入有误：{error}")
            return
        existing = self.preview.get("existing_task")
        self.request("import_thread", self.selected_source, self.preview["fingerprint"], title=title, goal=goal,
                     mode=self.mode.get(), source_stopped=True, existing_task_id=existing["id"] if existing else None,
                     max_tokens=tokens, max_minutes=minutes, auto_handoff=False if direct else self.auto.get(),
                     **({"direct": True} if direct else {}))


def assessment_content(value):
    labels = {"severity": "程度", "category": "类别", "finding": "观察", "suggestion": "建议",
              "evidence": "证据", "result": "结果", "path": "文件", "line": "行号", "source": "来源", "reference": "引用"}
    if isinstance(value, dict):
        return "\n".join(f"{labels.get(key, key)}：{assessment_content(item)}" for key, item in value.items())
    if isinstance(value, list):
        return "\n".join(f"• {assessment_content(item)}" for item in value) or "未提供"
    return str(value) if value is not None else "未提供"


def analysis_message_kind(task):
    """Classify internal output, never validate or adopt its contents."""
    kind = task.get("last_message_kind")
    if kind is not None:
        return kind if kind in ("brief", "review") else None
    if task.get("purpose") == "work":
        return None
    for kind in (task.get("purpose"), (task.get("assessment") or {}).get("kind")):
        if kind in ("brief", "review"):
            return kind
    # Older imported tasks lack a kind after reconciliation. Only recognize the
    # exact brief envelope in the pre-work brief phase; ordinary JSON stays text.
    if not task.get("brief_required") or task.get("work_turns", 0) != 0:
        return None
    raw = task.get("last_message") or ""
    if len(raw) > 64 * 1024:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    lists = ("decisions", "completed", "unknowns", "approaches", "acceptance")
    if (not isinstance(value, dict) or set(value) != {*lists, "goal", "next_step", "evidence"}
            or any(not isinstance(value.get(key), str) or not value[key].strip() for key in ("goal", "next_step"))
            or any(not isinstance(value.get(key), list) or not all(isinstance(item, str) for item in value[key]) for key in lists)
            or not value["acceptance"] or not isinstance(value["evidence"], list)):
        return None
    if any(not isinstance(item, dict) or set(item) != {"source", "reference", "finding"}
           or item["source"] not in ("file", "chat")
           or not all(isinstance(item[key], str) and item[key].strip() for key in ("reference", "finding"))
           for item in value["evidence"]):
        return None
    return "brief"


def task_error(task):
    message = str(task.get("error") or "")
    for original, readable in (
        ("chat evidence is not present in the bound source snapshot", "分析中的聊天引用不在已导入的资料中"),
        ("file evidence is not present in the bound workspace snapshot", "分析中的文件引用不在本次工作区记录中"),
    ):
        message = message.replace(original, readable)
    return message


def assessment_guidance(task, kind):
    label = "简报" if kind == "brief" else "审核"
    generate = "生成 / 重新整理简报" if kind == "brief" else "审核当前成果"
    record = task.get(kind) or {}
    if task.get("state") == "needs_reconcile":
        reason = f"\n原因：{task_error(task)}" if task.get("error") else ""
        return f"本轮{label}状态仍待核对。{reason}\n请回主窗口点击“核对恢复”；确认收束后再明确重新整理，不会自动重试或继续工作。"
    if task.get("state") in ACTIVE:
        return f"正在进行只读{label}分析；完成后仍需核验和人工处理，不会自动采用或开始工作。"
    if task.get("error"):
        return f"本次{label}未通过核验，不能采用。\n原因：{task_error(task)}\n请点击“{generate}”重新整理，会再次消耗模型用量。"
    if record.get("status") == "current":
        if kind == "brief":
            if record.get("decision") == "adopted":
                mode = "只读" if task.get("mode") == "read-only" else "项目可写"
                return f"目标和验收标准已采用。回主窗口点击“启动 / 继续”才开始工作；当前权限仍为{mode}。"
            return "简报已整理为候选。请打开“简报 / 审核”，核对并采用目标与验收标准，再明确启动工作。"
        return "AI 审核意见已生成；请在“简报 / 审核”中检查。自动化验收未独立验证，人工认可与发布验收另行处理。"
    return f"尚无通过核验的{label}结果。请点击“{generate}”重新整理，会消耗模型用量；不会自动开始工作。"


def task_reply(task):
    kind = analysis_message_kind(task)
    if kind is None:
        return task.get("last_message") or "尚无回复。"
    lines = [assessment_guidance(task, kind)]
    record = task.get(kind) or {}
    if not task.get("error") and task.get("state") in ("queued", "idle", "paused") and record.get("status") == "current":
        report = record.get("report") or {}
        if kind == "brief":
            lines.extend((f"\n目标\n{record.get('adopted_goal', report.get('goal', '未提供'))}",
                          f"\n验收标准\n{assessment_content(record.get('adopted_acceptance', report.get('acceptance')))}",
                          f"\n建议下一步\n{assessment_content(report.get('next_step'))}"))
        else:
            lines.append(f"\n审核摘要\n{assessment_content(report.get('summary'))}")
    return "\n".join(lines)


class AssessmentDialog(tk.Toplevel):
    def __init__(self, app, task):
        super().__init__(app.root)
        self.app, self.task_id = app, task["id"]
        self.generation, self.last_binding, self.last_brief_report = 0, None, None
        self.info = tk.StringVar(value="简报、审核及开始返工均消耗模型用量；采用和返工前会重新核验资料。")
        self.task_status = tk.StringVar()
        self.title(f"简报 / 审核 · {task['title']}")
        self.geometry(f"{min(900, self.winfo_screenwidth() - 80)}x{min(800, self.winfo_screenheight() - 140)}")
        self.transient(app.root)
        self.heading_label = ttk.Label(self, text=task["title"], wraplength=820, justify="left")
        self.heading_label.pack(fill="x", padx=12, pady=(10, 4))
        ttk.Label(self, textvariable=self.task_status, wraplength=820, justify="left").pack(fill="x", padx=12)
        footer = ttk.Frame(self, padding=10)
        footer.pack(side="bottom", fill="x")
        ttk.Label(footer, textvariable=self.info, wraplength=820, justify="left").pack(fill="x")
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=10, pady=8)
        brief, review = ttk.Frame(self.tabs), ttk.Frame(self.tabs)
        self.tabs.add(brief, text="任务简报")
        self.tabs.add(review, text="阶段审核")
        brief_actions, brief_body = self.page(brief)
        self.generate_brief = ttk.Button(brief_actions, text="生成 / 重新整理简报", command=lambda: self.action("analyze", "brief"))
        self.generate_brief.pack(side="left", padx=4)
        self.adopt_button = ttk.Button(brief_actions, text="采用目标与验收标准", command=self.adopt)
        self.adopt_button.pack(side="left", padx=4)
        ttk.Label(brief_body, text="目标（采用前可编辑）").pack(anchor="w")
        self.goal_text = tk.Text(brief_body, height=3, wrap="word")
        self.goal_text.pack(fill="x", pady=4)
        ttk.Label(brief_body, text="验收标准（每行一项）").pack(anchor="w")
        self.acceptance_text = tk.Text(brief_body, height=4, wrap="word")
        self.acceptance_text.pack(fill="x", pady=4)
        self.brief_text = text_area(brief_body, 16)
        review_actions, review_body = self.page(review)
        self.generate_review = ttk.Button(review_actions, text="审核当前成果", command=lambda: self.action("analyze", "review"))
        self.generate_review.pack(side="left", padx=4)
        self.accept_button = ttk.Button(review_actions, text="人工采用本阶段结果", command=lambda: self.action("accept_review"))
        self.accept_button.pack(side="left", padx=4)
        self.revise_button = ttk.Button(review_actions, text="按意见开始返工", command=lambda: self.action("revise_from_review"))
        self.revise_button.pack(side="left", padx=4)
        self.review_text = text_area(review_body, 24)
        self.refresh(task)

    def page(self, parent):
        actions = ttk.Frame(parent, padding=8)
        actions.pack(side="bottom", fill="x")
        scrolling = ttk.Frame(parent)
        scrolling.pack(fill="both", expand=True)
        canvas = tk.Canvas(scrolling, highlightthickness=0)
        scrollbar = ttk.Scrollbar(scrolling, command=canvas.yview)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.configure(yscrollcommand=scrollbar.set)
        body = ttk.Frame(canvas, padding=8)
        body_id = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(body_id, width=event.width))
        return actions, body

    def refresh(self, task):
        brief, review = task.get("brief") or {}, task.get("review") or {}
        binding = (task.get("revision"), brief, review)
        if binding != self.last_binding:
            self.last_binding = deepcopy(binding)
            self.generation += 1
        kind = analysis_message_kind(task) or ("brief" if task.get("brief_required") else "review" if review else "brief")
        set_changed(self.task_status,
                    f"任务状态：{STATES.get(task.get('state'), '未知')}\n{assessment_guidance(task, kind)}")
        report = brief.get("report") or {}
        fields = (brief.get("adopted_goal", report.get("goal", "")),
                  brief.get("adopted_acceptance", report.get("acceptance", [])))
        if fields != self.last_brief_report:
            self.last_brief_report = deepcopy(fields)
            for editor, value in ((self.goal_text, fields[0]), (self.acceptance_text, "\n".join(fields[1]))):
                editor.configure(state="normal")
                editor.delete("1.0", "end")
                editor.insert("1.0", value)
        brief_state = "暂无简报。" if not brief else (
            "简报已过期，请重新整理。" if brief.get("status") != "current" else
            "简报已采用。" if brief.get("decision") == "adopted" else "简报待采用；采用前待重新核验。")
        brief_lines = [brief_state]
        for key, label in (("decisions", "已记录决策"), ("completed", "已完成事项"), ("unknowns", "未知与缺口"),
                           ("approaches", "候选方法"), ("next_step", "建议下一步"), ("evidence", "证据引用")):
            brief_lines.append(f"\n{label}\n{assessment_content(report.get(key))}")
        set_text(self.brief_text, "\n".join(brief_lines))
        report = review.get("report") or {}
        verdict = {"ready_for_user": "可交人工检查", "changes_requested": "建议修改", "inconclusive": "结论不足"}.get(report.get("verdict"), "暂无意见")
        review_lines = [f"AI 审查意见：{verdict}", "自动化验收：未独立验证；已有测试报告仅作参考。",
                        "人工已认可本阶段结果。" if review.get("human_acceptance") == "accepted" else "人工未认可。",
                        "是否发布仍需按任务要求另行验收。",
                        "审核记录已过期，请重新审核。" if review and review.get("status") != "current" else
                        "采用或返工前待重新核验。"]
        for key, label in (("summary", "摘要"), ("findings", "发现与建议"), ("checks", "AI 检查观察"),
                           ("unknowns", "未知与缺口"), ("test_evidence", "已有测试资料（参考）")):
            review_lines.append(f"\n{label}\n{assessment_content(report.get(key))}")
        set_text(self.review_text, "\n".join(review_lines))
        available = (self.app.ready and not self.app.busy and not self.app.closing and self.app.recovery_info is None
                     and self.task_id in self.app.visible_ids and not task.get("archived")
                     and task.get("state") in ("queued", "idle", "paused") and task.get("run_started") is None
                     and not any(task.get(key) for key in ("inflight", "intent", "pending", "receiver_id", "analysis_thread_id", "assessment")))
        generate = available and not budget_status(task)["reached"]
        brief_pending = available and brief.get("status") == "current" and brief.get("decision") == "pending" and not (task.get("error") and kind == "brief")
        review_pending = available and review.get("status") == "current" and review.get("decision") == "pending" and not (task.get("error") and kind == "review")
        for widget, allowed in ((self.generate_brief, generate), (self.generate_review, generate and task.get("work_turns", 0) > 0),
                                (self.adopt_button, brief_pending), (self.accept_button, review_pending and report.get("verdict") == "ready_for_user"),
                                (self.revise_button, review_pending and generate and bool(task.get("thread_id")))):
            configure_changed(widget, state="normal" if allowed else "disabled")
        for editor in (self.goal_text, self.acceptance_text):
            configure_changed(editor, state="normal" if brief_pending else "disabled")

    def action(self, method, *values):
        button = (self.generate_brief if values == ("brief",) else self.generate_review) if method == "analyze" else {
            "adopt_brief": self.adopt_button, "accept_review": self.accept_button, "revise_from_review": self.revise_button}[method]
        if button.instate(["disabled"]):
            return
        if self.app.submit_assessment(self, self.generation, method, self.task_id, *values):
            self.info.set("正在核验并开始返工，会消耗模型用量。" if method == "revise_from_review" else
                          "正在处理此任务；不会自动开始项目执行。")

    def adopt(self):
        if self.adopt_button.instate(["disabled"]):
            return
        goal = self.goal_text.get("1.0", "end-1c").strip()
        acceptance = [line.strip() for line in self.acceptance_text.get("1.0", "end-1c").splitlines() if line.strip()]
        if not goal or not acceptance:
            self.info.set("请填写目标和至少一项验收标准。")
            return
        self.action("adopt_brief", goal, acceptance)

    def deliver(self, generation, method, error=None):
        if generation == self.generation:
            self.info.set(f"需要处理：{error}" if error else
                          "已提交返工请求；以任务状态和实际结果为准。" if method == "revise_from_review" else
                          "操作已记录；以任务状态和核验后的报告为准，尚未自动执行。")


class PreferencesDialog(tk.Toplevel):
    THEMES = {"经典蓝": "blue", "淡绿青": "mint"}
    FONT_SIZES = {"标准": "standard", "大字": "large"}
    DENSITIES = {"舒适": "comfortable", "紧凑": "compact"}

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.original = dict(app.preferences)
        self.title("外观与偏好")
        self.transient(app.root)
        self.resizable(False, False)
        body = ttk.Frame(self, padding=18, style="Surface.TFrame")
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="外观与偏好", style="Surface.Title.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        ttk.Label(body, text="更改会立即预览；保存后下次启动继续使用。",
                  style="Surface.Muted.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 14))
        self.theme = tk.StringVar(value=self._label(self.THEMES, self.original["theme"]))
        self.font_size = tk.StringVar(value=self._label(self.FONT_SIZES, self.original["font_size"]))
        self.density = tk.StringVar(value=self._label(self.DENSITIES, self.original["density"]))
        ttk.Label(body, text="配色", style="Surface.TLabel").grid(row=2, column=0, sticky="nw", pady=5)
        theme_choices = ttk.Frame(body, style="Surface.TFrame")
        theme_choices.grid(row=2, column=1, sticky="ew", padx=(18, 0), pady=5)
        for column, (label, key) in enumerate(self.THEMES.items()):
            card = ttk.Frame(theme_choices, style="Surface.TFrame")
            card.grid(row=0, column=column, sticky="w", padx=(0, 14))
            ttk.Radiobutton(card, text=label, value=label, variable=self.theme,
                            command=self.preview).pack(anchor="w")
            sample = tk.Frame(card, width=74, height=24, background=THEMES[key]["bg"],
                              highlightthickness=1, highlightbackground=THEMES[key]["border"])
            sample.pack(anchor="w", padx=(20, 0), pady=(3, 0))
            sample.pack_propagate(False)
            tk.Frame(sample, width=22, background=THEMES[key]["select"]).pack(side="left", fill="y")
            tk.Frame(sample, height=5, background=THEMES[key]["accent"]).pack(
                side="right", fill="x", expand=True, padx=8, pady=9)
        for row, (label, variable, values) in enumerate((
                ("字号", self.font_size, self.FONT_SIZES),
                ("间距", self.density, self.DENSITIES)), start=3):
            ttk.Label(body, text=label, style="Surface.TLabel").grid(row=row, column=0, sticky="w", pady=5)
            choice = ttk.Combobox(body, textvariable=variable, values=tuple(values), state="readonly", width=18)
            choice.grid(row=row, column=1, sticky="ew", padx=(18, 0), pady=5)
            choice.bind("<<ComboboxSelected>>", lambda _event: self.preview())
        self.info = tk.StringVar()
        ttk.Label(body, textvariable=self.info, style="Surface.Muted.TLabel", wraplength=390,
                  justify="left").grid(row=5, column=0, columnspan=2, sticky="ew", pady=(10, 4))
        actions = ttk.Frame(body, style="Surface.TFrame")
        actions.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(actions, text="保存", command=self.save, style="Primary.TButton").pack(side="right")
        ttk.Button(actions, text="取消", command=self.cancel, style="Secondary.TButton").pack(side="right", padx=8)
        ttk.Button(actions, text="恢复默认", command=self.restore_defaults,
                   style="Secondary.TButton").pack(side="left")
        self.protocol("WM_DELETE_WINDOW", self.cancel)

    @staticmethod
    def _label(options, value):
        return next(label for label, item in options.items() if item == value)

    def values(self):
        return {"theme": self.THEMES[self.theme.get()],
                "font_size": self.FONT_SIZES[self.font_size.get()],
                "density": self.DENSITIES[self.density.get()]}

    def preview(self):
        self.app.apply_preferences(self.values())
        self.info.set("当前为即时预览，尚未保存。")

    def restore_defaults(self):
        self.theme.set(self._label(self.THEMES, DEFAULTS["theme"]))
        self.font_size.set(self._label(self.FONT_SIZES, DEFAULTS["font_size"]))
        self.density.set(self._label(self.DENSITIES, DEFAULTS["density"]))
        self.preview()
        self.info.set("已恢复默认预览；点击保存后才会保留。")

    def save(self):
        try:
            saved = save_preferences(self.values())
        except (OSError, ValueError) as error:
            self.info.set(f"保存失败：{error}。当前预览未保存。")
            return
        self.app.apply_preferences(saved)
        self.original = dict(saved)
        self.app.status.set("外观与偏好已保存。")
        self.destroy()

    def cancel(self):
        self.app.apply_preferences(self.original)
        self.destroy()


class RelayApp:
    def __init__(self, root, state_dir=None, factory=manager_factory):
        self.root = root
        self.tasks = {}
        self.selected_id = None
        self.rendered_task_id = None
        self.message_drafts = {}
        self.visible_ids = set()
        self.recovery_info = None
        self.ready = False
        self.busy = False
        self.closing = False
        self.closed = False
        self.pending_requests = []
        self.question_values = {}
        self.import_dialog = None
        self.import_request = None
        self.assessment_dialogs = {}
        self.assessment_request = None
        self.phone_dialog = None
        self.phone_request = None
        self.diagnostics_window = None
        self.preferences_dialog = None
        self.chat_cache = {}
        self.chat_request = None
        self.chat_wanted = None
        self.chat_native_task = None
        self.chat_generation = 0
        self._last_clock_controls = time.monotonic()
        self.preferences = load_preferences()
        self.style = apply_theme(self.root, self.preferences)
        self.root.title("Context Relay · 本机任务管理器")
        width = min(1180, max(1, self.root.winfo_screenwidth() - 80))
        height = min(850, max(1, self.root.winfo_screenheight() - 120))
        left = max(20, (self.root.winfo_screenwidth() - width) // 2)
        self.root.geometry(f"{width}x{height}+{left}+20")
        self.root.minsize(min(940, width), min(720, height))
        self.root.protocol("WM_DELETE_WINDOW", self.request_close)
        self.status = tk.StringVar(value="正在读取本机任务…")
        self.root.report_callback_exception = self._callback_error
        self.details = tk.StringVar(value="选择左侧任务查看详情。")
        self.task_heading = tk.StringVar(value="请选择任务")
        self.task_alert = tk.StringVar()
        self.budget_details = tk.StringVar()
        self.details_expanded = False
        self.search = tk.StringVar()
        self.state_filter = tk.StringVar(value=FILTERS[0])
        self._build()
        self.search.trace_add("write", self._search_changed)
        self.state_filter.trace_add("write", lambda *args: self._apply_filters())
        self.worker = CommandWorker(factory, state_dir)
        self.worker.start()
        self._pump_after = self.root.after(40, self._pump)

    def _build(self):
        compact = self.preferences["density"] == "compact" or self.root.winfo_screenheight() < 900
        self._action_proxies = ttk.Frame(self.root)
        self.phone_button = ttk.Button(self._action_proxies, text="手机连接", command=self._open_phone)
        self.backup_button = ttk.Button(self._action_proxies, text="备份管理器", command=self._backup)
        self.backup_notice = ttk.Label(
            self._action_proxies,
            text="本地明文记录，含导入摘录；不含项目文件、登录信息或完整原生聊天。")
        self.diagnostics_button = ttk.Button(self._action_proxies, text="诊断信息", command=self._open_diagnostics)
        self.assessment_button = ttk.Button(self._action_proxies, text="简报 / 审核", command=self._open_assessment)
        self.recovery_banner = ttk.Label(self.root, wraplength=1140, justify="left", style="Alert.TLabel")

        panes = ttk.Panedwindow(self.root, orient="horizontal")
        self.panes = panes
        panes.pack(fill="both", expand=True)
        left = ttk.Frame(panes, width=268, padding=(16, 18, 12, 14), style="Sidebar.TFrame")
        right = ttk.Frame(panes, padding=(0, 0), style="Surface.TFrame")
        self.sidebar, self.chat_panel = left, right
        panes.add(left, weight=0)
        panes.add(right, weight=1)

        ttk.Label(left, text="我的任务", style="Title.TLabel").pack(anchor="w", pady=(0, 12))
        create_row = ttk.Frame(left, style="Sidebar.TFrame")
        create_row.pack(fill="x", pady=(0, 12))
        self.new_button = ttk.Button(create_row, text="新建", style="Primary.TButton",
                                     command=lambda: NewTaskDialog(self.root, self.submit))
        self.new_button.pack(side="left", fill="x", expand=True)
        self.import_button = ttk.Button(create_row, text="导入", command=self._open_import)
        self.import_button.pack(side="left", padx=(8, 0))
        find_row = ttk.Frame(left, style="Sidebar.TFrame")
        find_row.pack(fill="x", pady=(0, 8))
        self.search_entry = ttk.Entry(find_row, textvariable=self.search)
        self.search_entry.pack(side="left", fill="x", expand=True)
        self.search_placeholder = ttk.Label(find_row, text="搜索任务", style="Muted.TLabel", cursor="xterm")
        self.search_placeholder.place(in_=self.search_entry, x=9, rely=.5, anchor="w")
        self.search_placeholder.bind("<Button-1>", lambda _event: self.search_entry.focus_set())
        self.search_entry.bind("<FocusIn>", lambda _event: self._sync_search_placeholder())
        self.search_entry.bind("<FocusOut>", lambda _event: self._sync_search_placeholder())
        self.filter_choice = ttk.Combobox(find_row, width=9, textvariable=self.state_filter,
                                          values=FILTERS, state="readonly")
        self.filter_choice.pack(side="left", padx=(7, 0))
        self.attention_button = ttk.Button(left, command=self._show_attention)

        self.buttons = {}
        for label, method in (("操作记录", "get_task"), ("归档", "set_archived")):
            self.buttons[method] = ttk.Button(
                self._action_proxies, text=label, command=lambda action=method: self._action(action))
        self.buttons["update_settings"] = ttk.Button(
            self._action_proxies, text="任务设置", command=lambda: self._action("update_settings"))
        self.buttons["reopen_task"] = ttk.Button(
            self._action_proxies, text="重新打开", command=lambda: self._action("reopen_task"))
        self.settings_button = ttk.Menubutton(left, text="设置", style="Rounded.TMenubutton")
        self.settings_menu = tk.Menu(self.settings_button, tearoff=False)
        self.settings_button.configure(menu=self.settings_menu)
        self.settings_entries = {}
        self.settings_entry_menus = {}
        general_menu = tk.Menu(self.settings_menu, tearoff=False)
        general_menu.add_command(label="外观与偏好", command=self._open_preferences)
        self.settings_menu.add_cascade(label="通用", menu=general_menu)
        self.settings_entries["preferences"] = general_menu.index("end")
        self.settings_entry_menus["preferences"] = general_menu
        self.settings_menu.add_command(label="手机连接", command=self.phone_button.invoke)
        self.settings_entries["phone"] = self.settings_menu.index("end")
        self.settings_entry_menus["phone"] = self.settings_menu
        data_menu = tk.Menu(self.settings_menu, tearoff=False)
        for label, key, command in (("备份管理器", "backup", self.backup_button.invoke),
                                    ("诊断信息", "diagnostics", self.diagnostics_button.invoke)):
            data_menu.add_command(label=label, command=command)
            self.settings_entries[key] = data_menu.index("end")
            self.settings_entry_menus[key] = data_menu
        self.settings_menu.add_cascade(label="数据与诊断", menu=data_menu)
        self.settings_button.pack(side="bottom", fill="x", pady=(10, 0))
        self.task_tree = TaskCardList(left, self._choose_task)
        self.task_tree.pack(fill="both", expand=True)

        task_header = ttk.Frame(right, padding=(24, 18, 22, 12), style="Surface.TFrame")
        self.task_header = task_header
        task_header.pack(fill="x")
        title_area = ttk.Frame(task_header, style="Surface.TFrame")
        title_area.pack(side="left", fill="x", expand=True)
        ttk.Label(title_area, textvariable=self.task_heading, style="Surface.Title.TLabel",
                  wraplength=650, justify="left").pack(anchor="w")
        self.task_subtitle = tk.StringVar(value="选择左侧任务查看对话")
        ttk.Label(title_area, textvariable=self.task_subtitle, style="Surface.Muted.TLabel").pack(anchor="w", pady=(2, 0))
        self.more_button = ttk.Menubutton(task_header, width=3, text="⋯", style="Rounded.TMenubutton")
        self.more_button.pack(side="right", padx=(8, 0))
        self.details_button = ttk.Button(task_header, width=4, text="详情", command=self._toggle_details)
        self.details_button.pack(side="right")
        self.more_menu = tk.Menu(self.more_button, tearoff=False)
        self.more_button.configure(menu=self.more_menu)
        self.more_entries = {}

        self.task_alert_label = ttk.Label(right, textvariable=self.task_alert, wraplength=840,
                                          justify="left", style="Alert.TLabel")
        self.details_panel = ttk.Frame(right, padding=(24, 4, 24, 10), style="Surface.TFrame")
        ttk.Label(self.details_panel, textvariable=self.details, wraplength=820, justify="left",
                  style="Surface.TLabel").pack(fill="x", pady=(0, 6))
        self.budget_label = ttk.Label(self.details_panel, textvariable=self.budget_details, wraplength=820,
                                      justify="left", style="Surface.Muted.TLabel")
        self.budget_label.pack(fill="x", pady=(0, 6))
        goal = ttk.LabelFrame(self.details_panel, text="目标", style="Surface.TLabelframe")
        goal.pack(fill="x")
        self.goal_text = text_area(goal, 2 if compact else 3)

        workflow = ttk.Frame(right, padding=(22, 0, 22, 14), style="Surface.TFrame")
        self.workflow = workflow
        workflow.pack(side="bottom", fill="x")
        self.pending_frame = ttk.LabelFrame(workflow, text="需要审批 / 回答", style="Attention.TLabelframe")
        self.pending_choice = ttk.Combobox(self.pending_frame, state="disabled")
        self.pending_choice.pack(fill="x", padx=6, pady=(6, 0))
        self.pending_choice.bind("<<ComboboxSelected>>", lambda event: self._render_request())
        self.request_text = text_area(self.pending_frame, 3 if compact else 4)
        self.answer_frame = ttk.Frame(self.pending_frame, padding=(6, 0, 6, 6), style="Surface.TFrame")
        self.answer_frame.pack(fill="x")
        self.approval_allow = ttk.Button(
            self.answer_frame, text="仅此一次允许", command=lambda: self._answer_approval("accept"))
        self.approval_decline = ttk.Button(
            self.answer_frame, text="拒绝", command=lambda: self._answer_approval("decline"))
        self.controls_frame = ttk.Frame(workflow, style="Surface.TFrame")
        self.controls_frame.pack(fill="x")
        colors = self.root._context_relay_palette
        self.composer_frame = ttk.Frame(self.controls_frame, style="RoundedEntry.TFrame", padding=(12, 8))
        self.composer_frame.pack(fill="x", pady=(8, 0))
        self.primary_actions = ttk.Frame(self.composer_frame, style="Surface.TFrame")
        self.primary_actions.pack(side="bottom", fill="x", pady=(5, 0))
        ttk.Label(self.primary_actions, text="原话发送 · 可换行", style="Surface.Muted.TLabel").pack(side="left")
        for label, method in (("核对恢复", "reconcile"), ("暂停", "pause"), ("发送 / 继续", "start")):
            button = ttk.Button(self.primary_actions, text=label,
                                style="Primary.TButton" if method == "start" else "Secondary.TButton",
                                command=lambda action=method: self._action(action))
            button.pack(side="right", padx=(6, 0))
            self.buttons[method] = button
        self.message_text = tk.Text(self.composer_frame, height=2 if compact else 3, wrap="word", relief="flat",
                                    background=colors["surface"], foreground=colors["ink"],
                                    insertbackground=colors["ink"], selectbackground=colors["accent"],
                                    borderwidth=0, highlightthickness=0, padx=3, pady=4, font="TkTextFont")
        self.message_text.pack(fill="x")
        self.message_placeholder = ttk.Label(self.composer_frame, text="输入消息…",
                                             style="Surface.Muted.TLabel", cursor="xterm")
        self.message_placeholder.place(in_=self.message_text, x=6, y=7, anchor="nw")
        self.message_placeholder.bind("<Button-1>", lambda _event: self.message_text.focus_set())
        self.message_text.bind("<FocusIn>", self._message_focus_in)
        self.message_text.bind("<FocusOut>", self._message_focus_out)
        self.message_text.bind("<KeyRelease>", lambda _event: self._sync_message_placeholder())

        self.chat_nav = ttk.Frame(right, padding=(24, 2, 24, 4), style="Surface.TFrame")
        self.chat_position = tk.StringVar()
        ttk.Label(self.chat_nav, textvariable=self.chat_position, style="Surface.Muted.TLabel").pack(side="left")
        self.chat_older = ttk.Button(self.chat_nav, text="较早", command=lambda: self._navigate_chat("older"))
        self.chat_latest = ttk.Button(self.chat_nav, text="最新", command=lambda: self._navigate_chat("latest"))
        self.chat_latest.pack(side="right")
        self.chat_older.pack(side="right", padx=(0, 6))
        self.latest_frame = ConversationView(right)
        self.latest_frame.configure_tags(colors)
        self.latest_frame.pack(fill="both", expand=True)
        self.latest_text = self.latest_frame.text

        for label, method in (("预备快照", "prepare_snapshot"), ("交接", "handoff"),
                              ("标记完成", "finish"), ("导出", "export_task")):
            self.buttons[method] = ttk.Button(self._action_proxies, text=label,
                                               command=lambda action=method: self._action(action))
        for label, key, command in (
                ("任务设置", "update_settings", self.buttons["update_settings"].invoke),
                ("刷新原始对话", "open_chat", self.open_chat),
                ("操作记录", "get_task", self.buttons["get_task"].invoke),
                ("核对恢复", "reconcile", self.buttons["reconcile"].invoke),
                ("简报 / 审核", "assessment", self.assessment_button.invoke),
                ("预备快照", "prepare_snapshot", self.buttons["prepare_snapshot"].invoke),
                ("交接", "handoff", self.buttons["handoff"].invoke),
                ("标记完成", "finish", self.buttons["finish"].invoke),
                ("重新打开", "reopen_task", self.buttons["reopen_task"].invoke),
                ("归档", "set_archived", self.buttons["set_archived"].invoke),
                ("导出", "export_task", self.buttons["export_task"].invoke)):
            if key in ("update_settings", "prepare_snapshot", "finish"):
                self.more_menu.add_separator()
            self.more_menu.add_command(label=label, command=command)
            self.more_entries[key] = self.more_menu.index("end")
        ttk.Label(self.root, textvariable=self.status, wraplength=1140, justify="left",
                  style="Muted.TLabel").pack(fill="x", padx=18, pady=(5, 8))
        self._controls()

    def _search_changed(self, *_args):
        self._sync_search_placeholder()
        self._apply_filters()

    def _sync_search_placeholder(self):
        if not self.search.get() and self.root.focus_get() is not self.search_entry:
            self.search_placeholder.place(in_=self.search_entry, x=9, rely=.5, anchor="w")
        else:
            self.search_placeholder.place_forget()

    def _sync_message_placeholder(self):
        if (not self.message_text.get("1.0", "end-1c")
                and self.root.focus_get() is not self.message_text):
            self.message_placeholder.place(in_=self.message_text, x=6, y=7, anchor="nw")
        else:
            self.message_placeholder.place_forget()

    def _message_focus_in(self, _event):
        self.composer_frame.state(["focus"])
        self._sync_message_placeholder()

    def _message_focus_out(self, _event):
        self.composer_frame.state(["!focus"])
        self.root.after_idle(self._sync_message_placeholder)

    def _toggle_details(self):
        self.details_expanded = not self.details_expanded
        if self.details_expanded:
            self.details_panel.pack(fill="x", before=self.latest_frame)
            self.details_button.configure(text="收起")
        else:
            self.details_panel.pack_forget()
            self.details_button.configure(text="详情")

    def submit(self, method, *args, _before_enqueue=None, **kwargs):
        if not self.ready or self.busy or self.closing:
            return False
        if self.recovery_info is not None and method not in ("get_task", "export_task", "diagnostics", "export_diagnostics"):
            self.status.set("恢复库为永久只读检视，不能继续任务或修改记录。")
            return False
        if method not in ("create_task", "backup_state", "diagnostics", "export_diagnostics", "list_import_threads", "preview_import", "import_thread",
                          "remote_enable", "remote_disable", "remote_status", "remote_pair", "remote_revoke", "remote_network") and args and args[0] not in self.visible_ids:
            return False
        if _before_enqueue is not None:
            _before_enqueue()
        self.busy = True
        self.status.set("正在处理，请稍候…")
        self._controls()
        self.worker.commands.put((method, args, kwargs))
        return True

    def _open_phone(self):
        if self.phone_button.instate(["disabled"]):
            return
        if self.phone_dialog is not None and self.phone_dialog.winfo_exists():
            self.phone_dialog.lift()
            return
        from .phone import PhoneDialog
        self.phone_dialog = PhoneDialog(self)

    def _open_preferences(self):
        if self.preferences_dialog is not None and self.preferences_dialog.winfo_exists():
            self.preferences_dialog.lift()
            return
        self.preferences_dialog = PreferencesDialog(self)

    def apply_preferences(self, values):
        self.preferences = dict(values)
        self.style = apply_theme(self.root, self.preferences)
        if hasattr(self, "latest_frame"):
            self.latest_frame.configure_tags(self.root._context_relay_palette)
        self.root.update_idletasks()

    def submit_remote(self, dialog, method, *args):
        gate = (lambda: self.worker.phone.gate_control(method, *args)
                if method in ("remote_disable", "remote_revoke") else None)
        try:
            if self.submit(method, *args, _before_enqueue=gate):
                self.phone_request = (dialog, method)
                return True
        except Exception as error:
            if dialog.winfo_exists():
                dialog.deliver(error=str(error))
            self.status.set(f"需要处理：{error}")
        return False

    def _phone_result(self, method, result=None, error=None):
        request = self.phone_request
        if request is not None and request[1] == method:
            self.phone_request = None
            dialog = request[0]
            if not self.closing and dialog.winfo_exists():
                dialog.deliver(result, error)

    def _open_import(self):
        if self.import_button.instate(["disabled"]):
            return
        if self.import_dialog is not None and self.import_dialog.winfo_exists():
            self.import_dialog.lift()
            return
        self.import_dialog = ImportDialog(self)
        self.import_dialog.load_page()

    def submit_import(self, dialog, token, method, *args, **kwargs):
        if self.submit(method, *args, **kwargs):
            self.import_request = (dialog, token, method)
            return True
        return False

    def _import_result(self, method, result=None, error=None):
        request = self.import_request
        if request is not None and request[2] == method:
            self.import_request = None
            dialog, token, _ = request
            if not self.closing and dialog.winfo_exists():
                dialog.deliver(method, token, result, error)

    def _open_assessment(self):
        if self.assessment_button.instate(["disabled"]) or self.selected_id not in self.visible_ids:
            return
        dialog = self.assessment_dialogs.get(self.selected_id)
        if dialog is not None and dialog.winfo_exists():
            dialog.lift()
        else:
            self.assessment_dialogs[self.selected_id] = AssessmentDialog(self, self.tasks[self.selected_id])

    def submit_assessment(self, dialog, generation, method, *args):
        if self.submit(method, *args):
            self.assessment_request = (dialog, generation, method)
            return True
        return False

    def _assessment_result(self, method, error=None):
        request = self.assessment_request
        if request is not None and request[2] == method:
            self.assessment_request = None
            dialog, generation, _ = request
            if not self.closing and dialog.winfo_exists():
                dialog.deliver(generation, method, error)

    def _backup(self):
        if self.backup_button.instate(["disabled"]):
            return
        destination = filedialog.asksaveasfilename(parent=self.root, title="保存新的管理器备份（本地明文）",
                                                  defaultextension=".zip", initialfile="context-relay-backup.zip",
                                                  filetypes=(("ZIP", "*.zip"),), confirmoverwrite=False)
        if destination:
            self.submit("backup_state", destination)

    def _open_diagnostics(self):
        if self.diagnostics_button.instate(["disabled"]):
            return
        if self.diagnostics_window is not None and self.diagnostics_window.winfo_exists():
            self.diagnostics_window.lift()
            return
        self.diagnostics_window = DiagnosticsWindow(self)
        if not self.submit("diagnostics"):
            self.diagnostics_window.destroy()

    def open_chat(self):
        self._request_chat(None, force=True)

    def _choose_task(self, task_id):
        self.selected_id = task_id if task_id in self.visible_ids else None
        self._render_task()

    def _visible_messages(self, task):
        visible = []
        for message in task.get("messages") or []:
            if (isinstance(message, dict) and message.get("role") in ("user", "assistant")
                    and message.get("status") == "completed" and message.get("purpose") == "work"
                    and isinstance(message.get("text"), str) and message["text"]):
                visible.append({"role": message["role"], "text": message["text"]})
        if not visible:
            reply = task_reply(task)
            if reply and reply != "尚无回复。":
                visible.append({"role": "assistant", "text": reply})
        return visible

    def _chat_marker(self, task):
        return tuple(task.get(key) for key in (
            "state", "turn_id", "last_message", "updated_at", "work_turns", "generation"))

    def _task_preview(self, task):
        messages = self._visible_messages(task)
        value = messages[-1]["text"] if messages else "尚无已完成回复"
        value = " ".join(value.split())
        return value[:24] + ("…" if len(value) > 24 else "")

    def _task_card(self, task):
        state = "已归档" if task.get("archived") else STATES.get(task.get("state"), task.get("state", "未知"))
        stamp = task.get("updated_at") or task.get("created_at") or ""
        stamp = str(stamp).replace("T", " ")[:16]
        footer = f"{state}  ·  {stamp}" if stamp else state
        return {"id": task["id"], "title": task["title"],
                "preview": self._task_preview(task), "meta": footer}

    def _request_chat(self, cursor=None, force=False):
        task = self.tasks.get(self.selected_id)
        if (not task or self.closing or self.recovery_info is not None
                or task.get("connection_mode") != "direct" and not force):
            return False
        if force or task.get("connection_mode") == "direct":
            self.chat_native_task = task["id"]
        self.chat_wanted = (task["id"], cursor, self.chat_generation)
        return self._maybe_request_chat()

    def _maybe_request_chat(self):
        wanted = self.chat_wanted
        if wanted is None or self.chat_request is not None or self.busy or not self.ready or self.closing:
            return False
        task_id, cursor, generation = wanted
        if task_id != self.selected_id or task_id not in self.visible_ids:
            return False
        def mark():
            self.chat_request = (task_id, cursor, generation)
            self.chat_wanted = None
        if self.submit("read_chat", task_id, cursor, _before_enqueue=mark):
            self._render_conversation(self.tasks.get(task_id), loading=True)
            return True
        return False

    def _navigate_chat(self, direction):
        cached = self.chat_cache.get(self.selected_id) or {}
        cursor = cached.get("older_cursor") if direction == "older" else None
        if direction == "older" and cursor is None:
            return
        self._request_chat(cursor, force=True)

    def _render_conversation(self, task, loading=False):
        if not task:
            self.chat_nav.pack_forget()
            self.latest_frame.render([], ())
            return
        if task.get("connection_mode") == "direct" or self.chat_native_task == task["id"]:
            cached = self.chat_cache.get(task["id"])
            messages = []
            notices = []
            if isinstance(cached, dict) and isinstance(cached.get("entries"), list):
                messages = [{"role": item["role"], "text": item["text"]} for item in cached["entries"]
                            if isinstance(item, dict) and item.get("role") in ("user", "assistant")
                            and item.get("turn_status") == "completed"
                            and isinstance(item.get("text"), str)]
                notices.append((cached.get("notice", ""), "notice"))
                if cached.get("newer_available"):
                    notices.append(("任务有新状态；当前仍保留较早页。点击“最新”读取新内容。", "notice"))
                if cached.get("error"):
                    notices.append((f"刷新失败：{cached['error']}。已保留上次读取内容。", "error"))
                self.chat_position.set(f"原始对话 · 第 {cached.get('page', '?')} / {cached.get('pages', '?')} 页")
                self.chat_older.configure(state="normal" if cached.get("older_cursor") else "disabled")
                self.chat_latest.configure(state="normal")
                self.chat_nav.pack(fill="x", before=self.latest_frame)
            else:
                self.chat_position.set("原始对话")
                self.chat_nav.pack(fill="x", before=self.latest_frame)
                self.chat_older.configure(state="disabled")
                self.chat_latest.configure(state="disabled" if loading else "normal")
                if isinstance(cached, dict) and cached.get("error"):
                    notices.append((f"原始对话读取失败：{cached['error']}。未用任务摘要代替。", "error"))
            if loading:
                notices.append(("正在只读读取原始对话…", "notice"))
            elif not cached:
                notices.append(("尚未读到原始对话；不会用任务摘要代替。", "notice"))
            self.latest_frame.render(messages, notices, follow=not loading)
            return
        self.chat_nav.pack_forget()
        messages = self._visible_messages(task)
        notices = []
        if task.get("messages_truncated") or any(
                message.get("truncated") for message in task.get("messages") or [] if isinstance(message, dict)):
            notices.append(("较早的本机对话已截短；这里仅显示当前保留的已完成工作消息。", "notice"))
        if any(message.get("historical") for message in task.get("messages") or [] if isinstance(message, dict)):
            notices.append(("含导入的历史消息；它们不代表本机已重新执行。", "notice"))
        self.latest_frame.render(messages, notices, follow=True)

    def _action(self, method):
        if self.selected_id not in self.visible_ids or self.buttons[method].instate(["disabled"]):
            return
        if method == "start":
            raw = self.message_text.get("1.0", "end-1c")
            self.submit(method, self.selected_id, raw if raw.strip() else None)
        elif method == "update_settings":
            TaskSettingsDialog(self.root, self.tasks[self.selected_id], self.submit)
        elif method == "set_archived":
            self.submit(method, self.selected_id, not self.tasks[self.selected_id].get("archived", False))
        elif method == "export_task":
            task_id = self.selected_id
            destination = filedialog.asksaveasfilename(parent=self.root, title="导出任务记录", defaultextension=".md",
                                                      initialfile="context-relay-task.md",
                                                      filetypes=(("Markdown", "*.md"), ("JSON", "*.json")))
            if destination:
                self.submit(method, task_id, destination)
        elif method == "reopen_task":
            task_id = self.selected_id
            if messagebox.askyesno("重新打开任务", "仅将这个已完成任务改为待继续，不会自动启动或发送消息。\n"
                                  "继续时仍使用原任务的权限和预算。确定重新打开？", parent=self.root):
                self.submit(method, task_id)
        elif method == "finish":
            if messagebox.askyesno("确认完成", "确认这个项目已达到完成标准？单次回复结束不等于项目完成。", parent=self.root):
                self.submit(method, self.selected_id)
        else:
            self.submit(method, self.selected_id)

    def _select_task(self, event=None):
        selection = self.task_tree.selection()
        self.selected_id = selection[0] if selection and selection[0] in self.visible_ids else None
        self._render_task()

    def _render_tasks(self, tasks):
        previous = self.tasks.get(self.selected_id)
        self.tasks = {task["id"]: task for task in tasks}
        self._apply_filters()
        current = self.tasks.get(self.selected_id)
        if (previous and current and previous.get("id") == current.get("id")
                and current.get("connection_mode") == "direct"
                and self._chat_marker(previous) != self._chat_marker(current)
                and current["id"] in self.chat_cache):
            cached = self.chat_cache[current["id"]]
            if cached.get("newer_cursor") or cached.get("page", 1) < cached.get("pages", 1):
                cached["newer_available"] = True
                self._render_conversation(current)
            elif self.recovery_info is None:
                self.chat_wanted = (current["id"], None, self.chat_generation)
                self.root.after_idle(self._maybe_request_chat)

    def _show_attention(self):
        if self.closing or not self.attention_count:
            return
        self.search.set("")
        self.state_filter.set("待处理")

    def _apply_filters(self):
        query, state_filter = self.search.get().strip().casefold(), self.state_filter.get()
        visible = []
        for task in self.tasks.values():
            if bool(task.get("archived", False)) != (state_filter == "已归档"):
                continue
            state = task.get("state")
            if state_filter == "运行中" and state not in ACTIVE:
                continue
            if state_filter == "待处理" and not needs_attention(task):
                continue
            if state_filter == "等待继续" and state not in ("queued", "idle", "paused"):
                continue
            if state_filter == "已完成" and state != "completed":
                continue
            if query and not any(query in task.get(key, "").casefold() for key in ("title", "goal", "cwd")):
                continue
            visible.append(task)
        self.visible_ids = {task["id"] for task in visible}
        if self.selected_id not in self.visible_ids:
            self.selected_id = visible[0]["id"] if visible else None
        self.task_tree.render([self._task_card(task) for task in visible], self.selected_id)
        self._render_task()

    def _render_task(self):
        task = self.tasks.get(self.selected_id)
        switched = self.rendered_task_id != self.selected_id
        if switched:
            if self.rendered_task_id is not None:
                self.message_drafts[self.rendered_task_id] = self.message_text.get("1.0", "end-1c")
            self.message_text.delete("1.0", "end")
            self.message_text.insert("1.0", self.message_drafts.get(self.selected_id, ""))
            self._sync_message_placeholder()
            if self.details_expanded:
                self.details_expanded = False
                self.details_panel.pack_forget()
                self.details_button.configure(text="详情")
            self.chat_generation += 1
            self.chat_wanted = None
            self.chat_native_task = self.selected_id if task and task.get("connection_mode") == "direct" else None
        self.rendered_task_id = self.selected_id
        if not task:
            self.task_heading.set("请选择任务")
            self.task_alert.set("")
            self.task_alert_label.pack_forget()
            self.details.set("选择左侧任务查看详情。")
            self.task_subtitle.set("选择左侧任务查看对话")
            set_text(self.goal_text, "")
            self._render_conversation(None)
            self._render_pending([])
            self._controls()
            return
        pressure = task.get("context_estimate")
        pressure_text = f"{pressure:.0%}" if isinstance(pressure, (int, float)) else "未知"
        mode = "只读" if task.get("mode") == "read-only" else "可修改工作区"
        usage = task.get("usage", 0)
        usage_text = str(usage) if isinstance(usage, (int, float)) else "未知"
        if task.get("max_tokens"):
            usage_text += f" / {task['max_tokens']}"
        draft = task.get("draft")
        source = task.get("source_snapshot")
        state_text = STATES.get(task.get("state"), task.get("state", "未知"))
        self.task_heading.set(task["title"])
        self.task_subtitle.set(f"{state_text} · {'只读' if task.get('mode') == 'read-only' else '可修改工作区'}")
        alerts = []
        if task.get("state") == "needs_reconcile":
            alerts.append("需要核对恢复后才能继续。")
        if task.get("error"):
            alerts.append(f"需要处理：{task_error(task)}")
        brief = task.get("brief") or {}
        if (task.get("brief_required") and brief.get("status") == "current"
                and brief.get("decision") == "pending"):
            alerts.append("简报待采用；请到“更多 → 简报 / 审核”核对并采用后继续。")
        task_budget = budget_status(dict(task, run_started=None) if self.recovery_info is not None else task)
        if task_budget["reached"]:
            alerts.append("已达到预算；请在“更多 → 任务设置”调整，或先核对现有结果。")
        if task.get("telemetry_model_valid", True) is False:
            alerts.append("模型用量口径无效，自动交接不可用。")
        if self.recovery_info is not None:
            alerts.append("恢复库为只读检视，不能执行或修改任务。")
        self.task_alert.set("\n".join(dict.fromkeys(alerts)))
        if alerts:
            self.task_alert_label.pack(fill="x", pady=(0, 5), before=self.latest_frame)
        else:
            self.task_alert_label.pack_forget()
        self.details.set(f"{task['title']} · {STATES.get(task.get('state'), task.get('state', '未知'))}\n"
                         f"{task['cwd']}\n权限：{mode}  ·  交接代次：{task.get('generation', 0)}  ·  "
                         f"上下文估算：{pressure_text}  ·  压缩：{task.get('compactions', 0)} 次  ·  累计 Token：{usage_text}"
                         + (f"\n预备快照：{draft.get('created_at', '时间未知')} · 仅预备，交接前须重验" if draft else "")
                         + (f"\n历史摘录来源：{source.get('thread_id', '未知')} · 可导出查看，不继承原聊天授权" if source else "")
                         + ("\n首次启动先只读整理简报，会消耗模型用量；在“简报 / 审核”采用后再执行。" if task.get("brief_required") else "")
                         + (f"\n需要处理：{task_error(task)}" if task.get("error") else ""))
        set_text(self.goal_text, task.get("goal"))
        self._render_conversation(task, loading=(task.get("connection_mode") == "direct"
                                                   and task["id"] not in self.chat_cache))
        if switched and task.get("connection_mode") == "direct" and self.recovery_info is None:
            self.chat_wanted = (task["id"], None, self.chat_generation)
            self.root.after_idle(self._maybe_request_chat)
        if switched or task.get("pending", []) != self.pending_requests:
            self._render_pending(task.get("pending", []))
        elif self._current_request() and self._current_request().get("method") in APPROVALS:
            self._render_request()
        self._controls()

    def _render_pending(self, pending):
        current = self._current_request()
        previous_id = current.get("id") if current else None
        self.pending_requests = pending
        self.pending_choice.configure(values=[f"{item.get('method')} · {item.get('id')}" for item in pending],
                                      state="readonly" if pending else "disabled")
        if pending:
            self.pending_frame.pack(fill="x", pady=(0, 6), before=self.controls_frame)
            index = next((i for i, item in enumerate(pending) if item.get("id") == previous_id), 0)
            self.pending_choice.current(index)
        else:
            self.pending_frame.pack_forget()
            self.pending_choice.set("")
        self._render_request()

    def _current_request(self):
        index = self.pending_choice.current()
        return self.pending_requests[index] if 0 <= index < len(self.pending_requests) else None

    def _request_item(self, request):
        task = self.tasks.get(self.selected_id) or {}
        return task.get("inflight", {}).get(request.get("params", {}).get("itemId"), {})

    def _has_action(self, request):
        params, item = request.get("params", {}), self._request_item(request)
        if request.get("method") == "item/commandExecution/requestApproval":
            return bool(params.get("command") or params.get("commandActions") or item.get("command"))
        if request.get("method") == "item/fileChange/requestApproval":
            return bool(params.get("changes") or item.get("changes"))
        return False

    def _render_request(self):
        for child in self.answer_frame.winfo_children():
            if child in (self.approval_allow, self.approval_decline):
                child.pack_forget()
            else:
                child.destroy()
        self.question_values = {}
        request = self._current_request()
        if request is None:
            set_text(self.request_text, "")
            return
        details = {"request": request.get("params", {})}
        item = self._request_item(request)
        if item:
            details["action"] = item
        params = request.get("params", {})
        summary = ""
        if request.get("method") == "item/commandExecution/requestApproval":
            command = params.get("command") or item.get("command")
            if command:
                summary = "拟执行命令：\n" + command + "\n\n"
            elif params.get("commandActions"):
                summary = "拟执行动作：\n" + json.dumps(params["commandActions"], ensure_ascii=False) + "\n\n"
        elif request.get("method") == "item/fileChange/requestApproval":
            paths = [change.get("path", "未知路径") for change in params.get("changes") or item.get("changes") or []]
            if paths:
                summary = "拟修改文件：\n" + "\n".join(paths) + "\n\n"
        set_text(self.request_text, summary + json.dumps(details, ensure_ascii=False, indent=2))
        if self.recovery_info is not None:
            ttk.Label(self.answer_frame, text="备份中的历史请求：只读检视，不能审批、回答或继续。",
                      wraplength=780, foreground="#8a3b00").pack(anchor="w")
        elif request.get("method") in APPROVALS:
            if not self._has_action(request):
                ttk.Label(self.answer_frame, text="动作资料不完整，暂不能允许；可拒绝或核对恢复。",
                          foreground="#8a3b00").pack(side="left", padx=(0, 6))
            self.approval_allow.pack(side="left", padx=(0, 6))
            self.approval_decline.pack(side="left")
        elif request.get("method") == USER_INPUT:
            questions = request.get("params", {}).get("questions", [])
            if any(question.get("isSecret") for question in questions):
                ttk.Label(self.answer_frame, text="不在管理器中收集密码或密钥。请使用 Codex 原生登录流程。",
                          wraplength=780, foreground="#8a3b00").pack(anchor="w")
                ttk.Button(self.answer_frame, text="取消并暂停",
                           command=lambda: self.submit("pause", self.selected_id)).pack(anchor="e")
                self._controls()
                return
            for question in questions:
                identifier = question.get("id")
                if not identifier or identifier in self.question_values:
                    self.status.set("问题标识无效，无法提交；请核对恢复。")
                    return
                ttk.Label(self.answer_frame, text=question.get("question", identifier), wraplength=780).pack(anchor="w")
                variable = tk.StringVar()
                self.question_values[identifier] = variable
                options = [option["label"] for option in question.get("options") or [] if isinstance(option, dict) and "label" in option]
                if options:
                    widget = ttk.Combobox(self.answer_frame, values=options, textvariable=variable,
                                         state="normal" if question.get("isOther") else "readonly")
                else:
                    widget = ttk.Entry(self.answer_frame, textvariable=variable)
                widget.pack(fill="x", pady=(0, 4))
            ttk.Button(self.answer_frame, text="提交回答", command=self._answer_questions).pack(anchor="e")
        else:
            ttk.Label(self.answer_frame, text="不支持此类授权；由控制器拒绝。", foreground="#8a3b00").pack(anchor="w")
        self._controls()

    def _answer_approval(self, decision):
        request = self._current_request()
        if (request and request.get("method") in APPROVALS and decision in ("accept", "decline")
                and (decision == "decline" or self._has_action(request))):
            self.submit("answer", self.selected_id, request["id"], {"decision": decision})

    def _answer_questions(self):
        request = self._current_request()
        if not request or request.get("method") != USER_INPUT or not self.question_values:
            return
        answers = {key: {"answers": [value.get().strip()]} for key, value in self.question_values.items()}
        if any(not value["answers"][0] for value in answers.values()):
            self.status.set("请先回答所有问题。")
            return
        self.submit("answer", self.selected_id, request["id"], {"answers": answers})

    def _controls(self):
        available = self.ready and not self.busy and not self.closing
        inspection = self.recovery_info is not None
        self.attention_count = sum(needs_attention(task) for task in self.tasks.values())
        configure_changed(self.attention_button, text=f"待处理 {self.attention_count} 项 · 查看",
                          state="normal" if self.attention_count and not self.closing else "disabled")
        if self.attention_count:
            if not self.attention_button.winfo_manager():
                self.attention_button.pack(fill="x", pady=(0, 8), before=self.task_tree)
        elif self.attention_button.winfo_manager():
            self.attention_button.pack_forget()
        configure_changed(self.new_button, state="normal" if available and not inspection else "disabled")
        configure_changed(self.import_button, state="normal" if available and not inspection else "disabled")
        configure_changed(self.phone_button, state="normal" if available and not inspection else "disabled")
        if self.phone_dialog is not None and self.phone_dialog.winfo_exists():
            self.phone_dialog.controls()
        if self.import_dialog is not None and self.import_dialog.winfo_exists():
            self.import_dialog.controls()
        backup_safe = not any(task.get("state") in ACTIVE or task.get("pending") for task in self.tasks.values())
        configure_changed(self.backup_button,
                          state="normal" if available and not inspection and backup_safe else "disabled")
        configure_changed(self.diagnostics_button, state="normal" if available else "disabled")
        if self.diagnostics_window is not None and self.diagnostics_window.winfo_exists():
            configure_changed(self.diagnostics_window.save_button,
                              state="normal" if available and self.diagnostics_window.report_ready else "disabled")
        configure_changed(self.message_text, state="disabled" if inspection else "normal")
        task = self.tasks.get(self.selected_id) if self.selected_id in self.visible_ids else None
        configure_changed(self.details_button, state="normal" if task else "disabled")
        configure_changed(self.assessment_button,
                          state="normal" if task and not self.closing and task.get("connection_mode") != "direct" else "disabled")
        for task_id, dialog in list(self.assessment_dialogs.items()):
            if not dialog.winfo_exists():
                del self.assessment_dialogs[task_id]
            elif task_id in self.tasks:
                dialog.refresh(self.tasks[task_id])
        state = task.get("state") if task else None
        archived = bool(task and task.get("archived", False))
        quiet = bool(task) and not any(
            task.get(key) for key in ("inflight", "intent", "pending", "receiver_id")) and task.get("run_started") is None
        can_archive = quiet and state == "completed"
        budget = budget_status(dict(task, run_started=None) if inspection else task) if task else None
        if task:
            limit = f"{task['max_minutes']:g}" if task.get("max_minutes") else "不限"
            text = (f"已记录用时（分钟）：{budget['elapsed_seconds'] / 60:.2f} / {limit}  ·  "
                    f"自动交接：{'已开启' if task.get('auto_handoff') else '未开启'}")
            if budget["reached"]:
                text += "\n已达预算：先调整预算或核对结果，再继续执行。"
            if task.get("telemetry_model_valid", True) is False:
                text += "\n模型口径无效，不能开启自动交接，请先核对遥测。"
            set_changed(self.budget_details, text)
        else:
            set_changed(self.budget_details, "")
        allowed = {"start": state in ("queued", "paused", "idle"), "pause": state in ACTIVE,
                   "prepare_snapshot": state in ("idle", "paused"),
                   "handoff": state == "idle", "finish": state in ("idle", "paused"),
                   "reconcile": bool(task), "export_task": bool(task), "get_task": bool(task),
                   "set_archived": archived or can_archive,
                   "reopen_task": quiet and state == "completed" and not archived and not task.get("analysis_thread_id"),
                   "update_settings": quiet and state in ("queued", "idle", "paused") and not archived}
        if budget and budget["reached"]:
            allowed["start"] = allowed["handoff"] = False
        if task and task.get("connection_mode") == "direct":
            allowed["handoff"] = False
        brief = (task.get("brief") or {}) if task else {}
        if task and task.get("brief_required") and brief.get("status") == "current" and brief.get("decision") == "pending":
            allowed["start"] = False
        configure_changed(self.buttons["start"],
                          text="先整理简报" if task and task.get("brief_required") else "发送 / 继续")
        if archived:
            for method in ("start", "pause", "prepare_snapshot", "handoff", "finish", "reconcile"):
                allowed[method] = False
        if inspection:
            allowed = {method: bool(task) and method in ("get_task", "export_task") for method in allowed}
        configure_changed(self.buttons["set_archived"], text="取消归档" if archived else "归档")
        for method, button in self.buttons.items():
            configure_changed(button, state="normal" if available and allowed[method] else "disabled")
        primary = ("pause",) if state in ACTIVE else (("reconcile",) if state in ("needs_reconcile", "blocked") else
                  ("start",) if state in ("queued", "paused", "idle") else ())
        for method in ("start", "pause", "reconcile"):
            button = self.buttons[method]
            if method not in primary and button.winfo_manager():
                button.pack_forget()
        for method in primary:
            button = self.buttons[method]
            if not button.winfo_manager():
                button.pack(side="right", padx=(6, 0))
        menu_widgets = {"assessment": self.assessment_button,
                        **{key: value for key, value in self.buttons.items() if key in self.more_entries}}
        for key, widget in menu_widgets.items():
            menu_changed(self.more_menu, self.more_entries[key],
                         state="disabled" if widget.instate(["disabled"]) else "normal")
        for key, widget in (("phone", self.phone_button), ("backup", self.backup_button),
                            ("diagnostics", self.diagnostics_button)):
            menu_changed(self.settings_entry_menus[key], self.settings_entries[key],
                         state="disabled" if widget.instate(["disabled"]) else "normal")
        menu_changed(self.settings_entry_menus["preferences"], self.settings_entries["preferences"],
                     state="disabled" if self.closing else "normal")
        configure_changed(self.settings_button, state="disabled" if self.closing else "normal")
        can_open_chat = bool(task) and available and not inspection and bool(task.get("thread_id") or task.get("source_snapshot"))
        menu_changed(self.more_menu, self.more_entries["open_chat"],
                     state="normal" if can_open_chat else "disabled")
        menu_changed(self.more_menu, self.more_entries["set_archived"],
                     label="取消归档" if archived else "归档")
        configure_changed(self.more_button, state="disabled" if self.closing else "normal")
        request = self._current_request()
        allow_approval = available and not inspection and request and request.get("method") in APPROVALS
        configure_changed(self.approval_decline, state="normal" if allow_approval else "disabled")
        configure_changed(self.approval_allow,
                          state="normal" if allow_approval and self._has_action(request) else "disabled")

    def _callback_error(self, exception_type, exception, _traceback):
        message = f"{exception_type.__name__}: {exception}"
        self.status.set(f"界面回调出错：{message}")
        messagebox.showerror("界面回调出错", message, parent=self.root)
        # A failed refresh must not strand worker results or the close receipt.
        if self._pump_after is None and not self.closed:
            self._pump_after = self.root.after(40, self._pump)

    def _pump(self):
        self._pump_after = None
        changed = False
        while True:
            try:
                kind, value = self.worker.events.get_nowait()
            except queue.Empty:
                break
            changed = True
            if kind == "ready":
                self.ready = True
                self.recovery_info = value
                if value is not None:
                    self.recovery_banner.configure(text=(
                        "恢复库 · 永久只读检视。下列为备份历史状态，并非当前执行状态；不能继续任务，只能浏览和导出。\n"
                        f"备份时间：{value.get('backup_created_at', '未知')}  ·  恢复时间：{value.get('restored_at', '未知')}"))
                    self.recovery_banner.pack(fill="x", padx=16, pady=(0, 8), before=self.panes)
                    self.status.set("恢复库已加载；不连接 Codex，只检视本地历史记录。")
                else:
                    self.status.set("本机任务已加载；原话任务选中后读取对话，发送才继续任务。")
            elif kind == "tasks":
                self._render_tasks(value)
            elif kind == "command_done":
                self.busy = False
                method, args, result = value
                self._import_result(method, result=result)
                self._assessment_result(method)
                self._phone_result(method, result=result)
                if method in ("create_task", "import_thread") and isinstance(result, dict):
                    self.search.set("")
                    self.state_filter.set(FILTERS[0])
                    self.tasks[result["id"]] = result
                    self.selected_id = result["id"]
                    self._apply_filters()
                if method == "start":
                    task_id = args[0]
                    sent = (args[1] if len(args) > 1 else None) or ""
                    if self.message_drafts.get(task_id, "") == sent:
                        self.message_drafts.pop(task_id, None)
                    if self.rendered_task_id == task_id and self.message_text.get("1.0", "end-1c") == sent:
                        self.message_text.delete("1.0", "end")
                if method == "get_task" and isinstance(result, dict) and not self.closing:
                    HistoryWindow(self.root, result)
                if method == "diagnostics" and isinstance(result, dict) and not self.closing:
                    window = self.diagnostics_window
                    if window is not None and window.winfo_exists():
                        set_text(window.report_text, json.dumps(result, ensure_ascii=False, indent=2))
                        window.report_ready = True
                if method == "read_chat" and isinstance(result, dict):
                    request = self.chat_request
                    self.chat_request = None
                    if request is not None and request[0] == args[0]:
                        self.chat_cache[args[0]] = result
                        if (not self.closing and self.selected_id == args[0]
                                and request[2] == self.chat_generation):
                            self._render_conversation(self.tasks.get(args[0]))
                if method == "backup_state" and isinstance(result, dict):
                    self.status.set(f"备份已保存：{result['path']}\n任务 {result['task_count']} · 事件 {result['event_count']} · "
                                    f"文件 {result['file_count']} · 创建时间 {result['created_at']}")
                else:
                    self.status.set("已导出任务记录。" if method == "export_task" else
                                    "已保存诊断信息 JSON。" if method == "export_diagnostics" else
                                    "诊断信息已读取；仅在明确选择文件后保存。" if method == "diagnostics" else
                                    "任务已重新打开，待继续；未启动任务。" if method == "reopen_task" else
                                    ("已连接原聊天；发送消息时原话接续，勿在原窗口同时执行。" if result.get("connection_mode") == "direct" else
                                     "已导入为待启动任务；原聊天未修改，尚未启动。") if method == "import_thread" else
                                    "设置已保存；未启动任务。" if method == "update_settings" else
                                    "操作已处理；以任务状态和实际回复为准。")
            elif kind in ("command_error", "poll_error", "startup_error", "close_error"):
                if kind == "command_error":
                    self.busy = False
                    if value[0] == "read_chat":
                        request = self.chat_request
                        self.chat_request = None
                        if request is not None:
                            cached = dict(self.chat_cache.get(request[0]) or {})
                            cached["error"] = value[1]
                            self.chat_cache[request[0]] = cached
                            if self.selected_id == request[0] and request[2] == self.chat_generation:
                                self._render_conversation(self.tasks.get(request[0]))
                    self._import_result(value[0], error=value[1])
                    self._assessment_result(value[0], error=value[1])
                    self._phone_result(value[0], error=value[1])
                    if value[0] == "diagnostics" and not self.closing:
                        window = self.diagnostics_window
                        if window is not None and window.winfo_exists():
                            set_text(window.report_text, f"无法读取诊断信息：{value[1]}\n关闭预览后可重新尝试。")
                    value = value[1]
                if kind == "close_error":
                    self.closing = False
                    self.busy = False
                self.status.set(f"需要处理：{value}")
            elif kind == "remote_error":
                self.status.set(f"手机操作需核对：{value}")
            elif kind == "closed":
                self.closed = True
                self.root.destroy()
                return
        now = time.monotonic()
        clock_due = (now - self._last_clock_controls >= 1.0
                     and any(task.get("state") in ACTIVE and task.get("run_started") is not None
                             for task in self.tasks.values()))
        if changed or clock_due:
            self._controls()
            self._last_clock_controls = now
        if changed:
            self._maybe_request_chat()
        self._pump_after = self.root.after(40, self._pump)

    def request_close(self):
        if self.closing or self.closed:
            return
        active = self.recovery_info is None and (self.busy or any(task.get("state") in ACTIVE for task in self.tasks.values()))
        if active and not messagebox.askokcancel("关闭并中断任务？",
                "有任务或后台操作正在进行。关闭将先请求中断并清理；结果无法确认的任务会保留为待核对恢复。\n\n选择“取消”保留窗口。",
                default=messagebox.CANCEL, parent=self.root):
            return
        if not self.worker.is_alive():
            self.closed = True
            self.root.destroy()
            return
        self.closing = True
        self.status.set("正在关闭本地检视窗口…" if self.recovery_info is not None else
                        "正在中断任务并关闭，请保持窗口开启…")
        self._controls()
        self.worker.stop_requested.set()


def main(state_dir=None):
    enable_native_dpi()
    root = tk.Tk()
    app = RelayApp(root, state_dir=state_dir)
    root.mainloop()
    app.worker.stop_requested.set()
    app.worker.join()
