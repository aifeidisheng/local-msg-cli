#!/usr/bin/env python3
"""管理本机 MCP Server 的 macOS launchd 服务。

服务安装为当前用户的 LaunchAgent。所有路径都使用绝对路径，避免
launchd 依赖 shell、PATH 或已激活的虚拟环境。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import plistlib
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from runtime_guard import require_macos_execution_mode


DEFAULT_LABEL = "com.wechatdecrypt.light.mcp"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
STATUS_READY = "ready"
STATUS_WAITING = "waiting_for_wechat"
STATUS_STARTING = "starting"
STATUS_RECOVERING = "recovering"
STATUS_STOPPED = "stopped"
STATUS_STALE = "stale_configuration"
STATUS_CONFLICT = "port_conflict"


class ServiceAlreadyRunningError(RuntimeError):
    """已有 MCP 实例持有单实例锁。"""


@dataclass(frozen=True)
class LaunchJobInfo:
    loaded: bool
    state: str = ""
    pid: int | None = None
    program: str = ""
    last_exit_code: int | None = None


@dataclass(frozen=True)
class ServiceInspection:
    status: str
    job: LaunchJobInfo
    port_pids: frozenset[int]
    process_command: str = ""
    configured_root: str = ""


def project_dir() -> Path:
    """返回项目目录；打包启动器可以通过环境变量覆盖该目录。"""
    configured = os.environ.get("WECHAT_DECRYPT_APP_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parent


def service_paths(root: Path | None = None, home: Path | None = None) -> dict[str, Path]:
    root = (root or project_dir()).resolve()
    home = (home or Path.home()).expanduser().resolve()
    configured_data_dir = os.environ.get("WECHAT_DECRYPT_DATA_DIR")
    data_dir = (
        Path(configured_data_dir).expanduser().resolve()
        if configured_data_dir
        else home / "Library" / "Application Support" / "WeChatDecryptLight" / "data"
    )
    return {
        "root": root,
        "python": root / ".venv" / "bin" / "python3",
        "main": root / "main.py",
        "service": root / "service.py",
        "plist_dir": home / "Library" / "LaunchAgents",
        "plist": home / "Library" / "LaunchAgents" / f"{DEFAULT_LABEL}.plist",
        "state_dir": home / "Library" / "Application Support" / "WeChatDecryptLight" / "state",
        "lock": home / "Library" / "Application Support" / "WeChatDecryptLight" / "state" / "service.lock",
        "log_dir": home / "Library" / "Logs" / "WeChatDecryptLight",
        "stdout": home / "Library" / "Logs" / "WeChatDecryptLight" / "mcp.stdout.log",
        "stderr": home / "Library" / "Logs" / "WeChatDecryptLight" / "mcp.stderr.log",
        "data_dir": data_dir,
    }


def acquire_instance_lock(paths: dict[str, Path] | None = None, *, blocking: bool = False) -> int:
    """获取 MCP 单实例锁；返回的文件描述符必须在进程存活期间保持打开。"""
    import fcntl

    paths = paths or service_paths()
    paths["state_dir"].mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_fd = os.open(paths["lock"], os.O_CREAT | os.O_RDWR, 0o600)
    os.chmod(paths["lock"], 0o600)
    try:
        operation = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(lock_fd, operation)
    except BlockingIOError as exc:
        os.close(lock_fd)
        raise ServiceAlreadyRunningError("已有本地 MCP 实例正在运行") from exc

    os.ftruncate(lock_fd, 0)
    os.write(lock_fd, f"{os.getpid()}\n".encode("ascii"))
    os.fsync(lock_fd)
    # service.py 会通过 exec 切换为 main.py，锁必须跨 exec 保持。
    os.set_inheritable(lock_fd, True)
    return lock_fd


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
            "WECHAT_DECRYPT_DATA_DIR": str(paths["data_dir"]),
        },
        "StandardOutPath": str(paths["stdout"]),
        "StandardErrorPath": str(paths["stderr"]),
    }


def _parse_launch_job(output: str) -> LaunchJobInfo:
    """解析 launchctl print 的顶层作业状态。"""
    state_match = re.search(r"^\s*state = ([^\n]+)", output, re.MULTILINE)
    program_match = re.search(r"^\s*program = ([^\n]+)", output, re.MULTILINE)
    pid_match = re.search(r"^\s*pid = (\d+)\s*$", output, re.MULTILINE)
    exit_match = re.search(r"^\s*last exit code = (-?\d+)\s*$", output, re.MULTILINE)
    return LaunchJobInfo(
        loaded=True,
        state=state_match.group(1).strip() if state_match else "",
        pid=int(pid_match.group(1)) if pid_match else None,
        program=program_match.group(1).strip() if program_match else "",
        last_exit_code=int(exit_match.group(1)) if exit_match else None,
    )


def _job_info() -> LaunchJobInfo:
    result = _run_launchctl(["print", service_target()])
    if result.returncode != 0:
        return LaunchJobInfo(loaded=False)
    return _parse_launch_job(result.stdout)


def _read_plist(paths: dict[str, Path]) -> dict:
    try:
        with paths["plist"].open("rb") as plist_file:
            data = plistlib.load(plist_file)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, plistlib.InvalidFileException):
        return {}


def _configured_root(paths: dict[str, Path]) -> str:
    plist = _read_plist(paths)
    return str(plist.get("WorkingDirectory") or "")


def _configured_endpoint(paths: dict[str, Path]) -> tuple[str, int]:
    arguments = _read_plist(paths).get("ProgramArguments") or []
    host = DEFAULT_HOST
    port = DEFAULT_PORT
    try:
        host = str(arguments[arguments.index("--host") + 1])
        port = int(arguments[arguments.index("--port") + 1])
    except (ValueError, IndexError, TypeError):
        pass
    return host, port


def _plist_matches_current(
    paths: dict[str, Path],
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> bool:
    plist = _read_plist(paths)
    expected = build_plist(paths, host=host, port=port)
    return (
        plist.get("ProgramArguments") == expected["ProgramArguments"]
        and plist.get("WorkingDirectory") == expected["WorkingDirectory"]
        and (plist.get("EnvironmentVariables") or {}).get("WECHAT_DECRYPT_APP_DIR")
        == str(paths["root"])
        and (plist.get("EnvironmentVariables") or {}).get("WECHAT_DECRYPT_DATA_DIR")
        == str(paths["data_dir"])
    )


def _port_owner_pids(port: int) -> set[int]:
    """返回监听指定 TCP 端口的 PID；无法检查时拒绝假定端口安全。"""
    try:
        result = subprocess.run(
            ["/usr/sbin/lsof", "-nP", f"-tiTCP:{port}", "-sTCP:LISTEN"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("系统缺少 /usr/sbin/lsof，无法验证 MCP 端口归属") from exc

    if result.returncode not in (0, 1):
        raise RuntimeError(f"无法检查端口 {port} 的监听进程: {result.stderr.strip()}")
    return {int(line) for line in result.stdout.splitlines() if line.strip().isdigit()}


def _process_command(pid: int | None) -> str:
    if not pid:
        return ""
    result = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "command="],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def inspect_service(
    paths: dict[str, Path] | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> ServiceInspection:
    """综合 launchd、plist 和端口归属判断服务状态。"""
    paths = paths or service_paths()
    job = _job_info()
    port_pids = frozenset(_port_owner_pids(port))
    command = _process_command(job.pid)
    configured_root = _configured_root(paths)

    if not job.loaded:
        status = STATUS_CONFLICT if port_pids else STATUS_STOPPED
    elif not _plist_matches_current(paths, host=host, port=port) or (
        job.program and job.program != str(paths["python"])
    ):
        status = STATUS_STALE
    elif port_pids:
        status = STATUS_READY if job.pid in port_pids else STATUS_CONFLICT
    elif job.pid and str(paths["service"]) in command and " run" in command:
        status = STATUS_WAITING
    elif job.pid:
        status = STATUS_STARTING
    else:
        status = STATUS_RECOVERING

    return ServiceInspection(
        status=status,
        job=job,
        port_pids=port_pids,
        process_command=command,
        configured_root=configured_root,
    )


def run_service(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, retry_interval: int = 10) -> int:
    """立即启动 MCP Server，供 launchd 作为常驻入口调用。

    MCP 启动不依赖微信运行或版本门禁通过。版本门禁在每次工具调用时
    由 mcp_server.py 的 _guarded_tool 装饰器执行 check_or_raise，
    调用失败时错误信息会直接返回给会话，用户可以看到具体原因。
    """
    paths = service_paths()
    try:
        # launchd 入口等待已有手动实例释放锁，避免 KeepAlive 形成重启风暴。
        lock_fd = acquire_instance_lock(paths, blocking=True)
    except ServiceAlreadyRunningError as exc:
        print(f"[错误] {exc}", file=sys.stderr, flush=True)
        return 3

    os.environ["WECHAT_DECRYPT_SERVICE_LOCK_HELD"] = "1"
    print(f"[*] 常驻服务已启动，直接启动 MCP Server: {host}:{port}", flush=True)
    print("[*] 版本门禁将在工具调用时按需检查", flush=True)

    try:
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
    finally:
        os.close(lock_fd)


def _run_launchctl(args: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/launchctl", *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _is_loaded() -> bool:
    return _job_info().loaded


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise RuntimeError("launchd service management is only supported on macOS")


def _write_plist(paths: dict[str, Path], plist: dict) -> None:
    """使用同目录原子替换写入 plist，避免中途失败留下半个配置文件。"""
    temporary = paths["plist"].with_name(f".{paths['plist'].name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as plist_file:
            plistlib.dump(plist, plist_file, sort_keys=False)
        os.chmod(temporary, 0o600)
        os.replace(temporary, paths["plist"])
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_plist_bytes(paths: dict[str, Path], content: bytes) -> None:
    temporary = paths["plist"].with_name(f".{paths['plist'].name}.{os.getpid()}.rollback")
    try:
        with temporary.open("wb") as plist_file:
            plist_file.write(content)
        os.chmod(temporary, 0o600)
        os.replace(temporary, paths["plist"])
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _bootout_loaded_service() -> subprocess.CompletedProcess[str] | None:
    if not _job_info().loaded:
        return None
    return _run_launchctl(["bootout", service_target()])


def _wait_for_port_release(port: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _port_owner_pids(port):
            return True
        time.sleep(0.1)
    return not _port_owner_pids(port)


def _wait_for_service_inspection(
    paths: dict[str, Path],
    host: str,
    port: int,
    timeout: float = 15.0,
) -> ServiceInspection:
    deadline = time.monotonic() + timeout
    latest = inspect_service(paths, host=host, port=port)
    while time.monotonic() < deadline:
        latest = inspect_service(paths, host=host, port=port)
        if latest.status in (STATUS_READY, STATUS_WAITING, STATUS_CONFLICT, STATUS_STALE):
            return latest
        time.sleep(0.25)
    return latest


def _format_pid_commands(pids: set[int] | frozenset[int]) -> str:
    details = []
    for pid in sorted(pids):
        command = _process_command(pid)
        details.append(f"PID {pid}: {command or '无法读取命令行'}")
    return "\n       ".join(details)


def _rollback_install(
    paths: dict[str, Path],
    previous_plist: bytes | None,
    previous_loaded: bool,
) -> None:
    """新服务验证失败时恢复安装前的 plist 和加载状态。"""
    _bootout_loaded_service()
    if previous_plist is None:
        try:
            paths["plist"].unlink()
        except FileNotFoundError:
            pass
        return

    _write_plist_bytes(paths, previous_plist)
    if previous_loaded:
        restored = _run_launchctl(["bootstrap", launch_domain(), str(paths["plist"])])
        if restored.returncode != 0:
            print(f"[警告] 旧服务配置恢复后重新加载失败: {restored.stderr.strip()}", file=sys.stderr)


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

    previous_plist = paths["plist"].read_bytes() if paths["plist"].is_file() else None
    previous_job = _job_info()
    try:
        port_pids = _port_owner_pids(port)
    except RuntimeError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1

    # 只允许当前 launchd 作业占用目标端口；其他进程一律拒绝自动终止。
    if port_pids and (previous_job.pid is None or port_pids != {previous_job.pid}):
        print(f"[错误] 端口 {host}:{port} 已被其他进程占用，未修改现有服务。", file=sys.stderr)
        print(f"       {_format_pid_commands(port_pids)}", file=sys.stderr)
        return 1

    stopped = _bootout_loaded_service()
    if stopped is not None and stopped.returncode != 0:
        print(f"[错误] 无法停止旧 LaunchAgent: {stopped.stderr.strip()}", file=sys.stderr)
        return stopped.returncode or 1
    if port_pids:
        try:
            released = _wait_for_port_release(port)
        except RuntimeError as exc:
            print(f"[错误] {exc}", file=sys.stderr)
            _rollback_install(paths, previous_plist, previous_job.loaded)
            return 1
        if not released:
            print(f"[错误] 旧服务停止后端口 {host}:{port} 仍未释放", file=sys.stderr)
            _rollback_install(paths, previous_plist, previous_job.loaded)
            return 1

    try:
        _write_plist(paths, build_plist(paths, host=host, port=port))
    except OSError as exc:
        print(f"[错误] 无法写入 LaunchAgent 配置: {exc}", file=sys.stderr)
        _rollback_install(paths, previous_plist, previous_job.loaded)
        return 1

    loaded = _run_launchctl(["bootstrap", launch_domain(), str(paths["plist"])])
    if loaded.returncode != 0:
        print(f"[错误] launchd 加载失败: {loaded.stderr.strip()}", file=sys.stderr)
        _rollback_install(paths, previous_plist, previous_job.loaded)
        return loaded.returncode or 1

    try:
        inspection = _wait_for_service_inspection(paths, host, port)
    except RuntimeError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        _rollback_install(paths, previous_plist, previous_job.loaded)
        return 1
    if inspection.status == STATUS_WAITING:
        print(f"[提示] 常驻服务已加载，但微信尚未就绪，MCP 暂未监听 {host}:{port}")
        print("       微信启动并通过版本门禁后，服务会自动开始监听。")
    elif inspection.status == STATUS_READY:
        print(f"[完成] 已安装并启动 macOS 常驻服务: {DEFAULT_LABEL}")
    else:
        print(f"[错误] 新服务未通过启动验证，状态: {inspection.status}", file=sys.stderr)
        if inspection.port_pids:
            print(f"       {_format_pid_commands(inspection.port_pids)}", file=sys.stderr)
        _rollback_install(paths, previous_plist, previous_job.loaded)
        print("       已恢复安装前的服务配置。", file=sys.stderr)
        return 1
    print(f"       MCP 地址: http://{host}:{port}/mcp")
    print(f"       日志目录: {paths['log_dir']}")
    print("       电脑重启或重新登录后会自动启动，无需再次执行命令。")
    return 0


def start_service() -> int:
    _require_macos()
    paths = service_paths()
    if not paths["plist"].is_file():
        print("[错误] 服务尚未安装，请先运行: python3 service.py install", file=sys.stderr)
        return 1
    host, port = _configured_endpoint(paths)
    if not _plist_matches_current(paths, host=host, port=port):
        print("[错误] 已安装的 LaunchAgent 指向另一份项目或使用了不同端口。", file=sys.stderr)
        print(f"       当前配置目录: {_configured_root(paths) or '未知'}", file=sys.stderr)
        print(f"       当前项目目录: {paths['root']}", file=sys.stderr)
        print("       请在当前项目中执行 service.py install 完成迁移。", file=sys.stderr)
        return 1

    try:
        before = inspect_service(paths, host=host, port=port)
    except RuntimeError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1
    if before.status in (STATUS_READY, STATUS_WAITING):
        print("[提示] MCP 常驻服务已经运行")
        return 0
    if before.status == STATUS_STALE:
        print("[错误] launchd 当前仍加载旧项目配置，请执行 service.py install 完成迁移", file=sys.stderr)
        return 1
    if before.status == STATUS_CONFLICT:
        print(f"[错误] 端口 {host}:{port} 被非当前服务进程占用", file=sys.stderr)
        print(f"       {_format_pid_commands(before.port_pids)}", file=sys.stderr)
        return 1

    if not before.job.loaded:
        loaded = _run_launchctl(["bootstrap", launch_domain(), str(paths["plist"])])
        if loaded.returncode != 0:
            print(f"[错误] launchd 加载失败: {loaded.stderr.strip()}", file=sys.stderr)
            return loaded.returncode or 1
    else:
        result = _run_launchctl(["kickstart", "-k", service_target()])
        if result.returncode != 0:
            print(f"[错误] 启动失败: {result.stderr.strip()}", file=sys.stderr)
            return result.returncode or 1

    try:
        inspection = _wait_for_service_inspection(paths, host, port)
    except RuntimeError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1
    if inspection.status == STATUS_READY:
        print("[完成] MCP Server 已启动")
        return 0
    if inspection.status == STATUS_WAITING:
        print("[提示] 常驻服务已启动，正在等待微信和版本门禁就绪")
        return 0
    print(f"[错误] 服务启动后状态异常: {inspection.status}", file=sys.stderr)
    return 1


def stop_service() -> int:
    _require_macos()
    if not _is_loaded():
        print("[提示] 服务未安装或已经停止")
        return 0
    result = _run_launchctl(["bootout", service_target()])
    if result.returncode != 0 and _is_loaded():
        print(f"[错误] 停止失败: {result.stderr.strip()}", file=sys.stderr)
        return result.returncode or 1
    print("[完成] MCP Server 已停止；保留登录自启配置，下次登录仍会加载")
    return 0


def restart_service() -> int:
    _require_macos()
    paths = service_paths()
    host, port = _configured_endpoint(paths)
    if not paths["plist"].is_file() or not _plist_matches_current(paths, host=host, port=port):
        print("[错误] 当前项目不是已安装服务的运行目录，请先执行 service.py install", file=sys.stderr)
        return 1
    stopped = stop_service()
    if stopped != 0:
        return stopped
    return start_service()


def uninstall_service() -> int:
    _require_macos()
    paths = service_paths()
    stopped = _bootout_loaded_service()
    if stopped is not None and stopped.returncode != 0 and _is_loaded():
        print(f"[错误] 无法停止常驻服务，未删除 LaunchAgent: {stopped.stderr.strip()}", file=sys.stderr)
        return stopped.returncode or 1
    try:
        paths["plist"].unlink()
    except FileNotFoundError:
        pass
    print("[完成] 已移除 macOS 常驻服务（不会删除项目、配置或解密数据）")
    return 0


def status_service(host: str | None = None, port: int | None = None) -> int:
    _require_macos()
    paths = service_paths()
    configured_host, configured_port = _configured_endpoint(paths)
    host = host or configured_host
    port = port if port is not None else configured_port
    try:
        inspection = inspect_service(paths, host=host, port=port)
    except RuntimeError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1
    status_labels = {
        STATUS_READY: "运行正常",
        STATUS_WAITING: "等待微信和版本门禁",
        STATUS_STARTING: "正在启动",
        STATUS_RECOVERING: "正在恢复",
        STATUS_STOPPED: "未运行",
        STATUS_STALE: "配置指向其他项目",
        STATUS_CONFLICT: "端口被其他进程占用",
    }
    print(f"[服务] {status_labels[inspection.status]} ({DEFAULT_LABEL})")
    if inspection.job.pid:
        print(f"[进程] PID {inspection.job.pid} ({inspection.job.state or '状态未知'})")
    print(
        f"[端口] {'监听中' if inspection.port_pids else '未监听'} "
        f"({host}:{port}{', PID ' + ','.join(map(str, sorted(inspection.port_pids))) if inspection.port_pids else ''})"
    )
    if inspection.status == STATUS_STALE:
        print(f"[当前配置目录] {inspection.configured_root or '未知'}")
        print(f"[期望项目目录] {paths['root']}")
    if inspection.status == STATUS_CONFLICT and inspection.port_pids:
        print(f"[占用进程] {_format_pid_commands(inspection.port_pids)}")
    print(f"[配置] {paths['plist']}")
    print(f"[日志] {paths['log_dir']}")
    print(f"[数据] {'已初始化' if _has_valid_keys(paths) else '未初始化'}")
    return 0 if inspection.status in (STATUS_READY, STATUS_WAITING, STATUS_STARTING) else 1


def _has_valid_keys(paths: dict[str, Path]) -> bool:
    """Check initialization readiness without exposing key names or values."""
    config_path = paths["data_dir"] / "config.json"
    keys_path = paths["data_dir"] / "all_keys.json"
    try:
        with config_path.open(encoding="utf-8") as config_file:
            configured = json.load(config_file).get("keys_file")
        if configured:
            candidate = Path(str(configured)).expanduser()
            keys_path = candidate if candidate.is_absolute() else paths["data_dir"] / candidate
    except (FileNotFoundError, OSError, json.JSONDecodeError, AttributeError):
        pass

    try:
        with keys_path.open(encoding="utf-8") as key_file:
            payload = json.load(key_file)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    return any(
        isinstance(value, dict)
        and bool(re.fullmatch(r"[0-9a-fA-F]{64}", str(value.get("enc_key") or "")))
        for name, value in payload.items()
        if not str(name).startswith("_")
    )


def service_status_payload(
    paths: dict[str, Path],
    inspection: ServiceInspection,
    host: str,
    port: int,
) -> dict:
    """生成不包含消息、密钥或本机账号信息的机器可读状态。"""
    transport_ready = inspection.status == STATUS_READY
    initialized = _has_valid_keys(paths)
    return {
        "ok": inspection.status in (STATUS_READY, STATUS_WAITING, STATUS_STARTING),
        "status": inspection.status,
        "transport_ready": transport_ready,
        "initialized": initialized,
        "query_ready": transport_ready and initialized,
        "label": DEFAULT_LABEL,
        "endpoint": f"http://{host}:{port}/mcp",
        "launchd_loaded": inspection.job.loaded,
        "launchd_pid": inspection.job.pid,
        "port_pids": sorted(inspection.port_pids),
        "runtime_dir": str(paths["root"]),
        "data_dir": str(paths["data_dir"]),
        "plist": str(paths["plist"]),
        "log_dir": str(paths["log_dir"]),
    }


def status_service_json(host: str | None = None, port: int | None = None) -> int:
    """输出供本地安装器和 Agent 使用的单行 JSON 状态。"""
    _require_macos()
    paths = service_paths()
    configured_host, configured_port = _configured_endpoint(paths)
    host = host or configured_host
    port = port if port is not None else configured_port
    try:
        inspection = inspect_service(paths, host=host, port=port)
        payload = service_status_payload(paths, inspection, host, port)
    except RuntimeError as exc:
        payload = {"ok": False, "status": "inspection_failed", "error": str(exc)}
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0 if payload["ok"] else 1


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
    status.add_argument("--host", default=None)
    status.add_argument("--port", type=int, default=None)
    status.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    subparsers.add_parser("uninstall", help="移除常驻服务，不删除数据")
    run = subparsers.add_parser("run", help="等待微信就绪后启动 MCP，供 launchd 调用")
    run.add_argument("--host", default=DEFAULT_HOST)
    run.add_argument("--port", type=int, default=DEFAULT_PORT)
    run.add_argument("--retry-interval", type=int, default=10)

    args = parser.parse_args(argv)
    if args.command != "status":
        require_macos_execution_mode(f"service.py {args.command}", system=platform.system())
    if args.command == "install":
        return install_service(host=args.host, port=args.port)
    if args.command == "start":
        return start_service()
    if args.command == "stop":
        return stop_service()
    if args.command == "restart":
        return restart_service()
    if args.command == "status":
        if args.json:
            return status_service_json(host=args.host, port=args.port)
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
