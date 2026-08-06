from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import getpass
import json
from pathlib import Path
import subprocess
import sys
import time

import psutil

from .botpy_safety import silence_botpy_logging
from .installer import find_codex_command, install_personal_plugin, marketplace_contains_plugin
from .paths import data_dir, database_path, ensure_data_dir, runtime_python
from .processes import ensure_daemon, process_matches
from .security import generate_pairing_code, load_credentials, redact_secrets, store_credentials
from .store import Store
from .subprocess_utils import hidden_console_subprocess_kwargs


STANDALONE_SETTING = "daemon_standalone"


def _safe_cli_text(value: object, *, fallback: str = "外部命令失败", limit: int = 300) -> str:
    """Redact and bound text originating outside CodexBot before printing it."""

    text = redact_secrets(str(value or "")).replace("\r", " ").replace("\n", " ")
    text = " ".join(text.split())[:limit]
    return text or fallback


def create_pairing(store: Store) -> tuple[str, float]:
    code = generate_pairing_code()
    expiry = time.time() + 30 * 60
    store.create_pairing(code, expiry)
    return code, expiry


def command_pair(_: argparse.Namespace) -> int:
    ensure_data_dir()
    code, expiry = create_pairing(Store(database_path()))
    print(f"一次性配对码：{code}")
    print(f"有效期至：{datetime.fromtimestamp(expiry).strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"请用沙箱 QQ 私聊机器人发送：/bind {code}")
    return 0


def command_setup(args: argparse.Namespace) -> int:
    ensure_data_dir()
    current = load_credentials()
    if current and not args.replace_credentials:
        print("Windows Credential Manager 中已有 QQ 凭据，将继续使用。")
    else:
        print("请输入 QQ 机器人沙箱凭据；AppSecret 输入时不会回显。")
        app_id = input("AppID: ").strip()
        app_secret = getpass.getpass("AppSecret: ").strip()
        store_credentials(app_id, app_secret)
        print("凭据已保存到 Windows Credential Manager。")

    result = install_personal_plugin(Path(args.repo_root))
    print(f"插件已安装：{result.plugin_path}")
    print(f"个人 marketplace：{result.marketplace_path}")
    if result.codex_output:
        print(_safe_cli_text(result.codex_output))

    code, expiry = create_pairing(Store(database_path()))
    print("")
    print("接下来：")
    print("1. 重启 Codex，在 /hooks 中检查并信任 codexbot Hooks。")
    print("2. 确认你的 QQ 已加入机器人沙箱并允许机器人主动发送。")
    print(f"3. 在 {datetime.fromtimestamp(expiry).strftime('%H:%M:%S')} 前私聊发送：/bind {code}")
    print("   如果过期，请在源码目录运行 .\\codexbot.cmd pair 重新生成。")
    return 0


async def _qq_online_check() -> tuple[bool, str]:
    credentials = load_credentials()
    if credentials is None:
        return False, "未配置凭据"
    try:
        silence_botpy_logging()
        from botpy.api import BotAPI
        from botpy.http import BotHttp
        from botpy.robot import Token

        http = BotHttp(timeout=8, is_sandbox=True)
        try:
            robot = await http.login(Token(credentials.app_id, credentials.app_secret))
            gateway = await BotAPI(http).get_ws_url()
        finally:
            await http.close()
        connected = bool(robot and isinstance(gateway, dict) and gateway.get("url"))
        return connected, "沙箱认证与 Gateway 检查成功" if connected else "沙箱 API 未返回机器人或 Gateway 信息"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {_safe_cli_text(exc, limit=180)}"


def _codex_plugin_installed() -> tuple[bool, str]:
    command = find_codex_command()
    if not command:
        return False, "找不到 codex/codex.cmd"
    try:
        completed = subprocess.run(
            [command, "plugin", "list", "--json"],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=20,
            check=False,
            **hidden_console_subprocess_kwargs(),
        )
        if completed.returncode:
            return False, _safe_cli_text(completed.stderr or completed.stdout, limit=180)
        payload = json.loads(completed.stdout)
        installed = payload.get("installed", []) if isinstance(payload, dict) else []
        match = next((item for item in installed if isinstance(item, dict) and item.get("name") == "codexbot"), None)
        if match:
            return bool(match.get("enabled", True)), "已安装并启用" if match.get("enabled", True) else "已安装但未启用"
        return False, "Codex 未报告已安装的 codexbot"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {_safe_cli_text(exc, limit=180)}"


def command_doctor(args: argparse.Namespace) -> int:
    root = ensure_data_dir()
    store = Store(database_path())
    checks: list[tuple[str, bool, str, bool]] = []

    python_ok = sys.version_info[:2] == (3, 11)
    checks.append(("Python", python_ok, sys.version.split()[0], True))
    runtime = runtime_python()
    checks.append(("隔离运行时", runtime.is_file(), str(runtime), True))
    credentials = load_credentials()
    checks.append(("QQ 凭据", credentials is not None, "已存入凭据管理器" if credentials else "缺失", True))

    home = Path.home()
    plugin_manifest = home / "plugins" / "codexbot" / ".codex-plugin" / "plugin.json"
    checks.append(("个人插件文件", plugin_manifest.is_file(), str(plugin_manifest), True))
    marketplace = home / ".agents" / "plugins" / "marketplace.json"
    checks.append(("Marketplace 条目", marketplace_contains_plugin(marketplace), str(marketplace), True))
    installed, installed_detail = _codex_plugin_installed()
    checks.append(("Codex 插件状态", installed, installed_detail, True))

    bound = store.get_bound_openid()
    checks.append(("QQ 单用户绑定", bool(bound), "已绑定" if bound else "尚未绑定", False))
    daemon = store.get_daemon_info()
    daemon_alive = bool(daemon and process_matches(*daemon))
    checks.append(("伴随进程", daemon_alive, f"PID {daemon[0]}" if daemon_alive and daemon else "当前未运行", False))

    if not args.offline:
        online_ok, detail = asyncio.run(_qq_online_check())
        checks.append(("QQ 沙箱连接", online_ok, detail, True))

    failed_required = False
    for label, ok, detail, required in checks:
        marker = "OK" if ok else ("WARN" if not required else "FAIL")
        print(f"[{marker}] {label}: {detail}")
        failed_required = failed_required or (required and not ok)
    print(f"[INFO] 数据目录: {root}")
    standalone_marker = "是" if store.get_setting(STANDALONE_SETTING) == "1" else "否"
    print(f"[INFO] 常驻模式: {standalone_marker}（常驻进程运行 .\\codexbot.cmd start / stop）")
    print("[INFO] Hook 信任状态需在 Codex /hooks 中人工确认。")
    return 1 if failed_required else 0


def command_start(_: argparse.Namespace) -> int:
    """Start a standalone daemon that stays online without Codex hosts."""
    ensure_data_dir()
    store = Store(database_path())
    info = store.get_daemon_info()
    if info and process_matches(*info):
        detail = "常驻运行中" if store.get_setting(STANDALONE_SETTING) == "1" else "运行中（跟随 Codex）"
        print(f"CodexBot 伴随进程已在运行（PID {info[0]}，{detail}）。")
        return 0
    if load_credentials() is None:
        print("错误：未配置 QQ 凭据，请先运行 .\\codexbot.cmd setup 或 install.cmd。", file=sys.stderr)
        return 1
    launched = ensure_daemon(store, standalone=True)
    info = store.get_daemon_info()
    if launched and info and process_matches(*info):
        store.set_setting(STANDALONE_SETTING, "1")
        print(f"CodexBot 常驻进程已启动（PID {info[0]}），QQ 机器人将保持在线。")
        print("停止请运行：.\\codexbot.cmd stop")
        return 0
    print("错误：未能启动常驻进程，请查看日志排查。", file=sys.stderr)
    return 1


def command_stop(_: argparse.Namespace) -> int:
    """Stop the running CodexBot companion process."""
    ensure_data_dir()
    store = Store(database_path())
    info = store.get_daemon_info()
    if not info:
        print("CodexBot 进程未在运行。")
        store.delete_settings([STANDALONE_SETTING])
        return 0
    pid, create_time = info
    if not process_matches(pid, create_time):
        print(f"CodexBot 进程（PID {pid}）已退出，清理记录。")
        store.clear_daemon_info(pid)
        store.delete_settings([STANDALONE_SETTING])
        return 0
    try:
        process = psutil.Process(pid)
        process.terminate()
        process.wait(timeout=10)
    except psutil.NoSuchProcess:
        pass
    except psutil.TimeoutExpired:
        print("进程未在 10 秒内退出，强制结束。")
        try:
            process.kill()
            process.wait(timeout=5)
        except Exception:
            pass
    store.clear_daemon_info(pid)
    store.delete_settings([STANDALONE_SETTING])
    print(f"CodexBot 进程（PID {pid}）已停止，QQ 机器人已下线。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codexbot", description="Codex → QQ 官方沙箱通知机器人")
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup = subparsers.add_parser("setup", help="保存凭据并安装个人 Codex 插件")
    setup.add_argument("--repo-root", required=True, help="codexbot 源码目录")
    setup.add_argument("--replace-credentials", action="store_true", help="替换已有 QQ 凭据")
    setup.set_defaults(func=command_setup)

    pair = subparsers.add_parser("pair", help="生成 30 分钟有效的一次性 QQ 配对码")
    pair.set_defaults(func=command_pair)

    doctor = subparsers.add_parser("doctor", help="检查安装、凭据、插件和 QQ 沙箱连接")
    doctor.add_argument("--offline", action="store_true", help="跳过 QQ 沙箱网络认证")
    doctor.set_defaults(func=command_doctor)

    start = subparsers.add_parser("start", help="启动常驻 daemon，QQ 机器人保持在线")
    start.set_defaults(func=command_start)

    stop = subparsers.add_parser("stop", help="停止正在运行的 daemon")
    stop.set_defaults(func=command_stop)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"错误：{_safe_cli_text(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
