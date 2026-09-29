#!/usr/bin/env python3
"""Preview or install Context Relay on Windows; never grants hook trust."""
import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

FILES = (
    "SKILL.md", "agents/openai.yaml", "references/protocol.md",
    "references/handoff-template.json", "scripts/context_handoff.py",
    "tests/test_context_handoff.py",
)
EVENTS = {"SessionStart": "startup|resume|clear|compact",
          "UserPromptSubmit": None, "PostToolUse": None, "PostCompact": "auto|manual"}
SOURCE = Path(__file__).resolve().parent / "skills" / "context-handoff"


def read_optional(path):
    return path.read_bytes() if path.exists() else None


def load_config(raw):
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON configuration key")
            result[key] = value
        return result

    config = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_keys) if raw is not None else {}
    if not isinstance(config, dict) or not isinstance(config.get("hooks", {}), dict):
        raise ValueError("hooks.json must contain an object with a hooks object")
    for groups in config.get("hooks", {}).values():
        if not isinstance(groups, list):
            raise ValueError("Hook event groups must be lists")
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError("Hook groups must contain a hooks list")
            for hook in group["hooks"]:
                if not isinstance(hook, dict) or not isinstance(hook.get("type"), str):
                    raise ValueError("Malformed hook definition")
                if hook["type"] == "command" and not isinstance(hook.get("command"), str):
                    raise ValueError("Command hooks require a command string")
    return config


def pinned_command(target):
    """Reuse the shipped runtime's Windows command builder without writing bytecode."""
    script = SOURCE / "scripts" / "context_handoff.py"
    spec = importlib.util.spec_from_file_location("context_relay_runtime", script)
    runtime = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(runtime)
    finally:
        sys.dont_write_bytecode = previous
    command = runtime.hook_command(script, sys.executable)
    suffix = " " + subprocess.list2cmdline([str(script)]) + " " + runtime.file_hash(script)
    if not command.endswith(suffix):
        raise ValueError("Unsupported runtime command format")
    return (command[:-len(suffix)] + " " + subprocess.list2cmdline([str(target)])
            + " " + runtime.file_hash(script))


def merged_config(config, target, command):
    config = copy.deepcopy(config)
    events = config.setdefault("hooks", {})
    target_argument = subprocess.list2cmdline([str(target)]).replace("\\", "/").casefold()

    def owned(hook):
        if hook.get("type") != "command":
            return False
        value = hook["command"].replace("\\", "/").casefold()
        prefix, _, digest = value.rpartition(" ")
        # Match our specific bootstrap and script argument, not other commands that
        # merely mention the script (for example a backup or integrity monitor).
        return ("context-handoff integrity mismatch; review installation" in prefix
                and "'--hook-origin','configured'" in prefix
                and prefix.endswith(" " + target_argument)
                and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest))

    for event, matcher in EVENTS.items():
        groups = events.setdefault(event, [])
        existing = [hook for group in groups for hook in group["hooks"] if owned(hook)]
        if len(existing) > 1:
            raise ValueError("Duplicate Context Relay hooks in " + event + "; review hooks.json")
        if existing:
            # Trust keys include group/handler positions. Preserve all positions,
            # matchers, and user options; only update this install's command.
            existing[0]["command"] = command
            continue
        definition = {"type": "command", "command": command, "timeout": 5}
        if event != "PostCompact":
            definition["additionalContextLimit"] = 512
        group = {"hooks": [definition]}
        if matcher:
            group["matcher"] = matcher
        groups.append(group)
    return config


def replace_checked(path, content, expected):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=".context-relay-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if read_optional(path) != expected:
            raise ValueError("Installation target changed during install: " + str(path))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def install(codex_home, apply=False):
    if os.name != "nt":
        raise ValueError("Only Windows installation is supported; no files were changed")
    home = Path(codex_home).expanduser().resolve()
    destination = home / "skills" / "context-handoff"
    hooks_path = home / "hooks.json"
    old_hooks = read_optional(hooks_path)
    config = load_config(old_hooks)
    contents = {destination / relative: (SOURCE / relative).read_bytes() for relative in FILES}
    script = destination / "scripts" / "context_handoff.py"
    command = pinned_command(script)
    if not command.endswith(" " + hashlib.sha256(contents[script]).hexdigest()):
        raise ValueError("Source runtime changed during install; retry after reviewing it")
    merged = merged_config(config, script, command)
    if merged != config:
        contents[hooks_path] = (json.dumps(merged, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    before = {path: read_optional(path) for path in contents}
    changed = {path: content for path, content in contents.items() if content != before[path]}
    result = {"mode": "preview" if not apply else "unchanged", "codex_home": str(home),
              "changed": [str(path.relative_to(home)) for path in changed],
              "backup": None, "hook_trust": "Review in Codex; this installer never grants trust"}
    if not apply or not changed:
        return result
    existing = {path: before[path] for path in changed if before[path] is not None}
    if existing:
        backups = home / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        backup = Path(tempfile.mkdtemp(prefix="context-relay-", dir=backups))
        for path, content in existing.items():
            saved = backup / path.relative_to(home)
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_bytes(content)
        result["backup"] = str(backup)
    # Install exact script bytes before publishing its pin. If interrupted, an old
    # mismatching pin refuses execution; never roll back config over concurrent edits.
    for path, content in changed.items():
        if path != hooks_path:
            replace_checked(path, content, before[path])
    if script.read_bytes() != contents[script]:
        raise ValueError("Installed runtime bytes changed; hook configuration was not updated")
    if read_optional(hooks_path) != old_hooks:
        raise ValueError("hooks.json changed during install; hook configuration was not updated")
    if hooks_path in changed:
        replace_checked(hooks_path, changed[hooks_path], old_hooks)
    result["mode"] = "applied"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write the previewed installation")
    parser.add_argument("--codex-home", default=os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"))
    args = parser.parse_args()
    try:
        print(json.dumps(install(args.codex_home, args.apply), ensure_ascii=False, indent=2))
    except (OSError, ValueError) as error:
        parser.exit(1, "Install stopped: " + str(error) + "\n")


if __name__ == "__main__":
    main()
