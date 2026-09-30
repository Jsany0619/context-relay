"""Pure validation and normalization checks for external Codex thread snapshots."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import unittest
from unittest import mock

from relay.imports import normalize_thread


THREAD_ID = "019d0000-0000-7000-8000-000000000001"


def item(item_type, item_id, **values):
    return {"type": item_type, "id": item_id, **values}


def thread(*, status="idle", turns=None, source="vscode"):
    return {
        "id": THREAD_ID,
        "name": "Existing desktop work",
        "preview": "First request preview",
        "cwd": str(Path.cwd().resolve()),
        "updatedAt": 1_800_000_000,
        "source": source,
        "status": {"type": status},
        "turns": turns if turns is not None else [],
    }


class ImportNormalizationTests(unittest.TestCase):
    def test_completed_thread_extracts_only_user_and_assistant_text(self):
        value = thread(turns=[{
            "id": "turn-1", "status": "completed", "items": [
                item("userMessage", "u1", content=[
                    {"type": "text", "text": "Please continue this task."},
                    {"type": "image", "url": "https://private.invalid/image"},
                ]),
                item("commandExecution", "tool1", command="secret-command",
                     aggregatedOutput="private tool output", status="completed"),
                item("agentMessage", "a1", text="The first stage is complete."),
            ],
        }])
        snapshot = normalize_thread(value, expected_id=THREAD_ID)
        self.assertEqual(snapshot["kind"], "external-reference")
        self.assertEqual(snapshot["thread_id"], THREAD_ID)
        self.assertEqual(snapshot["title"], "Existing desktop work")
        self.assertEqual(snapshot["source"], "vscode")
        self.assertEqual(snapshot["status"], "idle")
        self.assertTrue(snapshot["can_import"])
        self.assertEqual(snapshot["messages"], [
            {"role": "user", "text": "Please continue this task.",
             "turn_id": "turn-1", "item_id": "u1"},
            {"role": "assistant", "text": "The first stage is complete.",
             "turn_id": "turn-1", "item_id": "a1"},
        ])
        self.assertEqual(snapshot["non_text_items"], 2)
        self.assertNotIn("private tool output", json.dumps(snapshot))
        self.assertNotIn("private.invalid", json.dumps(snapshot))
        datetime.fromisoformat(snapshot["read_at"])

    def test_fingerprint_covers_unabridged_history_but_ignores_runtime_status(self):
        value = thread(turns=[{"id": "t", "status": "completed", "items": [
            item("agentMessage", "a", text="answer"),
            item("commandExecution", "c", command="one", aggregatedOutput="first", status="completed"),
        ]}])
        first = normalize_thread(value)["fingerprint"]
        value["status"] = {"type": "notLoaded"}
        self.assertEqual(normalize_thread(value)["fingerprint"], first)
        value["preview"] = "changed source preview"
        preview_changed = normalize_thread(value)["fingerprint"]
        self.assertNotEqual(preview_changed, first)
        value["turns"][0]["items"][1]["aggregatedOutput"] = "changed outside excerpt"
        self.assertNotEqual(normalize_thread(value)["fingerprint"], preview_changed)

    def test_not_loaded_is_importable_with_cross_client_warning_but_active_is_not(self):
        turns = [{"id": "t", "status": "completed", "items": []}]
        unloaded = normalize_thread(thread(status="notLoaded", turns=turns))
        self.assertTrue(unloaded["can_import"])
        self.assertIn("cross_client_activity_unknown", unloaded["warnings"])
        self.assertIn("stop_original_thread_before_import", unloaded["warnings"])
        active = normalize_thread(thread(status="active", turns=turns))
        self.assertFalse(active["can_import"])
        self.assertIn("source_thread_active", active["warnings"])

    def test_identity_source_and_path_are_strict(self):
        completed = [{"id": "t", "status": "completed", "items": []}]
        cases = [
            (thread(turns=completed, source="cli"), None),
            (thread(turns=completed, source={"subAgent": {"threadId": "x"}}), None),
            ({**thread(turns=completed), "cwd": "relative"}, None),
            ({**thread(turns=completed), "id": ""}, None),
            (thread(turns=completed), "different-id"),
        ]
        for value, expected in cases:
            with self.subTest(value=value.get("source"), expected=expected), self.assertRaises(ValueError):
                normalize_thread(value, expected_id=expected)

    def test_incomplete_latest_turn_or_active_tool_blocks_import(self):
        prior_failure = {"id": "old", "status": "failed", "items": []}
        latest = {"id": "new", "status": "completed", "items": []}
        historical = normalize_thread(thread(turns=[prior_failure, latest]))
        self.assertTrue(historical["can_import"])
        self.assertIn("history_contains_incomplete_turns", historical["warnings"])
        for last_status in ("failed", "interrupted", "inProgress", "mystery"):
            with self.subTest(status=last_status):
                value = normalize_thread(thread(turns=[{"id": "last", "status": last_status, "items": []}]))
                self.assertFalse(value["can_import"])
                self.assertIn("latest_turn_not_completed", value["warnings"])
        running_tool = thread(turns=[{"id": "t", "status": "completed", "items": [
            item("mcpToolCall", "m", server="x", tool="y", status="inProgress")
        ]}])
        snapshot = normalize_thread(running_tool)
        self.assertFalse(snapshot["can_import"])
        self.assertIn("unfinished_action", snapshot["warnings"])

    def test_latest_known_subagent_state_controls_importability(self):
        spawn = item(
            "collabAgentToolCall", "spawn", tool="spawnAgent", senderThreadId=THREAD_ID,
            receiverThreadIds=["child"], status="completed",
            agentsStates={"child": {"status": "running"}},
        )
        value = thread(turns=[{"id": "t", "status": "completed", "items": [spawn]}])
        active = normalize_thread(value)
        self.assertFalse(active["can_import"])
        self.assertIn("active_subagent", active["warnings"])
        waited = item(
            "collabAgentToolCall", "wait", tool="wait", senderThreadId=THREAD_ID,
            receiverThreadIds=["child"], status="completed",
            agentsStates={"child": {"status": "completed"}},
        )
        value["turns"][0]["items"].append(waited)
        settled = normalize_thread(value)
        self.assertTrue(settled["can_import"])
        self.assertNotIn("active_subagent", settled["warnings"])
        value["turns"][0]["items"].append(item(
            "collabAgentToolCall", "followup", tool="followupTask", senderThreadId=THREAD_ID,
            receiverThreadIds=["child"], status="completed", agentsStates={},
        ))
        unknown = normalize_thread(value)
        self.assertFalse(unknown["can_import"])
        self.assertIn("subagent_activity_unknown", unknown["warnings"])
        value["turns"][0]["items"].append(item(
            "subAgentActivity", "activity-1", agentThreadId="child", agentPath="/root/child",
            kind="interacted",
        ))
        self.assertFalse(normalize_thread(value)["can_import"])
        value["turns"][0]["items"].append(item(
            "subAgentActivity", "activity-2", agentThreadId="child", agentPath="/root/child",
            kind="completed",
        ))
        self.assertTrue(normalize_thread(value)["can_import"])

    def test_excerpt_keeps_first_user_and_recent_messages_with_hard_size_bound(self):
        items = [item("userMessage", "first", content=[{"type": "text", "text": "FIRST " + "界" * 6000}])]
        for index in range(30):
            kind = "agentMessage" if index % 2 else "userMessage"
            if kind == "agentMessage":
                items.append(item(kind, f"m{index}", text=f"recent-{index} " + "文" * 6000))
            else:
                items.append(item(kind, f"m{index}", content=[
                    {"type": "text", "text": f"recent-{index} " + "文" * 6000}
                ]))
        snapshot = normalize_thread(thread(turns=[{"id": "t", "status": "completed", "items": items}]))
        self.assertEqual(snapshot["messages"][0]["item_id"], "first")
        self.assertEqual(snapshot["messages"][-1]["item_id"], "m29")
        self.assertLessEqual(len(snapshot["messages"]), 21)
        self.assertEqual(snapshot["omitted_messages"], 10)
        self.assertGreater(snapshot["truncated_messages"], 0)
        self.assertTrue(all(len(message["text"]) <= 3000 for message in snapshot["messages"]))
        self.assertLessEqual(len(json.dumps(snapshot, ensure_ascii=False).encode("utf-8")), 64 * 1024)

    def test_metadata_mode_needs_no_turns_and_cannot_claim_importability(self):
        value = thread(status="notLoaded")
        value.pop("turns")
        snapshot = normalize_thread(value, include_messages=False)
        self.assertEqual(snapshot["messages"], [])
        self.assertFalse(snapshot["can_import"])
        self.assertIn("metadata_only", snapshot["warnings"])

    def test_oversized_input_is_rejected_explicitly(self):
        value = thread(turns=[{"id": "t", "status": "completed", "items": [
            item("agentMessage", "a", text="x" * 200)
        ]}])
        with mock.patch("relay.imports.MAX_INPUT_BYTES", 100):
            with self.assertRaisesRegex(ValueError, "too large"):
                normalize_thread(value)


if __name__ == "__main__":
    unittest.main()
