"""Pinned-HTTPS phone gateway for the single Context Relay manager worker."""

from __future__ import annotations

import base64
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit
import uuid

from .transport import RequestTimeout, RpcError
from . import secrets as protected_secrets


MAX_BODY_BYTES = 64 * 1024
RATE_LIMIT = 60
RATE_WINDOW_SECONDS = 60
PAIRING_TTL_SECONDS = 5 * 60
DEVICE_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_DEVICE_TASKS = 25
MAX_MESSAGES = 40
MAX_MESSAGE_TEXT = 3000
MAX_MESSAGES_BYTES = 48 * 1024
DEVICE_SCOPES = {"read_only", "control"}
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_TASK_FIELDS = (
    "id", "title", "state", "goal", "mode", "last_message", "error", "usage",
    "max_tokens", "max_minutes", "work_turns", "brief_required", "acceptance_criteria", "archived",
)
_ASSESSMENT_FIELDS = (
    "report", "status", "decision", "machine_checks", "human_acceptance",
    "adopted_goal", "adopted_acceptance",
)
_PENDING_PARAM_FIELDS = {"command", "commandActions", "changes", "questions", "reason", "cwd"}
_APPROVALS = {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}
_USER_INPUT = "item/tool/requestUserInput"
_COMMANDS = {
    "start", "pause", "reconcile", "answer", "analyze", "adopt_brief",
    "accept_review", "revise_from_review",
}


class GatewayError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise GatewayError("invalid_json", "JSON 内容无效。") from error


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _credential_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _random_token() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


def _json_copy(value: Any, *, maximum: int = MAX_BODY_BYTES) -> Any:
    encoded = _canonical(value)
    if len(encoded) > maximum:
        raise GatewayError("body_too_large", "内容超过允许大小。", 413)
    return json.loads(encoded)


def _valid_uuid(value: Any) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _openssl_command() -> str:
    found = shutil.which("openssl")
    if found:
        return found
    candidates = []
    if os.name == "nt":
        candidates.extend([
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git/usr/bin/openssl.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git/mingw64/bin/openssl.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Git/usr/bin/openssl.exe",
        ])
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise ValueError("找不到 OpenSSL，无法生成手机网关证书。")


def _certificate_fingerprint(path: Path) -> str:
    try:
        text = path.read_text(encoding="ascii")
        der = ssl.PEM_cert_to_DER_cert(text)
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("手机网关证书无法读取。") from error
    return hashlib.sha256(der).hexdigest()


def ensure_certificate(root) -> tuple[Path, Path, str]:
    """Preserve the certificate identity; keep its key in current-user DPAPI."""
    directory = protected_secrets.private_directory(root)
    private = protected_secrets.private_directory(directory / "secrets")
    certificate = directory / "gateway-cert.pem"
    legacy = directory / "gateway-key.pem"
    key = private / "gateway-key.dpapi"
    if certificate.exists() != (key.exists() or legacy.exists()):
        raise ValueError("手机网关证书或私钥缺失，不能静默替换。")
    if certificate.exists():
        protected_secrets.restrict_file(certificate)
        old_raw = protected_secrets.restrict_file(legacy).read_bytes() if legacy.exists() else None
        if key.exists():
            protected_secrets.restrict_file(key)
            raw = protected_secrets.load(key, "gateway-tls-key")
            if old_raw is not None and not hmac.compare_digest(old_raw, raw):
                raise ValueError("现有明文私钥与受保护私钥不同，保留原文件并拒绝开启。")
        else:
            raw = old_raw
        with protected_secrets.private_temporary_directory(private) as temporary:
            check_key = temporary / "key.pem"
            protected_secrets.write_private(check_key, raw)
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(str(certificate), str(check_key))
        if not key.exists():
            protected_secrets.save(key, raw, "gateway-tls-key")
        if not hmac.compare_digest(protected_secrets.load(key, "gateway-tls-key"), raw):
            raise ValueError("受保护私钥回读失败，保留原文件并拒绝开启。")
        if old_raw is not None:
            if not hmac.compare_digest(legacy.read_bytes(), old_raw):
                raise ValueError("迁移期间原私钥发生变化，保留原文件并拒绝开启。")
            try:
                legacy.unlink()
            except OSError as error:
                raise ValueError("无法移除已迁移的明文私钥，拒绝开启。") from error
    else:
        with protected_secrets.private_temporary_directory(private) as temporary:
            temp_cert, temp_key = temporary / "cert.pem", temporary / "key.pem"
            command = [_openssl_command(), "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes",
                       "-days", "3650", "-subj", "/CN=Context Relay", "-keyout", str(temp_key),
                       "-out", str(temp_cert)]
            completed = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                       timeout=30, check=False)
            if completed.returncode != 0 or not temp_cert.is_file() or not temp_key.is_file():
                raise ValueError("OpenSSL 未能生成手机网关证书。")
            protected_secrets.restrict_file(temp_key)
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(str(temp_cert), str(temp_key))
            protected_secrets.save(key, temp_key.read_bytes(), "gateway-tls-key")
            protected_secrets.write_private(certificate, temp_cert.read_bytes())
    return certificate, key, _certificate_fingerprint(certificate)


