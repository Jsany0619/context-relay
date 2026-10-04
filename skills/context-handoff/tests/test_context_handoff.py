"""Run with: python -m unittest discover -s tests -v (Python standard library only)."""

import importlib.util
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "context_handoff.py"
SPEC = importlib.util.spec_from_file_location("context_handoff_under_test", SCRIPT)
handoff = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = handoff
SPEC.loader.exec_module(handoff)

SESSION = "11111111-1111-4111-8111-111111111111"
MODEL = "test-model"
TARGET = "22222222-2222-4222-8222-222222222222"
OTHER_TARGET = "33333333-3333-4333-8333-333333333333"


class TranscriptFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cwd = Path(self.temp.name)
        self.path = self.cwd / "rollout.jsonl"
        self.clock = datetime.now(timezone.utc) - timedelta(minutes=2)
        self.rows = []
        self.header()

    def row(self, kind, payload):
        self.clock += timedelta(seconds=1)
        record = {"timestamp": self.clock.isoformat(), "type": kind, "payload": payload}
        self.rows.append(record)
        return record

    def header(self, source="vscode", session=SESSION, cwd=None):
        self.rows.clear()
        self.row("session_meta", {"id": session, "cwd": str(cwd or self.cwd), "source": source})
        self.row("turn_context", {"model": MODEL, "cwd": str(cwd or self.cwd), "turn_id": "turn-start"})

    def usage(self, input_tokens, window=10000, total_tokens=900000):
        return self.row("event_msg", {
            "type": "token_count",
            "info": {
                "last_token_usage": {"input_tokens": input_tokens, "output_tokens": 0,
                                     "total_tokens": input_tokens},
                "total_token_usage": {"input_tokens": total_tokens, "output_tokens": 0,
                                      "total_tokens": total_tokens},
                "model_context_window": window,
            },
        })

    def compact(self, identity, canonical=True):
        payload = {"id": identity, "turn_id": identity, "message": "summary"}
        if canonical:
            return self.row("compacted", payload)
        return self.row("event_msg", {"type": "context_compacted", **payload})

    def inspect(self, *, cache=None, session=SESSION, model=MODEL, cwd=None):
        self.path.write_text("".join(json.dumps(row) + "\n" for row in self.rows), encoding="utf-8")
        return handoff.inspect_transcript(self.path, session, model=model,
                                          cwd=str(cwd or self.cwd), cache=cache)


class TranscriptTests(TranscriptFixture):
    def test_verified_context_uses_last_input_not_cumulative_billing(self):
        self.usage(5000, total_tokens=900000)
        result = self.inspect()
        self.assertTrue(result["root_verified"])
        self.assertAlmostEqual(result["pressure"], 0.5)
        self.assertIsInstance(result["pressure_basis"], str)
        self.assertTrue(result["pressure_basis"])
        self.assertEqual(result["compactions"], 0)
        self.assertEqual(result["count_kind"], "lower_bound")
        self.assertIsInstance(result["reasons"], list)
        self.assertIsInstance(result["cache"], dict)

    def test_action_thresholds(self):
        for count, tokens, expected in [(0, 6999, "observe"), (0, 7000, "checkpoint"),
                                        (0, 8000, "checkpoint"), (1, 8000, "checkpoint"),
                                        (2, 7999, "checkpoint"), (2, 8000, "handoff")]:
            with self.subTest(compactions=count, input_tokens=tokens):
                self.header()
                for index in range(count):
                    self.compact(f"compaction-{index}")
                self.usage(tokens)
                self.assertEqual(handoff.choose_action(self.inspect()), expected)

    def test_finished_task_does_not_migrate(self):
        self.compact("one")
        self.compact("two")
        self.usage(9000)
        result = self.inspect()
        self.assertNotEqual(handoff.choose_action(result, task_status="complete"), "handoff")

    def test_cumulative_tokens_alone_are_unknown(self):
        self.row("event_msg", {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 900000, "total_tokens": 900000},
            "model_context_window": 10000,
        }})
        result = self.inspect()
        self.assertIsNone(result["pressure"])
        self.assertEqual(handoff.choose_action(result), "unknown")

    def test_compaction_invalidates_pre_compaction_usage(self):
        self.usage(9000)
        self.compact("one")
        result = self.inspect()
        self.assertEqual(result["compactions"], 1)
        self.assertIsNone(result["pressure"])
        self.assertNotEqual(handoff.choose_action(result), "handoff")

    def test_alias_invalidates_usage_and_pair_does_not_double_count(self):
        self.usage(9000)
        self.compact("one", canonical=False)
        self.compact("one", canonical=True)
        self.assertIsNone(self.inspect()["pressure"])
        self.usage(6000)
        result = self.inspect()
        self.assertEqual(result["compactions"], 1)
        self.assertAlmostEqual(result["pressure"], 0.6)

    def test_alias_only_deduplicates_identity(self):
        self.compact("one", canonical=False)
        self.compact("one", canonical=False)
        self.compact("two", canonical=False)
        self.usage(8000)
        self.assertEqual(self.inspect()["compactions"], 2)

    def test_duplicate_canonical_record_does_not_double_count(self):
        record = self.compact("one")
        self.rows.append(record.copy())
        self.usage(8000)
        self.assertEqual(self.inspect()["compactions"], 1)

    def test_two_distinct_compactions_in_one_turn_are_counted(self):
        self.compact("one")["payload"]["turn_id"] = "shared-turn"
        self.compact("two")["payload"]["turn_id"] = "shared-turn"
        self.usage(8000)
        self.assertEqual(self.inspect()["compactions"], 2)

    def test_replayed_pre_compaction_timestamp_does_not_restore_old_pressure(self):
        old_usage = self.usage(9000)
        self.compact("one")
        self.rows.append(old_usage.copy())
        self.assertIsNone(self.inspect()["pressure"])

    def test_model_change_invalidates_old_usage(self):
        self.usage(9000)
        self.row("turn_context", {"model": "other-model", "cwd": str(self.cwd), "turn_id": "new-turn"})
        result = self.inspect(model=None)
        self.assertIsNone(result["pressure"])
        self.assertNotEqual(handoff.choose_action(result), "handoff")

    def test_model_change_rejects_identical_replayed_usage_with_new_timestamp(self):
        self.usage(8500)
        self.row("turn_context", {"model": "other-model", "cwd": str(self.cwd), "turn_id": "new-turn"})
        self.usage(8500)
        self.assertIsNone(self.inspect(model=None)["pressure"])

    def test_requested_model_mismatch_is_unknown(self):
        self.usage(9000)
        self.assertIsNone(self.inspect(model="different-model")["pressure"])

    def test_subagent_source_cannot_trigger_root_handoff(self):
        self.header(source={"subagent": {"parent_thread_id": SESSION, "depth": 1}})
        self.compact("one")
        self.compact("two")
        self.usage(9000)
        result = self.inspect()
        self.assertFalse(result["root_verified"])
        self.assertNotEqual(handoff.choose_action(result), "handoff")

    def test_unknown_source_is_not_verified(self):
        self.header(source="unrecognized-agent-source")
        self.usage(9000)
        result = self.inspect()
        self.assertFalse(result["root_verified"])
        self.assertNotEqual(handoff.choose_action(result), "handoff")

    def test_wrong_session_or_working_directory_is_not_verified(self):
        self.compact("one")
        self.compact("two")
        self.usage(9000)
        for kwargs in [{"session": "22222222-2222-4222-8222-222222222222"},
                       {"cwd": self.cwd / "unrelated-project"}]:
            with self.subTest(kwargs=kwargs):
                result = self.inspect(**kwargs)
                self.assertFalse(result["root_verified"])
                self.assertNotEqual(handoff.choose_action(result), "handoff")

    def test_missing_usage_fields_are_unknown(self):
        for info in [{}, {"last_token_usage": {"input_tokens": 9000}},
                     {"last_token_usage": {"input_tokens": 9000}, "model_context_window": 0},
                     {"last_token_usage": {"input_tokens": -1}, "model_context_window": 10000}]:
            with self.subTest(info=info):
                self.header()
                self.row("event_msg", {"type": "token_count", "info": info})
                result = self.inspect()
                self.assertIsNone(result["pressure"])
                self.assertNotEqual(handoff.choose_action(result), "handoff")

    def test_bad_line_cannot_reuse_preceding_metrics(self):
        self.usage(9000)
        self.inspect()
        with self.path.open("a", encoding="utf-8") as output:
            output.write('{"type": invalid-json}\n')
        result = handoff.inspect_transcript(self.path, SESSION, model=MODEL, cwd=str(self.cwd))
        self.assertIsNone(result["pressure"])
        self.assertNotEqual(handoff.choose_action(result), "handoff")

    def test_incremental_cache_matches_full_read_and_counts_once(self):
        self.compact("one")
        self.usage(5000)
        initial = self.inspect()
        self.compact("two")
        self.usage(8200)
        incremental = self.inspect(cache=initial["cache"])
        full = self.inspect()
        for key in ["root_verified", "pressure", "pressure_basis", "compactions", "count_kind"]:
            self.assertEqual(incremental[key], full[key], key)
        again = self.inspect(cache=incremental["cache"])
        self.assertEqual(again["compactions"], 2)
        self.assertAlmostEqual(again["pressure"], 0.82)

    def test_truncated_log_does_not_preserve_old_count_or_usage(self):
        self.compact("one")
        self.compact("two")
        self.usage(9000)
        previous = self.inspect()
        self.header()
        fresh = self.inspect(cache=previous["cache"])
        self.assertEqual(fresh["compactions"], 0)
        self.assertIsNone(fresh["pressure"])


