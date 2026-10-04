"""Opt-in native smoke for interrupt, restart reconciliation, and safe continuation."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from relay.manager import Manager
from relay.transport import CodexClient
from tests.live_smoke import ControlledStop, codex_version, now, write_report


class RecordingClient:
    """Record app-server requests while keeping this smoke's turns inexpensive."""

    def __init__(self):
        self.client = CodexClient()
        self.methods: list[str] = []
        self.response_count = 0
        self._events: list[dict] = []
        self.events: list[dict] = []
        self.started: set[tuple[str, str]] = set()
        self.interrupt_after_started = False

    def request(self, method, params=None, timeout=30):
        if method == "turn/interrupt":
            key = ((params or {}).get("threadId"), (params or {}).get("turnId"))
            if key not in self.started:
                raise AssertionError("Manager sent turn/interrupt before the matching native turn/started")
            self.interrupt_after_started = True
        self.methods.append(method)
        if method == "turn/start":
            params = dict(params or {})
            params["effort"] = "low"
        return self.client.request(method, params, timeout)

    def respond(self, request_id, result):
        if result.get("decision") == "accept" or result.get("permissions"):
            raise AssertionError("The recovery smoke must not approve an action or permission expansion")
        self.response_count += 1
        return self.client.respond(request_id, result)

    def drain_events(self):
        self._events.extend(self.client.drain_events())
        for index, event in enumerate(self._events):
            if "id" in event or event.get("method") == "turn/started":
                ready, self._events = self._events[:index + 1], self._events[index + 1:]
                break
        else:
            ready, self._events = self._events, []
        for event in ready:
            params = event.get("params", {})
            turn = params.get("turn", {})
            self.events.append({
                "method": event.get("method"), "threadId": params.get("threadId"),
                "turnId": params.get("turnId") or turn.get("id"),
            })
            if event.get("method") == "turn/started":
                self.started.add((params.get("threadId"), params.get("turnId") or turn.get("id")))
        return ready

    def close(self):
        return self.client.close()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def print_state(task: dict, previous: dict | None) -> dict:
    current = {
        "state": task["state"], "purpose": task["purpose"],
        "generation": task["generation"], "workTurns": task["work_turns"],
        "threadId": task["thread_id"], "turnId": task["turn_id"],
        "pending": len(task["pending"]), "inflight": len(task["inflight"]),
    }
    if current != previous:
        print(json.dumps(current, ensure_ascii=False), flush=True)
    return current


def stop_on_unsafe_state(task: dict) -> None:
    if task["pending"]:
        raise ControlledStop("A native approval or user-input request is pending")
    if task["state"] == "needs_reconcile":
        raise ControlledStop("The manager requires reconciliation; no request was retried")
    if task["state"] in {"blocked", "completed"}:
        raise ControlledStop(f"Unexpected terminal state: {task['state']}")


def wait_for_interrupted(manager: Manager, task_id: str, turn_id: str,
                         timeout: float = 300) -> dict:
    deadline = time.monotonic() + timeout
    previous = None
    while time.monotonic() < deadline:
        manager.poll()
        task = manager.get_task(task_id)
        previous = print_state(task, previous)
        stop_on_unsafe_state(task)
        if task["state"] == "idle":
            raise ControlledStop("The first turn reached idle instead of confirming interruption")
        if task["state"] == "paused":
            completions = [event for event in task["events"] if event["kind"] == "turn_completed"
                           and event["data"].get("turn_id") == turn_id]
            if not completions or completions[0]["data"].get("status") != "interrupted":
                raise ControlledStop("Paused state lacks a native interrupted completion for the known turn")
            return task
        time.sleep(2)
    raise TimeoutError(f"Native turn did not confirm interruption within {timeout:g} seconds")


def wait_for_idle(manager: Manager, task_id: str, expected_turns: int,
                  timeout: float = 300) -> dict:
    deadline = time.monotonic() + timeout
    previous = None
    while time.monotonic() < deadline:
        manager.poll()
        task = manager.get_task(task_id)
        previous = print_state(task, previous)
        stop_on_unsafe_state(task)
        if task["state"] == "paused":
            raise ControlledStop("The verification turn was interrupted instead of completing")
        if task["state"] == "idle" and task["work_turns"] == expected_turns:
            return task
        time.sleep(2)
    raise TimeoutError(f"Verification turn did not reach idle within {timeout:g} seconds")


