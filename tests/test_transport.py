"""Transport checks with a fake line-delimited Codex app-server."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from relay.transport import CodexClient, RequestTimeout, RpcError


def _read() -> dict:
    line = sys.stdin.readline()
    if not line:
        raise EOFError
    return json.loads(line)


def _write(message: dict) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _handshake() -> None:
    request = _read()
    assert request["method"] == "initialize"
    assert request["params"]["clientInfo"]["name"] == "context-relay"
    _write({"id": request["id"], "result": {"userAgent": "fake"}})
    assert _read() == {"method": "initialized"}


def _fake_server(mode: str) -> None:
    _handshake()
    if mode == "reorder":
        requests = [_read(), _read()]
        _write({"method": "fake/notice", "params": {"ok": True}})
        _write({"id": "server-1", "method": "fake/approve", "params": {"x": 1}})
        for request in reversed(requests):
            _write({"id": request["id"], "result": {"method": request["method"]}})
    elif mode == "respond":
        _write({"id": "server-2", "method": "fake/question", "params": {}})
        response = _read()
        _write({"method": "fake/responded", "params": response})
    elif mode == "timeout":
        request = _read()
        time.sleep(0.15)
        _write({"id": request["id"], "result": {"thread": {"id": "created-late"}}})
        time.sleep(0.05)
    elif mode == "rpc-error":
        request = _read()
        _write({"id": request["id"], "error": {"code": -32001, "message": "nope", "data": {"why": "fake"}}})
    elif mode == "pending-eof":
        _read()


def _command(mode: str) -> list[str]:
    return [sys.executable, str(pathlib.Path(__file__).resolve()), "--fake-server", mode]


def _wait_for_event(client: CodexClient, method: str, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event in client.drain_events():
            if event.get("method") == method:
                return event
        time.sleep(0.01)
    raise AssertionError(f"event {method!r} did not arrive")


class TransportTests(unittest.TestCase):
    def test_reordered_responses_and_raw_events(self) -> None:
        client = CodexClient(_command("reorder"))
        self.addCleanup(client.close)
        results: dict[str, dict] = {}
        barrier = threading.Barrier(3)

        def call(method: str) -> None:
            barrier.wait()
            results[method] = client.request(method, {"sent": method})

        threads = [threading.Thread(target=call, args=(method,)) for method in ("fake/one", "fake/two")]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(2)

        self.assertEqual(results, {
            "fake/one": {"method": "fake/one"},
            "fake/two": {"method": "fake/two"},
        })
        events = client.drain_events()
        self.assertIn({"method": "fake/notice", "params": {"ok": True}}, events)
        self.assertIn({"id": "server-1", "method": "fake/approve", "params": {"x": 1}}, events)

    def test_responds_to_server_request(self) -> None:
        client = CodexClient(_command("respond"))
        self.addCleanup(client.close)
        request = _wait_for_event(client, "fake/question")
        client.respond(request["id"], {"answer": "yes"})

        event = _wait_for_event(client, "fake/responded")
        self.assertEqual(event["params"], {"id": "server-2", "result": {"answer": "yes"}})

    def test_timeout_preserves_late_result_as_diagnostic_event(self) -> None:
        client = CodexClient(_command("timeout"))
        self.addCleanup(client.close)

        with self.assertRaises(RequestTimeout) as raised:
            client.request("thread/start", {"cwd": "C:/work"}, timeout=0.03)

        event = _wait_for_event(client, "transport/lateResponse")
        self.assertEqual(event["params"], {
            "requestId": raised.exception.request_id,
            "requestMethod": "thread/start",
            "result": {"thread": {"id": "created-late"}},
        })

    def test_rpc_error_exposes_protocol_fields(self) -> None:
        client = CodexClient(_command("rpc-error"))
        self.addCleanup(client.close)

        with self.assertRaises(RpcError) as raised:
            client.request("fake/error")

        self.assertEqual(raised.exception.code, -32001)
        self.assertEqual(raised.exception.message, "nope")
        self.assertEqual(raised.exception.data, {"why": "fake"})

    def test_eof_unblocks_pending_request_and_emits_closed_event(self) -> None:
        client = CodexClient(_command("pending-eof"))
        self.addCleanup(client.close)

        with self.assertRaises(RpcError) as raised:
            client.request("fake/pending", timeout=2)

        self.assertEqual(raised.exception.code, -32000)
        event = _wait_for_event(client, "transport/closed")
        self.assertIn("returncode", event["params"])


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--fake-server":
        _fake_server(sys.argv[2])
    else:
        unittest.main()
