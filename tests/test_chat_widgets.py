"""Real native bubble geometry and preserved reading state; use an isolated display."""
import tkinter as tk
import time
import unittest
from unittest import mock
from tkinter import ttk

from relay.chat_widgets import ConversationView
from relay.ui import THEMES, apply_theme
from tests.test_ui import quiet_window


class BubbleTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        quiet_window(self.root, mapped=True)
        self.root.geometry("760x480")
        apply_theme(self.root, {"theme": "mint", "font_size": "standard", "density": "comfortable"})
        self.view = ConversationView(self.root)
        self.view.configure_tags(THEMES["mint"])
        self.view.pack(fill="both", expand=True)
        self.root.update()

    def tearDown(self):
        self.root.destroy()

    def test_content_width_alignment_and_verbatim_text(self):
        messages = [{"role": "assistant", "text": "简短回复"}, {"role": "user", "text": "  好的\n继续。  "}]
        self.view.render(messages)
        self.root.update()
        left, right = self.view.rows
        self.assertEqual([w.get("1.0", "end-1c") for w in self.view.message_widgets], [m["text"] for m in messages])
        self.assertLess(left["bubble"].winfo_width(), self.view.canvas.winfo_width() / 2)
        self.assertGreater(right["bubble"].winfo_rootx(), left["bubble"].winfo_rootx())
        self.assertEqual(right["bubble"].itemcget(right["shape"], "smooth"), "true")
        self.assertIsNotNone(right["text"].dlineinfo("end-1c"))

    def test_long_text_wraps_and_new_message_preserves_existing_selection(self):
        messages = [{"role": "assistant", "text": "中英文 long message，保留原文。" * 90}]
        durations = []
        original_layout = self.view._layout

        def timed_layout():
            started = time.monotonic()
            try:
                original_layout()
            finally:
                durations.append(time.monotonic() - started)

        with mock.patch.object(self.view, "_layout", side_effect=timed_layout) as layouts:
            self.view.render(messages)
            self.root.update()
        self.assertLessEqual(layouts.call_count, 3, "One message must settle without a geometry loop")
        self.assertLess(sum(durations), 1.0, durations)
        text = self.view.message_widgets[0]
        self.assertLess(self.view.rows[0]["bubble"].winfo_width(), self.view.canvas.winfo_width() * .81)
        self.assertIsNotNone(text.dlineinfo("end-1c"), "Final wrapped line must not be clipped inside the bubble")
        text.tag_add("sel", "1.0", "1.4")
        selected = tuple(map(str, text.tag_ranges("sel")))
        self.view.canvas.yview_moveto(.3)
        before = self.view.canvas.yview()
        self.assertFalse(self.view.render(messages, follow=True))
        self.root.update()
        self.assertEqual(before, self.view.canvas.yview())
        self.view.render(messages + [{"role": "user", "text": "继续"}], follow=True)
        self.root.update()
        self.assertIs(text, self.view.message_widgets[0])
        self.assertEqual(selected, tuple(map(str, text.tag_ranges("sel"))))

    def test_resize_theme_and_error_notice_keep_original_content(self):
        original = "  多行原文\n带空格和 emoji 🙂\n" * 5
        self.view.render([{"role": "user", "text": original}], [("原文读取失败", "error")])
        self.root.update()
        text = self.view.message_widgets[0]
        self.root.geometry("480x480")
        self.view.configure_tags(THEMES["blue"])
        self.root.update()
        self.assertEqual(text.get("1.0", "end-1c"), original)
        self.assertEqual(text.cget("background"), THEMES["blue"]["bubble_user"])
        self.assertEqual(self.view.notice_widgets[0].cget("text"), "原文读取失败")
        self.assertIsNotNone(text.dlineinfo("end-1c"))

    def test_wheel_over_scrollbar_scrolls_the_conversation(self):
        self.view.render([{"role": "assistant", "text": f"消息 {index}\n" * 5}
                          for index in range(20)])
        self.root.update()
        scrollbar = next(child for child in self.view.winfo_children() if isinstance(child, ttk.Scrollbar))
        self.view.canvas.yview_moveto(0)
        before = self.view.canvas.yview()
        scrollbar.event_generate("<MouseWheel>", delta=-120)
        self.root.update()
        self.assertGreater(self.view.canvas.yview()[0], before[0])

    def test_mixed_whitespace_tabs_and_unbroken_text_keep_the_final_line_visible(self):
        values = [
            "中文English连续排版 mixed words " * 24,
            "第一行\n\n   \n最后一行   ",
            "\t缩进\tvalue\tend",
            "A" * 1000,
        ]
        self.view.render([{"role": "assistant" if index % 2 == 0 else "user", "text": value}
                          for index, value in enumerate(values)])
        self.root.update()
        self.assertEqual([widget.get("1.0", "end-1c") for widget in self.view.message_widgets], values)
        for index, widget in enumerate(self.view.message_widgets):
            with self.subTest(case=index):
                self.assertIsNotNone(widget.dlineinfo("end-1c"),
                                     f"Final display line must remain inside bubble case {index}")


if __name__ == "__main__":
    unittest.main()
