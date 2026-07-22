#!/usr/bin/env python3
"""管理本机 MCP Server 的 macOS launchd 服务。

服务安装为当前用户的 LaunchAgent。所有路径都使用绝对路径，避免
launchd 依赖 shell、PATH 或已激活的虚拟环境。
"""

from __future__ import annotations

import argparse
import os
import plistlib
import socket
import subprocess
import sys
import time
from pathlib import Path


DEFAULT_LABEL = "com.wechatdecrypt.light.mcp"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def project_dir() -> Path:
    """返回项目目录；打包启动器可以通过环境变量覆盖该目录。"""
    configured = os.environ.get("WECHAT_DECRYPT_APP_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parent


def service_paths(root: Path | None = None, home: Path | None = None) -> dict[str, Path]:
    root = (root or project_dir()).resolve()
    home = (home or Path.home()).expanduser().resolve()
    return {
        "root": root,
        "python": root / ".venv" / "bin" / "python3",
        "main": root / "main.py",
        "service": root / "service.py",
        "plist_dir": home / "Library" / "LaunchAgents",
        "plist": home / "Library" / "LaunchAgents" / f"{DEFAULT_LABEL}.plist",
        "log_dir": home / "Library" / "Logs" / "WeChatDecryptLight",
        "stdout": home / "Library" / "Logs" / "WeChatDecryptLight" / "mcp.stdout.log",
        "stderr": home / "Library" / "Logs" / "WeChatDecryptLight" / "mcp.stderr.log",
    }


def launch_domain() -> str:
    return f"gui/{os.getuid()}"


def service_target() -> str:
    return f"{launch_domain()}/{DEFAULT_LABEL}"


def build_plist(paths: dict[str, Path], host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> dict:
    """生成只使用绝对路径的 launchd plist 配置。"""
    return {
        "Label": DEFAULT_LABEL,
        "ProgramArguments": [
            str(paths["python"]),
            str(paths["service"]),
            "run",
            "--host",
            host,
            "--port",
            str(port),
        ],
        "WorkingDirectory": str(paths["root"]),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "ExitTimeOut": 10,
        "EnvironmentVariables": {
            "PYTHONUNBUFFERED": "1",
            "WECHAT_DECRYPT_APP_DIR": str(paths["root"]),
        },
        "StandardOutPath": str(paths["stdout"]),
        "StandardErrorPath": str(paths["stderr"]),
    }


def run_service(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, retry_interval: int = 10) -> int:
    """等待微信和版本门禁就绪后启动 MCP，供 launchd 作为常驻入口调用。"""
    paths = service_paths()
    print(f"[*] 常驻服务已启动，等待微信就绪后提供 MCP: {host}:{port}", flush=True)

    while True:
        try:
            from config import load_config
            from wechat_version_guard import check_version

            result = check_version(load_config())
            if result.ok:
                print("[+] 微信环境和版本门禁已就绪，启动 MCP Server", flush=True)
                os.execv(
                    str(paths["python"]),
                    [
                        str(paths["python"]),
                        str(paths["main"]),
                        "serve",
                        "--host",
                        host,
                        "--port",
                        str(port),
                    ],
                )
                return 0

            reason = "；".join(result.reasons) or "微信尚未就绪"
            print(f"[*] MCP 暂不启动：{reason}；{retry_interval} 秒后重试", flush=True)
        except Exception as exc:
            print(f"[*] MCP 启动前检查失败：{exc}；{retry_interval} 秒后重试", flush=True)

        time.sleep(max(1, retry_interval))


def _run_launchctl(args: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/launchctl", *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _is_loaded() -> bool:
    result = _run_launchctl(["print", service_target()])
    return result.returncode == 0


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise RuntimeError("launchd service management is only supported on macOS")


def install_service(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> int:
    _require_macos()
    paths = service_paths()

    if not paths["python"].is_file() or not os.access(paths["python"], os.X_OK):
        print(f"[错误] 未找到可执行的虚拟环境 Python: {paths['python']}", file=sys.stderr)
        print("       请先运行 setup.sh 安装依赖。", file=sys.stderr)
        return 1
    if not paths["main"].is_file():
        print(f"[错误] 未找到服务入口: {paths['main']}", file=sys.stderr)
        return 1
    if not paths["service"].is_file():
        print(f"[错误] 未找到常驻服务入口: {paths['service']}", file=sys.stderr)
        return 1

    paths["plist_dir"].mkdir(parents=True, exist_ok=True)
    paths["log_dir"].mkdir(parents=True, exist_ok=True)
    paths["plist"].parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    # 替换配置前先卸载旧版本，保证重复安装具有幂等性。
    _run_launchctl(["bootout", launch_domain(), str(paths["plist"])])

    with paths["plist"].open("wb") as plist_file:
        plistlib.dump(build_plist(paths, host=host, port=port), plist_file, sort_keys=False)
    os.chmod(paths["plist"], 0o600)

    loaded = _run_launchctl(["bootstrap", launch_domain(), str(paths["plist"])])
    if loaded.returncode != 0:
        print(f"[错误] launchd 加载失败: {loaded.stderr.strip()}", file=sys.stderr)
        return loaded.returncode or 1

    # bootstrap 通常会遵循 RunAtLoad；补充 kickstart，避免 launchd 已加载旧任务
    # 时重复安装或启动的结果不确定。
    started = _run_launchctl(["kickstart", "-k", service_target()])
    if started.returncode != 0:
        print(f"[错误] MCP Server 启动失败: {started.stderr.strip()}", file=sys.stderr)
        return started.returncode or 1

    if not _wait_for_port(host, port):
        print(f"[提示] 常驻服务已加载，但微信尚未就绪，MCP 暂未监听 {host}:{port}")
        print("       微信启动并通过版本门禁后，服务会自动开始监听。")
    else:
        print(f"[完成] 已安装并启动 macOS 常驻服务: {DEFAULT_LABEL}")
    print(f"       MCP 地址: http://{host}:{port}/mcp")
    print(f"       日志目录: {paths['log_dir']}")
    print("       电脑重启或重新登录后会自动启动，无需再次执行命令。")
    return 0


def _wait_for_port(host: str, port: int, timeout: float = 15.0) -> bool:
    """等待 MCP 端口监听，避免仅凭 launchctl 成功就误报服务已可用。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_open(host, port):
            return True
        time.sleep(0.25)
    return False


def start_service() -> int:
    _require_macos()
    paths = service_paths()
    if not paths["plist"].is_file():
        print("[错误] 服务尚未安装，请先运行: python3 service.py install", file=sys.stderr)
        return 1
    if not _is_loaded():
        loaded = _run_launchctl(["bootstrap", launch_domain(), str(paths["plist"])])
        if loaded.returncode != 0:
            print(f"[错误] launchd 加载失败: {loaded.stderr.strip()}", file=sys.stderr)
            return loaded.returncode or 1
    result = _run_launchctl(["kickstart", "-k", service_target()])
    if result.returncode != 0:
        print(f"[错误] 启动失败: {result.stderr.strip()}", file=sys.stderr)
        return result.returncode or 1
    print("[完成] MCP Server 已启动")
    return 0


def stop_service() -> int:
    _require_macos()
    paths = service_paths()
    if not paths["plist"].is_file() and not _is_loaded():
        print("[提示] 服务未安装或已经停止")
        return 0
    result = _run_launchctl(["bootout", launch_domain(), str(paths["plist"])])
    if result.returncode != 0 and _is_loaded():
        print(f"[错误] 停止失败: {result.stderr.strip()}", file=sys.stderr)
        return result.returncode or 1
    print("[完成] MCP Server 已停止")
    return 0


def uninstall_service() -> int:
    _require_macos()
    paths = service_paths()
    _run_launchctl(["bootout", launch_domain(), str(paths["plist"])])
    try:
        paths["plist"].unlink()
    except FileNotFoundError:
        pass
    print("[完成] 已移除 macOS 常驻服务（不会删除项目、配置或解密数据）")
    return 0


def _port_open(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def status_service(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> int:
    _require_macos()
    paths = service_paths()
    loaded = _is_loaded()
    listening = _port_open(host, port)
    print(f"[服务] {'已加载' if loaded else '未加载'} ({DEFAULT_LABEL})")
    print(f"[端口] {'监听中' if listening else '未监听'} ({host}:{port})")
    print(f"[配置] {paths['plist']}")
    print(f"[日志] {paths['log_dir']}")
    return 0 if loaded and listening else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="管理本机 MCP Server 的 macOS 常驻服务")
    subparsers = parser.add_subparsers(dest="command", required=True)

    install = subparsers.add_parser("install", help="安装并启动登录自启服务")
    install.add_argument("--host", default=DEFAULT_HOST)
    install.add_argument("--port", type=int, default=DEFAULT_PORT)

    subparsers.add_parser("start", help="启动服务")
    subparsers.add_parser("stop", help="停止服务，下一次登录仍会自动加载")
    subparsers.add_parser("restart", help="重启服务")
    status = subparsers.add_parser("status", help="查看 launchd 和端口状态")
    status.add_argument("--host", default=DEFAULT_HOST)
    status.add_argument("--port", type=int, default=DEFAULT_PORT)
    subparsers.add_parser("uninstall", help="移除常驻服务，不删除数据")
    run = subparsers.add_parser("run", help="等待微信就绪后启动 MCP，供 launchd 调用")
    run.add_argument("--host", default=DEFAULT_HOST)
    run.add_argument("--port", type=int, default=DEFAULT_PORT)
    run.add_argument("--retry-interval", type=int, default=10)

    args = parser.parse_args(argv)
    if args.command == "install":
        return install_service(host=args.host, port=args.port)
    if args.command == "start":
        return start_service()
    if args.command == "stop":
        return stop_service()
    if args.command == "restart":
        stop_service()
        return start_service()
    if args.command == "status":
        return status_service(host=args.host, port=args.port)
    if args.command == "uninstall":
        return uninstall_service()
    if args.command == "run":
        return run_service(
            host=args.host,
            port=args.port,
            retry_interval=args.retry_interval,
        )
    parser.error(f"未知命令: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
