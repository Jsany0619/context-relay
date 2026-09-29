# Windows local manager implementation plan

Approved scope: a single-user Windows manager for Codex, retaining the standalone skill. This implements the first stage discussed with the user, not phone access or a multi-provider platform.

## Design

Python 3.10+ standard library: Tk desktop interface, SQLite durable task/event records, and a child `codex app-server` connected over stdio. No listening port, administrator service, internal Codex database edits, automatic hook trust, or desktop UI automation. Managed conversations are App Server conversations; official desktop sidebar integration is not promised.

A stable task owns successive thread IDs. Persist intent before create/start requests. Unknown outcomes are held for reconciliation, never retried automatically. Only one manager process may open the same state directory. A task retains its directory, original user messages, mode, budget, owner generation, checkpoint hash, and actual turn outcomes. A completed turn is not automatically a completed project.

The manager's SQLite ledger and checkpoints under `%LOCALAPPDATA%\ContextRelay` are independent of the standalone skill's `CODEX_HOME/context-handoffs` files. They do not share ownership or automatically migrate state. Opt-in automatic handoff runs only for unfinished idle tasks with fresh context pressure of at least 80% and at least two compactions. The standalone skill's 70% preparatory snapshot rule is not implemented in this manager.

Handoff: at an idle checkpoint request a structured summary in read-only mode, hash the user requirements and referenced evidence files, start a fresh read-only receiver, require a version-bound structured READY with concrete checks, recheck the original thread and file hashes, transfer the managed owner transactionally, then continue with the original permission ceiling. Read-only summary and READY phases are expected even for workspace-write tasks; restoration requires the original instructions and permission record plus the effective native session configuration, not a permission claim in the summary. Old thread events cannot start work. Unknown operations, missing evidence, failed checks, and ambiguous creation remain blocked. The controller cannot fence arbitrary shells, MCP servers, or other clients; this is explicitly not OS-level write isolation.

First version uses a conservative permission ceiling (read-only or workspace-write, on-request human approvals). No automatic session-wide permissions, no inherited full-access mode. Read-only preparation denies approvals that would escape its sandbox. Native approvals are tied to the exact pending request and cleared when their turn ends. No credential export. App Server itself may save transcripts; manager filtering is not comprehensive DLP.

## Units and interfaces

- `relay/transport.py`: `CodexClient(command=None, cwd=None)`, `request(method, params=None, timeout=30) -> dict`, `respond(request_id, result)`, `drain_events() -> list[dict]`, `close()`. Handshake automatically; reader handles out-of-order responses; timeout is an unknown result, never an automatic resend. Exceptions `RpcError`, `RequestTimeout`.
- `relay/manager.py`: `Manager(state_dir=None, client=None)`, `create_task(title, cwd, goal, mode='read-only', auto_handoff=False, max_tokens=0, max_minutes=0) -> dict`, `start(task_id, message=None)`, `pause(task_id)`, `reconcile(task_id)`, `handoff(task_id)`, `finish(task_id)`, `answer(task_id, request_id, answer)`, `poll()`, `list_tasks()`, `get_task(task_id)`, `export_task(task_id, destination)`, `close()`. Public operations serialize; polling consumes transport notifications. Task dictionaries include id/title/cwd/goal/mode/state/thread_id/turn_id/generation/last_message/error/pending/usage/context_estimate/compactions.
- `relay/ui.py`: `main(state_dir=None)` uses Manager. Tk main thread only; one command worker also polls. Chinese task list, new task dialog, explicit start/continue, pause, reconcile, handoff, approvals/input, export. Closing explains interruption; no invisible unattended launch.
- `relay/__main__.py`: GUI default, `--state-dir`, diagnostic `--check`, opt-in live smoke script kept in tests.

## Implementation and verification

- [x] Transport simulation: fake line-delimited server with reordered responses, server requests, timeout, EOF, and close.
- [x] Native transport: initialize and exchange requests/events with Codex 0.153.4 during a controlled live task; record separately from fake-server results.
- [x] Controller simulation: durable restart, exclusive manager, approval binding, stale file/checkpoint, unknown create/start, stale predecessor, duplicate compaction events, budget boundary, original goal retention, read-only inheritance, and shutdown during delayed creation.
- [x] Native controller: one stable task, three independent App Server threads, two six-check READY verifications, two ownership transfers, and actual work after each handoff; controlled counter advanced 0 → 1 → 2 → 3. Four reported MCP entries were disabled with zero tools.
- [x] Native restart reconciliation: after closing the original manager, reopen the same state directory and verify the existing task read-only. Generation remained 2, state became paused, and counter remained 3; only `config/read` and `thread/read` were sent, with no `turn/start`. This does not prove execution resumes after a crash or actual interruption.
- [x] UI: FakeManager interaction tests and real Tk window inspection with synthetic task/approval states; blocking RPCs stay off the UI thread. Default screen-bounded controls and 96/144-DPI-equivalent Tk layouts checked locally.
- [x] Regression: original 71 runtime and 6 installer tests remain green; the manager does not install or modify global hooks.
- [x] Review: independent review of ownership, restart, approval, and shutdown paths completed; no remaining blocking findings.
- [x] Document start commands, local storage/privacy, Python/Tk requirements, simulated versus native evidence, and unsupported recovery paths. Local launch check passed with Python 3.13.2, Tk 8.6.15, and Codex 0.153.4.

2026-09-30 Windows validation: 54 manager tests (38 controller, 5 transport, 11 UI), 71 standalone runtime tests, and 6 installer tests passed, for 131 total. The first native attempt safely stopped at a negative READY. After clarifying the manager ledger and read-only verification protocol, a new independent run passed without bypassing READY. That counter example took about 9 minutes 26 seconds; it is not a general performance benchmark. Handoffs add model usage and time.

High-pressure automatic triggering and pause/interruption recovery remain simulated coverage only. Physical sleep, network loss, crashes, and other environments have not been tested live. These are explicit acceptance limits, not claims implied by the successful counter example.

Live tests create only disposable managed conversations and controlled local artifacts. They do not publish, commit project work, send messages, or test destructive external actions.

The opt-in `tests/live_smoke.py --run --root <new-directory-outside-repository>` procedure uses real Codex usage to increment a disposable counter from 0 to 3 across two handoffs. It is excluded from default unit discovery and stops for pending approvals/input or uncertain outcomes; a script's presence does not establish native acceptance. The usage guide provides the Windows command.
