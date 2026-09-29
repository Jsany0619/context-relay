"""Line-delimited JSON transport for ``codex app-server``."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import platform
import queue
import shutil
import subprocess
import threading
from typing import Any, Sequence


class RpcError(Exception):
    """An error response from the app-server, or a closed transport."""

    def __init__(self, code: int, message: str, data: Any = None, request_id: str | int | None = None):
        super().__init__(f"RPC error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data
        self.request_id = request_id


class RequestTimeout(TimeoutError):
    """The outcome of a request is unknown because its response was not timely."""

    def __init__(self, method: str, request_id: int, timeout: float):
        super().__init__(f"RPC request {method!r} ({request_id}) timed out after {timeout:g}s")
        self.method = method
        self.request_id = request_id
        self.timeout = timeout


@dataclass
class _Pending:
    method: str
    ready: threading.Event
    response: dict[str, Any] | None = None


def discover_codex_command() -> list[str]:
    """Return an argv for app-server without starting it or invoking a shell."""
    if os.name != "nt":
        executable = shutil.which("codex")
        if not executable:
            raise FileNotFoundError("Codex executable was not found on PATH")
        return [executable, "app-server"]

    direct = shutil.which("codex.exe")
    if direct:
        return [direct, "app-server"]

    wrapper = shutil.which("codex.cmd") or shutil.which("codex")
    package_root = Path(wrapper).parent / "node_modules" / "@openai" / "codex" if wrapper else None
    machine = platform.machine().lower()
    target = "aarch64-pc-windows-msvc" if machine in {"arm64", "aarch64"} else "x86_64-pc-windows-msvc"
    suffix = "arm64" if target.startswith("aarch64") else "x64"
    candidates: list[Path] = []
    if package_root:
        candidates.extend([
            package_root / "node_modules" / "@openai" / f"codex-win32-{suffix}" / "vendor" / target / "bin" / "codex.exe",
            package_root / "vendor" / target / "bin" / "codex.exe",
        ])
        candidates.extend(package_root.glob("node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe"))
    for candidate in candidates:
        if candidate.is_file():
            return [str(candidate), "app-server"]

    if package_root:
        script = package_root / "bin" / "codex.js"
        local_node = Path(wrapper).parent / "node.exe" if wrapper else None
        node = str(local_node) if local_node and local_node.is_file() else shutil.which("node.exe")
        if script.is_file() and node:
            return [node, str(script), "app-server"]
    raise FileNotFoundError("Codex native executable or Node launcher was not found")


class CodexClient:
    """Thread-safe request/response client for one Codex app-server process."""

    def __init__(self, command: Sequence[str | os.PathLike[str]] | str | os.PathLike[str] | None = None,
                 cwd: str | os.PathLike[str] | None = None):
        argv = discover_codex_command() if command is None else self._argv(command)
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self._process = subprocess.Popen(
            argv,
            cwd=os.fspath(cwd) if cwd is not None else None,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            creationflags=creationflags,
            shell=False,
        )
        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending: dict[int, _Pending] = {}
        self._timed_out: dict[int, str] = {}
        self._events: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()
        self._next_id = 1
        self._closed = False
        self._close_called = False
        self._reader = threading.Thread(target=self._read_messages, name="codex-app-server-reader", daemon=True)
        self._reader.start()
        try:
            self.request("initialize", {
                "clientInfo": {"name": "context-relay", "title": "Context Relay", "version": "0.1.0"},
            })
            self._send({"method": "initialized"})
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _argv(command: Sequence[str | os.PathLike[str]] | str | os.PathLike[str]) -> list[str]:
        parts = [command] if isinstance(command, (str, os.PathLike)) else list(command)
        if not parts:
            raise ValueError("command must not be empty")
        return [os.fspath(part) for part in parts]

    def request(self, method: str, params: Any = None, timeout: float = 30) -> dict[str, Any]:
        if not isinstance(method, str) or not method:
            raise ValueError("method must be a non-empty string")
        if timeout < 0:
            raise ValueError("timeout must not be negative")
        with self._state_lock:
            if self._closed:
                raise RpcError(-32000, "transport closed")
            request_id = self._next_id
            self._next_id += 1
            pending = _Pending(method, threading.Event())
            self._pending[request_id] = pending
        message: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        try:
            self._send(message)
        except BaseException:
            with self._state_lock:
                self._pending.pop(request_id, None)
            raise

        if not pending.ready.wait(timeout):
            with self._state_lock:
                if pending.response is None:
                    self._pending.pop(request_id, None)
                    self._timed_out[request_id] = method
                    raise RequestTimeout(method, request_id, timeout)
        response = pending.response
        if response is None:
            raise RpcError(-32000, "transport closed", request_id=request_id)
        if "error" in response:
            error = response["error"]
            raise RpcError(error.get("code", -32000), error.get("message", "RPC request failed"),
                           error.get("data"), request_id)
        return response.get("result")

    def respond(self, request_id: str | int, result: Any) -> None:
        if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
            raise ValueError("request_id must be a string or integer")
        self._send({"id": request_id, "result": result})

    def drain_events(self) -> list[dict[str, Any]]:
        events = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                return events

    def close(self) -> None:
        with self._state_lock:
            if self._close_called:
                return
            self._close_called = True
        if self._process.stdin:
            try:
                self._process.stdin.close()
            except OSError:
                pass
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2)
        self._reader.join(timeout=2)
        if self._process.stdout:
            self._process.stdout.close()

    def _send(self, message: dict[str, Any]) -> None:
        data = json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n"
        with self._write_lock:
            with self._state_lock:
                if self._closed:
                    raise RpcError(-32000, "transport closed")
            if self._process.stdin is None:
                raise RpcError(-32000, "transport has no stdin")
            try:
                self._process.stdin.write(data)
                self._process.stdin.flush()
            except (BrokenPipeError, OSError) as error:
                raise RpcError(-32000, "transport write failed", str(error)) from error

    def _read_messages(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("message is not an object")
            except (json.JSONDecodeError, ValueError) as error:
                self._events.put({"method": "transport/protocolError", "params": {"error": str(error)}})
                continue
            if "id" in message and "method" not in message and ("result" in message or "error" in message):
                self._accept_response(message)
            elif isinstance(message.get("method"), str):
                self._events.put(message)
            else:
                self._events.put({"method": "transport/protocolError", "params": {"error": "invalid message shape"}})
        self._finish_closed()

    def _accept_response(self, message: dict[str, Any]) -> None:
        request_id = message["id"]
        with self._state_lock:
            pending = self._pending.pop(request_id, None)
            timed_out_method = self._timed_out.pop(request_id, None)
            if pending is not None:
                pending.response = message
                pending.ready.set()
                return
        params: dict[str, Any] = {"requestId": request_id}
        if timed_out_method is not None:
            params["requestMethod"] = timed_out_method
            if "result" in message:
                params["result"] = message["result"]
            else:
                params["error"] = message["error"]
            self._events.put({"method": "transport/lateResponse", "params": params})
        else:
            params["response"] = message
            self._events.put({"method": "transport/orphanResponse", "params": params})

    def _finish_closed(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            item.response = {"error": {"code": -32000, "message": "transport closed"}}
            item.ready.set()
        self._events.put({"method": "transport/closed", "params": {"returncode": self._process.poll()}})
