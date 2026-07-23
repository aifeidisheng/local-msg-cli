#!/usr/bin/env python3
"""将本地消息 MCP 部署到独立运行目录，并管理其用户级常驻服务。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


APP_DIR_NAME = "WeChatDecryptLight"
MANIFEST_SCHEMA_VERSION = 2
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
REQUIRED_SOURCE_FILES = {
    "config.py",
    "installer.py",
    "main.py",
    "mcp_server.py",
    "requirements.txt",
    "service.py",
    "version-guard.policy.json",
}
MIGRATED_FILES = ("config.json", "all_keys.json")
MIGRATED_DIRS = ("decrypted", "decoded_images", "wechat_files", "mcp_cache")


class InstallerError(RuntimeError):
    """可向 Agent 安全展示的安装错误。"""


@dataclass(frozen=True)
class InstallLayout:
    root: Path
    runtime_dir: Path
    current: Path
    data_dir: Path
    state_dir: Path
    manifest: Path
    bin_dir: Path
    cli: Path


class Reporter:
    def __init__(self, json_mode: bool) -> None:
        self.json_mode = json_mode

    def progress(self, step: str, message: str) -> None:
        if self.json_mode:
            print(
                json.dumps(
                    {"event": "progress", "step": step, "message": message},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                file=sys.stderr,
                flush=True,
            )
        else:
            print(f"[{step}] {message}", flush=True)

    def result(self, payload: dict) -> None:
        if self.json_mode:
            print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2))


def default_layout(home: Path | None = None) -> InstallLayout:
    home = (home or Path.home()).expanduser().resolve()
    root = home / "Library" / "Application Support" / APP_DIR_NAME
    return InstallLayout(
        root=root,
        runtime_dir=root / "runtime",
        current=root / "runtime" / "current",
        data_dir=root / "data",
        state_dir=root / "state",
        manifest=root / "install.json",
        bin_dir=root / "bin",
        cli=root / "bin" / "wechat-decrypt-light",
    )


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    error_context: str,
    allow_failure: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            env=env,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise InstallerError(f"{error_context}：操作超时") from exc
    if result.returncode != 0 and not allow_failure:
        details = (result.stderr or result.stdout).strip().splitlines()
        tail = "\n".join(details[-12:])
        raise InstallerError(f"{error_context}（退出码 {result.returncode}）{': ' + tail if tail else ''}")
    return result


def _git(source: Path, *args: str, error_context: str) -> str:
    return _run(
        ["/usr/bin/git", "-C", str(source), *args],
        error_context=error_context,
    ).stdout.strip()


def _repository_identity(value: str) -> str:
    """把 HTTPS/SSH Git 地址归一为 host/path，避免仅因协议不同而误判。"""
    value = value.strip()
    if "://" in value:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        path = parsed.path.lstrip("/")
    elif "@" in value and ":" in value:
        host_part, path = value.rsplit(":", 1)
        host = host_part.rsplit("@", 1)[-1].lower()
    else:
        return value.removesuffix(".git").rstrip("/").lower()
    return f"{host}/{path.removesuffix('.git').rstrip('/')}".lower()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_branch(value: str) -> str:
    value = value.strip()
    result = _run(
        ["/usr/bin/git", "check-ref-format", "--branch", value],
        error_context="发布通道分支名称无效",
        allow_failure=True,
    )
    if result.returncode != 0:
        raise InstallerError(f"发布通道分支名称无效：{value or '<empty>'}")
    return value


def verify_source(
    source: Path,
    *,
    expected_repository: str,
    branch: str = "main",
    expected_commit: str | None = None,
    expected_installer_sha256: str | None = None,
    allow_dirty_source: bool = False,
) -> dict[str, str]:
    source = source.resolve()
    if not (source / ".git").exists():
        raise InstallerError("安装来源不是 Git 工作树")

    branch = _validate_branch(branch)
    commit = _git(source, "rev-parse", "HEAD", error_context="无法读取源码提交")
    repository = _git(
        source,
        "remote",
        "get-url",
        "origin",
        error_context="无法读取 origin 仓库地址",
    )
    branch_ref = f"refs/remotes/origin/{branch}"
    branch_commit = _git(
        source,
        "rev-parse",
        "--verify",
        f"{branch_ref}^{{commit}}",
        error_context=f"未找到 origin/{branch}，请按 main 发布通道安装流程重新拉取",
    )
    dirty = _git(
        source,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        error_context="无法检查源码完整性",
    )
    installer_hash = _sha256(source / "installer.py")

    if commit.lower() != branch_commit.lower():
        raise InstallerError(
            f"当前源码不是 origin/{branch} 的发布版本："
            f"发布提交 {branch_commit}，实际 {commit}"
        )
    if expected_commit and commit.lower() != expected_commit.lower():
        raise InstallerError(f"源码提交不匹配：期望 {expected_commit}，实际 {commit}")
    if _repository_identity(repository) != _repository_identity(expected_repository):
        raise InstallerError("origin 仓库与指定发布仓库不匹配")
    if expected_installer_sha256 and installer_hash.lower() != expected_installer_sha256.lower():
        raise InstallerError("installer.py 校验和与指定摘要不匹配")
    if dirty and not allow_dirty_source:
        raise InstallerError("源码工作树存在未提交或未跟踪文件，拒绝部署不可复现版本")

    return {
        "commit": commit,
        "repository": repository,
        "branch": branch,
        "installer_sha256": installer_hash,
    }


def _tracked_files(source: Path) -> list[Path]:
    raw = _run(
        ["/usr/bin/git", "-C", str(source), "ls-files", "-z"],
        error_context="无法读取 Git 文件清单",
    ).stdout
    files = [Path(item) for item in raw.split("\0") if item]
    names = {path.as_posix() for path in files}
    missing = sorted(REQUIRED_SOURCE_FILES - names)
    if missing:
        raise InstallerError(f"源码缺少运行文件：{', '.join(missing)}")
    return files


def copy_runtime(source: Path, destination: Path) -> None:
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    source_root = source.resolve()
    for relative in _tracked_files(source_root):
        source_path = source_root / relative
        if source_path.is_symlink():
            raise InstallerError(f"源码包含符号链接，拒绝部署：{relative}")
        target_path = destination / relative
        target_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)


def _create_runtime_environment(runtime: Path, python: Path) -> None:
    if sys.version_info < (3, 10) and python.resolve() == Path(sys.executable).resolve():
        raise InstallerError("需要 Python 3.10 或更高版本")
    _run(
        [str(python), "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"],
        error_context="指定的 Python 版本低于 3.10",
    )
    _run([str(python), "-m", "venv", str(runtime / ".venv")], error_context="创建独立 Python 环境失败")
    runtime_python = runtime / ".venv" / "bin" / "python3"
    _run(
        [str(runtime_python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(runtime / "requirements.txt")],
        error_context="安装 Python 依赖失败",
    )


def _build_macos_scanner(runtime: Path) -> None:
    source = runtime / "find_all_keys_macos.c"
    if not source.is_file():
        raise InstallerError("源码缺少 macOS 密钥扫描器")
    output = runtime / "find_all_keys_macos"
    _run(
        ["/usr/bin/cc", "-O2", "-o", str(output), str(source), "-framework", "Foundation"],
        cwd=runtime,
        error_context="编译 macOS 密钥扫描器失败，请先安装 Xcode Command Line Tools",
    )
    _run(["/usr/bin/codesign", "-s", "-", str(output)], error_context="签名 macOS 密钥扫描器失败")


def migrate_existing_data(source: Path, data_dir: Path) -> list[str]:
    """仅填充不存在的数据，不覆盖已安装版本的敏感数据。"""
    migrated: list[str] = []
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)
    for name in MIGRATED_FILES:
        source_path = source / name
        target_path = data_dir / name
        if source_path.is_file() and not target_path.exists():
            shutil.copy2(source_path, target_path)
            os.chmod(target_path, 0o600)
            migrated.append(name)
    for name in MIGRATED_DIRS:
        source_path = source / name
        target_path = data_dir / name
        if source_path.is_dir() and not target_path.exists():
            shutil.copytree(source_path, target_path)
            migrated.append(name)
    return migrated


def _read_manifest(layout: InstallLayout) -> dict:
    try:
        with layout.manifest.open(encoding="utf-8") as manifest_file:
            data = json.load(manifest_file)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_symlink(target: Path, link: Path) -> None:
    link.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = link.with_name(f".{link.name}.{os.getpid()}.tmp")
    try:
        temporary.symlink_to(target)
        os.replace(temporary, link)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_management_cli(layout: InstallLayout) -> None:
    """生成不依赖 Git 工作树的稳定管理入口。"""
    layout.bin_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    content = (
        "#!/bin/sh\n"
        f"ROOT={shlex.quote(str(layout.root))}\n"
        'exec "$ROOT/runtime/current/.venv/bin/python3" '
        '"$ROOT/runtime/current/installer.py" "$@"\n'
    )
    temporary = layout.cli.with_name(f".{layout.cli.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.chmod(temporary, 0o700)
        os.replace(temporary, layout.cli)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _runtime_env(runtime: Path, layout: InstallLayout) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "WECHAT_DECRYPT_APP_DIR": str(runtime),
            "WECHAT_DECRYPT_DATA_DIR": str(layout.data_dir),
            "WECHAT_DECRYPT_NONINTERACTIVE": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


def _service_command(
    runtime: Path,
    layout: InstallLayout,
    arguments: list[str],
    *,
    error_context: str,
) -> subprocess.CompletedProcess[str]:
    python = runtime / ".venv" / "bin" / "python3"
    if not python.is_file() or not (runtime / "service.py").is_file():
        raise InstallerError("已安装运行时不完整，请从可信 Git 版本重新安装")
    return _run(
        [str(python), str(runtime / "service.py"), *arguments],
        cwd=runtime,
        env=_runtime_env(runtime, layout),
        error_context=error_context,
    )


def install(args: argparse.Namespace, reporter: Reporter) -> dict:
    if platform.system().lower() != "darwin":
        raise InstallerError("当前独立常驻安装器仅支持 macOS")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        raise InstallerError("敏感本机 MCP 只允许监听回环地址")
    if not 1 <= args.port <= 65535:
        raise InstallerError("MCP 端口必须位于 1-65535")
    if args.expected_commit and not re.fullmatch(r"[0-9a-fA-F]{40}", args.expected_commit):
        raise InstallerError("指定的 commit 必须是完整 40 位 Git SHA")
    if args.expected_installer_sha256 and not re.fullmatch(
        r"[0-9a-fA-F]{64}", args.expected_installer_sha256
    ):
        raise InstallerError("指定的 installer SHA-256 格式错误")

    source = Path(args.source).expanduser().resolve()
    layout = default_layout(Path(args.home).expanduser() if args.home else None)
    reporter.progress("verify", f"校验 Git 来源和 {args.branch} 发布通道")
    source_info = verify_source(
        source,
        expected_repository=args.repository,
        branch=args.branch,
        expected_commit=args.expected_commit,
        expected_installer_sha256=args.expected_installer_sha256,
        allow_dirty_source=args.allow_dirty_source,
    )

    version = source_info["commit"]
    final_runtime = layout.runtime_dir / version
    if not final_runtime.exists():
        layout.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        staging = layout.runtime_dir / f".{version}.{os.getpid()}.staging"
        if staging.exists():
            raise InstallerError(f"安装暂存目录已存在：{staging}")
        try:
            reporter.progress("copy", "复制经过 Git 跟踪的运行文件")
            copy_runtime(source, staging)
            reporter.progress("runtime", "创建项目独立 Python 环境并安装依赖")
            _create_runtime_environment(staging, Path(args.python).expanduser())
            reporter.progress("build", "编译并签名 macOS 本地扫描器")
            _build_macos_scanner(staging)
            os.replace(staging, final_runtime)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    else:
        if not (final_runtime / ".venv" / "bin" / "python3").is_file():
            raise InstallerError("已存在的固定版本运行时不完整，拒绝直接复用")
        reporter.progress("runtime", "固定提交的运行时已存在，复用现有安装")

    reporter.progress("data", "准备独立数据目录并迁移已有本机数据")
    migrated = migrate_existing_data(source, layout.data_dir)
    old_manifest = _read_manifest(layout)
    installation_id = old_manifest.get("installation_id") or str(uuid.uuid4())
    old_current = layout.current.resolve() if layout.current.exists() else None
    _atomic_symlink(final_runtime, layout.current)
    _write_management_cli(layout)

    try:
        reporter.progress("service", "安装并验证用户级 LaunchAgent")
        _service_command(
            final_runtime,
            layout,
            ["install", "--host", args.host, "--port", str(args.port)],
            error_context="LaunchAgent 安装或启动验证失败",
        )
    except Exception:
        if old_current is not None:
            _atomic_symlink(old_current, layout.current)
        else:
            try:
                layout.current.unlink()
            except FileNotFoundError:
                pass
            try:
                layout.cli.unlink()
            except FileNotFoundError:
                pass
        raise

    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "installation_id": installation_id,
        "commit": version,
        "repository": source_info["repository"],
        "branch": source_info["branch"],
        "runtime_dir": str(final_runtime),
        "data_dir": str(layout.data_dir),
        "endpoint": f"http://{args.host}:{args.port}/mcp",
        "host": args.host,
        "port": args.port,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "management_cli": str(layout.cli),
    }
    _atomic_write_json(layout.manifest, manifest)
    status_payload = service_status(layout, final_runtime)
    return {
        "ok": bool(status_payload.get("ok")),
        "command": "install",
        "installation": manifest,
        "service": status_payload,
        "migrated": migrated,
        "next_step": "run_init_with_user_confirmation",
    }


def _installed_runtime(layout: InstallLayout, manifest: dict | None = None) -> Path:
    manifest = manifest or _read_manifest(layout)
    value = manifest.get("runtime_dir")
    if not value:
        raise InstallerError("未找到有效安装清单")
    runtime = Path(value).expanduser().resolve()
    if not runtime.is_dir():
        raise InstallerError("安装清单指向的运行目录不存在")
    return runtime


def service_status(layout: InstallLayout, runtime: Path) -> dict:
    python = runtime / ".venv" / "bin" / "python3"
    result = _run(
        [str(python), str(runtime / "service.py"), "status", "--json"],
        cwd=runtime,
        env=_runtime_env(runtime, layout),
        error_context="读取 LaunchAgent 状态失败",
        allow_failure=True,
    )
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise InstallerError("LaunchAgent 返回了无法解析的状态") from exc


def status(args: argparse.Namespace, reporter: Reporter) -> dict:
    layout = default_layout(Path(args.home).expanduser() if args.home else None)
    manifest = _read_manifest(layout)
    runtime = _installed_runtime(layout, manifest)
    reporter.progress("status", "核对安装清单、LaunchAgent、PID 和监听端口")
    service_payload = service_status(layout, runtime)
    return {
        "ok": bool(service_payload.get("ok")),
        "command": "status",
        "installation_id": manifest.get("installation_id"),
        "commit": manifest.get("commit") or manifest.get("version"),
        "branch": manifest.get("branch") or manifest.get("release_branch") or "main",
        "endpoint": manifest.get("endpoint"),
        "runtime_dir": str(runtime),
        "data_dir": str(layout.data_dir),
        "service": service_payload,
    }


def _remote_branch_commit(repository: str, branch: str) -> str:
    branch = _validate_branch(branch)
    if not repository or repository.startswith("-"):
        raise InstallerError("安装清单中的发布仓库地址无效")
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    result = _run(
        ["/usr/bin/git", "ls-remote", "--exit-code", repository, f"refs/heads/{branch}"],
        env=env,
        error_context=f"无法查询远端 {branch} 发布通道",
        timeout=15,
    )
    lines = [line.split() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1 or len(lines[0]) != 2 or not re.fullmatch(r"[0-9a-fA-F]{40}", lines[0][0]):
        raise InstallerError("远端发布分支返回了无法解析的提交信息")
    return lines[0][0].lower()


def check_update(args: argparse.Namespace, reporter: Reporter) -> dict:
    layout = default_layout(Path(args.home).expanduser() if args.home else None)
    manifest = _read_manifest(layout)
    _installed_runtime(layout, manifest)
    repository = str(manifest.get("repository") or "")
    branch = str(manifest.get("branch") or manifest.get("release_branch") or "main")
    installed_commit = str(manifest.get("commit") or manifest.get("version") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{40}", installed_commit):
        raise InstallerError("安装清单缺少有效的 Git commit，请重新安装当前版本")
    reporter.progress("update", f"查询远端 {branch} 发布通道")
    remote_commit = _remote_branch_commit(repository, branch)
    return {
        "ok": True,
        "command": "check-update",
        "installed_commit": installed_commit,
        "remote_commit": remote_commit,
        "branch": branch,
        "update_available": remote_commit != installed_commit,
    }


def _clone_branch(repository: str, branch: str, destination: Path) -> None:
    branch = _validate_branch(branch)
    if not repository or repository.startswith("-"):
        raise InstallerError("安装清单中的发布仓库地址无效")
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    _run(
        [
            "/usr/bin/git",
            "clone",
            "--quiet",
            "--depth",
            "1",
            "--branch",
            branch,
            "--single-branch",
            repository,
            str(destination),
        ],
        env=env,
        error_context=f"拉取远端 {branch} 发布版本失败",
        timeout=60,
    )


def _parse_json_result(result: subprocess.CompletedProcess[str], error_context: str) -> dict:
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise InstallerError(f"{error_context}：安装器返回了无法解析的结果") from exc
    if not isinstance(payload, dict):
        raise InstallerError(f"{error_context}：安装器返回了无效结果")
    if result.returncode != 0 or not payload.get("ok"):
        raise InstallerError(f"{error_context}：{payload.get('error') or '未知错误'}")
    return payload


def upgrade(args: argparse.Namespace, reporter: Reporter) -> dict:
    layout = default_layout(Path(args.home).expanduser() if args.home else None)
    manifest = _read_manifest(layout)
    _installed_runtime(layout, manifest)
    repository = str(manifest.get("repository") or "")
    branch = str(manifest.get("branch") or manifest.get("release_branch") or "main")
    installed_commit = str(manifest.get("commit") or manifest.get("version") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{40}", installed_commit):
        raise InstallerError("安装清单缺少有效的 Git commit，请重新安装当前版本")

    reporter.progress("update", f"检查远端 {branch} 发布通道")
    remote_commit = _remote_branch_commit(repository, branch)
    if remote_commit == installed_commit:
        return {
            "ok": True,
            "command": "upgrade",
            "upgraded": False,
            "commit": installed_commit,
            "message": "当前已是最新版本",
        }

    with tempfile.TemporaryDirectory(prefix="wechat-decrypt-light-upgrade-") as temporary:
        source = Path(temporary) / "source"
        reporter.progress("download", f"拉取 {branch} 最新发布版本")
        _clone_branch(repository, branch, source)
        source_info = verify_source(
            source,
            expected_repository=repository,
            branch=branch,
        )
        reporter.progress("install", f"升级到提交 {source_info['commit'][:12]}")
        result = _run(
            [
                sys.executable,
                str(source / "installer.py"),
                "--home",
                str(Path(args.home).expanduser()) if args.home else str(Path.home()),
                "install",
                "--json",
                "--source",
                str(source),
                "--repository",
                repository,
                "--branch",
                branch,
                "--expected-commit",
                source_info["commit"],
                "--host",
                str(manifest.get("host") or DEFAULT_HOST),
                "--port",
                str(manifest.get("port") or DEFAULT_PORT),
            ],
            error_context="执行新版本安装器失败",
            allow_failure=True,
        )
        install_payload = _parse_json_result(result, "升级失败")

    installation = install_payload.get("installation") or {}
    return {
        "ok": True,
        "command": "upgrade",
        "upgraded": True,
        "from_commit": installed_commit,
        "to_commit": installation.get("commit"),
        "installation": installation,
        "service": install_payload.get("service"),
    }


def repair(args: argparse.Namespace, reporter: Reporter) -> dict:
    layout = default_layout(Path(args.home).expanduser() if args.home else None)
    manifest = _read_manifest(layout)
    runtime = _installed_runtime(layout, manifest)
    host = str(manifest.get("host") or DEFAULT_HOST)
    port = int(manifest.get("port") or DEFAULT_PORT)
    reporter.progress("repair", "重新生成 LaunchAgent 并执行完整启动验证")
    _service_command(
        runtime,
        layout,
        ["install", "--host", host, "--port", str(port)],
        error_context="LaunchAgent 修复失败",
    )
    return {
        "ok": True,
        "command": "repair",
        "installation_id": manifest.get("installation_id"),
        "service": service_status(layout, runtime),
    }


def initialize(args: argparse.Namespace, reporter: Reporter) -> dict:
    """在用户单独确认敏感操作后执行初始化，并重新验证常驻服务。"""
    layout = default_layout(Path(args.home).expanduser() if args.home else None)
    manifest = _read_manifest(layout)
    runtime = _installed_runtime(layout, manifest)
    runtime_python = runtime / ".venv" / "bin" / "python3"
    env = _runtime_env(runtime, layout)
    env["WECHAT_DECRYPT_SKIP_SERVICE_INSTALL"] = "1"
    reporter.progress("initialize", "执行密钥提取和本地数据库预解密")
    _run(
        [str(runtime_python), str(runtime / "main.py"), "init"],
        cwd=runtime,
        env=env,
        error_context="本机消息数据初始化失败",
    )
    host = str(manifest.get("host") or DEFAULT_HOST)
    port = int(manifest.get("port") or DEFAULT_PORT)
    reporter.progress("service", "初始化完成，重新安装并验证 LaunchAgent")
    _service_command(
        runtime,
        layout,
        ["install", "--host", host, "--port", str(port)],
        error_context="初始化完成，但 LaunchAgent 启动验证失败",
    )
    service_payload = service_status(layout, runtime)
    return {
        "ok": service_payload.get("status") == "ready",
        "command": "initialize",
        "installation_id": manifest.get("installation_id"),
        "endpoint": manifest.get("endpoint"),
        "service": service_payload,
        "next_step": "register_with_mcporter" if service_payload.get("status") == "ready" else "wait_until_ready",
    }


def uninstall(args: argparse.Namespace, reporter: Reporter) -> dict:
    layout = default_layout(Path(args.home).expanduser() if args.home else None)
    manifest = _read_manifest(layout)
    runtime = _installed_runtime(layout, manifest)
    reporter.progress("uninstall", "停止并移除用户级 LaunchAgent")
    _service_command(runtime, layout, ["uninstall"], error_context="LaunchAgent 卸载失败")
    removed_runtime = False
    if args.remove_runtime:
        reporter.progress("uninstall", "删除已安装运行时，保留敏感数据目录")
        shutil.rmtree(layout.runtime_dir)
        try:
            layout.manifest.unlink()
        except FileNotFoundError:
            pass
        removed_runtime = True
    return {
        "ok": True,
        "command": "uninstall",
        "service_removed": True,
        "runtime_removed": removed_runtime,
        "data_preserved": True,
        "data_dir": str(layout.data_dir),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="安装和维护本机消息 MCP")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--home", default=None, help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)

    install_parser = subparsers.add_parser("install", help="部署独立运行时并安装 LaunchAgent")
    install_parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    install_parser.add_argument("--source", default=str(Path(__file__).resolve().parent))
    install_parser.add_argument(
        "--repository",
        "--expected-repository",
        dest="repository",
        required=True,
        help="独立 MCP 的发布仓库地址",
    )
    install_parser.add_argument(
        "--branch",
        "--release-branch",
        dest="branch",
        default="main",
        help="受保护的发布通道分支，默认 main",
    )
    install_parser.add_argument("--expected-commit", default=None, help="可选的额外 commit 固定校验")
    install_parser.add_argument(
        "--expected-installer-sha256",
        default=None,
        help="可选的额外安装器摘要校验",
    )
    install_parser.add_argument("--python", default=sys.executable)
    install_parser.add_argument("--host", default=DEFAULT_HOST)
    install_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    install_parser.add_argument("--allow-dirty-source", action="store_true", help=argparse.SUPPRESS)

    status_parser = subparsers.add_parser("status", help="读取安装和服务状态")
    status_parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    update_parser = subparsers.add_parser("check-update", help="检查 main 发布通道是否有新版本")
    update_parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    upgrade_parser = subparsers.add_parser("upgrade", help="经用户确认后升级到 main 最新版本")
    upgrade_parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    repair_parser = subparsers.add_parser("repair", help="按安装清单修复 LaunchAgent")
    repair_parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    initialize_parser = subparsers.add_parser("initialize", help="经用户确认后提取密钥并预解密本机数据库")
    initialize_parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    uninstall_parser = subparsers.add_parser("uninstall", help="卸载 LaunchAgent，默认保留全部数据和运行时")
    uninstall_parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    uninstall_parser.add_argument("--remove-runtime", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in argv
    argv = [argument for argument in argv if argument != "--json"]
    parser = build_parser()
    args = parser.parse_args(argv)
    args.json = json_mode
    reporter = Reporter(json_mode)
    try:
        handlers = {
            "install": install,
            "status": status,
            "check-update": check_update,
            "upgrade": upgrade,
            "repair": repair,
            "initialize": initialize,
            "uninstall": uninstall,
        }
        payload = handlers[args.command](args, reporter)
        reporter.result(payload)
        return 0 if payload.get("ok") else 1
    except InstallerError as exc:
        reporter.result({"ok": False, "command": args.command, "error": str(exc)})
        return 1
    except Exception as exc:
        reporter.result(
            {
                "ok": False,
                "command": args.command,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
