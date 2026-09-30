"""Pure structured brief and review contract checks."""

from __future__ import annotations

import json
import unittest

from relay.assessment import DEFAULT_IMPORT_GOAL, SCHEMAS, assessment_prompt, validate_assessment


FILES = {"src/main.py": "a" * 64, "README.md": "b" * 64}
SOURCE = {"messages": [
    {"role": "user", "text": "original request", "turn_id": "turn-1", "item_id": "user-1"},
    {"role": "assistant", "text": "candidate", "turn_id": "turn-1", "item_id": "agent-1"},
]}


def evidence(source="file", reference="src/main.py", finding="The file contains the implementation."):
    return {"source": source, "reference": reference, "finding": finding}


def brief():
    return {
        "goal": "Preserve the requested behavior.",
        "decisions": ["Keep the public interface stable."],
        "completed": ["Implemented the parser."],
        "unknowns": ["No machine check result is available."],
        "approaches": ["Inspect the current files before changing them."],
        "acceptance": ["The existing behavior remains available."],
        "next_step": "Read the current implementation and choose the smallest change.",
        "evidence": [
            evidence(),
            evidence("chat", "turn-1/user-1", "The user requested the behavior."),
        ],
    }


def review(verdict="inconclusive"):
    return {
        "verdict": verdict,
        "summary": "The current evidence supports part of the requested behavior.",
        "findings": [{
            "severity": "low", "category": "usability",
            "finding": "The label may be unclear.", "suggestion": "Use a more direct label.",
            "evidence": [evidence("file", "README.md", "The current label appears here.")],
        }],
        "checks": [
            {"category": "requirements", "result": "supported", "finding": "Required fields exist.",
             "evidence": [evidence()]},
            {"category": "correctness", "result": "unverified", "finding": "No machine check was run.",
             "evidence": []},
            {"category": "usability", "result": "issue", "finding": "The label is unclear.",
             "evidence": [evidence("file", "README.md", "The current wording is ambiguous.")]},
            {"category": "creative", "result": "unverified", "finding": "No creative criterion was supplied.",
             "evidence": []},
        ],
        "unknowns": ["Runtime behavior has not been checked."],
        "test_evidence": ["Reported by the task record: unit tests passed before this review."],
    }


