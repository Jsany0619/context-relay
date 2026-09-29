# Context Relay

A Codex skill and local hook helper for handing unfinished work to a fresh chat when context pressure becomes high after repeated compaction.

Context Relay prepares a small handoff packet, preserves evidence and permissions, and requires the receiving chat to verify the checkpoint before taking ownership. It uses Python's standard library, local files, and the native chat tools available in your Codex environment. No background service or database is required.

The repository is named **Context Relay**; the installed skill remains **`context-handoff`** so invocations and handoff references use one stable name.

## When it acts

| Verified signal | Response |
| --- | --- |
| Context-window estimate reaches 70% | Prepare or refresh a handoff draft. |
| At least two compactions and an estimate of 80% or more | Recommend a handoff at a safe checkpoint if work remains. |
| Missing, stale, or ambiguous telemetry | Report unknown; do not invent a percentage. |
| Work is complete and only the final reply remains | Finish in the current chat. |

These are preventive defaults, not validated error-rate thresholds. Context Relay cannot measure how much another compaction would increase mistakes. The hook prompts the agent; it does not independently create chats or carry out the project.

## Install

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

## Use

Ask Codex, for example:

> Use $context-handoff to inspect this task. If a handoff is needed, you may create a continuation chat for this same task and let it continue after verification. Preserve the task's existing permissions and stop conditions.

That authorization belongs to your task. Installing this repository does not inherit another user's permissions or authorize publishing, payments, or unrelated messages.

The source chat freezes a checkpoint, the receiver verifies its evidence, and ownership transfers once. Both chats check ownership before further project changes. The old chat remains available and is not automatically archived. Plan and read-only modes do not become execution sessions.

If native chat creation tools are unavailable, the skill saves the packet and a `startup.md` instruction. You create a fresh chat in the same working directory and paste the current instruction, including its invitation marker. The receiver verifies and claims the handoff before continuing. Automatic native chat creation is a conditional capability, not a verified feature of every host.

The [skill](skills/context-handoff/SKILL.md) and [protocol](skills/context-handoff/references/protocol.md) contain the full workflow. Agent-facing instructions are currently in Chinese.

## Evidence and limits

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

## 中文简介

Context Relay 为长任务提供上下文检测和跨聊天交接：提前准备资料，核验目标、授权、证据和在途操作，再移交执行权。缺少原生建聊工具时生成手动启动指令。默认 70%／80% 只是预防策略，不测量错误概率；本机验证范围为 Windows，安装后仍需在 Codex 审阅并信任 hook。技能调用名保持 `$context-handoff`。

## License

[MIT](LICENSE).
