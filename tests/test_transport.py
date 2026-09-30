"""Transport checks with a fake line-delimited Codex app-server."""

from __future__ import annotations

import json
import os
import pathlib
import queue
import signal
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
        _write({"id": "orphan-1", "result": {"ok": "orphan"}})
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
    elif mode == "inherited-stdout":
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
        )
        _write({"method": "fake/inheritedStdout", "params": {"pid": child.pid}})
    elif mode.startswith("invalid-"):
        request = _read()
        if mode == "invalid-id":
            _write({"id": [], "result": {"secret": "DO-NOT-LEAK"}})
            _write({"id": request["id"], "result": {"accepted": "must-not-win"}})
        elif mode == "invalid-bool-id":
            _write({"id": True, "result": {"secret": "DO-NOT-LEAK"}})
        elif mode == "invalid-error":
            _write({"id": request["id"], "error": {
                "code": "DO-NOT-LEAK", "message": {"secret": "DO-NOT-LEAK"}}})
        elif mode == "invalid-params":
            _write({"method": "fake/notice", "params": ["DO-NOT-LEAK"]})
        elif mode == "invalid-json":
            sys.stdout.write('{"secret":"DO-NOT-LEAK"\n')
            sys.stdout.flush()
        elif mode == "invalid-utf8":
            sys.stdout.buffer.write(b"\xffDO-NOT-LEAK\n")
            sys.stdout.buffer.flush()
        time.sleep(10)


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


def _wait_for_methods(client: CodexClient, methods: set[str], timeout: float = 2.0) -> list[dict]:
    deadline = time.monotonic() + timeout
    events = []
    while time.monotonic() < deadline:
        events.extend(client.drain_events())
        if methods <= {event.get("method") for event in events}:
            return events
        time.sleep(0.01)
    raise AssertionError(f"events {methods!r} did not all arrive; got {events!r}")


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
        self.assertIn({"method": "transport/orphanResponse", "params": {
            "requestId": "orphan-1", "response": {"id": "orphan-1", "result": {"ok": "orphan"}}}}, events)

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

    def test_close_is_bounded_when_descendant_inherits_stdout_pipe(self) -> None:
        client = CodexClient(_command("inherited-stdout"))
        child_pid = _wait_for_event(client, "fake/inheritedStdout")["params"]["pid"]
        client._process.wait(timeout=2)
        closer = threading.Thread(target=client.close)
        closer.start()
        try:
            closer.join(3)
            self.assertFalse(closer.is_alive(), "close blocked on stdout owned by the reader")
            with self.assertRaises(RpcError):
                client.request("fake/after-close")
            _wait_for_event(client, "transport/closed", timeout=0.2)
        finally:
            try:
                os.kill(child_pid, signal.SIGTERM)
            except OSError:
                pass
            closer.join(3)

    def test_invalid_server_frames_fail_closed_without_leaking_payload(self) -> None:
        for mode in ("invalid-id", "invalid-bool-id", "invalid-error", "invalid-params",
                     "invalid-json", "invalid-utf8"):
            with self.subTest(mode=mode):
                client = CodexClient(_command(mode))
                try:
                    with self.assertRaises(RpcError) as raised:
                        client.request("fake/pending", {"safe": True}, timeout=1)
                    self.assertNotIsInstance(raised.exception, RequestTimeout)
                    self.assertNotIn("DO-NOT-LEAK", str(raised.exception))
                    events = _wait_for_methods(
                        client, {"transport/protocolError", "transport/closed"})
                    serialized = json.dumps(events)
                    self.assertNotIn("DO-NOT-LEAK", serialized)
                    client._reader.join(1)
                    self.assertFalse(client._reader.is_alive())
                finally:
                    client.close()
                self.assertIsNotNone(client._process.poll())

    def test_read_error_is_generic_and_closes_transport(self) -> None:
        class BrokenOutput:
            def readline(self):
                raise OSError("DO-NOT-LEAK")

        class StoppedProcess:
            stdout = BrokenOutput()

            @staticmethod
            def poll():
                return 9

        client = CodexClient.__new__(CodexClient)
        client._process = StoppedProcess()
        client._state_lock = threading.Lock()
        client._pending = {}
        client._events = queue.SimpleQueue()
        client._closed = False

        client._read_messages()

        events = client.drain_events()
        self.assertEqual([event["method"] for event in events],
                         ["transport/protocolError", "transport/closed"])
        self.assertNotIn("DO-NOT-LEAK", json.dumps(events))


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--fake-server":
        _fake_server(sys.argv[2])
    else:
        unittest.main()
