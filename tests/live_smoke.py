"""Opt-in native smoke: three work turns separated by two verified handoffs."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from relay.manager import Manager
from relay.transport import CodexClient, discover_codex_command


class ControlledStop(RuntimeError):
    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.details = details


class LowEffortClient:
    """Use low effort for this smoke's turns without changing Manager defaults."""

    def __init__(self):
        self.client = CodexClient()

    def request(self, method, params=None, timeout=30):
        if method == "turn/start":
            params = dict(params or {})
            params["effort"] = "low"
        return self.client.request(method, params, timeout)

    def respond(self, request_id, result):
        return self.client.respond(request_id, result)

    def drain_events(self):
        return self.client.drain_events()

    def close(self):
        return self.client.close()


class RecordingClient(LowEffortClient):
    """Record method names so restart verification can prove no turn was started."""

    def __init__(self):
        super().__init__()
        self.methods = []

    def request(self, method, params=None, timeout=30):
        self.methods.append(method)
        return super().request(method, params, timeout)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def codex_version() -> str:
    command = discover_codex_command()
    result = subprocess.run(
        [*command[:-1], "--version"], capture_output=True, text=True, timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if result.returncode:
        raise RuntimeError("Could not read the native Codex version")
    return result.stdout.strip()


def counter_value(project: Path) -> int:
    text = (project / "counter.txt").read_text(encoding="utf-8").strip()
    if not text.isdigit():
        raise AssertionError("counter.txt is not a non-negative integer")
    return int(text)


def wait_for_idle(manager: Manager, task_id: str, generation: int, work_turns: int,
                  timeout: float = 300) -> dict:
    deadline = time.monotonic() + timeout
    previous = None
    while time.monotonic() < deadline:
        manager.poll()
        task = manager.get_task(task_id)
        signature = {
            "state": task["state"], "purpose": task["purpose"],
            "generation": task["generation"], "workTurns": task["work_turns"],
            "threadId": task["thread_id"], "receiverId": task["receiver_id"],
            "pending": len(task["pending"]), "inflight": len(task["inflight"]),
        }
        if signature != previous:
            print(json.dumps(signature, ensure_ascii=False), flush=True)
            previous = signature
        if task["pending"]:
            raise ControlledStop("A native approval or user-input request is pending")
        if task["state"] == "needs_reconcile":
            details = {"managerError": task.get("error", "")}
            if task.get("purpose") == "verify":
                try:
                    ready = json.loads(task.get("last_message", ""))
                    details["receiverReady"] = {
                        "ready": ready.get("ready"), "gaps": ready.get("gaps", []),
                        "checkpointHashMatches": ready.get("checkpoint_hash") == task.get("checkpoint_hash"),
                    }
                except (TypeError, ValueError):
                    pass
            raise ControlledStop("The manager requires reconciliation; no request was retried", details)
        if task["state"] in {"blocked", "completed"}:
            raise ControlledStop(f"Unexpected terminal state: {task['state']}")
        if task["state"] == "idle" and task["generation"] == generation and task["work_turns"] == work_turns:
            return task
        time.sleep(2)
    raise TimeoutError(f"Stage did not reach idle within {timeout:g} seconds")


def assert_handoff(task: dict, prior_thread: str, generation: int) -> dict:
    if task["thread_id"] == prior_thread or task["generation"] != generation:
        raise AssertionError("Handoff did not transfer ownership to a new generation")
    transfers = [event for event in task["events"] if event["kind"] == "ownership_transferred"]
    if len(transfers) < generation:
        raise AssertionError("Ownership transfer was not durably recorded")
    ready = transfers[0]["data"].get("ready", {})
    categories = {item.get("category") for item in ready.get("checks", [])}
    if ready.get("ready") is not True or ready.get("gaps") != [] or len(categories) != 6:
        raise AssertionError("Read-only receiver did not complete all READY checks")
    if len(task["history"]) < generation or task["history"][-1]["thread_id"] != prior_thread:
        raise AssertionError("Predecessor history was not retained")
    return {"ready": True, "checkCount": len(categories), "checkpointHash": task["checkpoint_hash"]}


def stage_record(task: dict, project: Path, expected: int, handoff: dict | None = None) -> dict:
    value = counter_value(project)
    if value != expected or task["state"] != "idle" or task["work_turns"] != expected:
        raise AssertionError(f"Expected idle work turn/counter {expected}, got {task['state']}/{task['work_turns']}/{value}")
    return {
        "taskId": task["id"], "threadId": task["thread_id"],
        "generation": task["generation"], "workTurns": task["work_turns"],
        "state": task["state"], "counter": value, "handoff": handoff,
    }


def write_report(path: Path, report: dict) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def verify_readonly_restart(root: Path, report: dict) -> None:
    expected = report["stages"][-1]
    client = RecordingClient()
    manager = None
    try:
        manager = Manager(state_dir=root / "state", client=client)
        before = manager.get_task(expected["taskId"])
        if (before["id"], before["thread_id"], before["generation"], before["state"]) != (
                expected["taskId"], expected["threadId"], expected["generation"], "idle"):
            raise AssertionError("Restart did not load the final idle task unchanged")
        reconciled = manager.reconcile(expected["taskId"])
        if (reconciled["id"], reconciled["thread_id"], reconciled["generation"], reconciled["state"]) != (
                expected["taskId"], expected["threadId"], expected["generation"], "paused"):
            raise AssertionError("Read-only reconciliation changed task identity or ownership")
        if counter_value(root / "project") != 3:
            raise AssertionError("Read-only restart changed counter.txt")
        if client.methods != ["config/read", "thread/read"]:
            raise AssertionError(f"Restart made unexpected requests: {client.methods}")
        report["readonlyRestartVerified"] = True
        report["restartVerification"] = {
            "taskId": reconciled["id"], "threadId": reconciled["thread_id"],
            "generation": reconciled["generation"], "state": reconciled["state"],
            "counter": 3, "requestMethods": client.methods,
        }
    finally:
        if manager is not None:
            manager.close()
        else:
            client.close()


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
    (project / "counter.txt").write_text("0\n", encoding="utf-8")
    report = {"status": "running", "startedAt": now(), "codexVersion": codex_version(),
              "stages": [], "mcpBoundary": None}
    report_path = root / "report.json"
    client = None
    manager = None
    exit_code = 1
    try:
        client = LowEffortClient()
        manager = Manager(state_dir=state, client=client)
        goal = (
            "In each work turn, inspect counter.txt and increment its integer by exactly one, then stop. "
            "Perform exactly one increment per work turn until the file reaches 3 across future verified handoffs. "
            "Report the resulting value after each increment. Modify no other project file. Do not use network, "
            "external tools, background processes, or subagents. Handoff summaries must report no unknown external "
            "operations and must treat the task as unfinished while counter.txt is below 3."
        )
        created = manager.create_task("Controlled counter handoff", project, goal, mode="workspace-write", direct=False)
        task_id = created["id"]
        manager.start(task_id)
        first = wait_for_idle(manager, task_id, 0, 1)
        report["stages"].append(stage_record(first, project, 1))

        status = client.request("mcpServerStatus/list", {
            "threadId": first["thread_id"], "detail": "toolsAndAuthOnly",
        })
        items = status.get("data", [])
        boundary = [{
            "enabled": item.get("runtimeStatus") != "disabled",
            "startupState": item.get("runtimeStatus"),
            "toolCount": len(item.get("tools") or {}),
        } for item in items]
        if any(item["enabled"] or item["toolCount"] for item in boundary):
            raise ControlledStop("A thread-scoped MCP server remained enabled")
        report["mcpBoundary"] = {"serverCount": len(boundary), "servers": boundary}

        prior = first["thread_id"]
        manager.handoff(task_id)
        second = wait_for_idle(manager, task_id, 1, 2)
        report["stages"].append(stage_record(second, project, 2, assert_handoff(second, prior, 1)))

        prior = second["thread_id"]
        manager.handoff(task_id)
        third = wait_for_idle(manager, task_id, 2, 3)
        report["stages"].append(stage_record(third, project, 3, assert_handoff(third, prior, 2)))

        if len({stage["taskId"] for stage in report["stages"]}) != 1:
            raise AssertionError("Stable task ID changed")
        if len({stage["threadId"] for stage in report["stages"]}) != 3:
            raise AssertionError("A handoff reused a thread ID")
        report.update(status="passed", finishedAt=now())
        exit_code = 0
    except Exception as error:
        report.update(status="stopped" if isinstance(error, ControlledStop) else "failed",
                      finishedAt=now(), error={"type": type(error).__name__,
                      "message": str(error).replace(str(root), "<smoke-root>")})
        if isinstance(error, ControlledStop) and error.details:
            report["error"]["details"] = error.details
    finally:
        try:
            if manager is not None:
                manager.close()
            elif client is not None:
                client.close()
            if exit_code == 0:
                verify_readonly_restart(root, report)
        except Exception as error:
            report.update(status="failed", closeError={"type": type(error).__name__,
                          "message": str(error).replace(str(root), "<smoke-root>")})
            exit_code = 1
        write_report(report_path, report)
        print(json.dumps({"status": report["status"], "report": str(report_path)}, ensure_ascii=False))
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="spend native model turns")
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
