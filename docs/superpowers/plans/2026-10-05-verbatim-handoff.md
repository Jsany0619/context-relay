# Verbatim conversations and managed handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Track the steps below.

**Goal:** Default future tasks to verbatim chat and support verified managed handoff without automatic generated work messages.

**Architecture:** Reuse `connection_mode="direct"`, existing App Server requests, durable intents and the current handoff controller. Keep standalone ownership read-only and separate; use the bundled skill's managed-adapter instructions. Extend existing Tk/Java UI and tests without a new framework.

**Tech Stack:** Python stdlib/Tk/unittest, Java Android and existing host checks.

## Global Constraints

- Existing task records, permissions, drafts, pairing and unknown-result receipts are preserved.
- No real task/model commands, new desktop chat, service, framework or permission expansion during development.
- Snapshot success requires complete coverage under existing exclusions; no fixed file-count or total-byte cap, no partial successful snapshot.
- Product changes remain in the current feature checkout; do not discard its pre-existing authorized edits.

## Task 1: Manager transport, ownership and snapshots

Files: `relay/manager.py`, `relay/imports.py`, existing manager/import/recovery tests.

- [x] Add focused FakeClient checks for first-send creation, exact raw input, handoff waiting, fingerprint/ownership conflicts and complete/cancelled large snapshots; run those tests to establish failures.
- [x] Add explicit direct creation support consumed by the default UI. Preserve legacy API behavior where callers intentionally request managed tasks. First direct send branches only on `thread_id`: create once if absent, otherwise validate and resume; then `_turn(task, message)` unchanged.
- [x] Separate supported external import origins from ordinary managed-owner idle checks. Reuse standalone `guard(root, actor)` read-only; reject frozen/conflicting/unknown ownership before sending and handoff.
- [x] Allow direct handoff. Bind source turn fingerprint to the packet; revalidate before owner transfer. Direct transfer leaves `state="paused"`, without calling `_turn` until explicit user input.
- [x] Remove snapshot count/byte rejection and add a cancellation check to streaming hash iteration. Preserve reparse/read-error/Git checks. All manager snapshot callers use the same cancellation source.
- [x] Run `D:/python/python.exe -B -m unittest tests.test_manager tests.test_import_manager tests.test_assessment_manager tests.test_recovery -v` (use actual recovery module name discovered in the repository).

## Task 2: Windows defaults

Files: `relay/ui.py`, `tests/test_import_ui.py`, relevant Tk UI tests.

- [x] New task and normal import call direct paths; goal text is optional metadata, never an implicit first send. Existing legacy tasks retain their controls.
- [x] Enable direct handoff/auto-handoff controls subject to existing idle, budget and telemetry checks.
- [x] In `_request_chat`, skip native history loading until a thread exists. Preserve the composer draft.
- [x] Run focused existing UI checks plus one new empty-direct-task/default-import check.

## Task 3: Mobile state and controls

Files: `relay/gateway.py`, Android `MainActivity.java`, `Protocol.java` and corresponding gateway/host tests only as needed.

- [x] Expose bounded `conversation_available` metadata; use it to avoid native-history calls for empty new tasks while keeping their composer usable.
- [x] Preserve Send confirmation and command/grant allowlists. Mark managed handoff waiting without an automatic retry.
- [x] Run affected gateway tests and Android host checks; build the signed candidate with existing cached tools.

## Task 4: Skill adapter and review

Files: `skills/context-handoff/SKILL.md`, `skills/context-handoff/references/managed-adapter.md`, current manager/mobile docs.

- [x] Document managed roles, six-category READY, frozen source/file evidence and standalone conflicts; do not alter standalone claim/transfer or hook script trust.
- [x] Ensure controller prompts reference the actual bundled adapter, not only a feature label.
- [x] Review each task diff, then review integrated behavior; fix substantive findings and run affected regressions once.
- [x] Record exact tests/build hashes and remaining native/device acceptance gaps. Preserve version-specific install receipts; query installed state before any authorized cover-install, never replay an unknown result.

## Verification record

Implementation and focused checks are complete. See [version-bound validation](../../mobile-validation.md#0214-原话对话与受管交接候选) for module counts, the interrupted full discovery run, installed APK hash, and owner-confirmed phone navigation. No real model handoff was performed. The owner also confirmed recent-app-card text is hidden. Public release remains a separate delivery step.
