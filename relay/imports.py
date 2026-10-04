"""Validate Codex thread reads and build bounded external-reference snapshots."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any


MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 64 * 1024
MAX_MESSAGE_CHARS = 3000
MAX_RECENT_MESSAGES = 20
MAX_TITLE_CHARS = 500

_THREAD_STATUSES = {"notLoaded", "idle", "systemError", "active"}
_TURN_STATUSES = {"completed", "interrupted", "failed", "inProgress"}
_ACTION_TERMINAL = {
    "commandExecution": {"completed", "failed", "declined"},
    "fileChange": {"completed", "failed", "declined"},
    "mcpToolCall": {"completed", "failed"},
    "dynamicToolCall": {"completed", "failed"},
    "collabAgentToolCall": {"completed", "failed", "interrupted"},
    "imageGeneration": {"completed", "failed"},
}


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("thread data is not valid JSON") from error


def _require_text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"thread {field} must be a non-empty string")
    return value


def _warning(warnings: list[str], value: str) -> None:
    if value not in warnings:
        warnings.append(value)


def _snapshot_size(snapshot: dict[str, Any]) -> int:
    return len(json.dumps(snapshot, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def _message(role: str, text: str, turn_id: str, item_id: str) -> dict[str, str]:
    return {"role": role, "text": text, "turn_id": turn_id, "item_id": item_id}


def normalize_thread(thread: Any, expected_id: str | None = None, *,
                     include_messages: bool = True, managed_owner: bool = False) -> dict[str, Any]:
    """Return a bounded, inert snapshot from a v2 ``thread/read`` Thread object."""
    if not isinstance(thread, dict):
        raise ValueError("thread must be an object")
    if len(_canonical(thread)) > MAX_INPUT_BYTES:
        raise ValueError("thread data is too large to import")

    thread_id = _require_text(thread.get("id"), "id")
    if expected_id is not None and thread_id != expected_id:
        raise ValueError("thread id does not match the requested thread")
    cwd = _require_text(thread.get("cwd"), "cwd")
    if not Path(cwd).is_absolute():
        raise ValueError("thread cwd must be absolute")
    updated_at = thread.get("updatedAt")
    if not isinstance(updated_at, int) or isinstance(updated_at, bool) or updated_at < 0:
        raise ValueError("thread updatedAt must be a non-negative integer")
    source = thread.get("source")
    if source not in (("vscode", "cli", "exec", "appServer") if managed_owner else ("vscode",)):
        raise ValueError("only vscode-source Codex threads can be imported")
    status_value = thread.get("status")
    if not isinstance(status_value, dict) or status_value.get("type") not in _THREAD_STATUSES:
        raise ValueError("thread status is invalid")
    status = status_value["type"]

    warnings: list[str] = []
    if status == "notLoaded":
        _warning(warnings, "cross_client_activity_unknown")
        _warning(warnings, "stop_original_thread_before_import")
    elif status == "active":
        _warning(warnings, "source_thread_active")
    elif status == "systemError":
        _warning(warnings, "source_thread_system_error")

    raw_title = thread.get("name")
    if not isinstance(raw_title, str) or not raw_title.strip():
        raw_title = thread.get("preview")
    if not isinstance(raw_title, str) or not raw_title.strip():
        raw_title = "Imported Codex thread"
    title = raw_title.strip()
    if len(title) > MAX_TITLE_CHARS:
        title = title[:MAX_TITLE_CHARS]
        _warning(warnings, "title_truncated")

    turns_for_fingerprint = thread.get("turns", [])
    fingerprint_value = {
        "id": thread_id,
        "name": thread.get("name"),
        "preview": thread.get("preview"),
        "cwd": cwd,
        "source": source,
        "updatedAt": updated_at,
        "turns": turns_for_fingerprint,
    }
    fingerprint = hashlib.sha256(_canonical(fingerprint_value)).hexdigest()
    snapshot: dict[str, Any] = {
        "kind": "external-reference",
        "thread_id": thread_id,
        "title": title,
        "cwd": cwd,
        "updated_at": updated_at,
        "status": status,
        "source": source,
        "fingerprint": fingerprint,
        "read_at": datetime.now(timezone.utc).isoformat(),
        "messages": [],
        "omitted_messages": 0,
        "truncated_messages": 0,
        "non_text_items": 0,
        "can_import": False,
        "warnings": warnings,
    }
    if not include_messages:
        _warning(warnings, "metadata_only")
        if _snapshot_size(snapshot) > MAX_SNAPSHOT_BYTES:
            raise ValueError("normalized thread metadata is too large")
        return snapshot

    turns = thread.get("turns")
    if not isinstance(turns, list):
        raise ValueError("thread turns must be an array")
    extracted: list[dict[str, str]] = []
    unfinished_action = False
    incomplete_items = False
    agent_states: dict[str, str | None] = {}
    turn_statuses: list[str] = []
    for turn in turns:
        if not isinstance(turn, dict):
            raise ValueError("thread turn must be an object")
        turn_id = _require_text(turn.get("id"), "turn id")
        turn_status = turn.get("status")
        if not isinstance(turn_status, str):
            raise ValueError("thread turn status must be a string")
        turn_statuses.append(turn_status)
        if turn_status not in _TURN_STATUSES:
            incomplete_items = True
        items_view = turn.get("itemsView", "full")
        if items_view != "full":
            incomplete_items = True
        items = turn.get("items")
        if not isinstance(items, list):
            raise ValueError("thread turn items must be an array")
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("thread item must be an object")
            item_type = _require_text(item.get("type"), "item type")
            item_id = _require_text(item.get("id"), "item id")
            if item_type == "userMessage":
                content = item.get("content")
                if not isinstance(content, list):
                    raise ValueError("user message content must be an array")
                text_parts = []
                for part in content:
                    if not isinstance(part, dict) or not isinstance(part.get("type"), str):
                        raise ValueError("user message content is invalid")
                    if part["type"] == "text":
                        text_parts.append(_require_text(part.get("text"), "message text", allow_empty=True))
                    else:
                        snapshot["non_text_items"] += 1
                if text_parts:
                    extracted.append(_message("user", "\n".join(text_parts), turn_id, item_id))
                continue
            if item_type == "agentMessage":
                text = _require_text(item.get("text"), "message text", allow_empty=True)
                extracted.append(_message("assistant", text, turn_id, item_id))
                continue
            snapshot["non_text_items"] += 1
            if item_type in _ACTION_TERMINAL:
                if item.get("status") not in _ACTION_TERMINAL[item_type]:
                    unfinished_action = True
            elif "status" in item:
                unfinished_action = True
            if item_type == "collabAgentToolCall":
                states = item.get("agentsStates")
                receivers = item.get("receiverThreadIds")
                if not isinstance(states, dict) or not isinstance(receivers, list):
                    raise ValueError("collab agent state is invalid")
                for agent_id, state in states.items():
                    if (not isinstance(agent_id, str) or not agent_id or not isinstance(state, dict)
                            or not isinstance(state.get("status"), str)):
                        raise ValueError("collab agent state is invalid")
                    agent_states[agent_id] = state["status"]
                for agent_id in receivers:
                    if not isinstance(agent_id, str) or not agent_id:
                        raise ValueError("collab receiver id is invalid")
                    if agent_id not in states:
                        agent_states[agent_id] = None
            elif item_type == "subAgentActivity":
                agent_id = _require_text(item.get("agentThreadId"), "subagent thread id")
                activity = item.get("kind")
                if activity in {"started", "interacted"}:
                    agent_states[agent_id] = "running"
                elif activity in {"interrupted", "completed"}:
                    agent_states[agent_id] = activity
                else:
                    agent_states[agent_id] = None

    if not turns:
        _warning(warnings, "no_completed_turn")
    elif turn_statuses[-1] != "completed":
        _warning(warnings, "latest_turn_not_completed")
    if any(value in {"failed", "interrupted"} for value in turn_statuses[:-1]):
        _warning(warnings, "history_contains_incomplete_turns")
    if any(value == "inProgress" for value in turn_statuses):
        _warning(warnings, "unfinished_turn")
    if incomplete_items:
        _warning(warnings, "incomplete_or_unknown_history")
    if unfinished_action:
        _warning(warnings, "unfinished_action")
    unsettled_agent = False
    if any(value in {"pendingInit", "running"} for value in agent_states.values()):
        unsettled_agent = True
        _warning(warnings, "active_subagent")
    if any(value is None or value == "notFound" or value not in {
            "pendingInit", "running", "interrupted", "completed", "errored", "shutdown", "notFound"
    } for value in agent_states.values()):
        unsettled_agent = True
        _warning(warnings, "subagent_activity_unknown")
    if any(value in {"interrupted", "errored", "shutdown"} for value in agent_states.values()):
        _warning(warnings, "subagent_incomplete")

    first_user = next((index for index, value in enumerate(extracted) if value["role"] == "user"), None)
    selected_indexes = set(range(max(0, len(extracted) - MAX_RECENT_MESSAGES), len(extracted)))
    if first_user is not None:
        selected_indexes.add(first_user)
    selected = [extracted[index] for index in sorted(selected_indexes)]
    snapshot["omitted_messages"] = len(extracted) - len(selected)

    def with_limit(limit: int) -> list[dict[str, str]]:
        return [{**value, "text": value["text"][:limit]} for value in selected]

    low, high = 0, MAX_MESSAGE_CHARS
    while low < high:
        middle = (low + high + 1) // 2
        snapshot["messages"] = with_limit(middle)
        snapshot["truncated_messages"] = sum(len(value["text"]) > middle for value in selected)
        if _snapshot_size(snapshot) <= MAX_SNAPSHOT_BYTES:
            low = middle
        else:
            high = middle - 1
    snapshot["messages"] = with_limit(low)
    snapshot["truncated_messages"] = sum(len(value["text"]) > low for value in selected)
    if _snapshot_size(snapshot) > MAX_SNAPSHOT_BYTES:
        raise ValueError("normalized thread metadata is too large")

    snapshot["can_import"] = (
        status in {"idle", "notLoaded"}
        and bool(turns)
        and turn_statuses[-1] == "completed"
        and not any(value == "inProgress" for value in turn_statuses)
        and not incomplete_items
        and not unfinished_action
        and not unsettled_agent
    )
    return snapshot
