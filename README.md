# Context Relay

A Codex skill and an optional Windows task manager for handing unfinished work to a fresh conversation when context pressure becomes high after repeated compaction.

Context Relay prepares a small handoff packet, preserves evidence and permissions, and requires the receiving chat to verify the checkpoint before taking ownership. Both entry points use Python's standard library. The standalone skill uses local files and available native chat tools; the optional manager uses SQLite and a child Codex App Server over stdio. Neither requires a listening port or background service.

The repository is named **Context Relay**; the installed skill remains **`context-handoff`** so invocations and handoff references use one stable name.

## Windows task manager — first version

Requires **Windows, Python 3.10+ with Tk, and a working Codex installation and login**. From the repository root:

```shell
python -m relay --check
python -m relay
```

You can also double-click `start-manager.cmd`. The Chinese interface provides task creation, start/continue, pause, recovery checks, preparatory snapshots, handoff, one-time approvals, and export. Automatic handoff is opt-in for each task. Read-only and workspace-write are the two permission ceilings; budgets are soft limits and may be exceeded by an in-flight call.

Find tasks by title, goal or directory, filter their status, and move completed tasks into a reversible local archive. Archiving preserves the task, files, permissions and conversation ownership; restoring it does not resume work. The operation-history window shows the latest 100 recorded events, distinguishing requests, native starts and confirmed results. These local management actions do not start Codex inference or archive native conversations.

With automatic handoff enabled, the manager saves a preparatory snapshot after a completed work turn becomes idle and fresh context telemetry reaches 70%. At 80% with at least two compactions, it can initiate the full handoff instead. You can also request a snapshot while idle or paused. Snapshots copy known local state and file hashes without extra model inference or a new conversation; only the latest `drafts/<task-id>.json` is retained. They contain recorded requirements and answers, the last reply, up to 20 source-turn references, and explicit unknowns, with `ready=false`. They are not complete decision summaries or project backups.

Formal handoff always regenerates the summary and checks current files and READY; an old draft is never accepted as a final checkpoint. Summary preparation and the receiver's READY verification intentionally run read-only, even for a workspace-write task. Continuation may restore only the original permission ceiling after checking the original task instructions, permission record, checkpoint, and effective session configuration.

The manager creates its own App Server conversations. It does not import existing desktop chats or promise official desktop sidebar integration. It disables external MCP servers, apps, and plugins for its own sessions and checks reported server status; your global Codex configuration and other sessions are unchanged. The first version manages Codex only, with no phone or multi-tool control.

State is local plaintext under `%LOCALAPPDATA%\ContextRelay`: SQLite task/event records, drafts, and checkpoints. A supplied `--state-dir` chooses another directory. The state directory and project directory must be separate: neither may equal or contain the other, including for existing task records. This manager ledger is separate from the standalone skill's `CODEX_HOME/context-handoffs` files; their ownership records are not interchangeable or automatically migrated. Ownership checks fence managed requests, not arbitrary processes or other clients. Read the [Chinese usage guide and exact limits](docs/windows-manager.md). Native session acceptance is tracked there separately from simulated tests.

Recovery requires the known native turn's terminal result and matching terminal tool IDs; missing evidence stays blocked. Recorded user corrections that were never sent remain in the next explicit continuation's context. Elapsed time is settled once when the result is reliable, stops growing while paused, and includes uncertain intervals conservatively.

A start receipt does not mean the native turn has begun. An immediate pause is persisted first; one interrupt is sent after the matching `turn/started`, with repeated pause requests or events deduplicated. If completion arrives first, the task remains paused, completed work is counted once, and automatic handoff does not start. Automatic-summary timeouts also retain the latest persisted unknown request intent for reconciliation rather than losing it in error handling.

The skill installer below does **not** install or start the manager; the two entry points remain separate.

## Standalone skill triggers

| Verified signal | Response |
| --- | --- |
| Context-window estimate reaches 70% | Prepare or refresh a handoff draft. |
| At least two compactions and an estimate of 80% or more | Recommend a handoff at a safe checkpoint if work remains. |
| Missing, stale, or ambiguous telemetry | Report unknown; do not invent a percentage. |
| Work is complete and only the final reply remains | Finish in the current chat. |

These are preventive defaults, not validated error-rate thresholds. Context Relay cannot measure how much another compaction would increase mistakes. The hook prompts the agent; it does not independently create chats or carry out the project.

## Install the standalone skill

Requirements: **Windows, Python 3.8+, and a Codex environment that supports lifecycle hooks and local skills.** Local tests used Python 3.13.2; older supported versions have not each been verified. The installer currently supports Windows only and refuses other platforms without changes. Support has not been validated for every Codex host.

From the repository root, preview the installation:

```shell
python install.py
```

Review the preview, then install:

```shell
python install.py --apply
```

The installer copies `skills/context-handoff` to your Codex skill directory and merges this project's four hook handlers into the user hook configuration, preserving unrelated handlers. It respects `CODEX_HOME`, falling back to `~/.codex`. **Installation does not trust hooks.** Review the definitions in Codex; the CLI provides `/hooks`. New or changed definitions require review before they can run. See the [official hooks documentation](https://learn.chatgpt.com/docs/hooks).

Existing files that will change are backed up under `CODEX_HOME/backups`; the installer reports the backup location. Repeating installation does not duplicate the four handlers. If installation fails, inspect the reported backup before retrying.

Hook commands pin the installed script's SHA-256 and run Python with `-I`. After an update, rerun the preview and installation, then review the changed definitions. Do not edit trust records to bypass that review.

## Use the standalone skill

Ask Codex, for example:

