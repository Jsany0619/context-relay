# Task briefs and stage review implementation plan

**Goal:** Reduce repeated goal entry and support exploratory development with editable task briefs and evidence-bound stage review.

**Architecture:** Reuse the existing Manager, durable task JSON, single UI worker and native App Server transport. Briefs and reviews run in a fresh read-only analysis conversation, with a distinct ID and intent; they never become the task executor. Keep machine test evidence, AI judgments and human adoption separate.

**Tech stack:** Python standard library, Tk, SQLite, existing Codex App Server calls.

## Authorized behavior and boundaries

- Import still saves locally without running a model. Optional current ideas may be empty; the queued task then requires a brief. First Start generates the brief, not project changes. Users can also request a new brief for an existing quiet task.
- A brief proposes the goal, preserved decisions, completed work, unknowns, up to three approaches, the next small action and acceptance criteria. Read source excerpts and current workspace evidence; omissions are explicit. User adoption can edit the proposal and is a new current instruction, not permission copied from history.
- Stage review is requested explicitly when there is a candidate result. A fresh read-only conversation checks requirements, correctness, usability and creative choices against the bound workspace and requirements. Prompts prohibit project tests/builds/scripts; sandbox and approval policy do not attest that no such command ran. Existing test reports are reported evidence, not a new successful test run.
- Bind each result to task revision, work turns, requirements, source fingerprint, native owner turn history and file hashes. Recheck before adopting a brief, accepting a review or requesting rework. Changed evidence makes the result stale. Pending reports block handoff; historical candidates cannot become new instructions. A generated judgment never records human acceptance or marks the whole project complete.
- Accepting a stage is local; rework explicitly starts a work turn with current permissions and review findings. A direction may remain exploratory. Important unknowns must be surfaced, rather than inventing a complete specification.
- Analysis costs model usage and counts toward task budgets. Preserve owner, pending operations, recovery fences, source provenance and backup inspection restrictions. Never replay unknown creates/turns. Interrupted/recovered analysis may be retained as history but is not automatically adopted.
- Existing manually specified tasks retain their behavior. Import updates after analysis but before actual work retain consumed usage/history rather than resetting budgets. No remote service, daemon, trusted hook edit, new provider or project-wide refactor.

## Implementation and checks

- [x] Add strict brief/review schemas, bounded output validation, source/file references and read-only prompts in `relay/assessment.py`; focused pure parser tests.
- [x] Extend Manager with analysis lifecycle, optional import ideas, brief adoption and version-bound review decisions; fake-client regressions for ownership, mutations, stale evidence and budget accounting.
- [x] Add one brief/review window through the existing UI worker. Preserve edited drafts, fixed task identity and inspection-only behavior; meaningful Tk checks.
- [x] Cover pause/restart/unknown receipts/late receipts and keep analysis telemetry separate from executor pressure. Preserve results in handoffs, exports and backups.
- [x] Run full regression, real Tk/Manager synthetic walkthrough and launcher check: 217 manager + 71 runtime + 6 installer tests passed. Native probes used independent disposable projects; a timeout and invalid evidence prevented full native acceptance.
- [x] Update usage and acceptance boundaries and review changes. Publish only to the existing feature branch, checking remote SHA at publication.

## Remaining acceptance

The complete native brief-adoption-work-review-rework workflow has not passed. The latest strict reference prompt/schema clarification is covered by local tests, not a repeated native inference run. Actual imported desktop-chat continuation also retains its earlier live-validation gap. See [current evidence and limitations](windows-manager.md#验证状态); earlier native handoff success does not validate these new paths.
