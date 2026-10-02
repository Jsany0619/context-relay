"""Read-only pages of native conversation text, without summaries or substitutions."""
import base64
import hashlib
import json
from datetime import datetime, timezone


def conversation_page(thread, cursor=None):
    if not isinstance(thread, dict) or not isinstance(thread.get("turns"), list):
        raise ValueError("原聊天正文尚不可读取。")
    encoded = json.dumps(thread["turns"], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > 16 * 1024 * 1024:
        raise ValueError("原聊天超过本版 16 MiB 读取上限，请在 Codex 查看；未用摘要替代。")
    version = hashlib.sha256(encoded).hexdigest()
    pages, entries, size, non_text = [], [], 0, 0
    for turn in thread["turns"]:
        if turn.get("itemsView", "full") != "full" or not isinstance(turn.get("items"), list):
            raise ValueError("宿主只返回了摘要，无法提供原文；请在 Codex 查看。")
        for item in turn["items"]:
            kind = item.get("type")
            if kind == "userMessage":
                content = item.get("content")
                if not isinstance(content, list):
                    raise ValueError("原消息结构不完整。")
                texts = [part["text"] for part in content if part.get("type") == "text"]
                non_text += sum(part.get("type") != "text" for part in content)
            elif kind == "agentMessage":
                texts = [item.get("text")]
            else:
                non_text += 1
                continue
            for block, text in enumerate(texts):
                if not isinstance(text, str):
                    raise ValueError("原消息文字不完整。")
                parts = max(1, (len(text) + 5999) // 6000)
                for part in range(parts):
                    value = text[part * 6000:(part + 1) * 6000]
                    if entries and (size + len(value) > 6000 or len(entries) >= 8):
                        pages.append(entries)
                        entries, size = [], 0
                    entries.append({"role": "user" if kind == "userMessage" else "assistant",
                                    "text": value, "turn_id": turn.get("id"), "item_id": item.get("id"),
                                    "phase": item.get("phase"), "turn_status": turn.get("status"),
                                    "block": block, "part": part + 1, "parts": parts})
                    size += len(value)
    if entries or not pages:
        pages.append(entries)
    index = len(pages) - 1
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or len(cursor) > 180:
                raise ValueError()
            saved, index = json.loads(base64.urlsafe_b64decode(cursor + "==").decode("ascii"))
            if saved != version:
                raise ValueError()
            if type(index) is not int or not 0 <= index < len(pages):
                raise ValueError()
        except (ValueError, TypeError, UnicodeError) as error:
            raise ValueError("原聊天已变化或页码无效，请刷新到最新原文。") from error
    def token(page):
        return base64.urlsafe_b64encode(json.dumps([version, page]).encode("ascii")).decode("ascii").rstrip("=")
    return {"thread_id": thread.get("id"), "status": thread.get("status", {}).get("type", "unknown"),
            "checked_at": datetime.now(timezone.utc).isoformat(), "entries": pages[index],
            "older_cursor": token(index - 1) if index > 0 else None,
            "newer_cursor": token(index + 1) if index + 1 < len(pages) else None,
            "page": index + 1, "pages": len(pages), "non_text_items": non_text,
            "notice": "用户与 Codex 文字原文；附件、工具执行详情不在文字页中。状态来自读取宿主，不能证明另一客户端已停止。"}
