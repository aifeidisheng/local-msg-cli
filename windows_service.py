#!/usr/bin/env python3
"""Manage the current user's Windows Task Scheduler MCP process."""

from __future__ import annotations

import argparse
import base64
import getpass
import html
import json
import locale
import os
import platform
import socket
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path


TASK_NAME = "WeChatDecryptLightMCP"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def project_dir() -> Path:
    configured = os.environ.get("WECHAT_DECRYPT_APP_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parent


def service_paths(root: Path | None = None) -> dict[str, Path]:
    root = (root or project_dir()).resolve()
    local_app_data = os.environ.get("LOCALAPPDATA")
    state_root = (
        Path(local_app_data) / "WeChatDecryptLight"
        if local_app_data
        else Path.home() / "AppData" / "Local" / "WeChatDecryptLight"
    )
    python = root / ".venv" / "Scripts" / "python.exe"
    pythonw = root / ".venv" / "Scripts" / "pythonw.exe"
    return {
        "root": root,
        "python": python,
        "task_python": pythonw,
        "main": root / "main.py",
        "service": root / "windows_service.py",
        "log_dir": state_root / "logs",
        "stdout": state_root / "logs" / "mcp.stdout.log",
        "stderr": state_root / "logs" / "mcp.stderr.log",
    }


def _require_windows() -> None:
    if platform.system().lower() != "windows":
        raise RuntimeError("Windows task management is only supported on Windows")


def _no_window_creation_flags() -> int:
    """Return the Windows flag that suppresses a child console window."""
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))


def _current_user() -> str:
    username = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{username}" if domain else username


def _task_xml(
    paths: dict[str, Path],
    host: str,
    port: int,
    user: str | None = None,
) -> str:
    user = user or _current_user()
    arguments = f'"{paths["service"]}" run --host {host} --port {port}'
    escape = lambda value: html.escape(str(value), quote=True)
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Local WeChat message MCP server</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>5</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author"><Exec>
    <Command>{escape(paths["task_python"])}</Command>
    <Arguments>{escape(arguments)}</Arguments>
    <WorkingDirectory>{escape(paths["root"])}</WorkingDirectory>
  </Exec></Actions>
