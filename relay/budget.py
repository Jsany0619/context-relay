"""Shared validation and soft-budget accounting for the controller and UI."""
import math
import time


def validate_limits(max_tokens, max_minutes):
    if isinstance(max_tokens, str) and max_tokens.strip().isdecimal():
        max_tokens = int(max_tokens.strip())
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 0:
        raise ValueError("Token 上限必须为非负整数；0 表示不限。")
    if isinstance(max_minutes, bool):
        raise ValueError("分钟上限必须为有限非负数；0 表示不限。")
    try:
        minutes = float(max_minutes)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("分钟上限必须为有限非负数；0 表示不限。") from exc
    if not math.isfinite(minutes * 60) or minutes < 0:
        raise ValueError("分钟上限必须为有限非负数；0 表示不限。")
    return max_tokens, minutes


def budget_status(task, now=None):
    elapsed = task.get("elapsed_seconds", 0)
    if task.get("run_started") is not None:
        elapsed += max(0, (time.time() if now is None else now) - task["run_started"])
    token_reached = bool(task.get("max_tokens") and task.get("usage", 0) >= task["max_tokens"])
    minutes_reached = bool(task.get("max_minutes") and elapsed >= task["max_minutes"] * 60)
    return {"elapsed_seconds": elapsed, "token_reached": token_reached,
            "minutes_reached": minutes_reached, "reached": token_reached or minutes_reached}
