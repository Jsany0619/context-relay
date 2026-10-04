"""Small native Tk widgets for the task rail and selectable conversation text."""
import tkinter as tk
from tkinter import ttk


class TaskCardList(ttk.Frame):
    """Scrollable native-button task list with a Treeview-compatible selection API."""

    def __init__(self, parent, command):
        super().__init__(parent, style="Sidebar.TFrame")
        self.command = command
        self._ids = []
        self._selected = None
        self._buttons = {}
        self._parts = {}
        self._row_signatures = {}
        self._row_positions = {}
        self.canvas = tk.Canvas(self, width=255, highlightthickness=0, borderwidth=0)
        self.inner = ttk.Frame(self.canvas, style="Sidebar.TFrame")
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self._canvas_width = 0
        self._scroll_region = None
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.inner.bind("<Configure>", self._update_region)
        self.canvas.bind("<Configure>", self._resize_inner)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.inner.bind("<MouseWheel>", self._wheel)

    def _update_region(self, _event=None):
        region = self.canvas.bbox("all")
        if region != self._scroll_region:
            self._scroll_region = region
            self.canvas.configure(scrollregion=region)

    def _resize_inner(self, event):
        if event.width != self._canvas_width:
            self._canvas_width = event.width
            self.canvas.itemconfigure(self.window, width=event.width)

    def _wheel(self, event):
        widget = self.winfo_containing(event.x_root, event.y_root) if self.winfo_exists() else None
        while widget is not None and widget is not self:
            widget = widget.master
        if widget is self:
            self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def render(self, rows, selected=None):
        wanted = [row["id"] for row in rows]
        for identifier in list(self._buttons):
            if identifier not in wanted:
                self._buttons.pop(identifier).destroy()
                self._parts.pop(identifier, None)
                self._row_signatures.pop(identifier, None)
                self._row_positions.pop(identifier, None)
        self._ids = wanted
        for index, row in enumerate(rows):
            identifier = row["id"]
            card = self._buttons.get(identifier)
            if card is None:
                card = ttk.Frame(self.inner, takefocus=True)
                title = ttk.Label(card)
                preview = ttk.Label(card)
                meta = ttk.Label(card)
                title.pack(fill="x")
                preview.pack(fill="x", pady=(4, 0))
                meta.pack(fill="x", pady=(3, 0))
                self._buttons[identifier] = card
                self._parts[identifier] = (title, preview, meta)
                for widget in (card, title, preview, meta):
                    widget.bind("<Button-1>", lambda _event, value=identifier: self.selection_set(value, focus=True))
                    widget.bind("<MouseWheel>", self._wheel)
                card.bind("<Return>", lambda _event, value=identifier: self.selection_set(value, focus=True))
                card.bind("<space>", lambda _event, value=identifier: self.selection_set(value, focus=True))
                card.bind("<Up>", lambda _event, value=identifier: self._move(value, -1))
                card.bind("<Down>", lambda _event, value=identifier: self._move(value, 1))
                card.bind("<FocusIn>", lambda _event, value=identifier: self._style_card(value, True))
                card.bind("<FocusOut>", lambda _event, value=identifier: self._style_card(value, False))
            title, preview, meta = self._parts[identifier]
            chosen = identifier == selected
            signature = (row["title"], row["preview"], row["meta"], chosen)
            if self._row_signatures.get(identifier) != signature:
                focused = card.focus_get() is card
                card.configure(style=("Focused.SelectedCard.TFrame" if chosen and focused else
                                      "Focused.TaskCard.TFrame" if focused else
                                      "Selected.TaskCard.TFrame" if chosen else "TaskCard.TFrame"))
                title.configure(text=row["title"], style=("Selected.TaskCardTitle.TLabel"
                                if chosen else "TaskCardTitle.TLabel"))
                preview.configure(text=row["preview"], style=("Selected.TaskCardPreview.TLabel"
                                  if chosen else "TaskCardPreview.TLabel"))
                meta.configure(text=row["meta"], style=("Selected.TaskCardMeta.TLabel"
                               if chosen else "TaskCardMeta.TLabel"))
                self._row_signatures[identifier] = signature
            if self._row_positions.get(identifier) != index:
                card.grid(row=index, column=0, sticky="ew", pady=(0, 12))
                self._row_positions[identifier] = index
        self.inner.columnconfigure(0, weight=1)
        self._selected = selected if selected in wanted else None
        self._update_region()

    def get_children(self):
        return tuple(self._ids)

    def selection(self):
        return (self._selected,) if self._selected in self._ids else ()

    def _style_card(self, identifier, focused=False):
        card = self._buttons.get(identifier)
        if card is None:
            return
        selected = identifier == self._selected
        prefix = "Selected" if selected else "Task"
        card.configure(style=(f"Focused.{prefix}Card.TFrame" if focused else
                              "Selected.TaskCard.TFrame" if selected else "TaskCard.TFrame"))

    def _move(self, identifier, offset):
        try:
            target = self._ids[max(0, min(len(self._ids) - 1, self._ids.index(identifier) + offset))]
        except (ValueError, IndexError):
            return "break"
        self.selection_set(target, focus=True)
        return "break"

    def selection_set(self, identifier, focus=False):
        if identifier not in self._ids:
            return
        changed = self._selected != identifier
        self._selected = identifier
        for item, card in self._buttons.items():
            chosen = item == identifier
            card.configure(style="Selected.TaskCard.TFrame" if chosen else "TaskCard.TFrame")
            title, preview, meta = self._parts[item]
            title.configure(style="Selected.TaskCardTitle.TLabel" if chosen else "TaskCardTitle.TLabel")
            preview.configure(style="Selected.TaskCardPreview.TLabel" if chosen else "TaskCardPreview.TLabel")
            meta.configure(style="Selected.TaskCardMeta.TLabel" if chosen else "TaskCardMeta.TLabel")
            signature = self._row_signatures.get(item)
            if signature is not None:
                self._row_signatures[item] = (*signature[:3], chosen)
        if focus:
            self._buttons[identifier].focus_set()
            self._style_card(identifier, True)
        if changed:
            self.command(identifier)


