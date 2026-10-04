"""Pure local report rendering; fixtures contain no native or private task data."""
import copy
import unittest

from relay.report import render_task_report


class TaskReportTests(unittest.TestCase):
    def test_private_snapshot_preserves_literal_text_without_interpreting_markdown(self):
        task = {"title": "示例 <script>alert(1)</script>",
                "goal": "  中文目标\n```html\n<img src='private'>\n```\n````\n尾部  ",
                "state": "idle", "mode": "read-only", "usage": 120,
                "elapsed_seconds": 90, "max_tokens": 1000, "max_minutes": 5}
        before = copy.deepcopy(task)
        report = render_task_report(task)
        self.assertEqual(task, before)
        self.assertEqual(report, render_task_report(task))
        self.assertIn("私人资料，未脱敏", report)
        self.assertIn("不是完整原生聊天", report)
        self.assertIn("`````text\n" + task["goal"] + "\n`````", report)
        self.assertIn("```text\n" + task["title"] + "\n```", report)
        self.assertIn("累计 Token：120 / 1000", report)
        self.assertIn("已记录用时：1.50 分钟 / 5 分钟", report)
        self.assertIn("只读", report)

    def test_only_work_messages_are_results_and_work_json_stays_literal(self):
        body = '  {"中文": "工作结果"}\n```python\nprint(1)\n```  '
        task = {"messages": [
            {"role": "assistant", "purpose": "brief", "status": "completed", "text": "INTERNAL_CANDIDATE"},
            {"role": "assistant", "purpose": "work", "status": "running", "text": "UNFINISHED_MESSAGE"},
            {"role": "tool", "purpose": "work", "status": "completed", "text": "TOOL_PAYLOAD"},
            {"role": "user", "purpose": "work", "status": "completed", "text": "原始输入", "historical": True},
            {"role": "assistant", "purpose": "work", "status": "completed", "text": body, "truncated": True}],
            "last_message": "INTERNAL_LAST_JSON", "last_message_kind": "review", "messages_truncated": True}
        report = render_task_report(task)
        self.assertIn(body, report)
        self.assertIn("原始输入", report)
        self.assertIn("历史摘录", report)
        self.assertIn("此条记录已截断", report)
        for hidden in ("INTERNAL_CANDIDATE", "UNFINISHED_MESSAGE", "TOOL_PAYLOAD", "INTERNAL_LAST_JSON"):
            self.assertNotIn(hidden, report)
        report = render_task_report({"last_message": body, "last_message_kind": "work"})
        self.assertIn(body, report)
        self.assertNotIn(body, render_task_report({"last_message": body}))

    def test_requirements_assessments_and_acceptance_levels_remain_separate(self):
        task = {"requirements": ["不可发布"], "acceptance_criteria": ["人工检查输出"],
                "brief": {"status": "current", "decision": "adopted", "adopted_goal": "采用后的目标",
                          "report": {"goal": "候选目标", "decisions": ["保留原文件"],
                                     "completed": ["曾核对目录"], "unknowns": ["授权待核对"],
                                     "approaches": ["候选路线"], "next_step": "先检查结果",
                                     "evidence": [{"source": "chat", "reference": "synthetic:1", "finding": "本地证据"}]}},
                "review": {"status": "stale", "decision": "accepted", "human_acceptance": "accepted",
                           "machine_checks": "not_verified", "report": {
                               "verdict": "ready_for_user", "summary": "候选可供人工查看",
                               "findings": [{"severity": "low", "category": "quality", "finding": "需看实物",
                                             "suggestion": "人工复核", "evidence": [
                                                 {"source": "file", "reference": "sample.txt", "finding": "模拟观察"}]}],
                               "checks": [{"category": "tests", "result": "claimed", "finding": "模型提及旧测试"}],
                               "unknowns": ["发布未核验"], "test_evidence": ["旧测试文字，不是独立回执"]}}}
        report = render_task_report(task)
        for expected in ("不可发布", "人工检查输出", "采用后的目标", "先检查结果", "授权待核对",
                         "本地证据", "需看实物", "人工复核", "sample.txt", "模拟观察", "模型提及旧测试", "发布未核验"):
            self.assertIn(expected, report)
        self.assertIn("导出时未重新核验", report)
        self.assertIn("已过期", report)
        self.assertIn("AI 审查意见", report)
        self.assertIn("自动化验收：未独立验证", report)
        self.assertIn("人工阶段采用：已记录", report)
        self.assertIn("人工采用不等于测试通过或正式发布", report)

    def test_inspection_snapshot_is_not_live_and_does_not_expand_source_or_private_keys(self):
        task = {"inspection_only": True, "run_started": 1, "elapsed_seconds": 30,
                "source_snapshot": {"thread_id": "synthetic-source", "title": "来源示例", "read_at": "示例时间",
                                    "omitted_messages": 5, "truncated_messages": 2, "non_text_items": 3,
                                    "messages": [{"text": "RAW_SOURCE_NOT_SELECTED"}]},
                "events": [{"data": "RAW_EVENT_NOT_SELECTED"}], "token": "UNSELECTED_SECRET",
                "brief": {"binding": {"files": "INTERNAL_BINDING"}, "report": {"goal": "已存候选"}}}
        report = render_task_report(task)
        self.assertIn("备份历史快照，只读检视", report)
        self.assertIn("不代表当前执行状态或权限归属", report)
        self.assertIn("synthetic-source", report)
        self.assertIn("遗漏消息：5", report)
        self.assertIn("非文字项目：3", report)
        self.assertIn("不估算", report)
        for hidden in ("RAW_SOURCE_NOT_SELECTED", "RAW_EVENT_NOT_SELECTED", "UNSELECTED_SECRET", "INTERNAL_BINDING"):
            self.assertNotIn(hidden, report)

    def test_missing_records_do_not_invent_work_results_or_verification(self):
        report = render_task_report({"last_message_kind": "brief", "last_message": '{"goal":"内部候选"}'})
        self.assertNotIn("内部候选", report)
        self.assertIn("没有可识别的本地工作回复记录", report)
        self.assertIn("未记录", report)
        self.assertNotIn("已通过", report)

    def test_usage_totals_keep_full_integer_precision_and_legacy_work_is_explicit(self):
        task = {"usage": 1234567, "max_tokens": 2345678, "max_minutes": 0,
                "purpose": "work", "last_message": "  旧工作正文\r\n原有换行  "}
        report = render_task_report(task)
        self.assertIn("累计 Token：1234567 / 2345678", report)
        self.assertIn(task["last_message"], report)
        self.assertIn(" / 不限", report)
        huge = 10 ** 400
        self.assertIn("累计 Token：" + str(huge) + " / " + str(huge),
                      render_task_report({"usage": huge, "max_tokens": huge}))


if __name__ == "__main__":
    unittest.main()
