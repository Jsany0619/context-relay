"""Run the Windows manager, prerequisite checks, or offline backup inspection/restore."""
import argparse
import json
import sys


def check_environment():
    result = {"python": sys.version.split()[0], "platform": sys.platform,
              "windows": sys.platform == "win32", "tkinter": False, "codex_command": None, "errors": []}
    if sys.version_info < (3, 10):
        result["errors"].append("The manager requires Python 3.10 or newer")
    try:
        import tkinter as tk
        from .display import enable_native_dpi
        enable_native_dpi()
        root = tk.Tk()
        root.withdraw()
        result["tkinter"] = True
        result["tk_version"] = str(root.tk.call("info", "patchlevel"))
        root.destroy()
    except Exception as error:
        result["errors"].append(f"Tk unavailable: {error}")
    try:
        from .transport import discover_codex_command
        result["codex_command"] = discover_codex_command()
    except Exception as error:
        result["errors"].append(f"Codex unavailable: {error}")
    result["ok"] = result["windows"] and result["tkinter"] and not result["errors"]
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Context Relay Windows 本机任务管理器")
    parser.add_argument("--state-dir", help="本机管理器状态目录")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="检查 Tk 和 Codex 命令，不创建任务或启动 App Server")
    action.add_argument("--inspect-backup", metavar="FILE", help="离线校验本机备份包，不启动 Tk 或 Codex")
    action.add_argument("--restore-backup", metavar="FILE", help="恢复到新的 --state-dir，仅供只读查看和导出")
    args = parser.parse_args(argv)
    if args.inspect_backup or args.restore_backup:
        if args.restore_backup and not args.state_dir:
            parser.error("恢复必须用 --state-dir 指定一个尚不存在的新目录。")
        if args.inspect_backup and args.state_dir:
            parser.error("校验备份不需要 --state-dir。")
        from .backup import inspect_backup, restore_backup
        try:
            result = (inspect_backup(args.inspect_backup) if args.inspect_backup else
                      restore_backup(args.restore_backup, args.state_dir))
        except (OSError, ValueError) as error:
            print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
            return 1
        if args.restore_backup:
            quote = lambda value: "'" + value.replace("'", "''") + "'"
            result["open_command"] = f"& {quote(sys.executable)} -m relay --state-dir {quote(result['path'])}"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.check:
        result = check_environment()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    if sys.platform != "win32" or sys.version_info < (3, 10):
        parser.error("本机管理器需要 Windows 和 Python 3.10+。")
    from .ui import main as launch
    launch(state_dir=args.state_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
