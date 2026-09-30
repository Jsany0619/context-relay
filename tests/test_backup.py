"""Bounded, offline backup and inspection-restore checks."""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest import mock
import warnings
import zipfile

from relay.backup import create_backup, inspect_backup, restore_backup


TASK_ID = "1" * 32
CHECKPOINT = {"checkpoint": "historical"}
DRAFT = {"kind": "preparatory", "ready": False}


def canonical_digest(value) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


DIGEST = canonical_digest(CHECKPOINT)
DRAFT_DIGEST = canonical_digest(DRAFT)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.project = self.root / "project"
        self.out = self.root / "out"
        self.state.mkdir()
        self.project.mkdir()
        self.out.mkdir()
        self.db = sqlite3.connect(self.state / "tasks.sqlite3")
        self.addCleanup(self.db.close)
        self.db.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self.db.execute("CREATE TABLE events (seq INTEGER PRIMARY KEY, task_id TEXT, at TEXT, kind TEXT, data TEXT)")
        checkpoint = self.state / "checkpoints" / TASK_ID / f"{DIGEST}.json"
        draft = self.state / "drafts" / f"{TASK_ID}.json"
        self.raw_task = {
            "id": TASK_ID, "cwd": str(self.project.resolve()), "state": "needs_reconcile",
            "intent": {"kind": "turn", "request_id": 44}, "generation": 3,
            "history": [{"thread_id": "historical-owner", "unknown": True}],
            "checkpoint_path": str(checkpoint), "checkpoint_hash": DIGEST,
            "draft": {"path": str(draft), "hash": DRAFT_DIGEST},
        }
        self.raw_json = json.dumps(self.raw_task, ensure_ascii=False, separators=(",", ":"))
        self.db.execute("INSERT INTO tasks VALUES (?,?)", (TASK_ID, self.raw_json))
        self.db.executemany("INSERT INTO events VALUES (?,?,?,?,?)", [
            (1, TASK_ID, "2026-09-30T00:00:00Z", "turn_requested", '{"unknown":true}'),
            (2, TASK_ID, "2026-09-30T00:00:01Z", "requires_reconciliation", "{}"),
        ])
        self.db.commit()
        checkpoint.parent.mkdir(parents=True)
        checkpoint.write_text(json.dumps(CHECKPOINT) + "\n", encoding="utf-8")
        draft.parent.mkdir()
        draft.write_text(json.dumps(DRAFT) + "\n", encoding="utf-8")
        (self.state / "manager.lock").write_text("not archived", encoding="utf-8")
        (self.state / "arbitrary.txt").write_text("not archived", encoding="utf-8")

    def archive(self, name="backup.zip") -> Path:
        path = self.out / name
        create_backup(self.db, self.state, path)
        return path

    def rewrite(self, source: Path, destination: Path, mutate) -> None:
        with zipfile.ZipFile(source) as old, zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as new:
            for info in old.infolist():
                data = old.read(info)
                replacement = mutate(info, data)
                if replacement is None:
                    continue
                new_info, new_data = replacement
                new.writestr(new_info, new_data)

    def test_round_trip_preserves_raw_ledger_and_only_allowlisted_files(self):
        archive = self.out / "round-trip.zip"
        created = create_backup(self.db, self.state, archive)
        self.assertEqual(created, {
            "path": str(archive.resolve()), "created_at": created["created_at"],
            "source_state_dir": str(self.state.resolve()), "task_count": 1,
            "event_count": 2, "file_count": 3,
        })
        with zipfile.ZipFile(archive) as bundle:
            self.assertEqual(set(bundle.namelist()), {
                "manifest.json", "tasks.sqlite3", f"checkpoints/{TASK_ID}/{DIGEST}.json",
                f"drafts/{TASK_ID}.json",
            })
        inspected = inspect_backup(archive)
        self.assertEqual(inspected["path"], str(archive.resolve()))
        self.assertEqual({key: inspected[key] for key in created if key != "path"},
                         {key: created[key] for key in created if key != "path"})

        destination = self.root / "restored"
        restored = restore_backup(archive, destination)
        self.assertEqual(restored["mode"], "inspection")
        self.assertEqual(restored["path"], str(destination.resolve()))
        self.assertEqual(restored["task_count"], 1)
        self.assertTrue((destination / "tasks.sqlite3").is_file())
        self.assertTrue((destination / "checkpoints" / TASK_ID / f"{DIGEST}.json").is_file())
        self.assertTrue((destination / "drafts" / f"{TASK_ID}.json").is_file())
        self.assertFalse((destination / "manager.lock").exists())
        self.assertFalse((destination / "arbitrary.txt").exists())
        restored_db = sqlite3.connect(destination / "tasks.sqlite3")
        self.addCleanup(restored_db.close)
        self.assertEqual(restored_db.execute("SELECT data FROM tasks").fetchone()[0], self.raw_json)
        self.assertEqual(restored_db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 2)
        rows = restored_db.execute("SELECT data FROM recovery").fetchall()
        self.assertEqual(len(rows), 1)
        marker = json.loads(rows[0][0])
        self.assertEqual(set(marker), {"mode", "source_state_dir", "backup_created_at", "restored_at"})
        self.assertEqual(marker["mode"], "inspection")
        self.assertEqual(marker["source_state_dir"], str(self.state.resolve()))
        self.assertEqual(marker["backup_created_at"], created["created_at"])

    def test_corruption_and_unsupported_manifest_leave_no_restore_directory(self):
        archive = self.archive()
        corrupt = self.out / "corrupt.zip"

        def corrupt_draft(info, data):
            if info.filename.startswith("drafts/"):
                data += b"changed"
            return info, data

        self.rewrite(archive, corrupt, corrupt_draft)
        with self.assertRaises(ValueError):
            inspect_backup(corrupt)
        destination = self.root / "failed-restore"
        with self.assertRaises(ValueError):
            restore_backup(corrupt, destination)
        self.assertFalse(destination.exists())

        unsupported = self.out / "unsupported.zip"

        def change_version(info, data):
            if info.filename == "manifest.json":
                value = json.loads(data)
                value["version"] = 2
                data = json.dumps(value).encode()
            return info, data

        self.rewrite(archive, unsupported, change_version)
        with self.assertRaises(ValueError):
            inspect_backup(unsupported)

    def test_traversal_duplicates_case_collisions_and_links_are_rejected(self):
        archive = self.archive()
        cases = {}
        traversal = self.out / "traversal.zip"
        self.rewrite(archive, traversal, lambda info, data: (info, data))
        with zipfile.ZipFile(traversal, "a") as bundle:
            bundle.writestr("../escape", b"bad")
        cases["traversal"] = traversal

        duplicate = self.out / "duplicate.zip"
        self.rewrite(archive, duplicate, lambda info, data: (info, data))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(duplicate, "a") as bundle:
                bundle.writestr("tasks.sqlite3", b"duplicate")
        cases["duplicate"] = duplicate

        collision = self.out / "case-collision.zip"
        self.rewrite(archive, collision, lambda info, data: (info, data))
        with zipfile.ZipFile(collision, "a") as bundle:
            bundle.writestr("MANIFEST.JSON", b"collision")
        cases["case collision"] = collision

        link = self.out / "link.zip"

        def make_link(info, data):
            if info.filename.startswith("drafts/"):
                copied = zipfile.ZipInfo(info.filename)
                copied.create_system = 3
                copied.external_attr = (stat.S_IFLNK | 0o777) << 16
                return copied, b"target"
            return info, data

        self.rewrite(archive, link, make_link)
        cases["link"] = link
        for label, path in cases.items():
            with self.subTest(label=label), self.assertRaises(ValueError):
                inspect_backup(path)

    def test_database_schema_and_integrity_are_strict(self):
        self.db.execute("CREATE TABLE surprise (value TEXT)")
        self.db.commit()
        with self.assertRaises(ValueError):
            create_backup(self.db, self.state, self.out / "unsupported-db.zip")
        self.assertFalse((self.out / "unsupported-db.zip").exists())

    def test_historical_checkpoint_path_without_current_hash_is_preserved(self):
        historical = dict(self.raw_task)
        historical.pop("checkpoint_hash")
        historical.pop("draft")
        raw = json.dumps(historical, ensure_ascii=False, separators=(",", ":"))
        self.db.execute("UPDATE tasks SET data=? WHERE id=?", (raw, TASK_ID))
        self.db.commit()
        archive = self.archive("historical-path.zip")
        self.assertEqual(inspect_backup(archive)["task_count"], 1)
        with zipfile.ZipFile(archive) as bundle:
            database = self.root / "historical.sqlite3"
            database.write_bytes(bundle.read("tasks.sqlite3"))
        with closing(sqlite3.connect(database)) as copied:
            self.assertEqual(copied.execute("SELECT data FROM tasks").fetchone()[0], raw)

    def test_managed_directory_link_is_rejected_before_current_packet_read(self):
        checkpoint_dir = self.state / "checkpoints"
        real_is_link = __import__("relay.backup", fromlist=["_is_link"])._is_link
        with (mock.patch("relay.backup._is_link",
                         side_effect=lambda path: path == checkpoint_dir or real_is_link(path)),
              mock.patch("relay.backup._load_json") as load_json):
            with self.assertRaisesRegex(ValueError, "regular directory"):
                create_backup(self.db, self.state, self.out / "linked-state.zip")
        load_json.assert_not_called()

    def test_existing_outputs_and_source_or_project_overlap_are_never_replaced(self):
        occupied = self.out / "occupied.zip"
        occupied.write_text("keep", encoding="utf-8")
        with self.assertRaises((FileExistsError, ValueError)):
            create_backup(self.db, self.state, occupied)
        self.assertEqual(occupied.read_text(encoding="utf-8"), "keep")
        for destination in (self.state / "inside.zip", self.project / "inside.zip"):
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                create_backup(self.db, self.state, destination)
            self.assertFalse(destination.exists())

        archive = self.archive("valid.zip")
        existing_dir = self.root / "existing"
        existing_dir.mkdir()
        marker = existing_dir / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        with self.assertRaises((FileExistsError, ValueError)):
            restore_backup(archive, existing_dir)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        for destination in (self.state / "restored", self.project / "restored"):
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                restore_backup(archive, destination)
            self.assertFalse(destination.exists())

    def test_backup_publish_race_does_not_replace_other_output(self):
        destination = self.out / "raced.zip"
        real_rename = os.rename

        def race(source, target):
            Path(target).write_text("other output", encoding="utf-8")
            return real_rename(source, target)

        with mock.patch("relay.backup.os.rename", side_effect=race):
            with self.assertRaises(ValueError):
                create_backup(self.db, self.state, destination)
        self.assertEqual(destination.read_text(encoding="utf-8"), "other output")

    def test_limits_are_enforced_before_restore_publication(self):
        archive = self.archive()
        with mock.patch("relay.backup.MAX_PAYLOAD_FILES", 1):
            with self.assertRaises(ValueError):
                inspect_backup(archive)
        with mock.patch("relay.backup.MAX_EXPANDED_BYTES", 10):
            destination = self.root / "too-large"
            with self.assertRaises(ValueError):
                restore_backup(archive, destination)
            self.assertFalse(destination.exists())

    def test_manifest_is_bounded_and_must_exactly_describe_archive(self):
        archive = self.archive()
        extra = self.out / "extra.zip"
        self.rewrite(archive, extra, lambda info, data: (info, data))
        with zipfile.ZipFile(extra, "a") as bundle:
            bundle.writestr("drafts/" + "3" * 32 + ".json", b"{}")
        with self.assertRaises(ValueError):
            inspect_backup(extra)

        huge = self.out / "huge-manifest.zip"
        with zipfile.ZipFile(huge, "w") as bundle:
            bundle.writestr("manifest.json", b" " * (1024 * 1024 + 1))
        with self.assertRaises(ValueError):
            inspect_backup(huge)


if __name__ == "__main__":
    unittest.main()