class ConversationView(ttk.Frame):
    """Selectable read-only conversation using native Text tags."""

    def __init__(self, parent):
        super().__init__(parent, style="Surface.TFrame")
        self.text = tk.Text(self, wrap="word", state="disabled", relief="flat", borderwidth=0,
                            highlightthickness=0, padx=20, pady=16, font="TkTextFont")
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scrollbar.set)
        self.text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self._signature = None

    def configure_tags(self, colors):
        self.text.configure(background=colors["surface"], foreground=colors["ink"],
                            insertbackground=colors["ink"], selectbackground=colors["accent"])
        self.text.tag_configure("user_label", justify="right", foreground=colors["muted"],
                                spacing1=14, rmargin=18)
        self.text.tag_configure("user", justify="right", background=colors["select"],
                                foreground=colors["ink"], lmargin1=90, lmargin2=90, rmargin=18,
                                spacing1=3, spacing3=12)
        self.text.tag_configure("assistant_label", foreground=colors["muted"], spacing1=14,
                                lmargin1=6, lmargin2=6)
        self.text.tag_configure("assistant", foreground=colors["ink"], lmargin1=6, lmargin2=6,
                                rmargin=70, spacing1=3, spacing3=12)
        self.text.tag_configure("notice", foreground=colors["muted"], justify="center",
                                spacing1=8, spacing3=8)
        self.text.tag_configure("error", foreground="#8a4b08", justify="center",
                                spacing1=8, spacing3=8)

    def render(self, messages, notices=(), follow=False):
        signature = (tuple((message["role"], message["text"]) for message in messages), tuple(notices))
        if signature == self._signature:
            return False
        self._signature = signature
        view = self.text.yview()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for message in messages:
            role = message["role"]
            if role == "user":
                self.text.insert("end", "你\n", ("user_label",))
                self.text.insert("end", message["text"], ("user",))
                self.text.insert("end", "\n")
            else:
                self.text.insert("end", "C  Codex\n", ("assistant_label",))
                self.text.insert("end", message["text"], ("assistant",))
                self.text.insert("end", "\n")
        for text, kind in notices:
            if text:
                self.text.insert("end", text + "\n", (kind,))
        if not messages and not any(text for text, _ in notices):
            self.text.insert("end", "尚无可显示的已完成工作对话。", ("notice",))
        self.text.configure(state="disabled")
        if follow and view[1] >= 0.98:
            self.text.see("end")
        else:
            self.text.yview_moveto(view[0] if follow else 0)
        return True