def run(root: Path) -> int:
    root = root.resolve()
    if root.exists():
        raise ValueError("--root must be a new path")
    if root.is_relative_to(REPO) or REPO.is_relative_to(root):
        raise ValueError("--root must be outside the repository")
    root.mkdir(parents=True)
    project = root / "project"
    state = root / "state"
    project.mkdir()
    state.mkdir()
    sentinel = "context-relay-recovery-" + uuid.uuid4().hex
    sentinel_path = project / "sentinel.txt"
    sentinel_path.write_text(sentinel + "\n", encoding="utf-8")
    before_hash = file_hash(sentinel_path)
    report = {
        "status": "running", "startedAt": now(), "codexVersion": codex_version(),
        "sentinel": sentinel, "beforeHash": before_hash, "interrupt": None,
        "restart": None, "verification": None, "manualSnapshotVerified": False,
    }
    report_path = root / "report.json"
    first_client = None
    first_manager = None
    second_client = None
    second_manager = None
    exit_code = 1
    try:
        first_client = RecordingClient()
        first_manager = Manager(state_dir=state, client=first_client)
        goal = (
            "Read sentinel.txt and report its complete contents. This is a read-only local task. "
            "Do not modify files, use network, MCP servers, apps, subagents, or background processes."
        )
        created = first_manager.create_task("Controlled interrupted read", project, goal, mode="read-only", direct=False)
        task_id = created["id"]
        started = first_manager.start(task_id)
        interrupted_turn = started["turn_id"]
        if not interrupted_turn:
            raise AssertionError("The first turn has no known native ID")
        report["interrupt"] = {
            "taskId": started["id"], "threadId": started["thread_id"],
            "generation": started["generation"], "turnId": interrupted_turn,
            "nativeStartedObserved": False, "interruptAfterStarted": False,
            "nativeStatus": None, "state": started["state"],
        }
        first_manager.pause(task_id)
        paused = wait_for_interrupted(first_manager, task_id, interrupted_turn)
        identity = (paused["id"], paused["thread_id"], paused["generation"])
        started_key = (paused["thread_id"], interrupted_turn)
        if started_key not in first_client.started or not first_client.interrupt_after_started:
            raise AssertionError("Interrupt was not sent after the matching native turn/started")
        report["interrupt"].update(
            nativeStartedObserved=True, interruptAfterStarted=True,
            nativeStatus="interrupted", state=paused["state"], methods=list(first_client.methods),
            events=list(first_client.events), responseCount=first_client.response_count)
        first_manager.close()

        second_client = RecordingClient()
        second_manager = Manager(state_dir=state, client=second_client)
        loaded = second_manager.get_task(task_id)
        if (loaded["id"], loaded["thread_id"], loaded["generation"], loaded["state"]) != (*identity, "paused"):
            raise AssertionError("Restart did not preserve the paused task identity")
        reconciled = second_manager.reconcile(task_id)
        if (reconciled["id"], reconciled["thread_id"], reconciled["generation"], reconciled["state"]) != (
                *identity, "paused"):
            raise AssertionError("Read-only reconciliation changed task identity or ownership")
        report["restart"] = {
            "taskId": reconciled["id"], "threadId": reconciled["thread_id"],
            "generation": reconciled["generation"], "state": reconciled["state"],
            "methodsThroughReconcile": list(second_client.methods),
        }

        verification_prompt = (
            "Read sentinel.txt now and return the complete unique string it contains in your response. "
            "Do not modify any file or use network, MCP servers, apps, subagents, or background processes."
        )
        resumed = second_manager.start(task_id, verification_prompt)
        verification_turn = resumed["turn_id"]
        if not verification_turn or verification_turn == interrupted_turn:
            raise AssertionError("The verification did not start a distinct native turn")
        completed = wait_for_idle(second_manager, task_id, 1)
        after_hash = file_hash(sentinel_path)
        if sentinel not in completed["last_message"]:
            raise AssertionError("The completed native response did not contain the sentinel")
        if after_hash != before_hash:
            raise AssertionError("sentinel.txt changed during read-only recovery")
        if (completed["id"], completed["thread_id"], completed["generation"]) != identity:
            raise AssertionError("Continuation changed task identity or ownership")
        allowed = {"config/read", "thread/start", "mcpServerStatus/list", "turn/start",
                   "turn/interrupt", "thread/read", "thread/resume"}
        actual_methods = first_client.methods + second_client.methods
        if any(method not in allowed for method in actual_methods):
            raise AssertionError(f"Unexpected app-server request sequence: {actual_methods}")
        report["verification"] = {
            "turnId": verification_turn, "state": completed["state"],
            "workTurns": completed["work_turns"], "responseContainsSentinel": True,
            "afterHash": after_hash, "hashUnchanged": True,
            "methods": list(second_client.methods), "responseCount": second_client.response_count,
        }

        turn_starts = second_client.methods.count("turn/start")
        thread_starts = second_client.methods.count("thread/start")
        method_count = len(second_client.methods)
        snapshotted = second_manager.prepare_snapshot(task_id)
        draft_path = Path(snapshotted["draft"]["path"]).resolve()
        if not draft_path.is_relative_to(state.resolve()):
            raise AssertionError("Manual snapshot was not stored under the isolated state directory")
        draft = json.loads(draft_path.read_text(encoding="utf-8"))
        if draft.get("kind") != "preparatory" or draft.get("ready") is not False:
            raise AssertionError("Manual snapshot was presented as final or READY")
        if draft.get("files", {}).get("sentinel.txt") != before_hash:
            raise AssertionError("Manual snapshot did not preserve the verified sentinel hash")
        if (second_client.methods.count("turn/start") != turn_starts
                or second_client.methods.count("thread/start") != thread_starts):
            raise AssertionError("Manual snapshot unexpectedly started inference or a new thread")
        if second_client.methods[method_count:] != ["thread/read"]:
            raise AssertionError("Manual snapshot made requests beyond one source thread/read")
        report["manualSnapshotVerified"] = True
        report["snapshot"] = {
            "kind": draft["kind"], "ready": draft["ready"],
            "sentinelHashMatches": True, "requestMethods": second_client.methods[method_count:],
            "turnStartCountUnchanged": True, "threadStartCountUnchanged": True,
        }
        report.update(status="passed", finishedAt=now())
        exit_code = 0
    except Exception as error:
        report.update(
            status="stopped" if isinstance(error, ControlledStop) else "failed",
            finishedAt=now(),
            error={"type": type(error).__name__, "message": str(error).replace(str(root), "<recovery-root>")},
        )
        report["observed"] = {
            "firstMethods": list(first_client.methods) if first_client else [],
            "firstEvents": list(first_client.events) if first_client else [],
            "firstResponseCount": first_client.response_count if first_client else 0,
            "secondMethods": list(second_client.methods) if second_client else [],
            "secondEvents": list(second_client.events) if second_client else [],
            "secondResponseCount": second_client.response_count if second_client else 0,
        }
    finally:
        try:
            if first_manager is not None:
                first_manager.close()
            elif first_client is not None:
                first_client.close()
            if second_manager is not None:
                second_manager.close()
            elif second_client is not None:
                second_client.close()
        except Exception as error:
            report.update(
                status="failed", finishedAt=now(),
                closeError={"type": type(error).__name__,
                            "message": str(error).replace(str(root), "<recovery-root>")},
            )
            exit_code = 1
        write_report(report_path, report)
        print(json.dumps({"status": report["status"], "report": str(report_path)}, ensure_ascii=False))
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="run the native recovery smoke")
    parser.add_argument("--root", type=Path, help="new isolated directory outside the repository")
    args = parser.parse_args()
    if not args.run:
        parser.print_help()
        return 0
    if args.root is None:
        parser.error("--run requires --root")
    return run(args.root)


if __name__ == "__main__":
    raise SystemExit(main())
