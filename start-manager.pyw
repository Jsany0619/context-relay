"""Windowed Windows entry point; shortcuts use pythonw -I with this file."""
import argparse
import ctypes
from pathlib import Path
import sys


def show_message(text, *, error=False):
    # Native dialog also works when Tk itself cannot be imported or initialized.
    ctypes.windll.user32.MessageBoxW(None, text, "Context Relay", 0x10 if error else 0x40)


def main(argv=None):
    try:
        if sys.platform != "win32" or sys.version_info < (3, 10):
            raise RuntimeError("需要 Windows 和 Python 3.10+。")
        parser = argparse.ArgumentParser(description="Context Relay 快捷启动", add_help=False, exit_on_error=False)
        parser.add_argument("--state-dir", help="已有任务库或只读恢复库目录")
        parser.add_argument("--check", action="store_true", help="弹出环境检查结果，不打开任务库")
        parser.add_argument("-h", "--help", action="store_true")
        args, unknown = parser.parse_known_args(argv)
        if unknown:
            raise ValueError("无法识别启动参数：" + " ".join(unknown))
        if args.help:
            show_message(parser.format_help(), error=False)
            return 0
        # -I ignores the working directory and PYTHONPATH; load only this checkout.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        if args.check:
            from relay.__main__ import check_environment
            result = check_environment()
            details = ["环境检查通过。" if result["ok"] else "环境检查未通过。",
                       f"Python: {result['python']}", f"Tk: {result.get('tk_version', '不可用')}"]
            if result.get("codex_command"):
                details.append("Codex: " + result["codex_command"][0])
            details.extend(result["errors"])
            details.append("\n此检查不验证账号登录，也不会创建或启动任务。")
            show_message("\n".join(details), error=not result["ok"])
            return 0 if result["ok"] else 1
        from relay.ui import main as launch
        launch(state_dir=args.state_dir)
        return 0
    except Exception as error:
        show_message(f"无法启动 Context Relay。\n{type(error).__name__}: {error}\n\n"
                     "可从开始菜单运行 Context Relay Check，或在项目目录运行 start-manager.cmd --check。",
                     error=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