> Use $context-handoff to inspect this task. If a handoff is needed, you may create a continuation chat for this same task and let it continue after verification. Preserve the task's existing permissions and stop conditions.

That authorization belongs to your task. Installing this repository does not inherit another user's permissions or authorize publishing, payments, or unrelated messages.

The source chat freezes a checkpoint, the receiver verifies its evidence, and ownership transfers once. Both chats check ownership before further project changes. The old chat remains available and is not automatically archived. Plan and read-only modes do not become execution sessions.

If native chat creation tools are unavailable, the skill saves the packet and a `startup.md` instruction. You create a fresh chat in the same working directory and paste the current instruction, including its invitation marker. The receiver verifies and claims the handoff before continuing. Automatic native chat creation is a conditional capability, not a verified feature of every host.

The [skill](skills/context-handoff/SKILL.md) and [protocol](skills/context-handoff/references/protocol.md) contain the full workflow. Agent-facing instructions are currently in Chinese.

## Standalone skill evidence and limits

- Handoff files and telemetry are **local plaintext**, stored under `CODEX_HOME/context-handoffs` or `~/.codex/context-handoffs`. Keep them out of public repositories.
- A heuristic rejects suspected credentials and selected personal information before saving handoff data. It is not comprehensive redaction and does not scan your whole project or attachment contents. Use source references instead of copying sensitive values.
- Authorization points to original user-role log records; receiver checks include file or line evidence and hashes. This proves record integrity, not human identity or the meaning of consent. The agent must review scope.
- Ownership is a **cooperative guard**, not an operating-system lock. It cannot stop another application or an agent that ignores the protocol. A user who can alter scripts, configuration, and logs can bypass local checks.
- `health` separates manual and configured command invocations. Its `client_activation: unverified` means there is no authenticated host receipt; local invocation counts alone do not prove automatic client activation.

## Validation

The runtime has **71 passing tests** from local Windows validation, including stale checkpoints, compaction signals, sensitive-data rejection, invitation binding, and ownership races. Simulated handoffs do not prove that a host can create chats or that a new chat has resumed real work.

Run the runtime suite:

```shell
python -m unittest discover -s skills/context-handoff/tests -v
```

The installer has **6 passing tests** covering previews, backups, configuration preservation, repeated installation, and interrupted updates:

```shell
python -m unittest -v test_install.py
```

The task-organization follow-up passed **90 manager tests** (38 controller, 5 transport, 22 Tk, 7 snapshot, 12 recovery, 6 local organization). The 71 standalone runtime and 6 installer tests were also rerun successfully: **167 tests total**. Manager tests use simulated controllers or servers and real Tk widgets. The first-version validation on 2026-09-30 used Windows, Python 3.13.2, Tk 8.6.15, and Codex 0.153.4. Run the manager suite separately:

```shell
python -m unittest discover -s tests -v
```

The real Tk window was also inspected with synthetic tasks: approval actions and the bottom controls remain visible with the default screen-bounded layout. Basic layout checks covered 96/144-DPI-equivalent Tk scaling on the local display. Fake-server, FakeManager, and window checks do not prove native conversation handoff. See the manager guide for the current acceptance status.

A separate native counter task on 2026-09-30, validating the first manager version (`d6d7acf`) with Codex 0.153.4, passed `0 → 1 → 2 → 3` across three independent App Server threads under one task ID. Both handoffs completed all six READY checks, transferred ownership, and continued actual work. Four reported MCP entries were disabled with zero tools. An earlier attempt safely stopped at a negative READY; the manager protocol was clarified and a new independent run passed without bypassing verification. The successful run took about 9 minutes 26 seconds; this simple example is not a performance benchmark, and handoffs add model usage and time.

The reliability version (`8e429e0`) passed a native run on 2026-09-30 with Codex 0.153.4 covering immediate pause, confirmed interruption, close/reopen, read-only reconciliation, and explicit continuation. The product waited for the matching native `turn/started` before sending its interrupt. The same task and thread remained at generation 0; a distinct read-only turn returned the unique test-file contents without changing its hash, with no approval responses. Manual preparation then saved a `preparatory`, `ready=false` draft with the matching file hash, issuing only `thread/read` and no new turn or thread. An earlier timing failure retained its unknown outcome without retry and prompted the pause-ordering fix.

For the current local-organization changes, a real Manager and Tk window with disposable data passed search, archive, history lookup and restore. Project files were unchanged and no native requests were issued. This local check does not rerun native handoff or interrupted inference.

High-context automatic triggering has only simulated coverage. Physical sleep, power loss, network loss, crashes, and other environments have not been tested live. The controlled read-only recovery example does not establish recovery of every interrupted external operation.

Opt-in [native smoke procedures](docs/windows-manager.md#验证状态) cover handoff (`tests/live_smoke.py`) and interruption/recovery (`tests/live_recovery.py`). They use real Codex usage and new directories outside the repository, are excluded from default tests, and stop when approval, user input, or an uncertain outcome needs attention.

## 中文简介

Context Relay 为长任务提供上下文检测和跨聊天交接：核验目标、授权、证据和在途操作，再移交执行权。独立技能调用名保持 `$context-handoff`，缺少建聊工具时生成手动启动指令。可选 Windows 本机管理器用 `python -m relay` 启动；按任务开启自动交接后，工作轮次结束且数据新鲜时，70% 保存无需额外推理的本地预备快照，80% 加至少两次压缩走正式交接。空闲或暂停时也可手动预备；正式交接须重新核验。阈值不测量错误概率。两者使用各自的状态记录；技能 hook 仍需在 Codex 审阅并信任，管理器不会默认随技能安装。

## License

[MIT](LICENSE).
