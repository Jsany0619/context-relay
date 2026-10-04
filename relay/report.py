"""Render a private local task snapshot without I/O or native chat access."""
import math
import re


def _literal(value):
    """Keep external text inert, including embedded Markdown fences and HTML."""
    fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", value)), default=0))
    return fence + "text\n" + value + "\n" + fence


def _field(parts, label, value):
    if isinstance(value, str) and value:
        parts.extend(["### " + label, _literal(value)])
    elif isinstance(value, list):
        values = [item for item in value if isinstance(item, str) and item]
        if values:
            parts.append("### " + label)
            parts.extend(_literal(item) for item in values)


def _number(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return value >= 0
    return isinstance(value, float) and math.isfinite(value) and value >= 0


def _limit(value):
    if not _number(value):
        return "未记录"
    return "不限" if value == 0 else str(value)


def _records(parts, label, records, fields):
    if not isinstance(records, list):
        return
    for index, record in enumerate(records, 1):
        if isinstance(record, dict):
            parts.append("### " + label + " " + str(index))
            for key, name in fields:
                _field(parts, name, record.get(key))
            for evidence in record.get("evidence") or []:
                if isinstance(evidence, dict):
                    for key, name in (("source", "证据来源类型"), ("reference", "证据引用"), ("finding", "证据观察")):
                        _field(parts, name, evidence.get(key))


def _messages(parts, task):
    parts.append("## 本地工作消息")
    parts.append("本地回复记录不证明项目完成、工具执行成功或原生聊天完整。用户输入记录不证明原生端已经接收。")
    replies = []
    for message in task.get("messages") or []:
        if not isinstance(message, dict) or message.get("purpose") != "work":
            continue
        role, text = message.get("role"), message.get("text")
        if role not in ("user", "assistant") or not isinstance(text, str) or not text:
            continue
        if role == "assistant" and message.get("status") != "completed":
            continue
        label = "用户输入" if role == "user" else "工作回复"
        parts.append("### " + label + ("（历史摘录）" if message.get("historical") else ""))
        _field(parts, "记录时间", message.get("created_at"))
        if message.get("truncated"):
            parts.append("此条记录已截断。")
        parts.append(_literal(text))
        if role == "assistant":
            replies.append(text)
    kind = task.get("last_message_kind") or task.get("purpose")
    latest = task.get("last_message")
    if kind == "work" and isinstance(latest, str) and latest and latest not in replies:
        parts.extend(["### 最新工作回复（本地记录，不保证本轮已终止）", _literal(latest)])
        replies.append(latest)
    if not replies:
        parts.append("没有可识别的本地工作回复记录；不能据此判断原生端从未产生结果。")
    if task.get("messages_truncated"):
        parts.append("本地消息记录存在裁剪或截断，不能据此还原全部对话。")


def _assessment(parts, task, kind):
    saved = task.get(kind)
    if not isinstance(saved, dict) or not isinstance(saved.get("report"), dict):
        return
    report = saved["report"]
    parts.append("## 简报候选与采用记录" if kind == "brief" else "## AI 审查意见")
    parts.append("绑定状态：" + {"current": "上次绑定检查可用；导出时未重新核验",
                               "stale": "已过期"}.get(saved.get("status"), "未记录"))
    if kind == "brief":
        parts.append("简报采用：" + {"pending": "尚未采用", "adopted": "已记录采用"}.get(
            saved.get("decision"), "未记录"))
        fields = (("goal", "候选目标"), ("decisions", "已存决策文字"), ("completed", "已存完成事项文字"),
                  ("unknowns", "未知项"), ("approaches", "候选路线"), ("acceptance", "候选验收要求"),
                  ("next_step", "候选下一步"))
        _field(parts, "采用后的目标", saved.get("adopted_goal"))
        _field(parts, "采用后的验收要求", saved.get("adopted_acceptance"))
    else:
        parts.extend(["自动化验收：未独立验证；已有测试文字仅作参考。",
                      "人工阶段采用：" + {"accepted": "已记录", "pending": "尚未认可"}.get(
                          saved.get("human_acceptance"), "未记录"),
                      "人工采用不等于测试通过或正式发布；本报告不提供发布验收证明。",
                      "审核处理：" + {"pending": "待处理", "accepted": "已记录采用", "revise": "已记录返工选择"}.get(
                          saved.get("decision"), "未记录")])
        fields = (("verdict", "模型判断原值"), ("summary", "审核摘要"), ("unknowns", "未知项"),
                  ("test_evidence", "模型引用的测试文字"))
        _records(parts, "发现", report.get("findings"), (("severity", "级别"), ("category", "类别"),
                 ("finding", "观察"), ("suggestion", "建议"), ("evidence", "证据文字")))
        _records(parts, "检查意见", report.get("checks"), (("category", "类别"), ("result", "模型记录结果"),
                 ("finding", "观察"), ("evidence", "证据文字")))
    for key, label in fields:
        _field(parts, label, report.get(key))
    _records(parts, "引用证据", report.get("evidence"), (("source", "来源类型"),
             ("reference", "来源引用"), ("finding", "观察")))


def render_task_report(task: dict) -> str:
    """Return deterministic Markdown; this is neither a backup nor a redactor."""
    parts = ["# Context Relay 本地任务报告",
             "私人资料，未脱敏。仅为本地任务记录快照，不是完整原生聊天；内容可能是历史摘录或已经截断。",
             "导出不会重新核验项目、运行测试或确认外部操作成功。"]
    if task.get("inspection_only") is True:
        parts.append("**备份历史快照，只读检视；不代表当前执行状态或权限归属。**")
    _field(parts, "任务名称", task.get("title"))
    _field(parts, "任务目标", task.get("goal"))
    states = {"queued": "等待启动", "idle": "本轮结束，任务尚未标记完成", "paused": "已暂停",
              "running": "运行中", "pausing": "正在请求暂停", "briefing": "正在整理简报",
              "reviewing": "正在审核", "summarizing": "正在整理交接", "verifying": "正在核验",
              "needs_reconcile": "结果待核对", "blocked": "需要处理", "completed": "已标记完成"}
    parts.extend(["## 已记录状态",
                  "任务状态：" + states.get(task.get("state"), "未记录或无法识别"),
                  "任务权限：" + {"read-only": "只读", "workspace-write": "允许工作区写入"}.get(
                      task.get("mode"), "未记录或无法识别")])
    usage, elapsed = task.get("usage"), task.get("elapsed_seconds")
    parts.append("累计 Token：" + (str(usage) if _number(usage) else "未记录")
                 + " / " + _limit(task.get("max_tokens")))
    parts.append("已记录用时：" + (format(elapsed / 60, ".2f") + " 分钟" if _number(elapsed) else "未记录")
                 + " / " + _limit(task.get("max_minutes"))
                 + (" 分钟" if _number(task.get("max_minutes")) and task["max_minutes"] else ""))
    parts.append("累计用量不是当前上下文占用；已记录用时包含活动轮次等待，不是模型推理耗时。")
    if task.get("run_started") is not None:
        parts.append("当前活动区间可能尚未结算；本报告不估算导出时的新增用量。")
    _field(parts, "已记录要求", task.get("requirements"))
    _field(parts, "验收要求", task.get("acceptance_criteria"))
    _field(parts, "已记录错误或提示", task.get("error"))
    _messages(parts, task)
    _assessment(parts, task, "brief")
    _assessment(parts, task, "review")
    _field(parts, "已记录下一步", task.get("next_step"))
    _field(parts, "已记录未知项", task.get("unknowns"))
    source = task.get("source_snapshot")
    if isinstance(source, dict):
        parts.extend(["## 历史来源索引", "仅列本地来源记录；历史文字不继承旧授权，也不证明源聊天当前状态。"])
        for key, label in (("thread_id", "来源聊天标识"), ("title", "来源名称"),
                           ("cwd", "来源目录"), ("read_at", "读取时间")):
            _field(parts, label, source.get(key))
        for key, label in (("omitted_messages", "遗漏消息"), ("truncated_messages", "截断消息"),
                           ("non_text_items", "非文字项目")):
            count = source.get(key)
            parts.append(label + "：" + (str(count) if isinstance(count, int) and not isinstance(count, bool) else "未记录"))
        parts.append("未展开源快照的原始消息、工具详情或附件；以上缺口不会由本报告补全。")
    return "\n\n".join(parts) + "\n"
