# Local backup and inspection restore implementation plan

**Goal:** Preserve the manager ledger and handoff files in one verifiable local archive, then recover them for inspection without creating a second executor.

**Architecture:** Use Python's SQLite backup API under the manager lock and a versioned ZIP manifest with file sizes and SHA-256. Restore only into a new directory, record inspection mode inside the recovered SQLite database, and disable all native activity and task mutations in the backend and UI. An old backup cannot prove the latest execution owner; there is no unlock option in this version.

**Constraints:** Python 3.10+ stdlib only. No global hooks or Codex configuration changes. No copying project files, native transcripts or Codex credential files. No overwrite of existing archives or directories. No restore into or around a recorded project directory. Archives are local plaintext, not safe public support attachments. Hashes detect corruption, not authenticity. Use trusted backups only. Keep old state, intent, permissions and ownership as historical evidence; never adopt old READY or replay unknown operations. Final acceptance uses simulated clients and offline files. An early red-test isolation mistake reached a disposable read-only native session and teardown called close; this is recorded separately, never counted as native backup acceptance. The corrected integration suite forbids native client construction.

## Archive implementation

- [x] Add `relay/backup.py`: `create_backup(db, state_root, destination)`, `inspect_backup(archive)`, `restore_backup(archive, destination)`. Return JSON-serializable reports with paths, timestamp, file/task/event counts and inspection mode as applicable.
- [x] Store only `tasks.sqlite3`, `checkpoints/<task-id>/<digest>.json`, and `drafts/<task-id>.json`, plus `manifest.json`. Exclude locks, WAL/SHM files and arbitrary state-directory files. Reject links/reparse points in included paths. Cap archive members and expanded bytes; validate canonical allowlisted paths, duplicates, case collisions, member types, manifest version, exact membership, hashes, SQLite integrity and supported schema before publishing restored data. Avoid `extractall`.
- [x] Refuse existing destinations; create validated temporary files/directories, clean partial output on failure, and never remove or overwrite user content. Backup creation and restore are Windows-only and publish with non-replacing `os.rename`. Restored ledger gets a single inspection metadata row; do not rewrite historical checkpoint paths or edit historical task payloads. Publish the fully validated, closed staging directory so interruption before publication leaves no partial target. Other platforms reject creation/restoration before writing.
- [x] Test round trips including full event history/packets, corruption, unsupported version/schema, traversal/duplicate/link archives, occupied destinations and failed writes.

## Manager, CLI and UI integration

- [x] Add `Manager.backup_state(destination)` under the existing lock. Reject active/pending tasks and state/project destination overlap; preserve unresolved intents in the backup. Do not call Codex. Recognize restoration metadata before startup recovery and set SQLite query-only mode. Guard shared persistence and native connection paths; poll/close must never act on historical running tasks.
- [x] CLI: `--inspect-backup FILE` checks and reports without Tk/Codex; `--restore-backup FILE --state-dir NEW_DIRECTORY` restores records and prints the inspection launch command. Require an explicit fresh destination. Keep ordinary GUI and `--check` behavior.
- [x] UI: add a global backup action independent of task selection. Restore remains an explicit offline CLI action. Worker ready event exposes optional restoration metadata. Inspection windows have a persistent banner, disabled task creation/mutations/approvals, and keep search/history/export available. Closing them must not claim to interrupt historical tasks.
- [x] Test backend prevention, restart fidelity, CLI argument failures and no-native behavior; test UI backup dispatch, restoration banner/disabled controls, and existing DPI layout. Run an actual Manager/Tk disposable-state backup/restore exercise with zero native calls.

## Acceptance and publication

- [x] Run focused tests first, then the combined manager, standalone runtime and installer suites plus launcher check. Independently review the change and distinguish simulations from native execution evidence.
- [x] Update README, usage and architecture limits with exact acceptance results and prepare reviewed files for publication. Perform final privacy/diff checks before committing to the existing `feat/windows-manager` branch; verify remote SHA and clean worktree after pushing. No private backups or runtime reports enter Git.

Acceptance on 2026-09-30: 137 manager + 71 standalone runtime + 6 installer tests passed (214 total), with launcher checks. Independent review found no remaining blockers after correcting historical pointer handling, link validation order, SQLite URI escaping and no-overwrite publication. A real Manager/Tk disposable exercise preserved 121 events, source drafts and project contents; the restored inspection window could export exact historical records but not execute, with zero native requests/approval replies and both workers closed. Ordinary 95/96/144 DPI and inspection 96/144 DPI equivalent layouts passed. Earlier red-test isolation failure is recorded above and in the guide; no new native acceptance claim is made.
