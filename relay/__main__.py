"""Run the local Windows manager, or check its prerequisites without starting Codex."""
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
    parser.add_argument("--check", action="store_true", help="检查 Tk 和 Codex 命令，不创建任务或启动 App Server")
    args = parser.parse_args(argv)
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
