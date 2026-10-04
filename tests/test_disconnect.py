"""Connection recovery contracts using fake sessions and a local fake RPC process."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from relay.manager import Manager
from relay.transport import CodexClient, RequestTimeout, RpcError
from tests.test_manager import FakeClient


class DisconnectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.client = FakeClient()
        self.manager = Manager(self.root / "state", self.client)
        self.addCleanup(self.manager.close)

    def start(self):
        task = self.manager.create_task("Disconnect", self.project, "Inspect the fake sample only",
                                        mode="workspace-write", direct=False)
        self.manager.start(task["id"])
        self.manager.poll()
        return self.manager.get_task(task["id"])

    def disconnect(self):
        self.client.push("transport/closed", {"returncode": 1})
        self.manager.poll()

    def finish(self, task, payload=None):
        thread_id = task["receiver_id"] if task["purpose"] == "verify" else task["thread_id"]
        items = []
        if payload is not None:
            items = [{"id": "message", "type": "agentMessage", "text": json.dumps(payload)}]
            self.client.push("item/completed", {
                "threadId": thread_id, "turnId": task["turn_id"], "item": items[0]})
        self.client.push("turn/completed", {
            "threadId": thread_id, "turn": {"id": task["turn_id"], "status": "completed", "items": items}})
        self.manager.poll()

    @staticmethod
    def receipt(task, status="completed", thread_id=None, items=None):
        return {"thread": {"id": thread_id or task["thread_id"], "status": {"type": "idle"},
                           "turns": [{"id": task["turn_id"], "status": status, "items": items or []}]}}

    def test_disconnect_clears_connection_scoped_approvals_and_partial_messages(self):
        task = self.start()
        self.client.push("item/commandExecution/requestApproval", {
            "threadId": task["thread_id"], "turnId": task["turn_id"],
            "itemId": "operation", "command": "echo simulated"}, request_id=77)
        self.client.push("item/agentMessage/delta", {
            "threadId": task["thread_id"], "turnId": task["turn_id"], "delta": "partial simulated reply"})
        self.manager.poll()
        self.assertTrue(self.manager._raw_requests)
        self.assertTrue(self.manager._messages)
        generation = self.manager._connection_id
        before_calls = list(self.client.calls)
        self.disconnect()
        saved = self.manager.get_task(task["id"])
        self.assertEqual("needs_reconcile", saved["state"])
        self.assertEqual([], saved["pending"])
        self.assertEqual(task["thread_id"], saved["thread_id"])
        self.assertEqual(task["turn_id"], saved["turn_id"])
        self.assertNotEqual(generation, self.manager._connection_id)
        self.assertIsNone(self.manager.client)
        self.assertEqual(before_calls, self.client.calls)
        with self.assertRaises(ValueError):
            self.manager.answer(task["id"], 77, {"decision": "accept"})
        self.assertEqual([], self.client.answers)
        self.assertEqual({}, self.manager._raw_requests)
        self.assertEqual({}, self.manager._messages)

    def test_explicit_reconcile_reconnects_read_only_and_requires_known_turn_terminal(self):
        task = self.start()
        self.disconnect()
        fresh = FakeClient()
        with patch("relay.transport.CodexClient", return_value=fresh) as constructor:
            with self.assertRaises(ValueError):
                self.manager.reconcile(task["id"])
            fresh.responses["thread/read"].append(self.receipt(task, "inProgress"))
            with self.assertRaises(ValueError):
                self.manager.reconcile(task["id"])
            fresh.responses["thread/read"].append(self.receipt(task))
            recovered = self.manager.reconcile(task["id"])
        constructor.assert_called_once()
        self.assertEqual("paused", recovered["state"])
        self.assertEqual(task["thread_id"], recovered["thread_id"])
        self.assertEqual(task["generation"], recovered["generation"])
        self.assertEqual(1, recovered["work_turns"])
        self.assertEqual(["config/read", "thread/read", "thread/read", "thread/read"],
                         [method for method, _ in fresh.calls])
        self.assertEqual([], fresh.answers)

    def test_unknown_creation_is_not_retried_or_bound_to_reused_connection_request_id(self):
        task = self.manager.create_task("Unknown", self.project, "Inspect only", direct=False)
        self.client.responses["thread/start"].append(RequestTimeout("thread/start", 2, .01))
        with self.assertRaises(RequestTimeout):
            self.manager.start(task["id"])
        self.disconnect()
        with patch("relay.transport.CodexClient") as constructor:
            for method in (self.manager.start, self.manager.reconcile):
                with self.assertRaises(ValueError):
                    method(task["id"])
            constructor.assert_not_called()
        other_project = self.root / "other"
        other_project.mkdir()
        other = self.manager.create_task("Other", other_project, "Inspect other sample", direct=False)
        fresh = FakeClient()
        fresh.responses["thread/start"].append(RequestTimeout("thread/start", 2, .01))
        with patch("relay.transport.CodexClient", return_value=fresh):
            with self.assertRaises(RequestTimeout):
                self.manager.start(other["id"])
        fresh.push("transport/lateResponse", {"requestId": 2, "result": {
            "thread": {"id": "new-source", "turns": []}, "cwd": str(other_project),
            "model": "test-model", "sandbox": {"type": "readOnly"}, "approvalPolicy": "on-request"}})
        self.manager.poll()
        old = self.manager.get_task(task["id"])
        self.assertIsNone(old["thread_id"])
        self.assertEqual("needs_reconcile", old["state"])
        self.assertEqual("new-source", self.manager.get_task(other["id"])["thread_id"])
        self.assertEqual(1, len(self.client.calls_for("thread/start")))
        self.assertEqual(1, len(fresh.calls_for("thread/start")))
        self.assertEqual([], fresh.calls_for("turn/start"))

    def test_receiver_disconnect_reconciles_both_threads_without_transferring_owner(self):
        source = self.start()
        self.finish(source)
        summary = self.manager.handoff(source["id"])
        self.finish(summary, {"summary": "Inspected sample", "decisions": [], "completed": [],
                              "next_step": "Review remaining requirements", "evidence": [],
                              "unknown_operations": [], "unrecoverable_resources": [], "task_complete": False})
        receiver = self.manager.get_task(source["id"])
        self.assertEqual("verifying", receiver["state"])
        self.disconnect()
        fresh = FakeClient()
        fresh.responses["thread/read"].append({"thread": {
            "id": source["thread_id"], "status": {"type": "idle"}, "turns": []}})
        fresh.responses["thread/read"].append(self.receipt(receiver, thread_id=receiver["receiver_id"],
            items=[{"id": "unadopted-ready", "type": "agentMessage", "text": '{"ready":true}'}]))
        with patch("relay.transport.CodexClient", return_value=fresh):
            recovered = self.manager.reconcile(source["id"])
        self.assertEqual("paused", recovered["state"])
        self.assertEqual(source["thread_id"], recovered["thread_id"])
        self.assertEqual(source["generation"], recovered["generation"])
        self.assertIsNone(recovered["receiver_id"])
        self.assertEqual([source["thread_id"], receiver["receiver_id"]],
                         [call["threadId"] for call in fresh.calls_for("thread/read")])
        self.assertEqual(["config/read", "thread/read", "thread/read"], [method for method, _ in fresh.calls])

    def test_malformed_rpc_response_releases_dead_reader_and_allows_read_only_reconnect(self):
        server = '''
import json, sys
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
for line in sys.stdin:
    message = json.loads(line)
    method, params = message.get("method"), message.get("params", {})
    if "id" not in message:
        continue
    if method == "initialize":
        result = {}
    elif method == "config/read":
        result = {"config": {"mcp_servers": {}}}
    elif method == "thread/start":
        result = {"thread": {"id": "local-source"}, "cwd": params["cwd"],
                  "model": "test-model", "sandbox": {"type": "readOnly"}, "approvalPolicy": "on-request"}
    elif method == "mcpServerStatus/list":
        result = {"data": [], "nextCursor": None}
    else:
        print(json.dumps({"id": [], "result": {}}), flush=True)
        continue
    print(json.dumps({"id": message["id"], "result": result}), flush=True)
'''
        class BoundedClient(CodexClient):
            def request(self, method, params=None, timeout=3):
                return super().request(method, params, .3 if method == "turn/start" else timeout)

        native = BoundedClient([sys.executable, "-u", "-c", server])
        self.addCleanup(native.close)
        manager = Manager(self.root / "local-rpc-state", native)
        self.addCleanup(manager.close)
        task = manager.create_task("Malformed", self.project, "Fake RPC only", direct=False)
        with self.assertRaises((RpcError, RequestTimeout)):
            manager.start(task["id"])
        deadline = time.monotonic() + 1
        while manager.client is not None and time.monotonic() < deadline:
            manager.poll()
            time.sleep(.005)
        held = manager.get_task(task["id"])
        self.assertEqual("needs_reconcile", held["state"])
        self.assertEqual("local-source", held["thread_id"])
        self.assertIsNone(manager.client)
        fresh = FakeClient()
        with patch("relay.transport.CodexClient", return_value=fresh):
            with self.assertRaises(ValueError):
                manager.reconcile(task["id"])
        self.assertEqual(["config/read", "thread/read"], [method for method, _ in fresh.calls])
        self.assertEqual("needs_reconcile", manager.get_task(task["id"])["state"])


if __name__ == "__main__":
    unittest.main()
