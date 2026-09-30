"""Bounded local archives for offline Context Relay inspection."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile


MAX_PAYLOAD_FILES = 4096
MAX_EXPANDED_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
_TASK = r"[0-9a-f]{32}"
_DIGEST = r"[0-9a-f]{64}"
_PAYLOAD = re.compile(rf"(?:tasks\.sqlite3|drafts/{_TASK}\.json|checkpoints/{_TASK}/{_DIGEST}\.json)\Z")
_MANIFEST_KEYS = {"format", "version", "created_at", "source_state_dir", "task_count",
                  "event_count", "file_count", "files"}
_FILE_KEYS = {"path", "size", "sha256"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(block)
            if size > MAX_EXPANDED_BYTES:
                raise ValueError("backup payload is too large")
            digest.update(block)
    return digest.hexdigest(), size


def _json_digest(value) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _load_json(path: Path):
    try:
        if path.stat().st_size > MAX_EXPANDED_BYTES:
            raise ValueError("backup JSON is too large")
        with path.open(encoding="utf-8-sig") as stream:
            return json.load(stream)
    except ValueError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("backup contains unreadable JSON") from error


def _is_link(path: Path) -> bool:
    details = path.lstat()
    return path.is_symlink() or bool(getattr(details, "st_file_attributes", 0)
                                     & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _overlap(left: Path, right: Path) -> bool:
    left, right = left.resolve(), right.resolve()
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _database_schema(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT type,name,tbl_name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()
    tables = {name for kind, name, _ in rows if kind == "table"}
    if tables not in ({"tasks", "events"}, {"tasks", "events", "recovery"}):
        raise ValueError("unsupported backup database tables")
    if any(kind != "table" for kind, _, _ in rows):
        raise ValueError("unsupported backup database objects")
    indexes = connection.execute(
        "SELECT name,tbl_name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_autoindex_%'"
    ).fetchall()
    if indexes:
        raise ValueError("unsupported backup database indexes")
    expected = {
        "tasks": [("id", "TEXT", 0, 1), ("data", "TEXT", 1, 0)],
        "events": [("seq", "INTEGER", 0, 1), ("task_id", "TEXT", 0, 0),
                   ("at", "TEXT", 0, 0), ("kind", "TEXT", 0, 0), ("data", "TEXT", 0, 0)],
        "recovery": [("data", "TEXT", 1, 0)],
    }
    for table in tables:
        actual = [(row[1], row[2].upper(), row[3], row[5])
                  for row in connection.execute(f"PRAGMA table_info({table})")]
        if actual != expected[table]:
            raise ValueError("unsupported backup database schema")
    if "recovery" in tables:
        recovery = connection.execute("SELECT data FROM recovery").fetchall()
        if len(recovery) != 1:
            raise ValueError("invalid inspection recovery marker")
        try:
            marker = json.loads(recovery[0][0])
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("invalid inspection recovery marker") from error
        if (not isinstance(marker, dict)
                or set(marker) != {"mode", "source_state_dir", "backup_created_at", "restored_at"}
                or marker.get("mode") != "inspection"
                or any(not isinstance(marker.get(key), str) or not marker[key]
                       for key in ("source_state_dir", "backup_created_at", "restored_at"))):
            raise ValueError("invalid inspection recovery marker")


def _validate_current_references(tasks: list[tuple[str, dict]], root: Path) -> None:
    for row_id, task in tasks:
        if not isinstance(row_id, str) or not isinstance(task, dict):
            raise ValueError("invalid task record")
        task_id = task.get("id", row_id)
        checkpoint_path, checkpoint_hash = task.get("checkpoint_path"), task.get("checkpoint_hash")
        current_checkpoint = checkpoint_hash is not None or task.get("checkpoint") is not None
        if not isinstance(task_id, str) or not re.fullmatch(_TASK, task_id):
            if current_checkpoint or task.get("draft"):
                raise ValueError("current packet has an invalid task id")
            continue
        if current_checkpoint:
            if (not isinstance(checkpoint_path, str) or not isinstance(checkpoint_hash, str)
                    or not re.fullmatch(_DIGEST, checkpoint_hash)):
                raise ValueError("current checkpoint reference is incomplete")
            relative = Path("checkpoints") / task_id / f"{checkpoint_hash}.json"
            expected = (root / relative).resolve()
            try:
                actual = Path(checkpoint_path).resolve(strict=True)
            except OSError as error:
                raise ValueError("current checkpoint file is missing") from error
            if actual != expected or _is_link(actual) or not actual.is_file():
                raise ValueError("current checkpoint is outside the managed state")
            if _json_digest(_load_json(actual)) != checkpoint_hash:
                raise ValueError("current checkpoint digest does not match")
        draft = task.get("draft")
        if draft is not None:
            if (not isinstance(draft, dict) or not isinstance(draft.get("path"), str)
                    or not isinstance(draft.get("hash"), str)
                    or not re.fullmatch(_DIGEST, draft["hash"])):
                raise ValueError("current draft reference is incomplete")
            expected = (root / "drafts" / f"{task_id}.json").resolve()
            try:
                actual = Path(draft["path"]).resolve(strict=True)
            except OSError as error:
                raise ValueError("current draft file is missing") from error
            if actual != expected or _is_link(actual) or not actual.is_file():
                raise ValueError("current draft is outside the managed state")
            if _json_digest(_load_json(actual)) != draft["hash"]:
                raise ValueError("current draft digest does not match")


def _validate_archived_references(tasks: list[tuple[str, dict]], root: Path) -> None:
    for row_id, task in tasks:
        if not isinstance(task, dict):
            raise ValueError("invalid task record")
        task_id = task.get("id", row_id)
        checkpoint_path, checkpoint_hash = task.get("checkpoint_path"), task.get("checkpoint_hash")
        if checkpoint_hash is not None or task.get("checkpoint") is not None:
            if (not isinstance(task_id, str) or not re.fullmatch(_TASK, task_id)
                    or not isinstance(checkpoint_path, str) or not isinstance(checkpoint_hash, str)
                    or not re.fullmatch(_DIGEST, checkpoint_hash)):
                raise ValueError("current checkpoint reference is incomplete")
            packet = root / "checkpoints" / task_id / f"{checkpoint_hash}.json"
            if not packet.is_file() or _json_digest(_load_json(packet)) != checkpoint_hash:
                raise ValueError("archive is missing the current checkpoint")
        draft = task.get("draft")
        if draft is not None:
            if (not isinstance(task_id, str) or not re.fullmatch(_TASK, task_id)
                    or not isinstance(draft, dict) or not isinstance(draft.get("path"), str)
                    or not isinstance(draft.get("hash"), str)
                    or not re.fullmatch(_DIGEST, draft["hash"])):
                raise ValueError("current draft reference is incomplete")
            packet = root / "drafts" / f"{task_id}.json"
            if not packet.is_file() or _json_digest(_load_json(packet)) != draft["hash"]:
                raise ValueError("archive is missing the current draft")


def _validate_database(path: Path, reference_root: Path | None = None) -> dict:
    try:
        if path.stat().st_size > MAX_EXPANDED_BYTES:
            raise ValueError("backup database is too large")
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError("backup database integrity check failed")
            _database_schema(connection)
            task_rows = connection.execute("SELECT id,data FROM tasks").fetchall()
            event_count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    except ValueError:
        raise
    except sqlite3.Error as error:
        raise ValueError("invalid backup database") from error
    tasks = []
    projects = []
    for row_id, raw in task_rows:
        try:
            task = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("invalid task JSON in backup database") from error
        if not isinstance(task, dict):
            raise ValueError("invalid task JSON in backup database")
        tasks.append((row_id, task))
        cwd = task.get("cwd")
        if isinstance(cwd, str) and Path(cwd).is_absolute():
            projects.append(Path(cwd))
    if reference_root is not None:
        _validate_current_references(tasks, reference_root)
    else:
        _validate_archived_references(tasks, path.parent)
    return {"task_count": len(tasks), "event_count": event_count, "projects": projects}


def _payload_files(state_root: Path, database: Path) -> dict[str, Path]:
    files = {"tasks.sqlite3": database}
    for top, pattern in (("drafts", re.compile(rf"{_TASK}\.json\Z")),
                         ("checkpoints", re.compile(rf"{_DIGEST}\.json\Z"))):
        directory = state_root / top
        if not directory.exists():
            continue
        if _is_link(directory) or not directory.is_dir():
            raise ValueError(f"managed {top} path is not a regular directory")
        if top == "drafts":
            candidates = directory.iterdir()
            for path in candidates:
                if not pattern.fullmatch(path.name):
                    continue
                if _is_link(path) or not path.is_file():
                    raise ValueError("managed draft is not a regular file")
                files[f"drafts/{path.name}"] = path
        else:
            for task_dir in directory.iterdir():
                if not re.fullmatch(_TASK, task_dir.name):
                    continue
                if _is_link(task_dir) or not task_dir.is_dir():
                    raise ValueError("managed checkpoint path is not a regular directory")
                for path in task_dir.iterdir():
                    if not pattern.fullmatch(path.name):
                        continue
                    if _is_link(path) or not path.is_file():
                        raise ValueError("managed checkpoint is not a regular file")
                    files[f"checkpoints/{task_dir.name}/{path.name}"] = path
    return files


def _manifest_report(path: Path, manifest: dict, mode: str | None = None) -> dict:
    report = {"path": str(path.resolve()), "created_at": manifest["created_at"],
              "source_state_dir": manifest["source_state_dir"],
              "task_count": manifest["task_count"], "event_count": manifest["event_count"],
              "file_count": manifest["file_count"]}
    if mode:
        report["mode"] = mode
    return report


def create_backup(db: sqlite3.Connection, state_root, destination) -> dict:
    if os.name != "nt":
        raise ValueError("backup creation is supported only on Windows")
    root = Path(state_root).resolve(strict=True)
    target = Path(destination).resolve()
    if not root.is_dir() or _overlap(root, target):
        raise ValueError("backup destination overlaps the manager state")
    if target.exists():
        raise FileExistsError(str(target))
    if not target.parent.is_dir():
        raise ValueError("destination parent directory does not exist")
    snapshot = None
    temporary = None
    try:
        snapshot_fd, snapshot_name = tempfile.mkstemp(prefix=".context-relay-db-", suffix=".sqlite3",
                                                      dir=target.parent)
        os.close(snapshot_fd)
        snapshot = Path(snapshot_name)
        archive_fd, archive_name = tempfile.mkstemp(prefix=".context-relay-backup-", suffix=".zip",
                                                    dir=target.parent)
        os.close(archive_fd)
        temporary = Path(archive_name)
        try:
            with closing(sqlite3.connect(snapshot)) as copied:
                db.backup(copied)
        except sqlite3.Error as error:
            raise ValueError("could not snapshot backup database") from error
        payload = _payload_files(root, snapshot)
        database = _validate_database(snapshot, root)
        for project in database["projects"]:
            if _overlap(project, target):
                raise ValueError("backup destination overlaps a recorded project")
        if len(payload) > MAX_PAYLOAD_FILES:
            raise ValueError("backup contains too many payload files")
        entries, expanded = [], 0
        for name, path in sorted(payload.items()):
            digest, size = _sha256(path)
            expanded += size
            if expanded > MAX_EXPANDED_BYTES:
                raise ValueError("backup payload is too large")
            entries.append({"path": name, "size": size, "sha256": digest})
        manifest = {"format": "context-relay-backup", "version": 1, "created_at": _now(),
                    "source_state_dir": str(root), "task_count": database["task_count"],
                    "event_count": database["event_count"], "file_count": len(entries), "files": entries}
        manifest_bytes = (json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")) + "\n").encode("utf-8")
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise ValueError("backup manifest is too large")
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("manifest.json", manifest_bytes)
            for name, path in sorted(payload.items()):
                bundle.write(path, name)
        with tempfile.TemporaryDirectory(prefix=".context-relay-verify-", dir=target.parent) as folder:
            checked, _ = _read_archive(temporary, Path(folder))
        if checked != manifest:
            raise ValueError("created backup did not verify")
        os.rename(temporary, target)
        return _manifest_report(target, manifest)
    except (zipfile.BadZipFile, OSError, sqlite3.Error) as error:
        raise ValueError("could not create backup archive") from error
    finally:
        if snapshot is not None:
            snapshot.unlink(missing_ok=True)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _validate_manifest(value) -> dict:
    if not isinstance(value, dict) or set(value) != _MANIFEST_KEYS:
        raise ValueError("unsupported backup manifest")
    if (value.get("format") != "context-relay-backup"
            or type(value.get("version")) is not int or value["version"] != 1):
        raise ValueError("unsupported backup manifest version")
    if (not isinstance(value.get("created_at"), str) or not value["created_at"]
            or not isinstance(value.get("source_state_dir"), str)
            or not Path(value["source_state_dir"]).is_absolute()):
        raise ValueError("invalid backup manifest metadata")
    for key in ("task_count", "event_count", "file_count"):
        if not isinstance(value.get(key), int) or isinstance(value[key], bool) or value[key] < 0:
            raise ValueError("invalid backup manifest counts")
    files = value.get("files")
    if not isinstance(files, list) or value["file_count"] != len(files) or len(files) > MAX_PAYLOAD_FILES:
        raise ValueError("invalid backup manifest files")
    names = []
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != _FILE_KEYS:
            raise ValueError("invalid backup manifest file entry")
        name, size, digest = entry["path"], entry["size"], entry["sha256"]
        if (not isinstance(name, str) or not _PAYLOAD.fullmatch(name)
                or PurePosixPath(name).as_posix() != name):
            raise ValueError("invalid backup payload path")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("invalid backup payload size")
        if not isinstance(digest, str) or not re.fullmatch(_DIGEST, digest):
            raise ValueError("invalid backup payload digest")
        names.append(name)
    if len(names) != len(set(names)) or len(names) != len({name.casefold() for name in names}):
        raise ValueError("duplicate backup manifest path")
    if names.count("tasks.sqlite3") != 1 or sum(entry["size"] for entry in files) > MAX_EXPANDED_BYTES:
        raise ValueError("invalid or oversized backup payload")
    return value


def _regular_member(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    kind = stat.S_IFMT(mode)
    return not info.is_dir() and not (info.flag_bits & 1) and kind in (0, stat.S_IFREG)


def _read_archive(archive: Path, destination: Path) -> tuple[dict, dict]:
    try:
        with zipfile.ZipFile(archive) as bundle:
            infos = bundle.infolist()
            names = [info.filename for info in infos]
            if (len(infos) > MAX_PAYLOAD_FILES + 1 or len(names) != len(set(names))
                    or len(names) != len({name.casefold() for name in names})):
                raise ValueError("duplicate or excessive backup members")
            if any(not _regular_member(info) for info in infos):
                raise ValueError("backup contains a non-regular member")
            manifests = [info for info in infos if info.filename == "manifest.json"]
            if len(manifests) != 1 or manifests[0].file_size > MAX_MANIFEST_BYTES:
                raise ValueError("backup manifest is missing or too large")
            try:
                manifest = _validate_manifest(json.loads(bundle.read(manifests[0]).decode("utf-8")))
            except (UnicodeError, json.JSONDecodeError) as error:
                raise ValueError("invalid backup manifest JSON") from error
            expected = {"manifest.json", *(entry["path"] for entry in manifest["files"])}
            if set(names) != expected:
                raise ValueError("backup membership does not match its manifest")
            by_name = {info.filename: info for info in infos}
            destination.mkdir(parents=True, exist_ok=True)
            for entry in manifest["files"]:
                info = by_name[entry["path"]]
                if info.file_size != entry["size"]:
                    raise ValueError("backup payload size does not match manifest")
                path = destination.joinpath(*PurePosixPath(entry["path"]).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                digest, size = hashlib.sha256(), 0
                with bundle.open(info) as source, path.open("xb") as output:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        size += len(block)
                        if size > entry["size"] or size > MAX_EXPANDED_BYTES:
                            raise ValueError("backup payload expanded beyond its declared size")
                        digest.update(block)
                        output.write(block)
                if size != entry["size"] or digest.hexdigest() != entry["sha256"]:
                    raise ValueError("backup payload digest does not match manifest")
    except ValueError:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError) as error:
        raise ValueError("invalid backup archive") from error
    database = _validate_database(destination / "tasks.sqlite3")
    if database["task_count"] != manifest["task_count"] or database["event_count"] != manifest["event_count"]:
        raise ValueError("backup database counts do not match manifest")
    return manifest, database


def inspect_backup(archive) -> dict:
    path = Path(archive).resolve(strict=True)
    if not path.is_file():
        raise ValueError("backup archive is not a file")
    with tempfile.TemporaryDirectory(prefix="context-relay-inspect-") as folder:
        manifest, _ = _read_archive(path, Path(folder))
    return _manifest_report(path, manifest)


def restore_backup(archive, destination) -> dict:
    if os.name != "nt":
        raise ValueError("inspection restore is supported only on Windows")
    source = Path(archive).resolve(strict=True)
    target = Path(destination).resolve()
    if target.exists():
        raise FileExistsError(str(target))
    if not target.parent.is_dir():
        raise ValueError("restore parent directory does not exist")
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.restore-", dir=target.parent))
    published = False
    try:
        manifest, database = _read_archive(source, stage)
        source_state = Path(manifest["source_state_dir"])
        if _overlap(target, source_state) or any(_overlap(target, project) for project in database["projects"]):
            raise ValueError("restore destination overlaps source state or a recorded project")
        marker = {"mode": "inspection", "source_state_dir": manifest["source_state_dir"],
                  "backup_created_at": manifest["created_at"], "restored_at": _now()}
        try:
            with closing(sqlite3.connect(stage / "tasks.sqlite3")) as connection:
                connection.execute("CREATE TABLE IF NOT EXISTS recovery (data TEXT NOT NULL)")
                connection.execute("DELETE FROM recovery")
                connection.execute("INSERT INTO recovery VALUES (?)",
                                   (json.dumps(marker, ensure_ascii=False, separators=(",", ":")),))
                connection.commit()
            _validate_database(stage / "tasks.sqlite3")
        except sqlite3.Error as error:
            raise ValueError("could not mark restored database for inspection") from error
        os.rename(stage, target)
        published = True
        return _manifest_report(target, manifest, "inspection")
    except ValueError:
        raise
    except (OSError, sqlite3.Error, zipfile.BadZipFile) as error:
        raise ValueError("could not restore backup") from error
    finally:
        if not published:
            shutil.rmtree(stage, ignore_errors=True)
