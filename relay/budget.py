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


def budget_message(task, status=None, automatic=False):
    status = budget_status(task) if status is None else status
    details = []
    if status["token_reached"]:
        details.append(f"Token 累计 {task.get('usage', 0)} / 上限 {task.get('max_tokens', 0)}")
    if status["minutes_reached"]:
        details.append(f"时间累计 {status['elapsed_seconds'] / 60:.2f} 分钟 / 上限 "
                       f"{task.get('max_minutes', 0):g} 分钟")
    message = "任务已达到" + ("和".join(
        [name for hit, name in ((status["token_reached"], "Token 预算"),
                                (status["minutes_reached"], "时间预算")) if hit]))
    if details:
        message += "：" + "；".join(details)
    if automatic:
        return message + "，已因预算请求暂停；停止是否完成请看任务状态。不会自动继续。请在电脑调整预算后再继续。"
    return message + "。请在电脑调整预算后再继续。"
