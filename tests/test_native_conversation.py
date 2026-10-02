import copy
import unittest

from relay.conversation import conversation_page


class ConversationTests(unittest.TestCase):
    def thread(self):
        return {"id": "source", "status": {"type": "notLoaded"}, "turns": [
            {"id": "turn", "status": "completed", "items": [
                {"id": "user", "type": "userMessage", "content": [
                    {"type": "text", "text": "  原话\r\n不要改写  "}, {"type": "image", "url": "private"}]},
                {"id": "answer", "type": "agentMessage", "text": ("中文🙂\r\n" * 4000) + "结束  ", "phase": "final_answer"}]}]}

    def test_pages_reconstruct_exact_original_without_trimming_or_dropped_history(self):
        source = self.thread()
        page, pages = conversation_page(source), []
        while True:
            pages.insert(0, page)
            if page["older_cursor"] is None:
                break
            page = conversation_page(source, page["older_cursor"])
        entries = [entry for page in pages for entry in page["entries"]]
        self.assertEqual(entries[0]["text"], source["turns"][0]["items"][0]["content"][0]["text"])
        self.assertEqual("".join(e["text"] for e in entries if e["role"] == "assistant"), source["turns"][0]["items"][1]["text"])
        self.assertTrue(all(sum(len(e["text"]) for e in p["entries"]) <= 6000 for p in pages))
        self.assertEqual(page["non_text_items"], 1)
        self.assertEqual(conversation_page(source, page["newer_cursor"])["page"], 2)

    def test_changed_source_bad_cursor_and_summary_fail_explicitly(self):
        source = self.thread()
        old = conversation_page(source)["older_cursor"]
        changed = copy.deepcopy(source)
        changed["turns"][0]["items"][1]["text"] += "changed"
        for cursor in (old, "!", "a" * 181):
            with self.assertRaises(ValueError):
                conversation_page(changed, cursor)
        source["turns"][0]["itemsView"] = "summary"
        with self.assertRaises(ValueError):
            conversation_page(source)
