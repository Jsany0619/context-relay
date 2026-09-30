"""Tk interface; every Manager call belongs to the single command worker."""
from copy import deepcopy
import json
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


STATES = {
    "queued": "待启动", "creating": "正在创建", "running": "运行中",
    "pausing": "正在暂停", "paused": "已暂停", "idle": "等待继续",
    "summarizing": "准备交接", "verifying": "核验接管", "needs_reconcile": "待核对恢复",
    "blocked": "需要处理", "completed": "已完成",
}
ACTIVE = {"creating", "running", "pausing", "summarizing", "verifying"}
APPROVALS = {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}
USER_INPUT = "item/tool/requestUserInput"


def manager_factory(state_dir=None):
    from .manager import Manager
    return Manager(state_dir=state_dir)


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
        self.events.put(("ready", None))
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
                    self.events.put(("command_done", (method, deepcopy(result))))
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
            for key in ("max_tokens", "max_minutes"):
                values[key] = int(values[key] or "0")
                if values[key] < 0:
                    raise ValueError("上限必须为非负整数。")
        except (OSError, ValueError) as error:
            messagebox.showwarning("输入有误", str(error), parent=self)
            return
        values.update(mode="read-only" if self.mode.get() == "只读" else "workspace-write",
                      auto_handoff=self.auto_handoff.get())
        if self.submit("create_task", **values):
            self.destroy()


class RelayApp:
    def __init__(self, root, state_dir=None, factory=manager_factory):
        self.root = root
        self.tasks = {}
        self.selected_id = None
        self.rendered_task_id = None
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
        self.status = tk.StringVar(value="正在连接 Codex…")
        self.details = tk.StringVar(value="选择左侧任务查看详情。")
        self._build()
        self.worker = CommandWorker(factory, state_dir)
        self.worker.start()
        self.root.after(40, self._pump)

    def _build(self):
        compact = self.root.winfo_screenheight() < 900
        ttk.Label(self.root, text="Context Relay", font=("Segoe UI", 17, "bold")).pack(anchor="w", padx=16, pady=(12, 0))
        ttk.Label(self.root, text="仅管理这里创建的任务；现有聊天不会自动导入。", foreground="#555555").pack(anchor="w", padx=16, pady=(0, 8))
        panes = ttk.Panedwindow(self.root, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=12)
        left, right = ttk.Frame(panes, padding=4), ttk.Frame(panes, padding=4)
        panes.add(left, weight=1)
        panes.add(right, weight=4)
        self.new_button = ttk.Button(left, text="新建任务", command=lambda: NewTaskDialog(self.root, self.submit))
        self.new_button.pack(fill="x", pady=(0, 8))
        self.task_tree = ttk.Treeview(left, columns=("state",), show="tree headings", selectmode="browse")
        self.task_tree.heading("#0", text="任务")
        self.task_tree.heading("state", text="状态")
        self.task_tree.column("#0", width=160, minwidth=100)
        self.task_tree.column("state", width=92, minwidth=75, stretch=False)
        self.task_tree.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(left, command=self.task_tree.yview)
        self.task_tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.task_tree.bind("<<TreeviewSelect>>", self._select_task)
        ttk.Label(right, textvariable=self.details, wraplength=820, justify="left").pack(fill="x", pady=(0, 6))
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
        self.buttons = {}
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
        self.busy = True
        self.status.set("正在处理，请稍候…")
        self._controls()
        self.worker.commands.put((method, args, kwargs))
        return True

    def _action(self, method):
        if not self.selected_id:
            return
        if method == "start":
            self.submit(method, self.selected_id, self.message_text.get("1.0", "end").strip() or None)
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
        self.selected_id = selection[0] if selection else None
        self._render_task()

    def _render_tasks(self, tasks):
        self.tasks = {task["id"]: task for task in tasks}
        for item in self.task_tree.get_children():
            if item not in self.tasks:
                self.task_tree.delete(item)
        for task in tasks:
            values = (STATES.get(task.get("state"), task.get("state", "未知")),)
            if self.task_tree.exists(task["id"]):
                self.task_tree.item(task["id"], text=task["title"], values=values)
            else:
                self.task_tree.insert("", "end", iid=task["id"], text=task["title"], values=values)
        if self.selected_id not in self.tasks:
            self.selected_id = next(iter(self.tasks), None)
        if self.selected_id:
            self.task_tree.selection_set(self.selected_id)
        self._render_task()

    def _render_task(self):
        task = self.tasks.get(self.selected_id)
        switched = self.rendered_task_id != self.selected_id
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
        if request.get("method") in APPROVALS:
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
        self.new_button.configure(state="normal" if available else "disabled")
        task = self.tasks.get(self.selected_id)
        state = task.get("state") if task else None
        allowed = {"start": state in ("queued", "paused", "idle"), "pause": state in ACTIVE,
                   "prepare_snapshot": state in ("idle", "paused"),
                   "handoff": state == "idle", "finish": state in ("idle", "paused"),
                   "reconcile": bool(task), "export_task": bool(task)}
        for method, button in self.buttons.items():
            button.configure(state="normal" if available and allowed[method] else "disabled")
        request = self._current_request()
        allow_approval = available and request and request.get("method") in APPROVALS
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
                self.status.set("已就绪。新建任务后，点击启动。")
            elif kind == "tasks":
                self._render_tasks(value)
            elif kind == "command_done":
                self.busy = False
                method, result = value
                if method == "create_task" and isinstance(result, dict):
                    self.selected_id = result.get("id")
                if method == "start":
                    self.message_text.delete("1.0", "end")
                self.status.set("已导出任务记录。" if method == "export_task" else "操作已处理；以任务状态和实际回复为准。")
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
        active = self.busy or any(task.get("state") in ACTIVE for task in self.tasks.values())
        if active and not messagebox.askokcancel("关闭并中断任务？",
                "有任务或后台操作正在进行。关闭将先请求中断并清理；结果无法确认的任务会保留为待核对恢复。\n\n选择“取消”保留窗口。",
                default=messagebox.CANCEL, parent=self.root):
            return
        if not self.worker.is_alive():
            self.closed = True
            self.root.destroy()
            return
        self.closing = True
        self.status.set("正在中断任务并关闭，请保持窗口开启…")
        self._controls()
        self.worker.stop_requested.set()


def main(state_dir=None):
    root = tk.Tk()
    app = RelayApp(root, state_dir=state_dir)
    root.mainloop()
    app.worker.stop_requested.set()
    app.worker.join()
