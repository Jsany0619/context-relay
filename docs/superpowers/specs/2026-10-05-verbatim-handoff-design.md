# Verbatim conversations and managed handoff

Approved on 2026-10-05, including removal of fixed snapshot file-count and total-byte limits.

- Future desktop task creation and import default to direct, verbatim conversations. Creation itself sends no model request; first explicit nonempty message creates the native conversation if needed. Existing persisted managed tasks keep their behavior and grants.
- Import preserves selected native identity and explicit source-stopped verification. Raw messages, including whitespace and skill names, are never rewritten into goals, briefs, or continuation prompts.
- Windows and Android show an empty conversation for a newly created task without requesting nonexistent history. Both use the same Send semantics.
- Manual handoff and explicitly enabled automatic handoff use the existing controller and a documented context-handoff managed adapter. The standalone ledger is not impersonated or overwritten. Known standalone ownership conflicts block work.
- Freeze the source conversation fingerprint and complete workspace hashes. READY must match both, task revision and current permission ceiling. Unknown operations cannot be replayed. Persist owner transfer before any future work.
- Direct handoff completes in a waiting state; only the next actual user message starts work. Preserve the old conversation and task identity.
- Remove fixed workspace file-count and byte limits. Keep existing directory exclusions, streaming content hashes, link rejection and Git metadata verification. Cancellation or any read failure aborts the entire snapshot, never returning partial success. Large directories can take longer to verify.
- No new service/framework, permission expansion, real task messages or owner-verification bypass during development. Native Tk Windows and Java Android only. Device and release acceptance remain separate from isolated tests.

The adapter is a managed App Server handoff, not a claim of native Desktop chat-tool availability. Typing a skill name is still a verbatim message, not a hidden client command.
