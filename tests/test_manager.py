"""Controller contract checks; all sessions and projects here are simulated."""

import copy
from collections import defaultdict, deque
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from relay.manager import Manager, workspace_snapshot
from relay.transport import RequestTimeout


class FakeClient:
    def __init__(self):
        self.calls = []
        self.responses = defaultdict(deque)
        self.events = []
        self.answers = []
        self.closed = False
        self.thread_count = 0
        self.turn_count = 0
        self.interrupt_completes = True

    def request(self, method, params=None, timeout=30):
        self.calls.append((method, copy.deepcopy(params or {})))
        if self.responses[method]:
            result = self.responses[method].popleft()
            if isinstance(result, BaseException):
                raise result
            return copy.deepcopy(result)
        if method == "thread/start":
            self.thread_count += 1
            sandbox = "workspaceWrite" if params.get("sandbox") == "workspace-write" else "readOnly"
            return {"thread": {"id": f"thread-{self.thread_count}", "turns": []},
                    "cwd": params.get("cwd"), "model": params.get("model") or "test-model",
                    "sandbox": {"type": sandbox}, "approvalPolicy": params.get("approvalPolicy")}
        if method == "turn/start":
            self.turn_count += 1
            return {"turn": {"id": f"turn-{self.turn_count}", "status": "inProgress", "items": []}}
        if method == "turn/interrupt":
            if self.interrupt_completes:
                self.push("turn/completed", {"threadId": params["threadId"],
                    "turn": {"id": params["turnId"], "status": "interrupted", "items": []}})
            return {}
        if method == "thread/read":
            return {"thread": {"id": params["threadId"], "status": {"type": "idle"}, "turns": []}}
        if method == "config/read":
            return {"config": {"mcp_servers": {}}}
        if method == "mcpServerStatus/list":
            return {"data": [], "nextCursor": None}
        raise AssertionError(f"Unexpected RPC: {method}")

    def respond(self, request_id, result):
        self.answers.append((request_id, copy.deepcopy(result)))

    def drain_events(self):
        events, self.events = self.events, []
        return events

    def close(self):
        self.closed = True

    def push(self, method, params, request_id=None):
        event = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            event["id"] = request_id
        self.events.append(event)

    def calls_for(self, method):
        return [params for name, params in self.calls if name == method]


class ManagerContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "项目 with spaces"
        self.project.mkdir()
        self.client = FakeClient()
        self.manager = Manager(self.root / "state", self.client)
        self.addCleanup(self.manager.close)

    def task(self, **kwargs):
        return self.manager.create_task("Example", str(self.project), "Inspect the sample only", **kwargs)

    def start_task(self, **kwargs):
        task = self.task(**kwargs)
        self.manager.start(task["id"])
        return self.manager.get_task(task["id"])

    def complete(self, task, status="completed"):
        self.client.push("turn/completed", {
            "threadId": task["thread_id"],
            "turn": {"id": task["turn_id"], "status": status, "items": []},
        })
        self.manager.poll()

    def usage(self, task, last, total, window=1000):
        def counts(value):
            return {"totalTokens": value, "inputTokens": value, "cachedInputTokens": 0,
                    "outputTokens": 0, "reasoningOutputTokens": 0}
        self.client.push("thread/tokenUsage/updated", {
            "threadId": task["thread_id"], "turnId": task["turn_id"],
            "tokenUsage": {"last": counts(last), "total": counts(total), "modelContextWindow": window},
        })
        self.manager.poll()

    def finish_agent(self, thread_id, turn_id, payload):
        item = {"id": "message-" + turn_id, "type": "agentMessage", "text": json.dumps(payload)}
        self.client.push("item/completed", {"threadId": thread_id, "turnId": turn_id, "item": item})
        self.client.push("turn/completed", {
            "threadId": thread_id, "turn": {"id": turn_id, "status": "completed", "items": [item]},
        })
        self.manager.poll()

    def begin_handoff(self, mode="workspace-write"):
        evidence = self.project / "result.txt"
        evidence.write_text("Original evidence", encoding="utf-8")
        source = self.start_task(mode=mode)
        self.complete(source)
        self.manager.handoff(source["id"])
        return source, evidence

    def finish_summary(self, source, evidence, **changes):
        summary = {"summary": "Inspected the local sample", "decisions": ["Keep the original task mode"],
                   "completed": ["Read the sample"], "next_step": "Verify the sample contents once more",
                   "evidence": [{"path": str(evidence), "description": "Current sample artifact"}],
                   "unknown_operations": [], "unrecoverable_resources": [], "task_complete": False}
        summary.update(changes)
        turn = self.client.calls_for("turn/start")[-1]
        self.finish_agent(turn["threadId"], f"turn-{self.client.turn_count}", summary)
        return self.manager.get_task(source["id"])

    def finish_ready(self, task, **changes):
        ready = {"checkpoint_hash": task["checkpoint_hash"], "ready": True,
                 "checks": [{"category": name, "finding": "Verified the original evidence and scope"}
                            for name in ("goal", "authorization", "environment", "artifacts", "operations", "next_step")],
                 "gaps": []}
        ready.update(changes)
        turn = self.client.calls_for("turn/start")[-1]
        self.finish_agent(turn["threadId"], f"turn-{self.client.turn_count}", ready)

    def test_creation_is_local_and_durable(self):
        task = self.task()
        self.assertEqual([], self.client.calls)
        self.assertEqual("read-only", task["mode"])
        self.manager.close()
        reopened = Manager(self.root / "state", FakeClient())
        self.addCleanup(reopened.close)
        saved = reopened.get_task(task["id"])
        self.assertEqual(task["goal"], saved["goal"])
        self.assertEqual(task["cwd"], saved["cwd"])
        self.assertIsNone(saved["thread_id"])

    def test_second_manager_cannot_take_same_state_directory(self):
        with self.assertRaises((ValueError, RuntimeError, OSError)):
            unexpected = Manager(self.root / "state", FakeClient())
            unexpected.close()

    def test_start_uses_read_only_ceiling_and_human_approvals(self):
        task = self.start_task()
        thread = self.client.calls_for("thread/start")[0]
        turn = self.client.calls_for("turn/start")[0]
        self.assertEqual("read-only", thread["sandbox"])
        self.assertEqual("on-request", thread["approvalPolicy"])
        self.assertEqual("readOnly", turn["sandboxPolicy"]["type"])
        self.assertEqual("on-request", turn["approvalPolicy"])
        self.assertEqual(task["thread_id"], turn["threadId"])

    def test_workspace_mode_is_not_full_access(self):
        self.start_task(mode="workspace-write")
        thread = self.client.calls_for("thread/start")[0]
        turn = self.client.calls_for("turn/start")[0]
        self.assertEqual("workspace-write", thread["sandbox"])
        self.assertEqual("on-request", thread["approvalPolicy"])
        self.assertEqual("workspaceWrite", turn["sandboxPolicy"]["type"])
        self.assertEqual("on-request", turn["approvalPolicy"])

    def test_unknown_creation_is_not_retried(self):
        task = self.task()
        self.client.responses["thread/start"].append(RequestTimeout("thread/start", 1, 0.01))
        try:
            self.manager.start(task["id"])
        except (ValueError, RuntimeError, TimeoutError):
            pass
        calls = copy.deepcopy(self.client.calls)
        with self.assertRaises((ValueError, RuntimeError)):
            self.manager.start(task["id"])
        self.assertEqual(calls, self.client.calls)
        self.assertIsNone(self.manager.get_task(task["id"])["thread_id"])

    def test_unknown_turn_start_retains_thread_without_replay(self):
        task = self.task()
        self.client.responses["turn/start"].append(RequestTimeout("turn/start", 2, 0.01))
        try:
            self.manager.start(task["id"])
        except (ValueError, RuntimeError, TimeoutError):
            pass
        held = self.manager.get_task(task["id"])
        self.assertEqual("thread-1", held["thread_id"])
        calls = copy.deepcopy(self.client.calls)
        with self.assertRaises((ValueError, RuntimeError)):
            self.manager.start(task["id"])
        self.assertEqual(calls, self.client.calls)

    def test_idle_thread_does_not_resolve_unknown_turn_start_outcome(self):
        task = self.task()
        self.client.responses["turn/start"].append(RequestTimeout("turn/start", 2, 0.01))
        with self.assertRaises((ValueError, RuntimeError, TimeoutError)):
            self.manager.start(task["id"])
        held = self.manager.get_task(task["id"])
        self.client.responses["thread/read"].append({"thread": {
            "id": held["thread_id"], "status": {"type": "idle"},
            "turns": [{"id": "server-finished-turn", "status": "completed", "items": [
                {"id": "external-operation", "type": "commandExecution", "status": "completed",
                 "command": "an externally visible operation whose result was not received"}]}],
        }})
        try:
            self.manager.reconcile(task["id"])
        except (ValueError, RuntimeError):
            pass
        before = len(self.client.calls_for("turn/start"))
        with self.assertRaises((ValueError, RuntimeError)):
            self.manager.start(task["id"])
        self.assertEqual(before, len(self.client.calls_for("turn/start")))

    def test_host_permission_mismatch_stops_before_first_turn(self):
        task = self.task()
        self.client.responses["thread/start"].append({"thread": {"id": "thread-overpowered", "turns": []},
            "cwd": str(self.project), "model": "test-model", "approvalPolicy": "never",
            "sandbox": {"type": "dangerFullAccess"}})
        with self.assertRaises((ValueError, RuntimeError)):
            self.manager.start(task["id"])
        self.assertEqual([], self.client.calls_for("turn/start"))
        self.assertEqual("thread-overpowered", self.manager.get_task(task["id"])["thread_id"])

    def test_restart_does_not_automatically_resume_active_work(self):
        task = self.start_task()
        self.client.interrupt_completes = False
        self.manager.close()
        fresh_client = FakeClient()
        reopened = Manager(self.root / "state", fresh_client)
        self.addCleanup(reopened.close)
        saved = reopened.get_task(task["id"])
        self.assertEqual("needs_reconcile", saved["state"])
        self.assertEqual(task["thread_id"], saved["thread_id"])
        self.assertEqual([], fresh_client.calls)

    def test_pause_waits_for_turn_completion(self):
        task = self.start_task()
        self.manager.pause(task["id"])
        self.assertEqual([{"threadId": task["thread_id"], "turnId": task["turn_id"]}],
                         self.client.calls_for("turn/interrupt"))
        calls = copy.deepcopy(self.client.calls)
        with self.assertRaises((ValueError, RuntimeError)):
            self.manager.start(task["id"])
        self.assertEqual(calls, self.client.calls)
        self.complete(task, "interrupted")
        self.assertNotEqual("running", self.manager.get_task(task["id"])["state"])

    def test_stale_thread_notifications_cannot_finish_current_turn(self):
        task = self.start_task()
        self.client.push("turn/completed", {
            "threadId": "old-unowned-thread",
            "turn": {"id": task["turn_id"], "status": "completed", "items": []},
        })
        self.manager.poll()
        current = self.manager.get_task(task["id"])
        self.assertEqual(task["state"], current["state"])
        self.assertEqual(task["turn_id"], current["turn_id"])

    def test_context_estimate_uses_last_request_not_cumulative_tokens(self):
        task = self.start_task()
        self.usage(task, last=200, total=9500)
        estimate = self.manager.get_task(task["id"])["context_estimate"]
        self.assertAlmostEqual(0.2, estimate)

    def test_compaction_items_are_counted_once_and_invalidate_old_usage(self):
        task = self.start_task()
        self.usage(task, last=800, total=9500)
        for _ in range(2):
            self.client.push("item/completed", {
                "threadId": task["thread_id"], "turnId": task["turn_id"],
                "item": {"id": "compact-1", "type": "contextCompaction"},
            })
        self.manager.poll()
        current = self.manager.get_task(task["id"])
        self.assertEqual(1, current["compactions"])
        self.assertIsNone(current["context_estimate"])

    def test_stale_turn_approval_does_not_become_actionable(self):
        task = self.start_task(mode="workspace-write")
        self.client.push("item/commandExecution/requestApproval", {
            "threadId": task["thread_id"], "turnId": "prior-turn", "itemId": "cmd-old",
            "command": "echo test", "cwd": str(self.project),
            "availableDecisions": ["accept", "decline"],
        }, request_id=71)
        self.manager.poll()
        with self.assertRaises((ValueError, RuntimeError)):
            self.manager.answer(task["id"], 71, {"decision": "accept"})
        self.assertNotIn((71, {"decision": "accept"}), self.client.answers)

    def test_completed_turn_clears_its_approval(self):
        task = self.start_task(mode="workspace-write")
        self.client.push("item/commandExecution/requestApproval", {
            "threadId": task["thread_id"], "turnId": task["turn_id"], "itemId": "cmd-1",
            "command": "echo test", "cwd": str(self.project),
            "availableDecisions": ["accept", "decline"],
        }, request_id=72)
        self.manager.poll()
        self.complete(task)
        with self.assertRaises((ValueError, RuntimeError)):
            self.manager.answer(task["id"], 72, {"decision": "accept"})
        self.assertNotIn((72, {"decision": "accept"}), self.client.answers)

    def test_token_budget_blocks_next_turn_at_boundary(self):
        task = self.start_task(max_tokens=100)
        self.usage(task, last=100, total=100)
        self.complete(task)
        before = len(self.client.calls_for("turn/start"))
        with self.assertRaises((ValueError, RuntimeError)):
            self.manager.start(task["id"])
        self.assertEqual(before, len(self.client.calls_for("turn/start")))

    def test_invalid_permission_mode_cannot_create_task(self):
        with self.assertRaises((ValueError, RuntimeError)):
            self.task(mode="danger-full-access")
        self.assertEqual([], self.client.calls)

    def test_handoff_transfers_only_after_complete_ready_and_preserves_mode(self):
        source, evidence = self.begin_handoff(mode="read-only")
        verifying = self.finish_summary(source, evidence)
        self.assertEqual(source["thread_id"], verifying["thread_id"])
        self.assertEqual(source["generation"], verifying["generation"])
        receiver_id = self.client.calls_for("turn/start")[-1]["threadId"]
        self.assertNotEqual(source["thread_id"], receiver_id)
        self.finish_ready(verifying)
        adopted = self.manager.get_task(source["id"])
        self.assertEqual(receiver_id, adopted["thread_id"])
        self.assertEqual(source["generation"] + 1, adopted["generation"])
        self.assertEqual("read-only", adopted["mode"])
        self.assertEqual("readOnly", self.client.calls_for("turn/start")[-1]["sandboxPolicy"]["type"])

    def test_file_change_after_freeze_refuses_receiver_ready(self):
        source, evidence = self.begin_handoff()
        verifying = self.finish_summary(source, evidence)
        evidence.write_text("Modified by another process", encoding="utf-8")
        before = len(self.client.calls_for("turn/start"))
        self.finish_ready(verifying)
        held = self.manager.get_task(source["id"])
        self.assertEqual(source["thread_id"], held["thread_id"])
        self.assertEqual(source["generation"], held["generation"])
        self.assertEqual(before, len(self.client.calls_for("turn/start")))

    def test_ready_with_missing_authorization_check_does_not_transfer(self):
        source, evidence = self.begin_handoff()
        verifying = self.finish_summary(source, evidence)
        self.finish_ready(verifying, checks=[{"category": "goal", "finding": "Goal appears consistent"}])
        held = self.manager.get_task(source["id"])
        self.assertEqual(source["thread_id"], held["thread_id"])
        self.assertEqual(source["generation"], held["generation"])

    def test_unknown_external_operation_blocks_handoff(self):
        source, evidence = self.begin_handoff()
        before = len(self.client.calls_for("thread/start"))
        held = self.finish_summary(source, evidence, unknown_operations=["Publish request outcome unknown"])
        self.assertEqual(source["thread_id"], held["thread_id"])
        self.assertEqual(before, len(self.client.calls_for("thread/start")))

    def test_read_only_preparation_denies_command_escalation(self):
        source, _ = self.begin_handoff()
        turn = self.client.calls_for("turn/start")[-1]
        self.assertEqual("readOnly", turn["sandboxPolicy"]["type"])
        self.client.push("item/commandExecution/requestApproval", {
            "threadId": turn["threadId"], "turnId": f"turn-{self.client.turn_count}",
            "itemId": "escaped-command", "command": "write-outside-workspace",
            "cwd": str(self.project), "availableDecisions": ["accept", "decline"],
        }, request_id=73)
        self.manager.poll()
        answers = [result for request_id, result in self.client.answers if request_id == 73]
        self.assertEqual(1, len(answers))
        self.assertIn(answers[0].get("decision"), ("decline", "cancel"))
        with self.assertRaises((ValueError, RuntimeError)):
            self.manager.answer(source["id"], 73, {"decision": "accept"})

    def test_predecessor_event_after_transfer_cannot_end_successor_turn(self):
        source, evidence = self.begin_handoff()
        verifying = self.finish_summary(source, evidence)
        self.finish_ready(verifying)
        adopted = self.manager.get_task(source["id"])
        self.client.push("turn/completed", {
            "threadId": source["thread_id"],
            "turn": {"id": source["turn_id"], "status": "completed", "items": []},
        })
        self.manager.poll()
        current = self.manager.get_task(source["id"])
        self.assertEqual(adopted["thread_id"], current["thread_id"])
        self.assertEqual(adopted["turn_id"], current["turn_id"])
        self.assertEqual(adopted["state"], current["state"])

    def test_model_reroute_does_not_revalidate_old_usage(self):
        task = self.start_task(auto_handoff=True)
        self.usage(task, last=800, total=800)
        self.client.push("model/rerouted", {
            "threadId": task["thread_id"], "turnId": task["turn_id"],
            "fromModel": "test-model", "toModel": "different-model", "reason": "capacity",
        })
        self.manager.poll()
        self.assertIsNone(self.manager.get_task(task["id"])["context_estimate"])
        self.usage(task, last=800, total=800)
        current = self.manager.get_task(task["id"])
        self.assertIsNone(current["context_estimate"])
        self.assertFalse(current["auto_handoff"])

    def test_workspace_approval_rejects_session_scope_and_extra_fields(self):
        task = self.start_task(mode="workspace-write")
        self.client.push("item/commandExecution/requestApproval", {
            "threadId": task["thread_id"], "turnId": task["turn_id"], "itemId": "command-1",
            "command": "echo review", "cwd": str(self.project),
            "availableDecisions": ["accept", "acceptForSession", "decline"],
        }, request_id=74)
        self.manager.poll()
        for answer in ({"decision": "acceptForSession"}, {"decision": "accept", "scope": "session"}):
            with self.assertRaises((ValueError, RuntimeError)):
                self.manager.answer(task["id"], 74, answer)
        self.assertEqual([], self.client.answers)
        self.manager.answer(task["id"], 74, {"decision": "accept"})
        self.assertEqual([(74, {"decision": "accept"})], self.client.answers)

    def test_read_only_work_cannot_approve_write_escalation(self):
        task = self.start_task()
        self.client.push("item/fileChange/requestApproval", {
            "threadId": task["thread_id"], "turnId": task["turn_id"], "itemId": "write-1",
            "reason": "Need to edit sample", "grantRoot": str(self.project),
        }, request_id=75)
        self.manager.poll()
        self.assertEqual([(75, {"decision": "decline"})], self.client.answers)
        with self.assertRaises((ValueError, RuntimeError)):
            self.manager.answer(task["id"], 75, {"decision": "accept"})

    def test_pausing_receiver_keeps_source_owner_without_auto_continuation(self):
        source, evidence = self.begin_handoff()
        verifying = self.finish_summary(source, evidence)
        receiver_id = self.client.calls_for("turn/start")[-1]["threadId"]
        before = len(self.client.calls_for("turn/start"))
        self.manager.pause(source["id"])
        self.assertEqual(receiver_id, self.client.calls_for("turn/interrupt")[-1]["threadId"])
        self.manager.poll()
        paused = self.manager.get_task(source["id"])
        self.assertEqual("paused", paused["state"])
        self.assertEqual(source["thread_id"], paused["thread_id"])
        self.assertEqual(verifying["generation"], paused["generation"])
        self.assertEqual(before, len(self.client.calls_for("turn/start")))
        self.manager.start(source["id"])
        self.assertEqual(source["thread_id"], self.client.calls_for("turn/start")[-1]["threadId"])

    def test_reconnect_does_not_bind_new_late_receipt_to_old_request_number(self):
        old_task = self.task()
        self.client.responses["thread/start"].append(RequestTimeout("thread/start", 2, 0.01))
        with self.assertRaises((ValueError, RuntimeError, TimeoutError)):
            self.manager.start(old_task["id"])
        self.client.push("transport/closed", {"returncode": 1})
        self.manager.poll()

        other_project = self.root / "other-project"
        other_project.mkdir()
        new_task = self.manager.create_task("Other task", str(other_project), "Inspect another sample")
        replacement = FakeClient()
        replacement.responses["thread/start"].append(RequestTimeout("thread/start", 2, 0.01))
        with patch("relay.transport.CodexClient", return_value=replacement):
            with self.assertRaises((ValueError, RuntimeError, TimeoutError)):
                self.manager.start(new_task["id"])
        replacement.push("transport/lateResponse", {"requestId": 2, "result": {
            "thread": {"id": "new-connection-thread", "turns": []}, "cwd": str(other_project),
            "model": "test-model", "sandbox": {"type": "readOnly"}, "approvalPolicy": "on-request",
        }})
        self.manager.poll()
        self.assertIsNone(self.manager.get_task(old_task["id"])["thread_id"])
        self.assertEqual("new-connection-thread", self.manager.get_task(new_task["id"])["thread_id"])

    def test_native_user_answer_is_preserved_in_frozen_checkpoint(self):
        evidence = self.project / "result.txt"
        evidence.write_text("Sample evidence", encoding="utf-8")
        source = self.start_task(mode="workspace-write")
        instruction = "Do not modify protected.txt under any circumstance"
        self.client.push("item/tool/requestUserInput", {
            "threadId": source["thread_id"], "turnId": source["turn_id"], "itemId": "question-item",
            "questions": [{"id": "q1", "header": "Constraint", "question": "Which file must remain unchanged?",
                           "isOther": True, "isSecret": False, "options": None}],
        }, request_id=76)
        self.manager.poll()
        self.manager.answer(source["id"], 76, {"answers": {"q1": {"answers": [instruction]}}})
        self.complete(source)
        self.manager.handoff(source["id"])
        verifying = self.finish_summary(source, evidence)
        self.assertIn(instruction, json.dumps(verifying["checkpoint"]))

    def test_configured_plugins_and_mcp_servers_are_disabled_for_managed_threads(self):
        self.client.responses["config/read"].append({"config": {
            "plugins": {"sample@catalog": {"enabled": True}},
            "mcp_servers": {"sample-server": {"enabled": True}},
        }})
        self.start_task()
        config = self.client.calls_for("thread/start")[0]["config"]
        self.assertIs(False, config["plugins.sample@catalog.enabled"])
        self.assertIs(False, config["mcp_servers.sample-server.enabled"])
        self.assertIs(False, config["features.apps"])
        self.assertIs(False, config["apps._default.enabled"])

    def test_residual_plugin_tools_on_later_status_page_block_first_turn(self):
        self.client.responses["mcpServerStatus/list"].append({"data": [
            {"name": "disabled-server", "runtimeStatus": "disabled", "tools": {}},
        ], "nextCursor": "page-two"})
        self.client.responses["mcpServerStatus/list"].append({"data": [
            {"name": "plugin-server", "runtimeStatus": "ready", "tools": {"publish": {"name": "publish"}}},
        ], "nextCursor": None})
        task = self.task()
        with self.assertRaises((ValueError, RuntimeError)):
            self.manager.start(task["id"])
        self.assertEqual([], self.client.calls_for("turn/start"))
        pages = self.client.calls_for("mcpServerStatus/list")
        self.assertEqual(2, len(pages))
        self.assertEqual("page-two", pages[1]["cursor"])
        self.assertEqual("thread-1", self.manager.get_task(task["id"])["thread_id"])

    def test_start_does_not_steer_an_unexpected_active_host_turn(self):
        task = self.start_task()
        self.complete(task)
        self.client.responses["thread/read"].append({"thread": {
            "id": task["thread_id"], "status": {"type": "active", "activeFlags": []},
            "turns": [{"id": "outside-client-turn", "status": "inProgress", "items": []}],
        }})
        before = len(self.client.calls_for("turn/start"))
        with self.assertRaises((ValueError, RuntimeError)):
            self.manager.start(task["id"], "A new user instruction")
        self.assertEqual(before, len(self.client.calls_for("turn/start")))

    def test_transport_eof_during_close_still_releases_manager(self):
        self.start_task()
        self.client.push("transport/closed", {"returncode": 1})
        self.manager.close()
        reopened = Manager(self.root / "state", FakeClient())
        self.addCleanup(reopened.close)
        self.assertEqual(1, len(reopened.list_tasks()))

    def test_secret_labeled_question_is_not_collected_or_persisted(self):
        task = self.start_task(mode="workspace-write")
        self.client.push("item/tool/requestUserInput", {
            "threadId": task["thread_id"], "turnId": task["turn_id"], "itemId": "secret-question",
            "questions": [{"id": "q1", "header": "Password", "question": "Enter account password",
                           "isSecret": True, "isOther": True, "options": None}],
        }, request_id=77)
        self.manager.poll()
        self.assertIn((77, {"answers": {"q1": {"answers": []}}}), self.client.answers)
        with self.assertRaises((ValueError, RuntimeError)):
            self.manager.answer(task["id"], 77, {"answers": {"q1": {"answers": ["ordinary-example-value"]}}})
        stored = self.manager.get_task(task["id"])
        self.assertEqual([], stored["pending"])
        self.assertNotIn("ordinary-example-value", json.dumps(stored))

    def test_stop_requested_during_creation_prevents_new_work_turn(self):
        task = self.task()
        original_request = self.client.request

        def response_after_stop(method, params=None, timeout=30):
            result = original_request(method, params, timeout)
            if method == "thread/start":
                self.manager.stop_requested.set()
            return result

        with patch.object(self.client, "request", side_effect=response_after_stop):
            with self.assertRaises((ValueError, RuntimeError)):
                self.manager.start(task["id"])
        self.assertEqual([], self.client.calls_for("turn/start"))
        self.assertEqual("thread-1", self.manager.get_task(task["id"])["thread_id"])

    def test_initial_start_and_handoff_preserve_goal_with_supplement(self):
        evidence = self.project / "result.txt"
        evidence.write_text("Sample evidence", encoding="utf-8")
        task = self.task()
        supplement = "Report only the count and preserve every file."
        self.manager.start(task["id"], supplement)
        source = self.manager.get_task(task["id"])
        self.assertEqual([task["goal"], supplement], source["requirements"])
        initial_text = "\n".join(item.get("text", "") for item in self.client.calls_for("turn/start")[0]["input"])
        self.assertIn(task["goal"], initial_text)
        self.assertIn(supplement, initial_text)
        self.complete(source)
        self.manager.handoff(source["id"])
        verifying = self.finish_summary(source, evidence)
        self.assertEqual([task["goal"], supplement], verifying["checkpoint"]["requirements"])

    def test_unreadable_workspace_cannot_become_empty_valid_snapshot(self):
        with patch("relay.manager.os.scandir", side_effect=PermissionError("Simulated inaccessible directory")):
            with self.assertRaises(PermissionError):
                workspace_snapshot(self.project)

    def test_unverifiable_git_directory_cannot_become_valid_snapshot(self):
        (self.project / ".git").mkdir()
        responses = [
            subprocess.CompletedProcess([], 128, b"", b"fatal: cannot locate Git directory"),
            subprocess.CompletedProcess([], 0, b"refs/heads/main\n", b""),
            subprocess.CompletedProcess([], 0, b"0123456789abcdef\n", b""),
        ]
        with patch("relay.manager.shutil.which", return_value="git"), \
                patch("relay.manager.subprocess.run", side_effect=responses) as run:
            with self.assertRaises(ValueError):
                workspace_snapshot(self.project)
            self.assertEqual(1, run.call_count)
            self.assertIn("--absolute-git-dir", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