class GatewayCore:
    """Durable device and command journal plus immutable phone snapshots."""

    def __init__(self, root, enqueue_callable: Callable[[str], None], conversation_reader=None):
        self.root = protected_secrets.private_directory(root)
        self.path = self.root / "remote-control.sqlite3"
        for name in (self.path.name, self.path.name + "-wal", self.path.name + "-shm"):
            existing = self.root / name
            if existing.exists():
                protected_secrets.restrict_file(existing)
        self._enqueue = enqueue_callable
        self._conversation_reader = conversation_reader
        self._lock = threading.RLock()
        self._closed = False
        self._payloads: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, dict[str, Any]] = {}
        self._raw_tasks: dict[str, dict[str, Any]] = {}
        self._connection_id: str | None = None
        self._cursor = 0
        self._recovery_info = None
        self._rate_limit = RATE_LIMIT
        self._rate_windows: dict[str, deque[float]] = defaultdict(deque)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _database(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._database() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("""CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL, revoked_at TEXT, scope TEXT NOT NULL,
                task_ids_json TEXT NOT NULL, expires_at REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS pairings (
                secret_hash TEXT PRIMARY KEY, endpoint TEXT NOT NULL, certificate_sha256 TEXT NOT NULL,
                expires_at REAL NOT NULL, used_at TEXT, scope TEXT NOT NULL,
                task_ids_json TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS commands (
                request_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, task_id TEXT NOT NULL,
                command TEXT NOT NULL, payload_hash TEXT NOT NULL, expected_etag TEXT NOT NULL,
                state TEXT NOT NULL, result_json TEXT, error_code TEXT, error_message TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            device_columns = {row[1] for row in db.execute("PRAGMA table_info(devices)")}
            pairing_columns = {row[1] for row in db.execute("PRAGMA table_info(pairings)")}
            for name, column_type in (("scope", "TEXT"), ("task_ids_json", "TEXT"),
                                      ("expires_at", "REAL")):
                if name not in device_columns:
                    db.execute(f"ALTER TABLE devices ADD COLUMN {name} {column_type}")
            for name in ("scope", "task_ids_json"):
                if name not in pairing_columns:
                    db.execute(f"ALTER TABLE pairings ADD COLUMN {name} TEXT")
            now = _now()
            db.execute("""UPDATE commands SET state='unknown', error_code='outcome_unknown',
                error_message='电脑端在完成前停止；请核对任务状态，不要重发原操作。', updated_at=?
                WHERE state IN ('accepted','running')""", (now,))

    def check_rate(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            window = self._rate_windows[key]
            while window and window[0] <= now - RATE_WINDOW_SECONDS:
                window.popleft()
            if len(window) >= self._rate_limit:
                raise GatewayError("rate_limited", "请求过于频繁，请稍后再试。", 429)
            window.append(now)

    @staticmethod
    def _task_permissions(value) -> list[str] | None:
        if isinstance(value, str):
            if len(value) > 8192:
                return None
            try:
                value = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                return None
        if (not isinstance(value, list) or not 1 <= len(value) <= MAX_DEVICE_TASKS
                or any(not isinstance(item, str) or not item or len(item) > 128 for item in value)
                or len(set(value)) != len(value)):
            return None
        return list(value)

    def _device_access(self, device_id, task_id=None, *, control=False) -> dict[str, Any]:
        if not isinstance(device_id, str):
            raise GatewayError("unauthorized", "设备凭据无效。", 401)
        with self._database() as db:
            row = db.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        if not row or row["revoked_at"] is not None:
            raise GatewayError("unauthorized", "设备已撤销或不存在。", 401)
        task_ids = self._task_permissions(row["task_ids_json"])
        if (row["scope"] not in DEVICE_SCOPES or task_ids is None
                or not isinstance(row["expires_at"], (int, float)) or isinstance(row["expires_at"], bool)
                or not math.isfinite(row["expires_at"])):
            raise GatewayError("reauthorization_required", "旧设备权限已失效，请重新配对。", 401)
        if row["expires_at"] <= time.time():
            raise GatewayError("device_expired", "设备令牌已过期，请重新配对。", 401)
        if task_id is not None and task_id not in task_ids:
            raise KeyError(task_id)
        if control and row["scope"] != "control":
            raise GatewayError("read_only_device", "此设备仅有只读权限，不能提交命令。", 403)
        return {"id": row["id"], "scope": row["scope"], "task_ids": task_ids,
                "expires_at": row["expires_at"]}

    def new_pairing(self, endpoint, certificate_sha256, task_ids, scope="read_only") -> str:
        if not isinstance(endpoint, str) or len(endpoint) > 2048:
            raise ValueError("配对地址无效。")
        parsed = urlsplit(endpoint)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
            raise ValueError("配对地址必须是 HTTPS 主机地址。")
        if not isinstance(certificate_sha256, str) or not _HEX64.fullmatch(certificate_sha256):
            raise ValueError("证书指纹无效。")
        permissions = self._task_permissions(list(task_ids) if isinstance(task_ids, (list, tuple)) else task_ids)
        if permissions is None:
            raise ValueError("请选择 1 到 25 个允许手机访问的任务。")
        if scope not in DEVICE_SCOPES:
            raise ValueError("设备权限必须是 read_only 或 control。")
        secret = _random_token()
        with self._lock, self._database() as db:
            if self._closed:
                raise ValueError("手机网关已经关闭。")
            if any(task_id not in self._tasks for task_id in permissions):
                raise ValueError("所选任务已经变化，请刷新后重选。")
            db.execute("""INSERT INTO pairings
                (secret_hash,endpoint,certificate_sha256,expires_at,used_at,scope,task_ids_json)
                VALUES (?,?,?,?,NULL,?,?)""",
                       (_credential_hash(secret), endpoint.rstrip("/"), certificate_sha256,
                        time.time() + PAIRING_TTL_SECONDS, scope,
                        json.dumps(permissions, ensure_ascii=False, separators=(",", ":"))))
        value = {"version": 1, "endpoint": endpoint.rstrip("/"),
                 "certificate_sha256": certificate_sha256, "secret": secret}
        fragment = base64.urlsafe_b64encode(_canonical(value)).rstrip(b"=").decode("ascii")
        return "contextrelay://pair#" + fragment

    def pair(self, secret, device_name) -> dict[str, Any]:
        if self._closed:
            raise GatewayError("unavailable", "手机网关已经关闭。", 503)
        if (not isinstance(secret, str) or not secret or len(secret) > 256
                or not isinstance(device_name, str) or not device_name.strip() or len(device_name) > 100):
            raise GatewayError("invalid_pairing", "配对信息无效。", 401)
        digest, now = _credential_hash(secret), time.time()
        token, device_id, at = _random_token(), uuid.uuid4().hex, _now()
        with self._lock, self._database() as db:
            row = db.execute("SELECT * FROM pairings WHERE secret_hash=?", (digest,)).fetchone()
            permissions = self._task_permissions(row["task_ids_json"]) if row else None
            if (not row or row["used_at"] is not None
                    or not isinstance(row["expires_at"], (int, float)) or isinstance(row["expires_at"], bool)
                    or not math.isfinite(row["expires_at"])
                    or row["expires_at"] < now
                    or row["scope"] not in DEVICE_SCOPES or permissions is None
                    or any(task_id not in self._tasks for task_id in permissions)):
                raise GatewayError("invalid_pairing", "配对码无效或已经过期。", 401)
            updated = db.execute("UPDATE pairings SET used_at=? WHERE secret_hash=? AND used_at IS NULL",
                                 (at, digest)).rowcount
            if updated != 1:
                raise GatewayError("invalid_pairing", "配对码已经使用。", 401)
            device_expires = now + DEVICE_TTL_SECONDS
            db.execute("""INSERT INTO devices
                (id,name,token_hash,created_at,revoked_at,scope,task_ids_json,expires_at)
                VALUES (?,?,?,?,NULL,?,?,?)""",
                       (device_id, device_name.strip(), _credential_hash(token), at, row["scope"],
                        json.dumps(permissions, ensure_ascii=False, separators=(",", ":")), device_expires))
        return {"device_id": device_id, "token": token, "scope": row["scope"],
                "task_ids": permissions,
                "expires_at": datetime.fromtimestamp(device_expires, timezone.utc).isoformat()}

    def authenticate(self, token) -> str:
        if self._closed:
            raise GatewayError("unavailable", "手机网关已经关闭。", 503)
        if not isinstance(token, str) or not token or len(token) > 256:
            raise GatewayError("unauthorized", "设备令牌无效。", 401)
        with self._lock, self._database() as db:
            row = db.execute("SELECT id FROM devices WHERE token_hash=?", (_credential_hash(token),)).fetchone()
        if not row:
            raise GatewayError("unauthorized", "设备令牌无效或已撤销。", 401)
        self._device_access(row["id"])
        return row["id"]

    def local_status(self) -> dict[str, Any]:
        with self._lock, self._database() as db:
            rows = db.execute("SELECT * FROM devices ORDER BY created_at").fetchall()
        devices = []
        for row in rows:
            task_ids = self._task_permissions(row["task_ids_json"])
            valid_expiry = (isinstance(row["expires_at"], (int, float))
                            and not isinstance(row["expires_at"], bool) and math.isfinite(row["expires_at"]))
            legacy = row["scope"] not in DEVICE_SCOPES or task_ids is None or not valid_expiry
            expired = legacy or row["expires_at"] <= time.time()
            expires_at = None
            if valid_expiry:
                try:
                    expires_at = datetime.fromtimestamp(row["expires_at"], timezone.utc).isoformat()
                except (OSError, OverflowError, ValueError):
                    expired = True
            devices.append({"id": row["id"], "name": row["name"], "created_at": row["created_at"],
                            "revoked": row["revoked_at"] is not None, "revoked_at": row["revoked_at"],
                            "expired": expired, "scope": row["scope"] if row["scope"] in DEVICE_SCOPES else None,
                            "task_ids": task_ids or [], "expires_at": expires_at})
        return {"devices": devices}

    def revoke_device(self, device_id):
        if not isinstance(device_id, str):
            raise ValueError("设备编号无效。")
        with self._lock, self._database() as db:
            if db.execute("UPDATE devices SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                          (_now(), device_id)).rowcount != 1:
                raise ValueError("找不到可撤销的设备。")
            request_ids = [row[0] for row in db.execute(
                "SELECT request_id FROM commands WHERE device_id=? AND state='accepted'", (device_id,))]
            db.execute("""UPDATE commands SET state='unknown',error_code='device_revoked',
                error_message='设备已撤销；命令没有执行。',updated_at=?
                WHERE device_id=? AND state='accepted'""", (_now(), device_id))
            for request_id in request_ids:
                self._payloads.pop(request_id, None)

    @staticmethod
    def _assessment(value) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        return {key: _json_copy(value[key]) for key in _ASSESSMENT_FIELDS if key in value}

    @staticmethod
    def _bounded_field(value: Any, maximum=16 * 1024) -> tuple[Any | None, bool]:
        try:
            encoded = _canonical(value)
        except GatewayError:
            return None, True
        if len(encoded) > maximum:
            return None, True
        return json.loads(encoded), False

    @staticmethod
    def _answerable_questions(value: Any) -> bool:
        if not isinstance(value, list) or not value:
            return False
        identifiers = set()
        for question in value:
            if (not isinstance(question, dict) or question.get("isSecret") is True
                    or not isinstance(question.get("id"), str) or not question["id"]
                    or not isinstance(question.get("question"), str) or not question["question"].strip()
                    or question["id"] in identifiers):
                return False
            options = question.get("options")
            if options is not None and (not isinstance(options, list) or any(
                    not isinstance(option, dict) or not isinstance(option.get("label"), str)
                    for option in options)):
                return False
            identifiers.add(question["id"])
        return True

    @classmethod
    def _pending(cls, task: dict[str, Any]) -> list[dict[str, Any]]:
        result = []
        inflight = task.get("inflight") if isinstance(task.get("inflight"), dict) else {}
        for request in task.get("pending", []) if isinstance(task.get("pending"), list) else []:
            if not isinstance(request, dict):
                continue
            identifier, method, params = request.get("id"), request.get("method"), request.get("params", {})
            if (not isinstance(identifier, (str, int)) or isinstance(identifier, bool)
                    or not isinstance(method, str) or not isinstance(params, dict)):
                continue
            clean, truncated = {}, False
            for key in _PENDING_PARAM_FIELDS:
                if key in params:
                    value, cut = cls._bounded_field(params[key])
                    truncated = truncated or cut
                    if not cut:
                        clean[key] = value
            operation = inflight.get(params.get("itemId"))
            if isinstance(operation, dict):
                for key in ("command", "commandActions", "changes"):
                    if key not in clean and key in operation:
                        value, cut = cls._bounded_field(operation[key])
                        truncated = truncated or cut
                        if not cut:
                            clean[key] = value
            has_evidence = ((method == "item/commandExecution/requestApproval"
                             and bool(clean.get("command") or clean.get("commandActions")))
                            or (method == "item/fileChange/requestApproval" and bool(clean.get("changes"))))
            result.append({"id": identifier, "method": method, "params": clean,
                           "can_approve": bool(not truncated and (
                               method in _APPROVALS and has_evidence
                               or method == _USER_INPUT and cls._answerable_questions(clean.get("questions"))))})
        return result

    @staticmethod
    def _messages(value) -> tuple[list[dict[str, Any]], bool]:
        if not isinstance(value, list):
            return [], value is not None
        result, size, truncated = [], 2, False
        for item in reversed(value):
            if len(result) >= MAX_MESSAGES:
                truncated = True
                break
            if (not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]
                    or item.get("role") not in {"user", "assistant"}
                    or not isinstance(item.get("text"), str) or not isinstance(item.get("created_at"), str)
                    or item.get("status") != "completed" or item.get("purpose") != "work"):
                truncated = True
                continue
            text = item["text"]
            item_truncated = item.get("truncated") is True
            if len(text) > MAX_MESSAGE_TEXT:
                text, item_truncated = text[:MAX_MESSAGE_TEXT], True
            clean = {"id": item["id"], "role": item["role"], "text": text,
                     "created_at": item["created_at"], "status": "completed", "purpose": "work",
                     "historical": item.get("historical") is True, "truncated": item_truncated}
            encoded = _canonical(clean)
            if size + len(encoded) + 1 > MAX_MESSAGES_BYTES:
                truncated = True
                break
            result.append(clean)
            size += len(encoded) + 1
            truncated = truncated or item_truncated
        result.reverse()
        return result, truncated

    def _task_dto(self, task: dict[str, Any], connection_id: str | None,
                  remote_access=None) -> dict[str, Any]:
        if not isinstance(task, dict) or not isinstance(task.get("id"), str):
            raise ValueError("任务快照无效。")
        messages, messages_truncated = self._messages(task.get("messages"))
        dto = {key: _json_copy(task.get(key)) for key in _TASK_FIELDS}
        if task.get("connection_mode") == "direct":
            dto["connection_mode"] = "direct"
        if (task.get("last_message_kind") in {"brief", "review", "summary", "verify"}
                or task.get("purpose") in {"brief", "review", "summary", "verify"}):
            dto["last_message"] = "正在整理或核验资料；原始对话中可查看真实任务内容。"
        dto.update(etag=_hash({"connection_id": connection_id, "task": task}),
                   pending=self._pending(task), brief=self._assessment(task.get("brief")),
                   review=self._assessment(task.get("review")), messages=messages,
                   messages_truncated=messages_truncated or task.get("messages_truncated") is True)
        if remote_access is not None:
            dto["remote_access"] = remote_access
        return dto

    def publish_tasks(self, raw_tasks, recovery_info=None, connection_id=None):
        if not isinstance(raw_tasks, list) or connection_id is not None and not isinstance(connection_id, str):
            raise ValueError("任务快照无效。")
        copied, projected = {}, {}
        for task in raw_tasks:
            item = _json_copy(task, maximum=1024 * 1024)
            dto = self._task_dto(item, connection_id)
            copied[item["id"]], projected[item["id"]] = item, dto
        with self._lock:
            if (projected != self._tasks or recovery_info != self._recovery_info
                    or connection_id != self._connection_id):
                self._cursor += 1
            self._raw_tasks, self._tasks = copied, projected
            self._recovery_info = _json_copy(recovery_info) if recovery_info is not None else None
            self._connection_id = connection_id
            return self._cursor

    def _device_tasks(self, device_id) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        access = self._device_access(device_id)
        tasks = []
        for task_id in access["task_ids"]:
            if task_id in self._tasks:
                task = _json_copy(self._tasks[task_id], maximum=1024 * 1024)
                task["remote_access"] = access["scope"]
                tasks.append(task)
        return access, tasks

    @staticmethod
    def _device_cursor(tasks) -> int:
        return int(_hash(tasks)[:15], 16)

    def list_task_snapshots(self, device_id) -> dict[str, Any]:
        with self._lock:
            _, tasks = self._device_tasks(device_id)
            return {"tasks": _json_copy(tasks, maximum=2 * 1024 * 1024),
                    "cursor": self._device_cursor(tasks)}

    def get_task_snapshot(self, device_id, task_id) -> dict[str, Any]:
        with self._lock:
            access = self._device_access(device_id, task_id)
            value = self._tasks.get(task_id)
            if value is None:
                raise KeyError(task_id)
            result = _json_copy(value, maximum=1024 * 1024)
            result["remote_access"] = access["scope"]
            return result

    def status_snapshot(self, device_id) -> dict[str, Any]:
        with self._lock:
            _, tasks = self._device_tasks(device_id)
            return {"online": not self._closed, "cursor": self._device_cursor(tasks)}

    def read_conversation(self, device_id, task_id, cursor=None):
        with self._lock:
            self._device_access(device_id, task_id)
            if self._closed or self._conversation_reader is None:
                raise GatewayError("unavailable", "原文读取暂不可用，请更新并开启电脑端。", 503)
            if task_id not in self._tasks:
                raise KeyError(task_id)
        try:
            result = self._conversation_reader(task_id, cursor)
        except ValueError as error:
            raise GatewayError("conversation_unavailable", str(error), 409) from error
        with self._lock:
            # A revoked device must not receive a read that was in flight at revocation.
            self._device_access(device_id, task_id)
            if self._closed:
                raise GatewayError("unavailable", "手机连接已关闭。", 503)
            return _json_copy(result, maximum=64 * 1024)

    @staticmethod
    def _validate_command(body: Any) -> tuple[dict[str, Any], str]:
        if not isinstance(body, dict) or set(body) != {"request_id", "task_id", "command", "expected_etag", "payload"}:
            raise GatewayError("invalid_command", "命令字段无效。")
        request_id, task_id = body.get("request_id"), body.get("task_id")
        command, etag, payload = body.get("command"), body.get("expected_etag"), body.get("payload")
        if (not _valid_uuid(request_id) or not isinstance(task_id, str) or not task_id or len(task_id) > 128
                or not isinstance(command, str) or command not in _COMMANDS
                or not isinstance(etag, str) or not _HEX64.fullmatch(etag) or not isinstance(payload, dict)):
            raise GatewayError("invalid_command", "命令内容无效。")
        if command == "start":
            if set(payload) != {"message"} or payload["message"] is not None and (
                    not isinstance(payload["message"], str) or len(payload["message"]) > 4000):
                raise GatewayError("invalid_command", "继续任务参数无效。")
        elif command in {"pause", "reconcile", "accept_review", "revise_from_review"}:
            if payload:
                raise GatewayError("invalid_command", "此命令不接受额外参数。")
        elif command == "analyze":
            if set(payload) != {"kind"} or payload.get("kind") not in {"brief", "review"}:
                raise GatewayError("invalid_command", "分析类型无效。")
        elif command == "adopt_brief":
            goal, acceptance = payload.get("goal"), payload.get("acceptance")
            if (set(payload) != {"goal", "acceptance"} or not isinstance(goal, str) or not goal.strip()
                    or len(goal) > 4000 or not isinstance(acceptance, list) or not 1 <= len(acceptance) <= 30
                    or any(not isinstance(item, str) or not item.strip() or len(item) > 4000 for item in acceptance)):
                raise GatewayError("invalid_command", "简报采用参数无效。")
        elif command == "answer":
            pending, answer = payload.get("pending_request_id"), payload.get("answer")
            if (set(payload) != {"pending_request_id", "answer"}
                    or not isinstance(pending, (str, int)) or isinstance(pending, bool) or not isinstance(answer, dict)):
                raise GatewayError("invalid_command", "回答参数无效。")
        copied = _json_copy(body)
        return copied, _hash(copied)

    def submit_command(self, device_id, body) -> dict[str, Any]:
        command, payload_hash = self._validate_command(body)
        with self._lock:
            if self._closed:
                raise GatewayError("unavailable", "手机网关已经关闭。", 503)
            self._device_access(device_id, command["task_id"], control=True)
            with self._database() as db:
                existing = db.execute("SELECT * FROM commands WHERE request_id=?", (command["request_id"],)).fetchone()
                if existing:
                    if existing["device_id"] != device_id:
                        raise GatewayError("conflict", "请求编号已经使用。", 409)
                    if existing["payload_hash"] != payload_hash:
                        raise GatewayError("conflict", "同一请求编号不能更改内容。", 409)
                    return self._receipt(existing)
            snapshot = self._tasks.get(command["task_id"])
            if snapshot is None:
                raise GatewayError("not_found", "找不到任务。", 404)
            if snapshot["etag"] != command["expected_etag"]:
                raise GatewayError("stale_task", "任务已经变化，请刷新后再操作。", 409)
            now = _now()
            with self._database() as db:
                db.execute("""INSERT INTO commands
                    (request_id,device_id,task_id,command,payload_hash,expected_etag,state,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?)""", (
                    command["request_id"], device_id, command["task_id"], command["command"], payload_hash,
                    command["expected_etag"], "accepted", now, now))
            self._payloads[command["request_id"]] = command
            try:
                self._enqueue(command["request_id"])
            except Exception as error:
                self._finish(command["request_id"], "failed", error_code="queue_unavailable",
                             error_message="电脑端命令队列不可用。")
                raise GatewayError("unavailable", "电脑端命令队列不可用。", 503) from error
            return self.command_status(device_id, command["request_id"])

    @staticmethod
    def _receipt(row: sqlite3.Row) -> dict[str, Any]:
        value = {key: row[key] for key in ("request_id", "task_id", "command", "state", "created_at", "updated_at")}
        if row["state"] == "succeeded" and row["result_json"]:
            value["result"] = json.loads(row["result_json"])
        if row["state"] in {"failed", "unknown"}:
            value["error"] = {"code": row["error_code"] or "command_failed",
                              "message": row["error_message"] or "命令未完成。"}
        return value

    def command_status(self, device_id, request_id) -> dict[str, Any]:
        with self._lock, self._database() as db:
            access = self._device_access(device_id)
            row = db.execute("SELECT * FROM commands WHERE request_id=? AND device_id=?",
                             (request_id, device_id)).fetchone()
            if not row:
                raise KeyError(request_id)
            if row["task_id"] not in access["task_ids"]:
                raise KeyError(request_id)
            return self._receipt(row)

    def _finish(self, request_id, state, result=None, error_code=None, error_message=None,
                expected_state=None) -> bool:
        with self._lock, self._database() as db:
            sql = """UPDATE commands SET state=?,result_json=?,error_code=?,error_message=?,updated_at=?
                     WHERE request_id=?"""
            values = [state, json.dumps(result, ensure_ascii=False, separators=(",", ":")) if result else None,
                      error_code, error_message, _now(), request_id]
            if expected_state is not None:
                sql += " AND state=?"
                values.append(expected_state)
            changed = db.execute(sql, values).rowcount == 1
            if changed:
                self._payloads.pop(request_id, None)
            return changed

    def _command_receipt(self, request_id) -> dict[str, Any]:
        with self._database() as db:
            row = db.execute("SELECT * FROM commands WHERE request_id=?", (request_id,)).fetchone()
            if not row:
                raise KeyError(request_id)
            return self._receipt(row)

    def _failed_receipt(self, request_id, code, message) -> dict[str, Any]:
        self._finish(request_id, "failed", error_code=code, error_message=message, expected_state="running")
        return self._command_receipt(request_id)

    def execute(self, request_id, manager) -> dict[str, Any]:
        with self._lock:
            with self._database() as db:
                row = db.execute("SELECT * FROM commands WHERE request_id=?", (request_id,)).fetchone()
            if not row:
                raise KeyError(request_id)
            if row["state"] != "accepted":
                return self._receipt(row)
            try:
                access = self._device_access(row["device_id"], row["task_id"], control=True)
            except (GatewayError, KeyError) as error:
                code = error.code if isinstance(error, GatewayError) else "task_access_revoked"
                self._finish(request_id, "unknown", error_code=code,
                             error_message="设备权限已撤销或过期；命令没有执行。",
                             expected_state="accepted")
                return self._command_receipt(request_id)
            if self._closed:
                self._finish(request_id, "unknown", error_code="outcome_unknown",
                             error_message="手机网关已关闭；请核对任务状态，不要重发原操作。",
                             expected_state="accepted")
                return self._command_receipt(request_id)
            command = self._payloads.get(request_id)
            if command is None:
                self._finish(request_id, "unknown", error_code="outcome_unknown",
                             error_message="命令正文已经丢失；请核对任务状态，不要重发原操作。",
                             expected_state="accepted")
                return self._command_receipt(request_id)
            with self._database() as db:
                changed = db.execute(
                    "UPDATE commands SET state='running',updated_at=? WHERE request_id=? AND state='accepted'",
                    (_now(), request_id)).rowcount == 1
            if not changed:
                return self._command_receipt(request_id)

        try:
            tasks = manager.list_tasks()
            current = next((item for item in tasks if item.get("id") == command["task_id"]), None)
            if current is None:
                return self._failed_receipt(request_id, "not_found", "找不到任务。")
            current_etag = self._task_dto(_json_copy(current, maximum=1024 * 1024), self._connection_id)["etag"]
            if current_etag != command["expected_etag"]:
                return self._failed_receipt(request_id, "stale_task", "任务已经变化，请刷新后再操作。")
            name, task_id, payload = command["command"], command["task_id"], command["payload"]
            if name == "answer":
                visible = {item["id"]: item for item in self._pending(current)}
                pending = visible.get(payload["pending_request_id"])
                if pending is None:
                    return self._failed_receipt(request_id, "stale_approval", "待处理请求已经变化。")
                answer = payload["answer"]
                if pending["method"] in _APPROVALS:
                    if answer not in ({"decision": "accept"}, {"decision": "decline"}):
                        return self._failed_receipt(request_id, "invalid_answer", "审批回答格式无效。")
                    if answer["decision"] == "accept" and not pending["can_approve"]:
                        return self._failed_receipt(request_id, "incomplete_approval",
                                                    "审批详情缺失或已截断，不能允许。")
                elif pending["method"] != _USER_INPUT:
                    return self._failed_receipt(request_id, "unsupported_request", "不支持此待处理请求。")
                result = manager.answer(task_id, payload["pending_request_id"], answer)
            elif name == "start":
                result = manager.start(task_id, payload["message"])
            elif name == "pause":
                result = manager.pause(task_id)
            elif name == "reconcile":
                result = manager.reconcile(task_id)
            elif name == "analyze":
                result = manager.analyze(task_id, payload["kind"])
            elif name == "adopt_brief":
                result = manager.adopt_brief(task_id, payload["goal"], payload["acceptance"])
            elif name == "accept_review":
                result = manager.accept_review(task_id)
            else:
                result = manager.revise_from_review(task_id)
            latest = manager.list_tasks()
            self.publish_tasks(latest, self._recovery_info, self._connection_id)
            current = next((item for item in latest if item.get("id") == task_id), result)
            if isinstance(current, dict) and current.get("state") == "needs_reconcile":
                self._finish(request_id, "unknown", error_code="outcome_unknown",
                             error_message="电脑端需要核对原生结果；不要重发原操作。", expected_state="running")
                return self._command_receipt(request_id)
            dto = self._task_dto(_json_copy(current, maximum=1024 * 1024), self._connection_id,
                                  access["scope"])
            self._finish(request_id, "succeeded", {"task": dto}, expected_state="running")
        except Exception as error:
            unknown = isinstance(error, (RequestTimeout, RpcError))
            try:
                latest = manager.list_tasks()
                current = next((item for item in latest if item.get("id") == command["task_id"]), None)
                unknown = unknown or isinstance(current, dict) and current.get("state") == "needs_reconcile"
                self.publish_tasks(latest, self._recovery_info, self._connection_id)
            except Exception:
                pass
            if unknown:
                self._finish(request_id, "unknown", error_code="outcome_unknown",
                             error_message="原生请求结果未知；请核对任务状态，不要重发原操作。",
                             expected_state="running")
            else:
                self._finish(request_id, "failed", error_code="command_failed",
                             error_message=str(error)[:1000] or "命令未完成。", expected_state="running")
        return self._command_receipt(request_id)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            with self._database() as db:
                db.execute("""UPDATE commands SET state='unknown',error_code='outcome_unknown',
                    error_message='手机网关已经关闭；请核对任务状态，不要重发原操作。',updated_at=?
                    WHERE state IN ('accepted','running')""", (_now(),))
            self._payloads.clear()


class _TLSHandshakeFailed(Exception):
    pass


class _GatewayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 16

    def __init__(self, address, handler, core):
        self.core = core
        self._handler_slots = threading.BoundedSemaphore(16)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self._handler_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._handler_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._handler_slots.release()

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], _TLSHandshakeFailed):
            return
        super().handle_error(request, client_address)


class _Handler(BaseHTTPRequestHandler):
    server_version = "ContextRelayGateway/1"
    sys_version = ""

    def log_message(self, format, *args):
        return

    def setup(self):
        self.request.settimeout(5)
        try:
            self.request.do_handshake()
        except (OSError, ssl.SSLError, TimeoutError) as error:
            raise _TLSHandshakeFailed from error
        self.request.settimeout(10)
        super().setup()

    @property
    def core(self) -> GatewayCore:
        return self.server.core

    def _send(self, status: int, value: Any):
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, error: Exception):
        if isinstance(error, GatewayError):
            self._send(error.status, {"error": {"code": error.code, "message": str(error)}})
        elif isinstance(error, KeyError):
            self._send(404, {"error": {"code": "not_found", "message": "找不到请求的记录。"}})
        else:
            self._send(500, {"error": {"code": "internal_error", "message": "手机网关处理失败。"}})

    def _origin(self):
        if self.headers.get("Origin") is not None:
            raise GatewayError("origin_forbidden", "浏览器跨域请求已拒绝。", 403)

    def _device(self) -> str:
        self.core.check_rate("ip:" + self.client_address[0])
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer ") or header.count(" ") != 1:
            raise GatewayError("unauthorized", "缺少设备令牌。", 401)
        device = self.core.authenticate(header[7:])
        self.core.check_rate("device:" + device)
        return device

    def _json(self):
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise GatewayError("invalid_content_type", "请求必须使用 JSON。", 415)
        length = self.headers.get("Content-Length")
        try:
            size = int(length) if length is not None else -1
        except ValueError as error:
            raise GatewayError("invalid_body", "请求长度无效。") from error
        if size < 0:
            raise GatewayError("length_required", "请求必须提供长度。", 411)
        if size > MAX_BODY_BYTES:
            raise GatewayError("body_too_large", "请求内容过大。", 413)
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise GatewayError("invalid_json", "请求 JSON 含重复字段。")
                value[key] = item
            return value
        try:
            value = json.loads(self.rfile.read(size).decode("utf-8"), object_pairs_hook=unique)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise GatewayError("invalid_json", "请求 JSON 无效。") from error
        return value

    def do_OPTIONS(self):
        self._send(405, {"error": {"code": "method_not_allowed", "message": "不支持此方法。"}})

    def do_GET(self):
        try:
            self._origin()
            device = self._device()
            parsed = urlsplit(self.path)
            conversation = parsed.path.startswith("/v1/tasks/") and parsed.path.endswith("/conversation")
            if parsed.fragment or parsed.query and not conversation:
                raise GatewayError("not_found", "找不到接口。", 404)
            if conversation:
                query = parse_qs(parsed.query, keep_blank_values=True)
                if set(query) - {"cursor"} or "cursor" in query and (len(query["cursor"]) != 1 or len(query["cursor"][0]) > 180):
                    raise GatewayError("invalid_cursor", "原文页码无效。")
                task_id = parsed.path[len("/v1/tasks/"):-len("/conversation")]
                self._send(200, self.core.read_conversation(device, task_id, query.get("cursor", [None])[0]))
            elif parsed.path == "/v1/status":
                self._send(200, self.core.status_snapshot(device))
            elif parsed.path == "/v1/tasks":
                self._send(200, self.core.list_task_snapshots(device))
            elif parsed.path.startswith("/v1/tasks/"):
                self._send(200, self.core.get_task_snapshot(device, parsed.path[len("/v1/tasks/"):]))
            elif parsed.path.startswith("/v1/commands/"):
                self._send(200, self.core.command_status(device, parsed.path[len("/v1/commands/"):]))
            else:
                raise GatewayError("not_found", "找不到接口。", 404)
        except Exception as error:
            self._error(error)

    def do_POST(self):
        try:
            self._origin()
            parsed = urlsplit(self.path)
            if parsed.query or parsed.fragment:
                raise GatewayError("not_found", "找不到接口。", 404)
            if parsed.path == "/v1/pair":
                self.core.check_rate("pair:" + self.client_address[0])
                value = self._json()
                if not isinstance(value, dict) or set(value) != {"secret", "device_name"}:
                    raise GatewayError("invalid_pairing", "配对字段无效。", 401)
                self._send(200, self.core.pair(value["secret"], value["device_name"]))
            else:
                device = self._device()
                if parsed.path != "/v1/commands":
                    raise GatewayError("not_found", "找不到接口。", 404)
                value = self._json()
                self._send(202, self.core.submit_command(device, value))
        except Exception as error:
            self._error(error)


class RemoteGateway:
    """Small HTTPS-only JSON server; all mutations are queued to the worker."""

    def __init__(self, core: GatewayCore, host, port, cert_file, key_file):
        self.core, self.host, self.port = core, host, port
        self.cert_file, self.key_file = Path(cert_file), Path(key_file)
        self._server = None
        self._thread = None

    def start(self):
        if self._server is not None:
            return
        server = _GatewayHTTPServer((self.host, self.port), _Handler, self.core)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            with protected_secrets.plaintext_file(self.key_file, "gateway-tls-key") as temporary_key:
                context.load_cert_chain(str(self.cert_file), str(temporary_key))
            server.socket = context.wrap_socket(
                server.socket, server_side=True, do_handshake_on_connect=False)
        except Exception:
            server.server_close()
            raise
        self._server = server
        self.port = server.server_address[1]
        self._thread = threading.Thread(target=server.serve_forever, name="context-relay-https", daemon=False)
        self._thread.start()

    def close(self):
        server, thread = self._server, self._thread
        self._server = self._thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread and thread is not threading.current_thread():
            thread.join(timeout=3)
