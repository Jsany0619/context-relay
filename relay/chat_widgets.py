"""Small native Tk widgets for the task rail and selectable conversation text."""
import re
import tkinter as tk
from tkinter import font as tkfont, ttk


def _scroll_once(widget, steps):
    try:
        first, last = map(float, widget.yview())
        if last - first >= .999 or (steps < 0 and first <= 0) or (steps > 0 and last >= 1):
            return False
        before = (first, last)
        widget.yview_scroll(steps, "units")
        return tuple(map(float, widget.yview())) != before
    except (AttributeError, tk.TclError, TypeError):
        return False


def bind_mousewheel_tree(region, target):
    """Route vertical wheel input inside one widget tree; never installs a global binding."""
    def wheel(event):
        if getattr(event, "state", 0) & 0x0001:  # Keep native Shift+wheel horizontal behavior.
            return None
        delta = int(getattr(event, "delta", 0))
        if not delta:
            return None
        steps = (-1 if delta > 0 else 1) * max(1, abs(delta) // 120)
        candidate = None
        widget = event.widget
        while widget is not None:
            assigned = getattr(widget, "_context_relay_wheel_target", None)
            if assigned is not None:
                candidate = assigned
                break
            if isinstance(widget, ttk.Combobox):
                break
            if widget is target or isinstance(widget, (tk.Text, tk.Listbox, ttk.Treeview)):
                candidate = widget
                break
            if widget is region:
                break
            widget = getattr(widget, "master", None)
        fallback = getattr(event.widget, "_context_relay_wheel_fallback", target)
        if candidate is not None and _scroll_once(candidate, steps):
            return "break"
        _scroll_once(fallback, steps)
        return "break"

    pending = [region]
    while pending:
        widget = pending.pop()
        widget._context_relay_wheel_fallback = target
        widget.bind("<MouseWheel>", wheel)
        pending.extend(widget.winfo_children())


def _wrapped_line_count(value, font, width):
    """Measure word wrapping without forcing Tk to rebuild Text display lines."""
    width = max(1, width)
    glyph_widths = {}
    lines = 0
    for paragraph in value.split("\n"):
        lines += 1
        used = 0
        for chunk in re.findall(r"\S+\s*|\s+", paragraph):
            # Tk tab stops are based on average glyph width, while Segoe UI
            # spaces are much narrower. Eight wide glyphs safely bound a tab.
            chunk = chunk.replace("\t", "M" * 8)
            chunk_width = font.measure(chunk)
            if chunk_width <= width:
                if used and used + chunk_width > width:
                    lines += 1
                    used = 0
                used += chunk_width
                continue
            for character in chunk:
                character_width = glyph_widths.get(character)
                if character_width is None:
                    character_width = glyph_widths[character] = font.measure(character)
                if used and used + character_width > width:
                    lines += 1
                    used = 0
                used += character_width
    return max(1, lines)


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
        self.scrollbar._context_relay_wheel_target = self.canvas
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self._canvas_width = 0
        self._scroll_region = None
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.inner.bind("<Configure>", self._update_region)
        self.canvas.bind("<Configure>", self._resize_inner)
        bind_mousewheel_tree(self, self.canvas)

    def _update_region(self, _event=None):
        region = self.canvas.bbox("all")
        if region != self._scroll_region:
            self._scroll_region = region
            self.canvas.configure(scrollregion=region)

    def _resize_inner(self, event):
        if event.width != self._canvas_width:
            self._canvas_width = event.width
            self.canvas.itemconfigure(self.window, width=event.width)

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
                bind_mousewheel_tree(card, self.canvas)
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
    """Scrollable, selectable native messages in content-sized rounded bubbles."""

    def __init__(self, parent):
        super().__init__(parent, style="Surface.TFrame")
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        self.inner = tk.Frame(self.canvas)
        self.window = self.canvas.create_window(0, 0, anchor="nw", window=self.inner)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.scrollbar._context_relay_wheel_target = self.canvas
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.rows = []
        self.message_widgets = []
        self.notice_widgets = []
        self._messages = ()
        self._signature = None
        self._width = 0
        self._layout_after = None
        self._scroll_target = None
        self._colors = None
        self.context_menu = tk.Menu(self, tearoff=False)
        self.canvas.bind("<Configure>", self._resize)
        self.inner.bind("<Configure>", self._region)
        bind_mousewheel_tree(self, self.canvas)

    def configure_tags(self, colors):
        self._colors = colors
        self.canvas.configure(background=colors["surface"])
        self.inner.configure(background=colors["surface"])
        for row in self.rows:
            self._style(row)
        for label in self.notice_widgets:
            label.configure(background=colors["surface"], foreground=("#8a4b08" if label.notice_kind == "error" else colors["muted"]))
        self._schedule_layout()

    def _style(self, row):
        colors = self._colors
        fill = colors["bubble_user" if row["role"] == "user" else "bubble_assistant"]
        row["frame"].configure(background=colors["surface"])
        row["label"].configure(background=colors["surface"], foreground=colors["muted"])
        row["bubble"].configure(background=colors["surface"])
        row["bubble"].itemconfigure(row["shape"], fill=fill)
        row["text"].configure(background=fill, foreground=colors["ink"], font="TkTextFont",
                              selectbackground=colors["accent"], selectforeground=colors["surface"])

    def _resize(self, event):
        if event.width != self._width:
            self._width = event.width
            self.canvas.itemconfigure(self.window, width=event.width)
            self._schedule_layout()

    def _schedule_layout(self):
        if self._layout_after is None:
            self._layout_after = self.after_idle(self._layout)

    def _region(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        if self._scroll_target is not None:
            self.canvas.yview_moveto(self._scroll_target)
            self._scroll_target = None

    def _layout(self):
        self._layout_after = None
        if not self._colors or self._width <= 1:
            return
        scale = self.winfo_fpixels("1i") / 96
        pad, radius, margin = round(12 * scale), round(14 * scale), round(18 * scale)
        available = max(1, self._width - 2 * margin)
        maximum = max(1, min(round(640 * scale), int(available * .8)))
        font = tkfont.nametofont("TkTextFont", root=self)
        font_key = (font.cget("family"), font.cget("size"), font.cget("weight"), font.metrics("linespace"))
        for row in self.rows:
            text = row["text"]
            natural = max((font.measure(line.replace("\t", "M" * 8))
                           for line in row["value"].split("\n")), default=0)
            width = min(maximum, max(2 * radius, natural + 2 * pad + 4))
            inner_width = max(1, width - 2 * pad)
            layout_key = (width, inner_width, pad, radius, margin, font_key)
            if row.get("layout_key") == layout_key:
                continue
            row["layout_key"] = layout_key
            row["frame"].pack_configure(padx=margin, pady=(round(10 * scale), round(6 * scale)))
            row["bubble"].itemconfigure(row["window"], width=inner_width)
            lines = _wrapped_line_count(row["value"], font, inner_width)
            height = max(2 * radius, lines * font.metrics("linespace") + 2 * pad)
            row["bubble"].configure(width=width, height=height)
            row["bubble"].coords(row["window"], pad, pad)
            row["bubble"].itemconfigure(row["window"], height=max(1, height - 2 * pad))
            r = min(radius, width / 2, height / 2)
            row["bubble"].coords(row["shape"], r, 0, width-r, 0, width, 0, width, r,
                                   width, height-r, width, height, width-r, height, r, height,
                                   0, height, 0, height-r, 0, r, 0, 0)
        for label in self.notice_widgets:
            label.configure(wraplength=available)
        self._region()

    def _copy(self, value):
        self.clipboard_clear()
        self.clipboard_append(value)

    def _menu(self, event, value):
        menu = self.context_menu
        menu.delete(0, "end")
        menu.add_command(label="复制此条原文", command=lambda: self._copy(value))
        menu.add_command(label="复制整段对话", command=lambda: self._copy("\n\n".join(
            ("你" if role == "user" else "Codex") + "\n" + content for role, content in self._messages)))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _add_message(self, role, value):
        frame = tk.Frame(self.inner)
        frame.pack(fill="x")
        label = tk.Label(frame, text="你" if role == "user" else "Codex", font="TkMenuFont", anchor="e" if role == "user" else "w")
        label.pack(fill="x", pady=(0, 4))
        bubble = tk.Canvas(frame, borderwidth=0, highlightthickness=0)
        bubble.pack(anchor="e" if role == "user" else "w")
        shape = bubble.create_polygon(0, 0, 0, 0, 0, 0, smooth=True, splinesteps=24, outline="")
        text = tk.Text(bubble, width=1, height=1, wrap="word", relief="flat", borderwidth=0,
                       highlightthickness=0, padx=0, pady=0, font="TkTextFont", takefocus=True,
                       exportselection=False)
        text.insert("1.0", value)
        text.configure(state="disabled")
        text._context_relay_wheel_target = self.canvas
        window = bubble.create_window(0, 0, window=text, anchor="nw")
        row = dict(frame=frame, label=label, bubble=bubble, shape=shape, text=text, window=window, role=role, value=value)
        for widget in (frame, label, bubble, text):
            widget.bind("<Button-3>", lambda event, original=value: self._menu(event, original))
        text.bind("<Control-a>", lambda _event: (text.tag_add("sel", "1.0", "end-1c"), "break")[-1])
        text.bind("<Configure>", lambda _event: self._schedule_layout())
        text.bind("<FocusIn>", lambda _event: bubble.itemconfigure(shape, outline=self._colors["accent"]))
        text.bind("<FocusOut>", lambda _event: bubble.itemconfigure(shape, outline=""))
        self.rows.append(row)
        self.message_widgets.append(text)
        self._style(row)
        bind_mousewheel_tree(frame, self.canvas)

    def render(self, messages, notices=(), follow=False):
        signature = (tuple((message["role"], message["text"]) for message in messages), tuple(notices))
        if signature == self._signature:
            return False
        view = self.canvas.yview()
        self._scroll_target = (1.0 if view[1] >= .98 else view[0]) if follow else 0.0
        self._signature = signature
        common = 0
        for old, new in zip(self._messages, signature[0]):
            if old != new:
                break
            common += 1
        for row in self.rows[common:]:
            row["frame"].destroy()
        del self.rows[common:]
        del self.message_widgets[common:]
        for label in self.notice_widgets:
            label.destroy()
        self.notice_widgets.clear()
        self._messages = signature[0]
        for role, value in self._messages[common:]:
            self._add_message(role, value)
        visible_notices = [(value, kind) for value, kind in notices if value]
        if not messages and not visible_notices:
            visible_notices = [("尚无可显示的已完成工作对话。", "notice")]
        for value, kind in visible_notices:
            label = tk.Label(self.inner, text=value, justify="left", font="TkMenuFont",
                             background=self._colors["surface"], foreground="#8a4b08" if kind == "error" else self._colors["muted"])
            label.notice_kind = kind
            label.pack(fill="x", padx=18, pady=10)
            bind_mousewheel_tree(label, self.canvas)
            self.notice_widgets.append(label)
        self._schedule_layout()
        return True

    def destroy(self):
        if self._layout_after is not None:
            self.after_cancel(self._layout_after)
        super().destroy()