</Task>'''


def _decode_windows_output(payload: bytes | str | None) -> str:
    if payload is None or isinstance(payload, str):
        return payload or ""
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        return payload.decode("utf-16", errors="replace")
    if b"\x00" in payload[:32]:
        return payload.decode("utf-16-le", errors="replace")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload.decode(locale.getpreferredencoding(False), errors="replace")


def _run_schtasks(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["schtasks.exe", *arguments],
        capture_output=True,
        timeout=30,
    )
    return subprocess.CompletedProcess(
        result.args,
        result.returncode,
        _decode_windows_output(result.stdout),
        _decode_windows_output(result.stderr),
    )


def task_exists() -> bool:
    return _run_schtasks(["/Query", "/TN", TASK_NAME]).returncode == 0


def _task_matches(paths: dict[str, Path], host: str, port: int) -> bool:
    result = _run_schtasks(["/Query", "/TN", TASK_NAME, "/XML"])
    if result.returncode != 0:
        return False
    try:
        root = ET.fromstring((result.stdout or "").lstrip("\ufeff"))
    except ET.ParseError:
        return False
    namespace = {"task": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    command = root.findtext(".//task:Command", default="", namespaces=namespace)
    arguments = root.findtext(".//task:Arguments", default="", namespaces=namespace)
    working_directory = root.findtext(
        ".//task:WorkingDirectory", default="", namespaces=namespace
    )
    return (
        os.path.normcase(command) == os.path.normcase(str(paths["task_python"]))
        and os.path.normcase(working_directory) == os.path.normcase(str(paths["root"]))
        and os.path.normcase(str(paths["service"])) in os.path.normcase(arguments)
        and f"--host {host}" in arguments
        and f"--port {port}" in arguments
    )


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def _port_owner_details(host: str, port: int) -> list[dict[str, object]]:
    env = os.environ.copy()
    env["WECHAT_DECRYPT_HOST"] = host
    env["WECHAT_DECRYPT_PORT"] = str(port)
    script = (
        "$items=@(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | "
        "Where-Object { $_.LocalPort -eq [int]$env:WECHAT_DECRYPT_PORT -and "
        "$_.LocalAddress -eq $env:WECHAT_DECRYPT_HOST } | ForEach-Object { "
        "$p=Get-CimInstance Win32_Process -Filter \"ProcessId=$($_.OwningProcess)\"; "
        "@{Pid=$_.OwningProcess;ExecutablePath=$p.ExecutablePath;CommandLine=$p.CommandLine} }); "
        "$json=$items | ConvertTo-Json -Compress; "
        "[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding=locale.getpreferredencoding(False),
        errors="replace",
        env=env,
        timeout=10,
    )
    if result.returncode != 0 or not (result.stdout or "").strip():
        return []
    try:
        decoded = base64.b64decode(result.stdout.strip(), validate=True).decode("utf-8")
        payload = json.loads(decoded)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    if isinstance(payload, dict):
        return [payload]
    return payload if isinstance(payload, list) else []


def _service_owns_port(paths: dict[str, Path], host: str, port: int) -> bool:
    expected_python = os.path.normcase(str(paths["python"]))
    expected_main = os.path.normcase(str(paths["main"]))
    for item in _port_owner_details(host, port):
        executable = os.path.normcase(str(item.get("ExecutablePath") or ""))
        command = os.path.normcase(str(item.get("CommandLine") or ""))
        if executable == expected_python and expected_main in command:
            return True
    return False


def _wait_for_port(
    host: str,
    port: int,
    expected: bool,
    timeout: float = 10.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_open(host, port) is expected:
            return True
        time.sleep(0.2)
    return _port_open(host, port) is expected


def install_service(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> int:
    _require_windows()
    paths = service_paths()
    for key in ("python", "task_python", "main", "service"):
        if not paths[key].is_file():
            print(f"[error] Missing Windows service dependency: {paths[key]}", file=sys.stderr)
            return 1

    existed = task_exists()
    if not existed and _port_open(host, port):
        print(
            f"[error] Port {host}:{port} is already in use; the task was not changed",
            file=sys.stderr,
        )
        return 1
    if existed:
        _run_schtasks(["/End", "/TN", TASK_NAME])
        if not _wait_for_port(host, port, False):
            print(
                f"[error] Port {host}:{port} remained in use after stopping the existing task",
                file=sys.stderr,
            )
            return 1

    xml = _task_xml(paths, host, port)
    task_file = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".xml", encoding="utf-16", delete=False
        ) as handle:
            handle.write(xml)
            task_file = Path(handle.name)
        created = _run_schtasks(
            ["/Create", "/TN", TASK_NAME, "/XML", str(task_file), "/F"]
        )
    finally:
        if task_file is not None:
            task_file.unlink(missing_ok=True)

    if created.returncode != 0:
        print(
            created.stderr.strip()
            or created.stdout.strip()
            or "[error] Failed to create scheduled task",
            file=sys.stderr,
        )
        return 1
    started = _run_schtasks(["/Run", "/TN", TASK_NAME])
    if started.returncode != 0:
        print(
            started.stderr.strip()
            or started.stdout.strip()
            or "[error] Failed to start scheduled task",
            file=sys.stderr,
        )
        return 1
    if not _wait_for_port(host, port, True) or not _service_owns_port(paths, host, port):
        print(
            f"[error] Scheduled task started but {host}:{port} is not listening; "
            f"check {paths['stderr']}",
            file=sys.stderr,
        )
        return 1

    print(f"[ok] Windows logon task installed and listening at http://{host}:{port}/mcp")
    print(f"[log] {paths['log_dir']}")
    return 0


def start_service(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> int:
    _require_windows()
    paths = service_paths()
    if not task_exists():
        print("[error] Windows logon task is not installed", file=sys.stderr)
        return 1
    if _service_owns_port(paths, host, port):
        print("[info] MCP scheduled task is already running")
        return 0
    if _port_open(host, port):
        print(
            f"[error] Port {host}:{port} is owned by another process",
            file=sys.stderr,
        )
        return 1
    result = _run_schtasks(["/Run", "/TN", TASK_NAME])
    if (
        result.returncode == 0
        and _wait_for_port(host, port, True)
        and _service_owns_port(paths, host, port)
    ):
        print("[ok] MCP scheduled task is running")
        return 0
    print(
        result.stderr.strip() or f"[error] MCP did not listen on {host}:{port}",
        file=sys.stderr,
    )
    return 1


def stop_service(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> int:
    _require_windows()
    if not task_exists():
        print("[info] Windows logon task is not installed")
        return 0
    paths = service_paths()
    if not _port_open(host, port):
        print("[info] MCP scheduled task is already stopped")
        return 0
    if not _service_owns_port(paths, host, port):
        print(
            f"[error] Port {host}:{port} is owned by another process; it was not stopped",
            file=sys.stderr,
        )
        return 1
    result = _run_schtasks(["/End", "/TN", TASK_NAME])
    if result.returncode != 0:
        print(
            result.stderr.strip() or "[error] Failed to stop scheduled task",
            file=sys.stderr,
        )
        return 1
    if not _wait_for_port(host, port, False):
        print(f"[error] Port {host}:{port} is still listening", file=sys.stderr)
        return 1
    print("[ok] MCP scheduled task stopped; logon startup remains enabled")
    return 0


def status_service(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    json_mode: bool = False,
) -> int:
    _require_windows()
    installed = task_exists()
    listening = _port_open(host, port)
    paths = service_paths()
    configured = installed and _task_matches(paths, host, port)
    owned = listening and _service_owns_port(paths, host, port)
    if not installed:
        status = "not_installed"
    elif not configured:
        status = "stale_configuration"
    elif listening and not owned:
        status = "port_conflict"
    elif owned:
        status = "ready"
    else:
        status = "stopped"
    payload = {
        "ok": status == "ready",
        "status": status,
        "task_name": TASK_NAME,
        "task_installed": installed,
        "task_configuration_current": configured,
        "transport_ready": owned,
        "endpoint": f"http://{host}:{port}/mcp",
    }
    if json_mode:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    else:
        print(f"[service] {payload['status']} ({TASK_NAME})")
        print(f"[endpoint] {payload['endpoint']}")
    return 0 if payload["ok"] else 1


def uninstall_service(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> int:
    _require_windows()
    if not task_exists():
        print("[info] Windows logon task is already absent")
        return 0
    _run_schtasks(["/End", "/TN", TASK_NAME])
    result = _run_schtasks(["/Delete", "/TN", TASK_NAME, "/F"])
    if result.returncode != 0:
        print(
            result.stderr.strip() or "[error] Failed to remove scheduled task",
            file=sys.stderr,
        )
        return 1
    _wait_for_port(host, port, False)
    print("[ok] Windows logon task removed; project data was preserved")
    return 0


def run_service(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> int:
    _require_windows()
    paths = service_paths()
    paths["log_dir"].mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    with paths["stdout"].open("a", encoding="utf-8") as stdout, paths[
        "stderr"
    ].open("a", encoding="utf-8") as stderr:
        return subprocess.call(
            [
                str(paths["python"]),
                str(paths["main"]),
                "serve",
                "--host",
                host,
                "--port",
                str(port),
            ],
            cwd=paths["root"],
            env=env,
            stdout=stdout,
            stderr=stderr,
            creationflags=_no_window_creation_flags(),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Manage the current user's Windows MCP scheduled task"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("install", "start", "stop", "restart", "status", "uninstall", "run"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--host", default=DEFAULT_HOST)
        subparser.add_argument("--port", type=int, default=DEFAULT_PORT)
        if command == "status":
            subparser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "install":
        return install_service(args.host, args.port)
    if args.command == "start":
        return start_service(args.host, args.port)
    if args.command == "stop":
        return stop_service(args.host, args.port)
    if args.command == "restart":
        stopped = stop_service(args.host, args.port)
        return start_service(args.host, args.port) if stopped == 0 else stopped
    if args.command == "status":
        return status_service(args.host, args.port, args.json)
    if args.command == "uninstall":
        return uninstall_service(args.host, args.port)
    if args.command == "run":
        return run_service(args.host, args.port)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
