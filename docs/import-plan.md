# Selective import of existing Codex chats

The user selected existing Codex desktop chats as the import source. Add an explicit picker using the supported local App Server list/read interface. Preserve the original chat and create a separate queued managed task from reviewed historical evidence. Never resume, fork, interrupt, archive or change permissions on the source chat.

## Contract

- Fetch one metadata page at a time, with search and archived selection. Read conversation content only for the selected chat. Clearly label the native source; do not infer desktop identity from an unrecognized source or treat subagents as independent main chats.
- Preview bounded user/assistant excerpts, exact source identifiers, directory, time and content fingerprint. Display omissions, clipped text, tool/attachment gaps and uncertain operation status. Historical text is evidence, not a new instruction or permission grant.
- Require a current task goal, explicit permission ceiling (default read-only), and acknowledgment that source-side operations have stopped. Import itself is local and never starts model work. User acknowledgment and an idle response do not create an operating-system-level ownership lock.
- Retain the selected preview on the controller, re-read and compare it on import, and write the source snapshot and new task atomically. Do not place the source ID in the managed thread/receiver/history fields. Budget and approvals start from the current user's choices, not copied history.
- Deduplicate by source chat. Explicitly updating an existing import is allowed only while it has never started. A first start rechecks the source fingerprint and terminal state; a changed source requires preview and update again. No unknown native operation is replayed.
- Preserve the separate source-evidence field in prompts, preparatory/final handoff packets, exports and existing backups. Inspection-only restored databases cannot connect to native chats or import.

## Implementation and acceptance

- [x] Verify the installed protocol and a read-only local list/read probe. Record capability limitations without exposing private chat content.
- [x] Implement bounded source normalization and controller list/preview/import/initial-start checks. Add regressions for identity, permissions, stale preview, duplicate import, provenance and backup fidelity.
- [x] Add the picker to the existing worker/Tk flow. Cover search, paging, selection changes, late results, errors, import/update and inspection mode with fake clients.
- [x] Run existing regressions and a real Tk walkthrough with synthetic tasks. Keep native model execution separate from read-only discovery acceptance.
- [x] Update usage and scope documentation; independently review the import changes.

Acceptance: 177 manager/launcher/shortcut tests, 71 standalone runtime tests and 6 installer tests passed (254 total). A real Tk/Manager walkthrough used a synthetic source and never constructed a native client. The native read-only check read one metadata page and the current chat only, correctly refusing its unfinished work; no imported desktop chat was continued live. See [the guide](windows-manager.md#验证状态) for evidence scope and limitations. Publish to the existing `feat/windows-manager` branch and verify its remote SHA separately.
