"""Tk interface; every Manager call belongs to the single command worker."""
from copy import deepcopy
import json
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .budget import budget_status, validate_limits


STATES = {
    "queued": "待启动", "creating": "正在创建", "running": "运行中",
    "pausing": "正在暂停", "paused": "已暂停", "idle": "等待继续",
    "summarizing": "准备交接", "verifying": "核验接管", "needs_reconcile": "待核对恢复",
    "blocked": "需要处理", "completed": "已完成",
}
ACTIVE = {"creating", "running", "pausing", "summarizing", "verifying"}
APPROVALS = {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}
USER_INPUT = "item/tool/requestUserInput"
FILTERS = ("全部未归档", "运行中", "待处理", "等待继续", "已完成", "已归档")
EVENT_LABELS = {
    "task_created": "已记录任务", "create_requested": "已请求创建会话", "thread_created": "已收到会话创建回执",
    "turn_requested": "已请求启动轮次", "turn_started": "已收到启动回执（不代表已实际开始）",
    "native_turn_started": "原生轮次已开始", "turn_completed": "已收到轮次终态", "work_finished": "工作轮次结束",
    "interrupt_requested": "已记录暂停请求", "paused": "已暂停", "draft_saved": "已保存预备快照",
    "requires_reconciliation": "需要核对恢复", "restart_requires_reconciliation": "重启后需要核对恢复",
    "reconciled_read_only": "已完成只读恢复核对", "handoff_requested": "已请求交接",
    "handoff_unnecessary": "无需交接", "checkpoint_frozen": "已冻结检查点", "ownership_transferred": "已移交执行权",
    "receiver_abandoned": "已放弃原接收会话", "user_instruction": "已记录用户指令", "user_answer": "已记录用户回答",
    "awaiting_user": "等待用户处理", "approval_denied_by_guard": "执行检查拒绝审批",
    "permission_expansion_denied": "已拒绝扩大权限", "secret_input_refused": "已拒绝采集秘密信息",
    "late_receipt_recorded": "已记录迟到回执", "tool_state": "已记录工具状态",
    "user_marked_complete": "用户已标记完成", "task_archived": "已归档", "task_unarchived": "已取消归档",
    "task_settings_updated": "已更新任务设置",
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

    def run(self):
        try:
            manager = self.factory(state_dir=self.state_dir)
            manager.stop_requested = self.stop_requested
        except Exception as error:
            self.events.put(("startup_error", str(error)))
            return
        previous, last_poll_error = None, None
        self.events.put(("ready", deepcopy(getattr(manager, "recovery_info", None))))
        while True:
            if self.stop_requested.is_set():
                while True:
                    try:
                        self.commands.get_nowait()
                    except queue.Empty:
                        break
                try:
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
                try:
                    result = getattr(manager, method)(*args, **kwargs)
                except Exception as error:
                    self.events.put(("command_error", (method, str(error))))
                else:
                    self.events.put(("command_done", (method, args, deepcopy(result))))
                if self.stop_requested.is_set():
                    continue
            try:
                manager.poll()
                tasks = deepcopy(manager.list_tasks())
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


def text_area(parent, height):
    frame = ttk.Frame(parent)
    widget = tk.Text(frame, height=height, wrap="word", state="disabled", relief="flat")
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
                ("title", "任务名称", ""), ("cwd", "工作目录", str(Path.cwd())),
                ("max_tokens", "Token 上限（留空不限）", ""),
                ("max_minutes", "分钟上限（留空不限）", ""))):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=5)
            variable = tk.StringVar(value=value)
            self.fields[key] = variable
            ttk.Entry(body, textvariable=variable, width=54).grid(row=row, column=1, sticky="ew", pady=5)
        ttk.Button(body, text="选择…", command=self.choose_directory).grid(row=1, column=2, padx=(8, 0))
        ttk.Label(body, text="权限上限").grid(row=4, column=0, sticky="w", pady=5)
        self.mode = tk.StringVar(value="只读")
        ttk.Combobox(body, textvariable=self.mode, values=("只读", "允许修改工作区"),
                     state="readonly").grid(row=4, column=1, sticky="w", pady=5)
        self.auto_handoff = tk.BooleanVar(value=False)
        ttk.Checkbutton(body, text="允许上下文风险达到条件时自动交接此任务",
                        variable=self.auto_handoff).grid(row=5, column=0, columnspan=3, sticky="w", pady=8)
        ttk.Label(body, text="任务目标与完成标准").grid(row=6, column=0, columnspan=3, sticky="w")
        self.goal = tk.Text(body, height=7, width=68, wrap="word")
        self.goal.grid(row=7, column=0, columnspan=3, sticky="ew", pady=6)
        buttons = ttk.Frame(body)
        buttons.grid(row=8, column=0, columnspan=3, sticky="e", pady=(8, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="left", padx=5)
        ttk.Button(buttons, text="创建", command=self.save).pack(side="left")
        self.grab_set()

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
        ttk.Label(body, text=f"任务 ID：{self.task_id}").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.fields = {}
        for row, (key, label) in enumerate((("title", "任务名称"), ("max_tokens", "Token 上限（留空或 0 不限）"),
                                           ("max_minutes", "分钟上限（可小数，0 不限）")), 1):
            value = task.get(key) or ""
            self.fields[key] = tk.StringVar(value=str(value))
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=5)
            ttk.Entry(body, textvariable=self.fields[key], width=44).grid(row=row, column=1, sticky="ew", pady=5)
        self.auto_handoff = tk.BooleanVar(value=bool(task.get("auto_handoff")) and self.auto_allowed)
        self.auto_check = ttk.Checkbutton(body, text="允许按上下文风险自动预备与交接此任务", variable=self.auto_handoff,
                                         state="normal" if self.auto_allowed else "disabled")
        self.auto_check.grid(row=4, column=0, columnspan=2, sticky="w", pady=8)
        text = ("权限、目录和原始目标不在此修改。预算是软上限，已记录用时包括活动轮次的等待，"
                "运行中调用可能超出预算。保存变更后，原预备快照与检查点失效；保存不会启动任务。")
        if not self.auto_allowed:
            text += "\n模型口径无效，不能开启自动交接，请先核对遥测。"
        self.notice = ttk.Label(body, text=text, wraplength=580, justify="left")
        self.notice.grid(row=5, column=0, columnspan=2, sticky="w", pady=6)
        buttons = ttk.Frame(body)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="left", padx=5)
        self.save_button = ttk.Button(buttons, text="保存设置", command=self.save)
        self.save_button.pack(side="left")
        self.grab_set()

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
        self.root.title("Context Relay · 本机任务管理器")
        width = min(1180, max(1, self.root.winfo_screenwidth() - 80))
        height = min(850, max(1, self.root.winfo_screenheight() - 120))
        left = max(20, (self.root.winfo_screenwidth() - width) // 2)
        self.root.geometry(f"{width}x{height}+{left}+20")
        self.root.minsize(min(940, width), min(720, height))
        self.root.protocol("WM_DELETE_WINDOW", self.request_close)
        self.status = tk.StringVar(value="正在读取本机任务…")
        self.details = tk.StringVar(value="选择左侧任务查看详情。")
        self.budget_details = tk.StringVar()
        self.search = tk.StringVar()
        self.state_filter = tk.StringVar(value=FILTERS[0])
        self._build()
        self.search.trace_add("write", lambda *args: self._apply_filters())
        self.state_filter.trace_add("write", lambda *args: self._apply_filters())
        self.worker = CommandWorker(factory, state_dir)
        self.worker.start()
        self.root.after(40, self._pump)

    def _build(self):
        compact = self.root.winfo_screenheight() < 900
        ttk.Label(self.root, text="Context Relay", font=("Segoe UI", 17, "bold")).pack(anchor="w", padx=16, pady=(12, 0))
        ttk.Label(self.root, text="仅管理这里创建的任务；现有聊天不会自动导入。", foreground="#555555").pack(anchor="w", padx=16, pady=(0, 8))
        self.recovery_banner = ttk.Label(self.root, wraplength=1140, justify="left", foreground="#8a3b00")
        panes = ttk.Panedwindow(self.root, orient="horizontal")
        self.panes = panes
        panes.pack(fill="both", expand=True, padx=12)
        left, right = ttk.Frame(panes, padding=4), ttk.Frame(panes, padding=4)
        panes.add(left, weight=1)
        panes.add(right, weight=4)
        self.new_button = ttk.Button(left, text="新建任务", command=lambda: NewTaskDialog(self.root, self.submit))
        self.new_button.pack(fill="x", pady=(0, 8))
        self.backup_button = ttk.Button(left, text="备份管理器", command=self._backup)
        self.backup_button.pack(fill="x", pady=(0, 4))
        self.backup_notice = ttk.Label(left, text="本地明文记录；不含项目文件、Codex 登录或原生聊天。",
                                       wraplength=240, justify="left", foreground="#555555")
        self.backup_notice.pack(fill="x", pady=(0, 8))
        ttk.Label(left, text="搜索名称、目标或目录").pack(anchor="w")
        self.search_entry = ttk.Entry(left, textvariable=self.search)
        self.search_entry.pack(fill="x", pady=(2, 6))
        self.filter_choice = ttk.Combobox(left, textvariable=self.state_filter, values=FILTERS, state="readonly")
        self.filter_choice.pack(fill="x", pady=(0, 6))
        self.attention_button = ttk.Button(left, command=self._show_attention)
        self.attention_button.pack(fill="x", pady=(0, 8))
        organize = ttk.Frame(left)
        organize.pack(fill="x", pady=(0, 8))
        self.buttons = {}
        for label, method in (("操作记录", "get_task"), ("归档", "set_archived")):
            button = ttk.Button(organize, text=label, command=lambda action=method: self._action(action))
            button.pack(side="left", expand=True, fill="x", padx=(0, 4))
            self.buttons[method] = button
        self.buttons["update_settings"] = ttk.Button(left, text="任务设置", command=lambda: self._action("update_settings"))
        self.buttons["update_settings"].pack(fill="x", pady=(0, 8))
        task_list = ttk.Frame(left)
        task_list.pack(fill="both", expand=True)
        self.task_tree = ttk.Treeview(task_list, columns=("state",), show="tree headings", selectmode="browse")
        self.task_tree.heading("#0", text="任务")
        self.task_tree.heading("state", text="状态")
        self.task_tree.column("#0", width=160, minwidth=100)
        self.task_tree.column("state", width=92, minwidth=75, stretch=False)
        self.task_tree.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(task_list, command=self.task_tree.yview)
        self.task_tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.task_tree.bind("<<TreeviewSelect>>", self._select_task)
        ttk.Label(right, textvariable=self.details, wraplength=820, justify="left").pack(fill="x", pady=(0, 6))
        self.budget_label = ttk.Label(right, textvariable=self.budget_details, wraplength=820, justify="left")
        self.budget_label.pack(fill="x", pady=(0, 6))
        goal = ttk.LabelFrame(right, text="目标")
        goal.pack(fill="x")
        self.goal_text = text_area(goal, 2 if compact else 3)
        latest = ttk.LabelFrame(right, text="最新回复")
        latest.pack(fill="both", expand=True, pady=6)
        self.latest_text = text_area(latest, 6 if compact else 8)
        pending = ttk.LabelFrame(right, text="等待审批 / 回答")
        pending.pack(fill="x", pady=(0, 6))
        self.pending_choice = ttk.Combobox(pending, state="disabled")
        self.pending_choice.pack(fill="x", padx=6, pady=(6, 0))
        self.pending_choice.bind("<<ComboboxSelected>>", lambda event: self._render_request())
        self.request_text = text_area(pending, 3 if compact else 4)
        self.answer_frame = ttk.Frame(pending, padding=(6, 0, 6, 6))
        self.answer_frame.pack(fill="x")
        self.approval_allow = ttk.Button(self.answer_frame, text="仅此一次允许", command=lambda: self._answer_approval("accept"))
        self.approval_decline = ttk.Button(self.answer_frame, text="拒绝", command=lambda: self._answer_approval("decline"))
        controls = ttk.Frame(right)
        controls.pack(side="bottom", fill="x", before=goal)
        pending.pack_configure(side="bottom", before=goal)
        ttk.Label(controls, text="补充指令（可留空）").pack(anchor="w")
        self.message_text = tk.Text(controls, height=2, wrap="word")
        self.message_text.pack(fill="x", pady=(2, 6))
        actions = ttk.Frame(controls)
        actions.pack(fill="x")
        for label, method in (("启动 / 继续", "start"), ("暂停", "pause"), ("核对恢复", "reconcile"),
                              ("预备快照", "prepare_snapshot"), ("交接", "handoff"),
                              ("标记完成", "finish"), ("导出", "export_task")):
            button = ttk.Button(actions, text=label, command=lambda action=method: self._action(action))
            button.pack(side="left", padx=(0, 5))
            self.buttons[method] = button
        ttk.Label(self.root, textvariable=self.status, wraplength=1140, justify="left").pack(fill="x", padx=16, pady=10)
        self._controls()

    def submit(self, method, *args, **kwargs):
        if not self.ready or self.busy or self.closing:
            return False
        if self.recovery_info is not None and method not in ("get_task", "export_task"):
            self.status.set("恢复库为永久只读检视，不能继续任务或修改记录。")
            return False
        if method not in ("create_task", "backup_state") and args and args[0] not in self.visible_ids:
            return False
        self.busy = True
        self.status.set("正在处理，请稍候…")
        self._controls()
        self.worker.commands.put((method, args, kwargs))
        return True

    def _backup(self):
        if self.backup_button.instate(["disabled"]):
            return
        destination = filedialog.asksaveasfilename(parent=self.root, title="保存新的管理器备份（本地明文）",
                                                  defaultextension=".zip", initialfile="context-relay-backup.zip",
                                                  filetypes=(("ZIP", "*.zip"),), confirmoverwrite=False)
        if destination:
            self.submit("backup_state", destination)

    def _action(self, method):
        if self.selected_id not in self.visible_ids or self.buttons[method].instate(["disabled"]):
            return
        if method == "start":
            self.submit(method, self.selected_id, self.message_text.get("1.0", "end").strip() or None)
        elif method == "update_settings":
            TaskSettingsDialog(self.root, self.tasks[self.selected_id], self.submit)
        elif method == "set_archived":
            self.submit(method, self.selected_id, not self.tasks[self.selected_id].get("archived", False))
        elif method == "export_task":
            destination = filedialog.asksaveasfilename(parent=self.root, title="导出任务记录", defaultextension=".json",
                                                      initialfile="context-relay-task.json", filetypes=(("JSON", "*.json"),))
            if destination:
                self.submit(method, self.selected_id, destination)
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
        self.tasks = {task["id"]: task for task in tasks}
        self._apply_filters()

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
        for item in self.task_tree.get_children():
            if item not in self.visible_ids:
                self.task_tree.delete(item)
        for task in visible:
            values = ("已归档" if task.get("archived") else STATES.get(task.get("state"), task.get("state", "未知")),)
            if self.task_tree.exists(task["id"]):
                self.task_tree.item(task["id"], text=task["title"], values=values)
            else:
                self.task_tree.insert("", "end", iid=task["id"], text=task["title"], values=values)
        if self.selected_id not in self.visible_ids:
            self.selected_id = visible[0]["id"] if visible else None
        if self.selected_id:
            self.task_tree.selection_set(self.selected_id)
        self._render_task()

    def _render_task(self):
        task = self.tasks.get(self.selected_id)
        switched = self.rendered_task_id != self.selected_id
        if switched:
            if self.rendered_task_id is not None:
                self.message_drafts[self.rendered_task_id] = self.message_text.get("1.0", "end-1c")
            self.message_text.delete("1.0", "end")
            self.message_text.insert("1.0", self.message_drafts.get(self.selected_id, ""))
        self.rendered_task_id = self.selected_id
        if not task:
            self.details.set("选择左侧任务查看详情。")
            set_text(self.goal_text, "")
            set_text(self.latest_text, "")
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
        self.details.set(f"{task['title']} · {STATES.get(task.get('state'), task.get('state', '未知'))}\n"
                         f"{task['cwd']}\n权限：{mode}  ·  交接代次：{task.get('generation', 0)}  ·  "
                         f"上下文估算：{pressure_text}  ·  压缩：{task.get('compactions', 0)} 次  ·  累计 Token：{usage_text}"
                         + (f"\n预备快照：{draft.get('created_at', '时间未知')} · 仅预备，交接前须重验" if draft else "")
                         + (f"\n需要处理：{task['error']}" if task.get("error") else ""))
        set_text(self.goal_text, task.get("goal"))
        set_text(self.latest_text, task.get("last_message") or "尚无回复。", follow=True)
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
            index = next((i for i, item in enumerate(pending) if item.get("id") == previous_id), 0)
            self.pending_choice.current(index)
        else:
            self.pending_choice.set("没有待处理请求")
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
            set_text(self.request_text, "没有待处理请求。")
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
        self.attention_button.configure(text=f"待处理 {self.attention_count} 项 · 查看",
                                        state="normal" if self.attention_count and not self.closing else "disabled")
        self.new_button.configure(state="normal" if available and not inspection else "disabled")
        backup_safe = not any(task.get("state") in ACTIVE or task.get("pending") for task in self.tasks.values())
        self.backup_button.configure(state="normal" if available and not inspection and backup_safe else "disabled")
        self.message_text.configure(state="disabled" if inspection else "normal")
        task = self.tasks.get(self.selected_id) if self.selected_id in self.visible_ids else None
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
            self.budget_details.set(text)
        else:
            self.budget_details.set("")
        allowed = {"start": state in ("queued", "paused", "idle"), "pause": state in ACTIVE,
                   "prepare_snapshot": state in ("idle", "paused"),
                   "handoff": state == "idle", "finish": state in ("idle", "paused"),
                   "reconcile": bool(task), "export_task": bool(task), "get_task": bool(task),
                   "set_archived": archived or can_archive,
                   "update_settings": quiet and state in ("queued", "idle", "paused") and not archived}
        if budget and budget["reached"]:
            allowed["start"] = allowed["handoff"] = False
        if archived:
            for method in ("start", "pause", "prepare_snapshot", "handoff", "finish", "reconcile"):
                allowed[method] = False
        if inspection:
            allowed = {method: bool(task) and method in ("get_task", "export_task") for method in allowed}
        self.buttons["set_archived"].configure(text="取消归档" if archived else "归档")
        for method, button in self.buttons.items():
            button.configure(state="normal" if available and allowed[method] else "disabled")
        request = self._current_request()
        allow_approval = available and not inspection and request and request.get("method") in APPROVALS
        self.approval_decline.configure(state="normal" if allow_approval else "disabled")
        self.approval_allow.configure(state="normal" if allow_approval and self._has_action(request) else "disabled")

    def _pump(self):
        while True:
            try:
                kind, value = self.worker.events.get_nowait()
            except queue.Empty:
                break
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
                    self.status.set("本机任务已加载；启动任务时连接 Codex。")
            elif kind == "tasks":
                self._render_tasks(value)
            elif kind == "command_done":
                self.busy = False
                method, args, result = value
                if method == "create_task" and isinstance(result, dict):
                    self.search.set("")
                    self.state_filter.set(FILTERS[0])
                    self.tasks[result["id"]] = result
                    self.selected_id = result["id"]
                    self._apply_filters()
                if method == "start":
                    task_id = args[0]
                    sent = (args[1] if len(args) > 1 else None) or ""
                    if self.message_drafts.get(task_id, "").strip() == sent:
                        self.message_drafts.pop(task_id, None)
                    if self.rendered_task_id == task_id and self.message_text.get("1.0", "end-1c").strip() == sent:
                        self.message_text.delete("1.0", "end")
                if method == "get_task" and isinstance(result, dict) and not self.closing:
                    HistoryWindow(self.root, result)
                if method == "backup_state" and isinstance(result, dict):
                    self.status.set(f"备份已保存：{result['path']}\n任务 {result['task_count']} · 事件 {result['event_count']} · "
                                    f"文件 {result['file_count']} · 创建时间 {result['created_at']}")
                else:
                    self.status.set("已导出任务记录。" if method == "export_task" else
                                    "设置已保存；未启动任务。" if method == "update_settings" else
                                    "操作已处理；以任务状态和实际回复为准。")
            elif kind in ("command_error", "poll_error", "startup_error", "close_error"):
                if kind == "command_error":
                    self.busy = False
                    value = value[1]
                if kind == "close_error":
                    self.closing = False
                    self.busy = False
                self.status.set(f"需要处理：{value}")
            elif kind == "closed":
                self.closed = True
                self.root.destroy()
                return
        self._controls()
        self.root.after(40, self._pump)

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
    root = tk.Tk()
    app = RelayApp(root, state_dir=state_dir)
    root.mainloop()
    app.worker.stop_requested.set()
    app.worker.join()
