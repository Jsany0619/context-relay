"""HTTPS gateway and durable remote-command contract checks; no native client is used."""

from __future__ import annotations

import base64
from contextlib import closing
import hashlib
import http.client
import json
from pathlib import Path
import socket
import sqlite3
import ssl
import tempfile
import threading
import time
import unittest
import urllib.parse
import uuid

from relay.gateway import GatewayCore, RemoteGateway, ensure_certificate
from relay.transport import RequestTimeout


TASK_ID = "a" * 32
OTHER_ID = "b" * 32


def task(**changes):
    value = {
        "id": TASK_ID, "title": "Remote task", "state": "running", "goal": "Check progress",
        "mode": "workspace-write", "last_message": "Current result", "error": "", "usage": 12,
        "max_tokens": 1000, "max_minutes": 10, "work_turns": 1, "brief_required": False,
        "acceptance_criteria": ["User confirms"], "archived": False,
        "thread_id": "must-not-leak", "checkpoint_path": "must-not-leak", "source_snapshot": {"secret": "x"},
        "pending": [], "inflight": {}, "brief": None, "review": None, "updated_at": "now",
    }
    value.update(changes)
    return value


class FakeManager:
    def __init__(self, current):
        self.current = current
        self.calls = []

    def list_tasks(self):
        return [self.current]

    def start(self, task_id, message=None):
        self.calls.append(("start", task_id, message))
        self.current = dict(self.current, state="running", last_message=message or "")
        return self.current

    def pause(self, task_id):
        self.calls.append(("pause", task_id))
        self.current = dict(self.current, state="paused")
        return self.current

    def answer(self, task_id, request_id, answer):
        self.calls.append(("answer", task_id, request_id, answer))
        self.current = dict(self.current, pending=[])
        return None


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.enqueued = []
        self.core = GatewayCore(self.root, self.enqueued.append)
        self.addCleanup(self.core.close)
        self.core.publish_tasks([task()], connection_id="setup")

    def start_gateway(self):
        cert, key, fingerprint = ensure_certificate(self.root)
        self.gateway = RemoteGateway(self.core, "127.0.0.1", 0, cert, key)
        self.gateway.start()
        self.addCleanup(self.gateway.close)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cafile=str(cert))
        return fingerprint, context

    def request(self, context, method, path, body=None, token=None, headers=None):
        connection = http.client.HTTPSConnection("127.0.0.1", self.gateway.port, context=context, timeout=5)
        values = dict(headers or {})
        if token:
            values["Authorization"] = "Bearer " + token
        encoded = None
        if body is not None:
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            values["Content-Type"] = "application/json"
        connection.request(method, path, body=encoded, headers=values)
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, dict(response.getheaders()), json.loads(payload) if payload else None

    def pair(self, context, fingerprint, task_ids=(TASK_ID,), scope="control"):
        uri = self.core.new_pairing(
            f"https://127.0.0.1:{self.gateway.port}", fingerprint, task_ids, scope)
        fragment = json.loads(base64.urlsafe_b64decode(urllib.parse.urlsplit(uri).fragment + "=="))
        status, _, result = self.request(context, "POST", "/v1/pair",
                                         {"secret": fragment["secret"], "device_name": "Android"})
        self.assertEqual(status, 200)
        return result, fragment, uri

    def core_device(self, name="phone", task_ids=(TASK_ID,), scope="control"):
        uri = self.core.new_pairing("https://127.0.0.1:9443", "a" * 64, task_ids, scope)
        secret = json.loads(base64.urlsafe_b64decode(urllib.parse.urlsplit(uri).fragment + "=="))["secret"]
        return self.core.pair(secret, name)

    def test_device_task_scope_read_only_and_expiry_are_enforced_at_dispatch(self):
        self.core.publish_tasks([task(), task(id=OTHER_ID, title="Other")], connection_id="one")
        read_only = self.core_device("reader", scope="read_only")
        control = self.core_device("controller")
        listed = self.core.list_task_snapshots(read_only["device_id"])
        self.assertEqual([item["id"] for item in listed["tasks"]], [TASK_ID])
        self.assertEqual(listed["tasks"][0]["remote_access"], "read_only")
        with self.assertRaises(KeyError):
            self.core.get_task_snapshot(read_only["device_id"], OTHER_ID)
        body = {"request_id": str(uuid.uuid4()), "task_id": TASK_ID, "command": "pause",
                "expected_etag": listed["tasks"][0]["etag"], "payload": {}}
        with self.assertRaisesRegex(ValueError, "只读"):
            self.core.submit_command(read_only["device_id"], body)

        body["request_id"] = str(uuid.uuid4())
        body["expected_etag"] = self.core.get_task_snapshot(control["device_id"], TASK_ID)["etag"]
        self.core.submit_command(control["device_id"], body)
        with closing(sqlite3.connect(self.root / "remote-control.sqlite3")) as db:
            db.execute("UPDATE devices SET expires_at=0 WHERE id=?", (control["device_id"],))
            db.commit()
        manager = FakeManager(task(state="idle"))
        receipt = self.core.execute(body["request_id"], manager)
        self.assertEqual(receipt["state"], "unknown")
        self.assertFalse(manager.calls)
        with self.assertRaisesRegex(ValueError, "过期"):
            self.core.command_status(control["device_id"], body["request_id"])

    def test_task_messages_are_bounded_and_marked_when_omitted(self):
        messages = [{"id": f"m{i}", "role": "user" if i % 2 == 0 else "assistant",
                     "text": "x" * 3500, "created_at": f"2026-10-02T00:{i:02d}:00Z",
                     "status": "completed", "purpose": "work", "historical": i == 49,
                     "truncated": i == 49} for i in range(50)]
        self.core.publish_tasks([task(messages=messages, messages_truncated=True)], connection_id="messages")
        device = self.core_device()
        dto = self.core.get_task_snapshot(device["device_id"], TASK_ID)
        self.assertEqual(dto["remote_access"], "control")
        self.assertTrue(dto["messages_truncated"])
        self.assertLessEqual(len(dto["messages"]), 40)
        self.assertTrue(all(set(item) == {"id", "role", "text", "created_at", "status", "purpose",
                                                 "historical", "truncated"}
                            and len(item["text"]) <= 3000 for item in dto["messages"]))
        self.assertTrue(dto["messages"][-1]["historical"])
        self.assertTrue(dto["messages"][-1]["truncated"])
        self.assertLessEqual(len(json.dumps(dto["messages"]).encode()), 64 * 1024)

    def test_pairing_is_single_use_expiring_and_stores_no_plaintext_credentials(self):
        fingerprint, context = self.start_gateway()
        paired, fragment, uri = self.pair(context, fingerprint)
        self.assertEqual(urllib.parse.urlsplit(uri).scheme, "contextrelay")
        self.assertEqual(fragment["certificate_sha256"], fingerprint)
        status, _, _ = self.request(context, "POST", "/v1/pair",
                                    {"secret": fragment["secret"], "device_name": "again"})
        self.assertEqual(status, 401)
        raw = (self.root / "remote-control.sqlite3").read_bytes()
        self.assertNotIn(fragment["secret"].encode(), raw)
        self.assertNotIn(paired["token"].encode(), raw)
        device_status = self.core.local_status()["devices"][0]
        self.assertEqual(device_status["name"], "Android")
        self.assertEqual(device_status["scope"], "control")
        self.assertEqual(device_status["task_ids"], [TASK_ID])
        self.assertFalse(device_status["expired"])
        with closing(sqlite3.connect(self.root / "remote-control.sqlite3")) as db:
            expires_at = db.execute("SELECT expires_at FROM devices WHERE id=?", (paired["device_id"],)).fetchone()[0]
        self.assertGreater(expires_at, time.time() + 6.9 * 24 * 60 * 60)
        self.assertLess(expires_at, time.time() + 7.1 * 24 * 60 * 60)
        self.core.revoke_device(paired["device_id"])
        status, _, _ = self.request(context, "GET", "/v1/status", token=paired["token"])
        self.assertEqual(status, 401)

        expired = self.core.new_pairing(
            f"https://127.0.0.1:{self.gateway.port}", fingerprint, [TASK_ID], "control")
        secret = json.loads(base64.urlsafe_b64decode(urllib.parse.urlsplit(expired).fragment + "=="))["secret"]
        with closing(sqlite3.connect(self.root / "remote-control.sqlite3")) as db:
            db.execute("UPDATE pairings SET expires_at=0")
            db.commit()
        status, _, _ = self.request(context, "POST", "/v1/pair", {"secret": secret, "device_name": "late"})
        self.assertEqual(status, 401)
        with self.assertRaisesRegex(ValueError, "选择"):
            self.core.new_pairing(f"https://127.0.0.1:{self.gateway.port}", fingerprint, [], "read_only")

    def test_legacy_device_without_expiry_or_scope_requires_repair(self):
        legacy_root = self.root / "legacy"
        legacy_root.mkdir()
        token = "old-token"
        with closing(sqlite3.connect(legacy_root / "remote-control.sqlite3")) as db:
            db.execute("""CREATE TABLE devices (id TEXT PRIMARY KEY,name TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL,revoked_at TEXT)""")
            db.execute("INSERT INTO devices VALUES (?,?,?,?,NULL)",
                       ("old", "old phone", hashlib.sha256(token.encode()).hexdigest(), "old"))
            db.commit()
        legacy = GatewayCore(legacy_root, lambda request_id: None)
        self.addCleanup(legacy.close)
        with self.assertRaisesRegex(ValueError, "重新配对"):
            legacy.authenticate(token)
        status = legacy.local_status()["devices"][0]
        self.assertTrue(status["expired"])
        self.assertIsNone(status["scope"])
        from relay.secrets import assert_private_acl
        assert_private_acl(legacy.path)

    def test_task_snapshot_is_allowlisted_and_pending_approval_requires_complete_current_evidence(self):
        complete = {"id": "approval-1", "method": "item/commandExecution/requestApproval",
                    "params": {"itemId": "op", "reason": "needed", "cwd": "C:/project"}}
        raw = task(pending=[complete], inflight={"op": {"command": "python -m unittest"}})
        self.core.publish_tasks([raw], connection_id="connection-a")
        device = self.core_device()
        dto = self.core.get_task_snapshot(device["device_id"], TASK_ID)
        self.assertEqual(set(dto), {"id", "title", "state", "goal", "mode", "last_message", "error",
            "usage", "max_tokens", "max_minutes", "work_turns", "brief_required", "acceptance_criteria",
            "archived", "etag", "pending", "brief", "review", "messages", "messages_truncated",
            "remote_access"})
        self.assertNotIn("must-not-leak", json.dumps(dto))
        self.assertTrue(dto["pending"][0]["can_approve"])
        self.assertEqual(dto["pending"][0]["params"]["command"], "python -m unittest")

        missing = task(pending=[complete], inflight={})
        self.core.publish_tasks([missing], connection_id="connection-a")
        self.assertFalse(self.core.get_task_snapshot(device["device_id"], TASK_ID)["pending"][0]["can_approve"])
        self.core.publish_tasks([raw], connection_id="connection-b")
        self.assertNotEqual(dto["etag"], self.core.get_task_snapshot(device["device_id"], TASK_ID)["etag"])

        questions = [
            {"id": "valid", "question": "Choose a language", "options": [{"label": "Python"}]},
            {"id": "invalid", "method": "ignored"},
            {"id": "secret", "question": "Enter a credential", "isSecret": True},
        ]
        requests = [
            {"id": item["id"], "method": "item/tool/requestUserInput", "params": {"questions": [item]}}
            for item in questions
        ]
        requests.append({"id": "truncated", "method": "item/tool/requestUserInput",
                         "params": {"questions": [{"id": "large", "question": "x" * (17 * 1024)}]}})
        self.core.publish_tasks([task(pending=requests)], connection_id="connection-b")
        allowed = {item["id"]: item["can_approve"] for item in
                   self.core.get_task_snapshot(device["device_id"], TASK_ID)["pending"]}
        self.assertEqual(allowed, {"valid": True, "invalid": False, "secret": False, "truncated": False})

    def test_command_idempotency_cas_current_approval_and_restart_unknown(self):
        raw = task(state="idle")
        self.core.publish_tasks([raw], connection_id="one")
        device = self.core_device()
        request_id = str(uuid.uuid4())
        body = {"request_id": request_id, "task_id": TASK_ID, "command": "start",
                "expected_etag": self.core.get_task_snapshot(device["device_id"], TASK_ID)["etag"], "payload": {"message": "Continue"}}
        first = self.core.submit_command(device["device_id"], body)
        second = self.core.submit_command(device["device_id"], body)
        self.assertEqual(first, second)
        self.assertEqual(self.enqueued, [request_id])
        changed = json.loads(json.dumps(body))
        changed["payload"]["message"] = "Different"
        with self.assertRaises(ValueError):
            self.core.submit_command(device["device_id"], changed)

        manager = FakeManager(raw)
        completed = self.core.execute(request_id, manager)
        self.assertEqual(completed["state"], "succeeded")
        self.assertEqual(manager.calls, [("start", TASK_ID, "Continue")])
        self.assertNotIn("thread_id", completed["result"]["task"])

        other = self.core_device("other")
        with self.assertRaises(KeyError):
            self.core.command_status(other["device_id"], request_id)

        pending_id = str(uuid.uuid4())
        pending = dict(body, request_id=pending_id, expected_etag=self.core.get_task_snapshot(device["device_id"], TASK_ID)["etag"])
        self.core.submit_command(device["device_id"], pending)
        self.core.close()
        reopened = GatewayCore(self.root, self.enqueued.append)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.command_status(device["device_id"], pending_id)["state"], "unknown")
        self.assertEqual(self.enqueued, [request_id, pending_id])

    def test_https_routes_are_authenticated_strict_bounded_rate_limited_and_async(self):
        fingerprint, context = self.start_gateway()
        paired, _, _ = self.pair(context, fingerprint)
        token = paired["token"]
        self.core.publish_tasks([task(state="idle")], connection_id="one")
        status, headers, result = self.request(context, "GET", "/v1/tasks", token=token)
        self.assertEqual(status, 200)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(result["tasks"][0]["id"], TASK_ID)
        status, _, _ = self.request(context, "GET", "/v1/status")
        self.assertEqual(status, 401)
        status, _, _ = self.request(context, "GET", "/v1/status", token=token,
                                    headers={"Origin": "https://evil.invalid"})
        self.assertEqual(status, 403)

        command = {"request_id": str(uuid.uuid4()), "task_id": TASK_ID, "command": "pause",
                   "expected_etag": result["tasks"][0]["etag"], "payload": {}}
        status, _, receipt = self.request(context, "POST", "/v1/commands", command, token)
        self.assertEqual(status, 202)
        self.assertEqual(receipt["state"], "accepted")
        status, _, queried = self.request(context, "GET", "/v1/commands/" + command["request_id"], token=token)
        self.assertEqual(status, 200)
        self.assertEqual(queried, receipt)

        large = b"{" + b" " * (70 * 1024) + b"}"
        connection = http.client.HTTPSConnection("127.0.0.1", self.gateway.port, context=context, timeout=5)
        connection.request("POST", "/v1/commands", body=large,
                           headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 413)
        connection.close()

        self.core._rate_limit = 2
        self.core._rate_windows.clear()
        self.assertEqual(self.request(context, "GET", "/v1/status", token=token)[0], 200)
        self.assertEqual(self.request(context, "GET", "/v1/status", token=token)[0], 200)
        self.assertEqual(self.request(context, "GET", "/v1/status", token=token)[0], 429)

    def test_https_read_only_device_sees_only_selected_tasks_and_cannot_command(self):
        self.core.publish_tasks([task(), task(id=OTHER_ID, title="Other")], connection_id="http-scope")
        fingerprint, context = self.start_gateway()
        paired, _, _ = self.pair(context, fingerprint, scope="read_only")
        token = paired["token"]
        status, _, result = self.request(context, "GET", "/v1/tasks", token=token)
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in result["tasks"]], [TASK_ID])
        self.assertEqual(result["tasks"][0]["remote_access"], "read_only")
        self.assertEqual(self.request(context, "GET", "/v1/tasks/" + OTHER_ID, token=token)[0], 404)
        command = {"request_id": str(uuid.uuid4()), "task_id": TASK_ID, "command": "pause",
                   "expected_etag": result["tasks"][0]["etag"], "payload": {}}
        status, _, error = self.request(context, "POST", "/v1/commands", command, token)
        self.assertEqual(status, 403)
        self.assertEqual(error["error"]["code"], "read_only_device")
        self.assertFalse(self.enqueued)

    def test_incomplete_tls_clients_do_not_block_https_or_shutdown(self):
        _, context = self.start_gateway()
        blocker = socket.create_connection(("127.0.0.1", self.gateway.port), timeout=2)
        release = threading.Timer(.8, blocker.close)
        release.start()
        self.addCleanup(release.cancel)
        before = time.monotonic()
        self.assertEqual(self.request(context, "GET", "/v1/status")[0], 401)
        self.assertLess(time.monotonic() - before, .5)
        blocker.close()
        release.cancel()

        blocker = socket.create_connection(("127.0.0.1", self.gateway.port), timeout=2)
        release = threading.Timer(1.2, blocker.close)
        release.start()
        self.addCleanup(release.cancel)
        before = time.monotonic()
        self.gateway.close()
        self.assertLess(time.monotonic() - before, .8)
        blocker.close()
        release.cancel()

    def test_stale_or_incomplete_approval_never_reaches_manager_and_close_stops_execution(self):
        pending = {"id": 7, "method": "item/fileChange/requestApproval",
                   "params": {"itemId": "change", "reason": "apply"}}
        raw = task(pending=[pending], inflight={})
        self.core.publish_tasks([raw], connection_id="one")
        device = self.core_device()
        answer_id = str(uuid.uuid4())
        body = {"request_id": answer_id, "task_id": TASK_ID, "command": "answer",
                "expected_etag": self.core.get_task_snapshot(device["device_id"], TASK_ID)["etag"],
                "payload": {"pending_request_id": 7, "answer": {"decision": "accept"}}}
        self.core.submit_command(device["device_id"], body)
        manager = FakeManager(raw)
        receipt = self.core.execute(answer_id, manager)
        self.assertEqual(receipt["state"], "failed")
        self.assertFalse(manager.calls)

        stale_id = str(uuid.uuid4())
        stale = dict(body, request_id=stale_id, command="pause", payload={}, expected_etag="0" * 64)
        with self.assertRaisesRegex(ValueError, "变化"):
            self.core.submit_command(device["device_id"], stale)
        stale["expected_etag"] = self.core.get_task_snapshot(device["device_id"], TASK_ID)["etag"]
        self.core.submit_command(device["device_id"], stale)
        manager.current = dict(manager.current, updated_at="changed-after-enqueue")
        self.assertEqual(self.core.execute(stale_id, manager)["error"]["code"], "stale_task")
        self.assertFalse(manager.calls)

        closed_id = str(uuid.uuid4())
        closed = dict(stale, request_id=closed_id, expected_etag=self.core.get_task_snapshot(device["device_id"], TASK_ID)["etag"])
        self.core.submit_command(device["device_id"], closed)
        self.core.close()
        self.assertEqual(self.core.execute(closed_id, manager)["state"], "unknown")
        self.assertFalse(manager.calls)

    def test_revocation_cancels_accepted_work_and_command_schema_is_exact(self):
        self.core.publish_tasks([task(state="idle")], connection_id="one")
        device = self.core_device()
        request_id = str(uuid.uuid4())
        valid = {"request_id": request_id, "task_id": TASK_ID, "command": "pause",
                 "expected_etag": self.core.get_task_snapshot(device["device_id"], TASK_ID)["etag"], "payload": {}}
        for changed in (
            {**valid, "extra": True},
            {**valid, "request_id": "not-a-uuid"},
            {**valid, "command": "arbitrary_method"},
            {**valid, "payload": {"unexpected": True}},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.core.submit_command(device["device_id"], changed)
        self.core.submit_command(device["device_id"], valid)
        self.core.revoke_device(device["device_id"])
        manager = FakeManager(task(state="idle"))
        receipt = self.core.execute(request_id, manager)
        self.assertEqual(receipt["state"], "unknown")
        self.assertFalse(manager.calls)

    def test_unknown_native_outcome_is_a_permanent_tombstone_and_never_retried(self):
        class UnknownManager(FakeManager):
            def start(inner, task_id, message=None):
                inner.calls.append(("start", task_id, message))
                inner.current = dict(inner.current, state="needs_reconcile")
                raise RequestTimeout("turn/start", 41, 30)

        raw = task(state="idle")
        manager = UnknownManager(raw)
        self.core.publish_tasks([raw], connection_id="one")
        device = self.core_device()
        request_id = str(uuid.uuid4())
        body = {"request_id": request_id, "task_id": TASK_ID, "command": "start",
                "expected_etag": self.core.get_task_snapshot(device["device_id"], TASK_ID)["etag"], "payload": {"message": "Continue"}}
        self.core.submit_command(device["device_id"], body)
        receipt = self.core.execute(request_id, manager)
        self.assertEqual(receipt["state"], "unknown")
        self.assertEqual(receipt["error"]["code"], "outcome_unknown")
        queued = list(self.enqueued)
        self.assertEqual(self.core.submit_command(device["device_id"], body), receipt)
        self.assertEqual(self.enqueued, queued)
        self.assertEqual(self.core.execute(request_id, manager), receipt)
        self.assertEqual(manager.calls, [("start", TASK_ID, "Continue")])

    def test_running_native_command_does_not_block_phone_status_queries(self):
        started, release = threading.Event(), threading.Event()

        class SlowManager(FakeManager):
            def start(inner, task_id, message=None):
                inner.calls.append(("start", task_id, message))
                started.set()
                release.wait(2)
                return inner.current

        raw = task(state="idle")
        manager = SlowManager(raw)
        self.core.publish_tasks([raw], connection_id="one")
        device = self.core_device()
        request_id = str(uuid.uuid4())
        body = {"request_id": request_id, "task_id": TASK_ID, "command": "start",
                "expected_etag": self.core.get_task_snapshot(device["device_id"], TASK_ID)["etag"], "payload": {"message": "Continue"}}
        self.core.submit_command(device["device_id"], body)
        worker = threading.Thread(target=self.core.execute, args=(request_id, manager))
        worker.start()
        self.addCleanup(lambda: (release.set(), worker.join(3)))
        self.assertTrue(started.wait(1))
        before = time.monotonic()
        receipt = self.core.command_status(device["device_id"], request_id)
        elapsed = time.monotonic() - before
        self.assertEqual(receipt["state"], "running")
        self.assertLess(elapsed, .25)
        self.assertTrue(self.core.status_snapshot(device["device_id"])["online"])
        release.set()
        worker.join(3)


if __name__ == "__main__":
    unittest.main()