class AssessmentTests(unittest.TestCase):
    def test_public_contract_exposes_exact_schema_and_safe_import_goal(self):
        self.assertEqual(set(SCHEMAS), {"brief", "review"})
        self.assertEqual(set(SCHEMAS["brief"]["properties"]), set(brief()))
        self.assertEqual(set(SCHEMAS["review"]["properties"]), set(review()))
        self.assertFalse(SCHEMAS["brief"]["additionalProperties"])
        self.assertFalse(SCHEMAS["review"]["additionalProperties"])
        self.assertIn("只读", DEFAULT_IMPORT_GOAL)
        self.assertIn("未经用户确认", DEFAULT_IMPORT_GOAL)

    def test_valid_brief_accepts_bound_file_and_chat_evidence_and_returns_copy(self):
        value = brief()
        result = validate_assessment("brief", value, FILES, SOURCE)
        self.assertEqual(result, value)
        self.assertIsNot(result, value)
        result["decisions"].append("changed")
        self.assertNotEqual(result, value)

    def test_brief_rejects_bad_shape_limits_and_unbound_evidence(self):
        mutations = []
        mutations.append(lambda value: value.update(extra="no"))
        mutations.append(lambda value: value.update(goal=""))
        mutations.append(lambda value: value.update(acceptance=[]))
        mutations.append(lambda value: value.update(approaches=["x"] * 4))
        mutations.append(lambda value: value.update(decisions=["x"] * 31))
        mutations.append(lambda value: value.update(next_step="x" * 4001))
        mutations.append(lambda value: value.update(evidence=[evidence(reference="missing.py")]))
        mutations.append(lambda value: value.update(evidence=[evidence(reference="<git-metadata>")]))
        mutations.append(lambda value: value.update(evidence=[
            evidence("chat", "turn-x/item-x", "Not present in the source snapshot.")]))
        for mutate in mutations:
            value = brief()
            mutate(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_assessment("brief", value, FILES, SOURCE)

    def test_native_style_line_suffix_and_invented_chat_reference_are_rejected(self):
        value = brief()
        value["evidence"] = [evidence(reference="src/main.py:2")]
        with self.assertRaisesRegex(ValueError, "bound workspace"):
            validate_assessment("brief", value, FILES, None)

        value["evidence"] = [evidence(
            "chat", "当前用户请求及证据绑定 revision 1", "This is not a source message identity.")]
        with self.assertRaisesRegex(ValueError, "bound source"):
            validate_assessment("brief", value, FILES, None)

    def test_review_requires_exact_four_categories_and_bound_evidence(self):
        value = review()
        result = validate_assessment("review", value, FILES, SOURCE)
        self.assertEqual(result, value)
        for invalid_checks in (
            value["checks"][:3],
            [*value["checks"][:3], value["checks"][0]],
            [*value["checks"], {**value["checks"][0], "category": "other"}],
        ):
            changed = review()
            changed["checks"] = invalid_checks
            with self.subTest(checks=invalid_checks), self.assertRaises(ValueError):
                validate_assessment("review", changed, FILES, SOURCE)
        unsupported = review()
        unsupported["checks"][0]["evidence"] = []
        with self.assertRaisesRegex(ValueError, "unverified"):
            validate_assessment("review", unsupported, FILES, SOURCE)

    def test_ready_verdict_rejects_issues_and_material_findings(self):
        value = review("ready_for_user")
        value["checks"][2].update(result="supported", finding="The wording is usable.")
        self.assertEqual(validate_assessment("review", value, FILES, SOURCE)["verdict"],
                         "ready_for_user")
        for change in ("issue", "high", "medium"):
            changed = json.loads(json.dumps(value))
            if change == "issue":
                changed["checks"][0]["result"] = "issue"
            else:
                changed["findings"][0]["severity"] = change
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_assessment("review", changed, FILES, SOURCE)

    def test_unhashable_enum_values_are_rejected_as_validation_errors(self):
        changes = [
            lambda value: value.update(verdict={}),
            lambda value: value["findings"][0].update(severity=[]),
            lambda value: value["findings"][0].update(category={}),
            lambda value: value["checks"][0].update(result=[]),
            lambda value: value["checks"][0]["evidence"][0].update(source={}),
        ]
        for change in changes:
            value = review()
            change(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_assessment("review", value, FILES, SOURCE)

    def test_output_and_all_nested_text_are_bounded(self):
        value = brief()
        value["evidence"] = [evidence(finding="界" * 4000) for _ in range(30)]
        with self.assertRaisesRegex(ValueError, "64 KiB"):
            validate_assessment("brief", value, FILES, SOURCE)
        value = brief()
        value["evidence"][0]["finding"] = "x" * 4001
        with self.assertRaisesRegex(ValueError, "4000"):
            validate_assessment("brief", value, FILES, SOURCE)

    def test_prompts_bind_current_evidence_without_granting_authority(self):
        task = {"id": "task-1", "title": "Current task", "goal": "Current requirement",
                "requirements": ["Keep the interface stable"], "user_inputs": ["Use current files"],
                "acceptance_criteria": ["The behavior is reviewable"], "brief_required": True,
                "last_message": "Generator claim", "permissions": {"mode": "read-only"},
                "assessment": {"old": "STALE-ASSESSMENT"}, "review": {"old": "STALE-REVIEW"}}
        binding = {"revision": 3, "work_turns": 2, "requirements_hash": "hash",
                   "source_fingerprint": "source", "files": FILES}
        brief_prompt = assessment_prompt("brief", task, binding)
        self.assertIn("原始需求", brief_prompt)
        self.assertIn("最新修正", brief_prompt)
        self.assertIn("开放探索", brief_prompt)
        self.assertIn("不构成新权限", brief_prompt)
        self.assertIn("Generator claim", brief_prompt)
        self.assertIn("仅线索", brief_prompt)
        self.assertIn("临时梳理方向", brief_prompt)
        self.assertIn("逐字复制", brief_prompt)
        self.assertIn("不得追加 :行号", brief_prompt)
        self.assertIn("evidence 留空", brief_prompt)
        self.assertIn("不得生成 chat evidence", brief_prompt)
        self.assertNotIn("STALE-ASSESSMENT", brief_prompt)
        self.assertNotIn("STALE-REVIEW", brief_prompt)
        review_prompt = assessment_prompt("review", task, binding)
        self.assertIn("独立", review_prompt)
        self.assertIn("只读", review_prompt)
        self.assertIn("machine_checks=not_verified", review_prompt)
        self.assertIn("不得运行测试", review_prompt)
        self.assertIn("逐字复制", review_prompt)
        self.assertIn("不得生成 chat evidence", review_prompt)
        self.assertNotIn("Generator claim", review_prompt)
        self.assertNotIn("STALE-ASSESSMENT", review_prompt)
        self.assertNotIn("STALE-REVIEW", review_prompt)
        self.assertIn('"revision":3', review_prompt)
        self.assertIn("逐字复制当前证据绑定 files", SCHEMAS["brief"]["properties"]["evidence"]
                      ["items"]["properties"]["reference"]["description"])

    def test_invalid_kind_files_or_source_shape_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_assessment("other", brief(), FILES, SOURCE)
        with self.assertRaises(ValueError):
            validate_assessment("brief", brief(), ["src/main.py"], SOURCE)
        with self.assertRaises(ValueError):
            validate_assessment("brief", brief(), FILES, {"messages": "bad"})
        with self.assertRaises(ValueError):
            assessment_prompt("other", {}, {})


if __name__ == "__main__":
    unittest.main()