class HookTests(TranscriptFixture):
    def setUp(self):
        super().setUp()
        self.root = self.cwd / "state"

    def event(self, kind, **overrides):
        event = {"hook_event_name": kind, "session_id": SESSION, "cwd": str(self.cwd),
                 "transcript_path": str(self.path), "model": MODEL, "permission_mode": "default"}
        event.update(overrides)
        return event

    def write_rollout(self):
        self.path.write_text("".join(json.dumps(row) + "\n" for row in self.rows), encoding="utf-8")

    def run_cli(self, args, stdin=""):
        environment = os.environ.copy()
        environment.update(CODEX_HOME=str(self.cwd / "isolated-codex-home"), CODEX_THREAD_ID=SESSION,
                           PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, str(SCRIPT), *args, "--state-dir", str(self.root)],
                              input=stdin, capture_output=True, encoding="utf-8", env=environment,
                              cwd=self.cwd, timeout=15)

    def test_session_start_cli_outputs_standard_hook_context(self):
        self.usage(3000)
        self.write_rollout()
        completed = self.run_cli(["hook"], json.dumps(self.event("SessionStart")))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("context-handoff", result["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("decision", result)

    def test_low_pressure_post_tool_use_is_silent(self):
        self.usage(3000)
        self.write_rollout()
        completed = self.run_cli(["hook"], json.dumps(self.event("PostToolUse")))
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")

    def test_repeated_threshold_notice_is_throttled_after_poll_interval(self):
        self.compact("one")
        self.compact("two")
        self.usage(8500)
        self.write_rollout()
        current = datetime.now(timezone.utc).timestamp()
        with mock.patch.object(handoff.time, "time", return_value=current):
            first = handoff.hook(self.root, self.event("PostToolUse"))
        self.assertIn("handoff", first["hookSpecificOutput"]["additionalContext"])
        with mock.patch.object(handoff.time, "time", return_value=current + 20):
            second = handoff.hook(self.root, self.event("PostToolUse"))
        self.assertIsNone(second)

    def test_post_compact_before_flush_does_not_reuse_cached_metrics(self):
        self.compact("one")
        self.compact("two")
        self.usage(8500)
        self.write_rollout()
        handoff.hook(self.root, self.event("UserPromptSubmit"))
        self.assertIsNone(handoff.hook(self.root, self.event("PostCompact")))
        notice = handoff.hook(self.root, self.event("UserPromptSubmit"))
        if notice:
            self.assertNotIn("handoff，", notice["hookSpecificOutput"]["additionalContext"])
            self.assertNotIn("85%", notice["hookSpecificOutput"]["additionalContext"])
        telemetry = json.loads((self.root / "telemetry" / (SESSION + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(telemetry["action"], "unknown")

    def test_post_compact_before_flush_rejects_replayed_old_usage(self):
        self.compact("one")
        self.compact("two")
        old_usage = self.usage(8500)
        self.write_rollout()
        handoff.hook(self.root, self.event("UserPromptSubmit"))
        handoff.hook(self.root, self.event("PostCompact"))
        self.rows.append(old_usage.copy())
        self.write_rollout()
        handoff.hook(self.root, self.event("UserPromptSubmit"))
        telemetry = json.loads((self.root / "telemetry" / (SESSION + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(telemetry["action"], "unknown")

    def test_parent_id_subagent_never_emits_migration_notice(self):
        self.header(source={"subagent": {"parent_thread_id": SESSION, "depth": 1}})
        self.compact("one")
        self.compact("two")
        self.usage(8500)
        self.write_rollout()
        completed = self.run_cli(["hook"], json.dumps(self.event("UserPromptSubmit")))
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        self.assertFalse(self.root.exists())

    def test_shared_cwd_does_not_make_a_different_chat_the_owner(self):
        self.header(session=OTHER_TARGET)
        self.compact("one")
        self.compact("two")
        self.usage(8500)
        self.write_rollout()
        self.assertIsNone(handoff.hook(self.root, self.event("UserPromptSubmit")))
        self.assertFalse(self.root.exists())

    def test_conflicting_session_metadata_is_not_verified_by_last_record_only(self):
        self.header(source={"subagent": {"parent_thread_id": SESSION, "depth": 1}})
        self.row("session_meta", {"id": SESSION, "cwd": str(self.cwd), "source": "vscode"})
        self.compact("one")
        self.compact("two")
        self.usage(8500)
        self.write_rollout()
        self.assertIsNone(handoff.hook(self.root, self.event("UserPromptSubmit")))

    def test_plan_mode_produces_readonly_advice_without_writing_state(self):
        self.compact("one")
        self.compact("two")
        self.usage(8500)
        self.write_rollout()
        result = handoff.hook(self.root, self.event("UserPromptSubmit", permission_mode="plan"))
        self.assertIn("只读", result["hookSpecificOutput"]["additionalContext"])
        self.assertFalse(self.root.exists())

    def test_transcript_plan_mode_overrides_hook_default_permission(self):
        self.rows[1]["payload"]["collaboration_mode"] = {"mode": "plan"}
        self.usage(7500)
        self.write_rollout()
        result = handoff.hook(self.root, self.event("UserPromptSubmit"))
        self.assertIn("只读", result["hookSpecificOutput"]["additionalContext"])
        self.assertFalse(self.root.exists())

    def test_malformed_and_nonobject_hook_input_never_block_user_work(self):
        for payload in ["not-json", "[]", "null", '"text"', "{}"]:
            with self.subTest(payload=payload):
                completed = self.run_cli(["hook"], payload)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(completed.stdout, "")
        self.assertFalse(self.root.exists())

    def test_readonly_sandbox_does_not_write_observer_state(self):
        self.rows[1]["payload"]["collaboration_mode"] = {"mode": "default"}
        self.rows[1]["payload"]["sandbox_policy"] = {"type": "read-only"}
        self.usage(7500)
        self.write_rollout()
        result = handoff.hook(self.root, self.event("UserPromptSubmit"))
        self.assertIn("只读", result["hookSpecificOutput"]["additionalContext"])
        self.assertFalse(self.root.exists())

    def test_cli_actor_requires_real_root_mode_and_original_cwd(self):
        with mock.patch.object(handoff, "find_transcript", return_value=self.path), \
                mock.patch.object(handoff.os, "getcwd", return_value=str(self.cwd)):
            self.rows[1]["payload"]["collaboration_mode"] = {"mode": "default"}
            self.write_rollout()
            handoff.verify_actor_context(SESSION, str(self.cwd))
            self.rows[1]["payload"]["sandbox_policy"] = {"type": "read-only"}
            self.write_rollout()
            with self.assertRaises(ValueError):
                handoff.verify_actor_context(SESSION, str(self.cwd))
            self.header(source={"subagent": {"parent_thread_id": SESSION}})
            self.rows[1]["payload"]["collaboration_mode"] = {"mode": "default"}
            self.write_rollout()
            with self.assertRaises(ValueError):
                handoff.verify_actor_context(SESSION, str(self.cwd))
            self.header()
            self.rows[1]["payload"]["collaboration_mode"] = {"mode": "plan"}
            self.write_rollout()
            with self.assertRaises(ValueError):
                handoff.verify_actor_context(SESSION, str(self.cwd))
            with self.assertRaises(ValueError):
                handoff.verify_actor_context(SESSION, str(self.cwd / "different-project"))

    def test_probe_cli_reports_estimate_without_exposing_message_content(self):
        self.row("response_item", {"type": "message", "content": [{"type": "input_text", "text": "private sentinel"}]})
        self.usage(7500)
        self.write_rollout()
        completed = self.run_cli(["probe", "--session-id", SESSION, "--transcript", str(self.path),
                                  "--cwd", str(self.cwd), "--model", MODEL])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertAlmostEqual(result["pressure"], 0.75)
        self.assertEqual(result["action"], "checkpoint")
        self.assertNotIn("private sentinel", completed.stdout + completed.stderr)

    def test_actor_identity_is_independent_of_chat_creation_directory(self):
        chat_cwd = self.cwd / "chat-workspace"
        for invalid in (None, "actual_cwd", "subagent", "id", "conflict", "plan", "read-only"):
            with self.subTest(invalid=invalid):
                self.header(cwd=chat_cwd)
                self.rows[1]["payload"]["collaboration_mode"] = {"mode": "default"}
                if invalid == "subagent":
                    self.rows[0]["payload"]["source"] = {"subagent": {"parent_thread_id": SESSION}}
                elif invalid == "id":
                    self.rows[0]["payload"]["id"] = OTHER_TARGET
                elif invalid == "conflict":
                    self.row("session_meta", {"id": OTHER_TARGET, "cwd": str(chat_cwd), "source": "vscode"})
                elif invalid == "plan":
                    self.rows[1]["payload"]["collaboration_mode"] = {"mode": "plan"}
                elif invalid == "read-only":
                    self.rows[1]["payload"]["sandbox_policy"] = {"type": "read-only"}
                self.write_rollout()
                actual_cwd = chat_cwd if invalid == "actual_cwd" else self.cwd
                with mock.patch.object(handoff, "find_transcript", return_value=self.path), \
                        mock.patch.object(handoff.os, "getcwd", return_value=str(actual_cwd)):
                    if invalid:
                        with self.assertRaises(ValueError):
                            handoff.verify_actor_context(SESSION, str(self.cwd))
                    else:
                        handoff.verify_actor_context(SESSION, str(self.cwd))
        self.assertFalse(self.root.exists())

    def run_hook_command(self, command):
        environment = os.environ.copy()
        environment.update(CODEX_HOME=str(self.cwd / "isolated-codex-home"), CODEX_THREAD_ID=SESSION,
                           PYTHONIOENCODING="utf-8")
        return subprocess.run(command, input=json.dumps(self.event("SessionStart")), shell=False,
                              capture_output=True, encoding="utf-8", env=environment, cwd=self.cwd, timeout=15)

    def test_pinned_hook_refuses_changed_script_bytes(self):
        self.usage(3000)
        self.write_rollout()
        candidate = self.cwd / "hook-runtime.py"
        candidate.write_bytes(SCRIPT.read_bytes())
        command = handoff.hook_command(candidate, sys.executable)
        beacon = self.cwd / "tampered-script-ran.txt"
        candidate.write_text(f"from pathlib import Path\nPath({str(beacon)!r}).write_text('executed')\n", encoding="utf-8")
        completed = self.run_hook_command(command)
        self.assertFalse(beacon.exists())
        self.assertNotIn("hookSpecificOutput", completed.stdout)

    def test_hook_isolated_python_ignores_cwd_module_shadow(self):
        self.usage(3000)
        self.write_rollout()
        beacon = self.cwd / "shadow-module-ran.txt"
        (self.cwd / "json.py").write_text(
            f"from pathlib import Path\nPath({str(beacon)!r}).write_text('executed')\nraise RuntimeError('shadow module')\n",
            encoding="utf-8")
        command = handoff.hook_command(SCRIPT, sys.executable)
        self.assertIn("-I", command)
        completed = self.run_hook_command(command)
        self.assertFalse(beacon.exists())
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["hookSpecificOutput"]["hookEventName"], "SessionStart")

    def test_health_distinguishes_manual_and_configured_without_claiming_client_proof(self):
        self.usage(3000)
        self.write_rollout()
        handoff.hook(self.root, self.event("SessionStart"), origin="manual")
        manual = handoff.health(self.root, SESSION)
        self.assertEqual(manual["invocation_counts"]["manual"], 1)
        self.assertEqual(manual["invocation_counts"].get("configured", 0), 0)
        self.assertEqual(manual["client_activation"], "unverified")
        handoff.hook(self.root, self.event("SessionStart"), origin="configured")
        configured = handoff.health(self.root, SESSION)
        self.assertEqual(configured["invocation_counts"]["manual"], 1)
        self.assertEqual(configured["invocation_counts"]["configured"], 1)
        self.assertEqual(configured["client_activation"], "unverified")


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cwd = Path(self.temp.name)
        self.codex_home = self.cwd / "isolated-codex-home"
        self.root = self.codex_home / "context-handoffs"
        environment_patch = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home)})
        environment_patch.start()
        self.addCleanup(environment_patch.stop)
        self.user_reference = self.write_user_log(SESSION, "Implement the agreed change")
        self.artifact = self.cwd / "adopted.txt"
        self.artifact.write_text("adopted version 1\n", encoding="utf-8")
        template = SCRIPT.parents[1] / "references" / "handoff-template.json"
        self.packet = json.loads(template.read_text(encoding="utf-8"))
        self.packet.update(source_thread_id=SESSION, cwd=str(self.cwd), mode="execute")
        self.packet["task"].update(
            goal="Finish the approved change without repeating completed work",
            latest_user_request="Implement the agreed change", scope="Only the requested change",
            success_criteria=["Acceptance check passes", "Review the final output"], status="ongoing")
        self.packet["authorization"]["approved"] = [
            {"action": "Edit the task artifact", "evidence": "Implement the agreed change",
             "evidence_ref": self.user_reference}]
        self.packet["artifacts"] = [{"path": str(self.artifact), "required": True, "status": "adopted"}]
        self.packet["environment"].update(project_root=str(self.cwd), worktree=str(self.cwd))
        self.packet["next_step"].update(action="Review the adopted file against the acceptance criteria",
                                         checks=["Confirm the expected text"],
                                         stop_conditions=["Stop if an artifact changed after the snapshot"])

    def write_user_log(self, actor, text, *, role="user", at=None, cwd=None):
        at = at or datetime.now(timezone.utc).isoformat()
        rows = [
            {"timestamp": at, "type": "session_meta", "payload": {"id": actor, "cwd": str(cwd or self.cwd), "source": "vscode"}},
            {"timestamp": at, "type": "turn_context", "payload": {"model": MODEL, "cwd": str(cwd or self.cwd),
             "turn_id": "test-turn", "collaboration_mode": {"mode": "default"}}},
            {"timestamp": at, "type": "response_item", "payload": {"type": "message", "role": role,
             "content": [{"type": "input_text", "text": text}]}},
        ]
        path = self.codex_home / "sessions" / ("rollout-" + actor + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(row, ensure_ascii=False).encode("utf-8") for row in rows]
        path.write_bytes(b"\n".join(lines) + b"\n")
        return {"thread_id": actor, "line": 3, "sha256": hashlib.sha256(lines[2]).hexdigest()}

    def save(self, final=False):
        return handoff.save_packet(self.root, self.packet, SESSION, final=final)

    def transition(self, action, actor=SESSION, **kwargs):
        return handoff.transition(self.root, SESSION, actor, action, **kwargs)

    def checks(self, state, **overrides):
        value = {"checkpoint_hash": state["checkpoint_hash"], "goal": True, "authorization": True,
                 "environment": True, "artifacts": True, "operations": True, "next_step": True,
                 "mode": "execute", "notes": "Read the current adopted artifact and verified all prerequisites"}
        value["evidence"] = {
            name: [{"path": str(self.artifact), "finding": f"Read the adopted artifact and checked {name} against the user instruction"}]
            for name in ("goal", "authorization", "environment", "artifacts", "operations", "next_step")
        }
        value.update(overrides)
        return value

    def create_receiver(self):
        state = self.save(final=True)
        self.transition("dispatch", hash_value=state["checkpoint_hash"])
        return self.transition("created", target=TARGET, hash_value=state["checkpoint_hash"])

    def ready_receiver(self):
        state = self.create_receiver()
        return self.transition("ready", actor=TARGET, hash_value=state["checkpoint_hash"],
                               checks=self.checks(state))

    def assert_rejected(self, callback):
        with self.assertRaises((ValueError, OSError)):
            callback()

    def test_template_cannot_be_mistaken_for_completed_handoff(self):
        template = json.loads((SCRIPT.parents[1] / "references" / "handoff-template.json").read_text(encoding="utf-8"))
        self.assert_rejected(lambda: handoff.save_packet(self.root, template, SESSION, final=True))

    def test_prepare_freeze_verify_transfer_has_one_project_writer(self):
        prepared = self.save()
        self.assertEqual(prepared["status"], "prepared")
        self.assertTrue(handoff.guard(self.root, SESSION)["can_write_project"])
        frozen = self.save(final=True)
        self.assertEqual(frozen["status"], "frozen")
        self.assertFalse(handoff.guard(self.root, SESSION)["can_write_project"])
        pending = self.transition("dispatch", hash_value=frozen["checkpoint_hash"])
        self.assertEqual(pending["status"], "creation_pending")
        created = self.transition("created", target=TARGET, hash_value=frozen["checkpoint_hash"])
        self.assertEqual(created["status"], "created")
        self.assertFalse(handoff.guard(self.root, TARGET)["can_write_project"])
        ready = self.transition("ready", actor=TARGET, hash_value=created["checkpoint_hash"], checks=self.checks(created))
        self.assertEqual(ready["status"], "ready")
        self.assertFalse(handoff.guard(self.root, SESSION)["can_write_project"])
        self.assertFalse(handoff.guard(self.root, TARGET)["can_write_project"])
        transferred = self.transition("transfer", hash_value=ready["checkpoint_hash"])
        self.assertEqual(transferred["owner"], TARGET)
        self.assertFalse(handoff.guard(self.root, SESSION)["can_write_project"])
        self.assertTrue(handoff.guard(self.root, TARGET)["can_write_project"])
        self.assert_rejected(lambda: self.save())

    def test_duplicate_dispatch_is_rejected_even_when_creation_result_unknown(self):
        frozen = self.save(final=True)
        self.transition("dispatch", hash_value=frozen["checkpoint_hash"])
        self.assert_rejected(lambda: self.transition("dispatch", hash_value=frozen["checkpoint_hash"]))
        self.assertFalse(handoff.guard(self.root, SESSION)["can_write_project"])

    def test_source_transitions_reject_an_explicit_stale_hash(self):
        frozen = self.save(final=True)
        self.assert_rejected(lambda: self.transition("dispatch", hash_value="0" * 64))
        self.assert_rejected(lambda: self.transition("manual", hash_value="0" * 64))
        self.transition("dispatch", hash_value=frozen["checkpoint_hash"])
        self.assert_rejected(lambda: self.transition("created", target=TARGET, hash_value="0" * 64))

    def test_receiver_cannot_take_ownership_before_ready_or_impersonate_source(self):
        created = self.create_receiver()
        self.assert_rejected(lambda: self.transition("transfer", hash_value=created["checkpoint_hash"]))
        self.assert_rejected(lambda: self.transition("ready", actor=OTHER_TARGET,
                                                     hash_value=created["checkpoint_hash"], checks=self.checks(created)))
        self.assert_rejected(lambda: self.transition("transfer", actor=TARGET, hash_value=created["checkpoint_hash"]))

    def test_stale_checkpoint_hash_cannot_be_acknowledged(self):
        created = self.create_receiver()
        self.assert_rejected(lambda: self.transition("ready", actor=TARGET, hash_value="0" * 64,
                                                     checks=self.checks(created, checkpoint_hash="0" * 64)))

    def test_changed_user_request_invalidates_old_ready(self):
        old_ready = self.ready_receiver()
        self.packet["task"]["latest_user_request"] = "Revised: preserve the adopted wording exactly"
        updated = self.save(final=True)
        self.assertEqual(updated["target_thread_id"], TARGET)
        self.assertEqual(updated["status"], "created")
        self.assertNotEqual(updated["checkpoint_hash"], old_ready["checkpoint_hash"])
        self.assert_rejected(lambda: self.transition("transfer", hash_value=old_ready["checkpoint_hash"]))
        self.assertFalse(handoff.guard(self.root, TARGET)["can_write_project"])

    def test_prepared_snapshot_is_refreshed_before_final_freeze(self):
        prepared = self.save()
        self.artifact.write_text("adopted version 2\n", encoding="utf-8")
        self.packet["operations"].append({"description": "Updated the adopted file", "status": "completed",
                                            "external": False, "repeat_policy": "verify_first",
                                            "evidence": str(self.artifact)})
        final = self.save(final=True)
        self.assertNotEqual(final["checkpoint_hash"], prepared["checkpoint_hash"])
        stored = json.loads(Path(final["packet_path"]).read_text(encoding="utf-8"))
        self.assertEqual(stored["operations"][0]["status"], "completed")

    def test_required_artifact_must_exist_at_snapshot_and_receiver_check(self):
        self.artifact.unlink()
        self.assert_rejected(lambda: self.save(final=True))
        self.artifact.write_text("restored\n", encoding="utf-8")
        created = self.create_receiver()
        self.artifact.unlink()
        self.assert_rejected(lambda: self.transition("ready", actor=TARGET, hash_value=created["checkpoint_hash"],
                                                     checks=self.checks(created)))

    def test_artifact_change_after_ready_blocks_transfer(self):
        ready = self.ready_receiver()
        self.artifact.write_text("unreviewed concurrent change\n", encoding="utf-8")
        self.assert_rejected(lambda: self.transition("transfer", hash_value=ready["checkpoint_hash"]))
        self.assertFalse(handoff.guard(self.root, SESSION)["can_write_project"])
        self.assertFalse(handoff.guard(self.root, TARGET)["can_write_project"])

    def test_unfinished_operations_and_active_writers_block_freeze(self):
        self.packet["operations"] = [{"description": "Long-running export", "status": "in_progress",
                                       "external": False, "repeat_policy": "verify_first", "evidence": "job-123"}]
        self.assert_rejected(lambda: self.save(final=True))
        self.packet["operations"] = []
        self.packet["resources"] = [{"description": "Agent still editing", "status": "running", "writes_project": True}]
        self.assert_rejected(lambda: self.save(final=True))
        self.packet["resources"][0]["status"] = "quiesced"
        self.assertEqual(self.save(final=True)["status"], "frozen")

    def test_unknown_external_result_requires_never_repeat(self):
        self.packet["operations"] = [{"description": "Publishing request timed out", "status": "unknown",
                                       "external": True, "repeat_policy": "verify_first", "evidence": "query job-123"}]
        self.assert_rejected(lambda: self.save(final=True))
        self.packet["operations"][0]["repeat_policy"] = "never"
        self.assertEqual(self.save(final=True)["status"], "frozen")

    def test_missing_access_verification_blocks_ready(self):
        created = self.create_receiver()
        self.assert_rejected(lambda: self.transition("ready", actor=TARGET, hash_value=created["checkpoint_hash"],
                                                     checks=self.checks(created, environment=False)))

    def test_plan_mode_cannot_become_execute_through_handoff(self):
        self.packet["mode"] = "plan"
        self.save()
        self.assert_rejected(lambda: self.save(final=True))

    def test_manual_path_preserves_packet_and_allows_exactly_one_receiver(self):
        frozen = self.save(final=True)
        waiting = self.transition("manual", hash_value=frozen["checkpoint_hash"])
        self.assertEqual(waiting["status"], "manual_wait")
        self.assertIsNone(waiting["owner"])
        self.assertTrue(Path(waiting["packet_path"]).is_file())
        self.assertFalse(handoff.guard(self.root, SESSION)["can_write_project"])
        claimed = self.transition("claim", actor=TARGET, hash_value=waiting["checkpoint_hash"], checks=self.checks(waiting))
        self.assertEqual(claimed["owner"], TARGET)
        self.assert_rejected(lambda: self.transition("claim", actor=OTHER_TARGET, hash_value=waiting["checkpoint_hash"],
                                                     checks=self.checks(waiting)))
        self.assertFalse(handoff.guard(self.root, SESSION)["can_write_project"])
        self.assertTrue(handoff.guard(self.root, TARGET)["can_write_project"])

    def test_manual_wait_can_refresh_without_reclaiming_source_ownership(self):
        frozen = self.save(final=True)
        old_waiting = self.transition("manual", hash_value=frozen["checkpoint_hash"])
        self.packet["task"]["latest_user_request"] = "New instruction: preserve the exact adopted wording"
        refreshed = self.save(final=True)
        self.assertEqual(refreshed["status"], "manual_wait")
        self.assertIsNone(refreshed["owner"])
        self.assertFalse(handoff.guard(self.root, SESSION)["can_write_project"])
        self.assert_rejected(lambda: self.transition("claim", actor=TARGET,
                                                     hash_value=old_waiting["checkpoint_hash"], checks=self.checks(old_waiting)))
        claimed = self.transition("claim", actor=TARGET, hash_value=refreshed["checkpoint_hash"], checks=self.checks(refreshed))
        self.assertEqual(claimed["owner"], TARGET)

    def test_user_cancellation_revokes_untransferred_handoffs(self):
        for status in ["prepared", "frozen", "created", "ready", "manual_wait"]:
            with self.subTest(status=status):
                self.root = self.cwd / ("cancel-" + status)
                if status == "prepared":
                    state = self.save()
                elif status == "created":
                    state = self.create_receiver()
                elif status == "ready":
                    state = self.ready_receiver()
                else:
                    state = self.save(final=True)
                    if status == "manual_wait":
                        state = self.transition("manual", hash_value=state["checkpoint_hash"])
                cancelled = self.transition("cancel", hash_value=state["checkpoint_hash"],
                                            checks={"evidence": "User said: cancel this task in source chat"})
                self.assertEqual(cancelled["status"], "cancelled")
                self.assertIsNone(cancelled["owner"])
                self.assertFalse(handoff.guard(self.root, SESSION)["can_write_project"])
                self.assert_rejected(lambda: self.transition("claim", actor=TARGET,
                                                             hash_value=state["checkpoint_hash"], checks=self.checks(state)))

    def test_only_confirmed_noncreation_releases_creation_pending(self):
        frozen = self.save(final=True)
        proof = {"not_created": True, "evidence": "Native tool explicitly returned: no chat was created"}
        self.assert_rejected(lambda: self.transition("creation-failed", hash_value=frozen["checkpoint_hash"], checks=proof))
        self.transition("dispatch", hash_value=frozen["checkpoint_hash"])
        self.assert_rejected(lambda: self.transition("creation-failed", hash_value=frozen["checkpoint_hash"],
                                                     checks={"evidence": "Native creation tool timed out"}))
        self.assert_rejected(lambda: self.transition("creation-failed", hash_value=frozen["checkpoint_hash"],
                                                     checks={"not_created": False, "evidence": "Native creation tool timed out"}))
        recovered = self.transition("creation-failed", hash_value=frozen["checkpoint_hash"], checks=proof)
        self.assertEqual(recovered["status"], "frozen")
        self.assertEqual(recovered["creation_failure"], proof)
        self.assertEqual(self.transition("dispatch", hash_value=recovered["checkpoint_hash"])["status"], "creation_pending")

    def test_created_records_receiver_despite_inflight_artifact_change(self):
        frozen = self.save(final=True)
        self.transition("dispatch", hash_value=frozen["checkpoint_hash"])
        self.artifact.write_text("Changed while native creation was in flight\n", encoding="utf-8")
        created = self.transition("created", target=TARGET, hash_value=frozen["checkpoint_hash"])
        self.assertEqual(created["target_thread_id"], TARGET)
        self.assertEqual(created["status"], "created")
        self.assert_rejected(lambda: self.transition("ready", actor=TARGET, hash_value=created["checkpoint_hash"],
                                                     checks=self.checks(created)))
        refreshed = self.save(final=True)
        self.assertEqual(refreshed["target_thread_id"], TARGET)
        self.assertNotEqual(refreshed["checkpoint_hash"], created["checkpoint_hash"])
        self.assertEqual(self.transition("ready", actor=TARGET, hash_value=refreshed["checkpoint_hash"],
                                        checks=self.checks(refreshed))["status"], "ready")

    def test_member_registration_survives_one_failed_ledger_write(self):
        for action in ["created", "claim"]:
            with self.subTest(action=action):
                self.root = self.cwd / ("persist-failure-" + action)
                frozen = self.save(final=True)
                if action == "created":
                    self.transition("dispatch", hash_value=frozen["checkpoint_hash"])
                    kwargs = {"target": TARGET, "hash_value": frozen["checkpoint_hash"]}
                else:
                    self.transition("manual", hash_value=frozen["checkpoint_hash"])
                    kwargs = {"actor": TARGET, "hash_value": frozen["checkpoint_hash"], "checks": self.checks(frozen)}
                with mock.patch.object(handoff, "persist_state", side_effect=OSError("Simulated interrupted ledger write")):
                    self.assert_rejected(lambda: self.transition(action, **kwargs))
                self.assertTrue(handoff.member_file(self.root, TARGET).is_file())
                self.assertFalse(handoff.guard(self.root, TARGET)["can_write_project"])
                retried = self.transition(action, **kwargs)
                self.assertEqual(retried["target_thread_id"], TARGET)
                self.assertEqual(retried["status"], "created" if action == "created" else "transferred")

    def test_simultaneous_manual_claims_have_one_winner(self):
        frozen = self.save(final=True)
        waiting = self.transition("manual", hash_value=frozen["checkpoint_hash"])

        def claim(actor):
            try:
                state = self.transition("claim", actor=actor, hash_value=waiting["checkpoint_hash"], checks=self.checks(waiting))
                return state["owner"]
            except (ValueError, OSError):
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            winners = [result for result in pool.map(claim, [TARGET, OTHER_TARGET]) if result]
        self.assertEqual(len(winners), 1)
        self.assertTrue(handoff.guard(self.root, winners[0])["can_write_project"])

    def test_two_sources_cannot_simultaneously_register_the_same_receiver(self):
        first = self.save(final=True)
        self.transition("manual", hash_value=first["checkpoint_hash"])
        second_packet = json.loads(json.dumps(self.packet))
        second_packet["source_thread_id"] = OTHER_TARGET
        second = handoff.save_packet(self.root, second_packet, OTHER_TARGET, final=True)
        handoff.transition(self.root, OTHER_TARGET, OTHER_TARGET, "manual", hash_value=second["checkpoint_hash"])
        target_membership = handoff.member_file(self.root, TARGET)
        original_exists = Path.exists
        rendezvous = threading.Barrier(2)

        def concurrent_exists(path):
            exists = original_exists(path)
            if path == target_membership and not exists:
                try:
                    rendezvous.wait(timeout=0.5)
                except threading.BrokenBarrierError:
                    pass  # A serialized membership implementation need not rendezvous.
            return exists

        def claim(state):
            try:
                result = handoff.transition(self.root, state["source_thread_id"], TARGET, "claim",
                                            hash_value=state["checkpoint_hash"], checks=self.checks(state))
                return result["source_thread_id"]
            except (ValueError, OSError):
                return None

        with mock.patch.object(Path, "exists", concurrent_exists):
            with ThreadPoolExecutor(max_workers=2) as pool:
                winners = [result for result in pool.map(claim, [first, second]) if result]
        self.assertEqual(len(winners), 1)

    def test_code_and_content_packets_preserve_requirements_and_acceptance_levels(self):
        for scenario in ["code", "content"]:
            with self.subTest(scenario=scenario):
                self.packet["environment"]["uncommitted_changes"] = [
                    {"path": str(self.artifact), "owner": "user", "instruction": "Preserve existing changes"}]
                self.packet["decisions"] = [{"decision": "Keep adopted version", "reason": "User explicitly adopted it"}]
                self.packet["evidence"] = [{"kind": "verified", "path": str(self.artifact), "checked_at": "2026-09-29T00:00:00Z"}]
                self.packet["verification"]["machine"] = ["Local structure check passed"]
                self.packet["verification"]["human"] = ["Pending review"]
                self.packet["verification"]["release"] = ["Not published"]
                if scenario == "content":
                    self.packet["task"]["goal"] = "完成采用稿：保留道具状态与台词连续性"
                    self.packet["verification"]["content_continuity"] = ["Character holds the book in the left hand"]
                    self.packet["verification"]["source_licensing"] = ["User-owned reference; public release review pending"]
                state = self.save()
                stored = json.loads(Path(state["packet_path"]).read_text(encoding="utf-8"))
                for key in ["task", "authorization", "decisions", "evidence", "environment", "verification", "next_step"]:
                    self.assertEqual(stored[key], self.packet[key], key)

    def run_cli(self, args, actor=SESSION):
        environment = os.environ.copy()
        environment.update(CODEX_HOME=str(self.codex_home), CODEX_THREAD_ID=actor, PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, "-I", str(SCRIPT), *args, "--state-dir", str(self.root)],
                              capture_output=True, encoding="utf-8", env=environment, cwd=self.cwd, timeout=15)

    def test_sensitive_findings_do_not_include_raw_values_or_dictionary_keys(self):
        synthetic_values = [
            "sk-test-" + "a" * 36,
            "ghp_" + "b" * 36,
            "-----BEGIN PRIVATE KEY-----\nSYNTHETICONLY\n-----END PRIVATE KEY-----",
            "Bearer synthetic_auth_value_123456789",
            "password=synthetic_password_123",
            "https://user:synthetic_password@example.invalid/private",
            "https://example.invalid/private?signature=synthetic_signature_value",
            "person@example.invalid", "13800138000", "11010519491231002X",
        ]
        for secret in synthetic_values:
            with self.subTest(kind=synthetic_values.index(secret)):
                findings = handoff.sensitive_findings({"unknowns": [secret]})
                self.assertTrue(findings)
                self.assertNotIn(secret, json.dumps(findings))
        secret_key = "sk-test-" + "c" * 36
        findings = handoff.sensitive_findings({secret_key: "innocuous text"})
        self.assertTrue(findings)
        self.assertNotIn(secret_key, json.dumps(findings))
        for value in [8675309, [8675309], {"nested": 8675309}, True]:
            with self.subTest(credential_type=type(value).__name__):
                findings = handoff.sensitive_findings({"password": value})
                self.assertTrue(findings)
                self.assertNotIn("8675309", json.dumps(findings))

    def test_phone_detection_does_not_misclassify_hexadecimal_fingerprints(self):
        fingerprint = "a" * 20 + "13812345678" + "b" * 33
        self.assertEqual(len(fingerprint), 64)
        self.assertEqual(handoff.sensitive_findings({"sha256": fingerprint}), [])
        self.assertTrue(handoff.sensitive_findings({"notes": "Call 13812345678 to continue"}))

    def test_rejected_secret_never_reaches_saved_packet_or_cli_error(self):
        secret = "sk-test-" + "d" * 36
        self.packet["unknowns"] = [{"nested": secret}]
        packet_input = self.cwd / "input-packet.json"
        packet_input.write_text(json.dumps(self.packet), encoding="utf-8")
        completed = self.run_cli(["save", "--packet", str(packet_input), "--final"])
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Sensitive", completed.stderr)
        self.assertNotIn(secret, completed.stdout + completed.stderr)
        self.packet["unknowns"] = []
        created = self.create_receiver()
        checks = self.checks(created, notes="Review notes: " + secret)
        self.assert_rejected(lambda: self.transition("ready", actor=TARGET,
                                                     hash_value=created["checkpoint_hash"], checks=checks))
        self.assert_rejected(lambda: self.transition("cancel", hash_value=created["checkpoint_hash"],
                                                     checks={"evidence": secret}))
        if self.root.exists():
            for saved in self.root.rglob("*"):
                if saved.is_file():
                    self.assertNotIn(secret.encode("utf-8"), saved.read_bytes())

    def test_authorization_requires_unchanged_native_user_reference(self):
        approval = self.packet["authorization"]["approved"][0]
        valid_ref = dict(approval["evidence_ref"])
        for invalid in [None, {**valid_ref, "line": 1}, {**valid_ref, "sha256": "0" * 64}]:
            with self.subTest(reference=invalid):
                approval["evidence_ref"] = invalid
                self.assert_rejected(lambda: self.save(final=True))
        approval["evidence_ref"] = self.write_user_log(SESSION, "I propose editing this task", role="assistant")
        self.assert_rejected(lambda: self.save(final=True))
        approval["evidence_ref"] = self.write_user_log(SESSION, "Implement the agreed change")
        self.assertEqual(self.save(final=True)["status"], "frozen")

    def test_user_reference_entrypoints_reject_unverified_transcript_identity(self):
        for identity in ["subagent", "mismatched_id", "conflicting_metadata"]:
            with self.subTest(identity=identity):
                reference = self.write_user_log(SESSION, "Implement the agreed change")
                path = self.codex_home / "sessions" / ("rollout-" + SESSION + ".jsonl")
                lines = path.read_bytes().splitlines()
                original_meta = lines[0]
                metadata = json.loads(lines[0])
                if identity == "mismatched_id":
                    metadata["payload"]["id"] = OTHER_TARGET
                else:
                    metadata["payload"]["source"] = {"subagent": {"parent_thread_id": SESSION, "depth": 1}}
                lines[0] = json.dumps(metadata).encode("utf-8")
                if identity == "conflicting_metadata":
                    lines.append(original_meta)
                path.write_bytes(b"\n".join(lines) + b"\n")
                for entrypoint, invoke in [
                    ("resolve_user_ref", lambda: handoff.resolve_user_ref(reference)),
                    ("user_messages", lambda: handoff.user_messages(SESSION)),
                ]:
                    with self.subTest(entrypoint=entrypoint):
                        self.assert_rejected(invoke)

    def test_real_root_user_evidence_remains_valid_without_fresh_usage(self):
        reference = self.write_user_log(SESSION, "Implement the agreed change", at="2020-01-01T00:00:00+00:00")
        path = self.codex_home / "sessions" / ("rollout-" + SESSION + ".jsonl")
        self.assertIsNone(handoff.inspect_transcript(path, SESSION)["pressure"])
        self.assertEqual(handoff.resolve_user_ref(reference)["payload"]["role"], "user")
        self.assertEqual(handoff.user_messages(SESSION)[0]["sha256"], reference["sha256"])
        self.packet["authorization"]["approved"][0]["evidence_ref"] = reference
        self.assertEqual(self.save(final=True)["status"], "frozen")

    def test_receiver_true_flags_need_real_specific_evidence(self):
        created = self.create_receiver()
        for mutation in ["missing", "missing_file", "vague"]:
            with self.subTest(mutation=mutation):
                checks = self.checks(created)
                if mutation == "missing":
                    del checks["evidence"]["authorization"]
                elif mutation == "missing_file":
                    checks["evidence"]["environment"][0]["path"] = str(self.cwd / "not-found.txt")
                else:
                    checks["evidence"]["goal"][0]["finding"] = "checked"
                self.assert_rejected(lambda: self.transition("ready", actor=TARGET,
                                                             hash_value=created["checkpoint_hash"], checks=checks))

    def test_receiver_evidence_change_is_rechecked_at_transfer(self):
        created = self.create_receiver()
        verification_file = self.cwd / "verification.txt"
        verification_file.write_text("Reviewed the actual worktree and dependencies\n", encoding="utf-8")
        checks = self.checks(created)
        checks["evidence"]["environment"][0]["path"] = str(verification_file)
        ready = self.transition("ready", actor=TARGET, hash_value=created["checkpoint_hash"], checks=checks)
        verification_file.write_text("Changed verification evidence\n", encoding="utf-8")
        self.assert_rejected(lambda: self.transition("transfer", hash_value=ready["checkpoint_hash"]))

    def test_user_messages_cli_lists_references_without_user_text(self):
        private_text = "PRIVATE_USER_MESSAGE_SENTINEL_DO_NOT_DISPLAY"
        expected_ref = self.write_user_log(SESSION, private_text)
        completed = self.run_cli(["user-messages", "--session-id", SESSION])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(expected_ref["sha256"], completed.stdout)
        self.assertNotIn(private_text, completed.stdout + completed.stderr)

    def test_manual_claim_cli_requires_fresh_native_user_invitation(self):
        frozen = self.save(final=True)
        waiting = self.transition("manual", hash_value=frozen["checkpoint_hash"])
        marker = f"CONTEXT-HANDOFF-CLAIM:{SESSION}:{waiting['checkpoint_hash']}:{waiting['claim_nonce']}"
        issued_at = datetime.fromisoformat(waiting["manual_issued_at"].replace("Z", "+00:00"))
        checks_path = self.cwd / "checks.json"
        checks_path.write_text(json.dumps(self.checks(waiting)), encoding="utf-8")
        arguments = ["claim", "--source", SESSION, "--hash", waiting["checkpoint_hash"], "--checks", str(checks_path)]
        for text, role, at in [
            ("Continue unrelated work", "user", issued_at + timedelta(seconds=1)),
            (marker, "assistant", issued_at + timedelta(seconds=1)),
            (marker, "user", issued_at - timedelta(seconds=1)),
        ]:
            with self.subTest(role=role, at=at.isoformat()):
                self.write_user_log(TARGET, text, role=role, at=at.isoformat(), cwd=self.cwd / "chat-workspace")
                completed = self.run_cli(arguments, actor=TARGET)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIsNone(handoff.read_json(handoff.state_file(self.root, SESSION))["owner"])
        self.write_user_log(TARGET, "Accept this project handoff. " + marker,
                            at=(issued_at + timedelta(seconds=1)).isoformat(), cwd=self.cwd / "chat-workspace")
        completed = self.run_cli(arguments, actor=TARGET)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["owner"], TARGET)

    def test_claim_checks_current_nonce_inside_ledger_lock_even_when_packet_hash_is_unchanged(self):
        frozen = self.save(final=True)
        original = self.transition("manual", hash_value=frozen["checkpoint_hash"])
        old_marker = f"CONTEXT-HANDOFF-CLAIM:{SESSION}:{original['checkpoint_hash']}:{original['claim_nonce']}"
        refreshed = self.save(final=True)
        self.assertEqual(refreshed["checkpoint_hash"], original["checkpoint_hash"])
        self.assertNotEqual(refreshed["claim_nonce"], original["claim_nonce"])
        after_refresh = datetime.fromisoformat(refreshed["manual_issued_at"].replace("Z", "+00:00")) + timedelta(seconds=1)
        self.write_user_log(TARGET, old_marker, at=after_refresh.isoformat())
        verify_invitation = handoff.verify_manual_invitation
        observed_lock = []

        def verify_under_lock(state, actor):
            observed_lock.append((Path(state["state_path"]).parent / "state.lock").is_file())
            return verify_invitation(state, actor)

        with mock.patch.object(handoff, "verify_manual_invitation", side_effect=verify_under_lock):
            self.assert_rejected(lambda: self.transition("claim", actor=TARGET,
                                                         hash_value=refreshed["checkpoint_hash"], checks=self.checks(refreshed),
                                                         require_user_invitation=True))
            self.assertIsNone(handoff.read_json(handoff.state_file(self.root, SESSION))["owner"])
            new_marker = f"CONTEXT-HANDOFF-CLAIM:{SESSION}:{refreshed['checkpoint_hash']}:{refreshed['claim_nonce']}"
            self.write_user_log(TARGET, new_marker, at=after_refresh.isoformat())
            claimed = self.transition("claim", actor=TARGET, hash_value=refreshed["checkpoint_hash"],
                                      checks=self.checks(refreshed), require_user_invitation=True)
        self.assertEqual(observed_lock, [True, True])
        self.assertEqual(claimed["owner"], TARGET)


if __name__ == "__main__":
    unittest.main()
