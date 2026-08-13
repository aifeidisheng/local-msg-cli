#!/usr/bin/env python3
"""将本地消息 MCP 部署到独立运行目录，并管理其用户级常驻服务。"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import platform
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from install_protocol import (
    MCP_CONNECTOR_DISPLAY_NAME,
    MCP_CONNECTOR_NAME,
    MCP_CONNECTOR_TRANSPORT,
)
from runtime_guard import INSTALLED_RUNTIME_MARKER


APP_DIR_NAME = "WeChatDecryptLight"
MANIFEST_SCHEMA_VERSION = 3
ACTIVATION_SCHEMA_VERSION = 1
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
REQUIRED_SOURCE_FILES = {
    "config.py",
    "installer.py",
    "install_protocol.py",
    "main.py",
    "mcp_server.py",
    "requirements.txt",
    "service.py",
    "runtime_guard.py",
    "version-guard.policy.json",
    "wechat-release-catalog.json",
}
MIGRATED_FILES = ("config.json", "all_keys.json")
MIGRATED_DIRS = ("decrypted", "decoded_images", "wechat_files", "mcp_cache")
APP_MANAGEMENT_SETTINGS_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_AppBundles"
)


class InstallerError(RuntimeError):
    """可向 Agent 安全展示、并可携带恢复动作的安装错误。"""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "operation_failed",
        next_action: str | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.next_action = next_action
        self.details = details or {}


@dataclass(frozen=True)
class InstallLayout:
    home: Path
    root: Path
    runtime_dir: Path
    current: Path
    data_dir: Path
    state_dir: Path
    activation_state: Path
    manifest: Path
    bin_dir: Path
    cli: Path


class Reporter:
    def __init__(self, json_mode: bool) -> None:
        self.json_mode = json_mode

    def progress(self, step: str, message: str, extra: dict | None = None) -> None:
        """Report a step, optionally carrying transfer progress fields.

        ``extra`` is for long-running work such as a several-hundred-MB download,
        where a silent stretch is indistinguishable from a hang. Reserved keys are
        ``percent``, ``transferred_bytes``, ``total_bytes``,
        ``speed_bytes_per_second`` and ``eta_seconds``; ``event``, ``step`` and
        ``message`` cannot be overridden. Omitting ``extra`` keeps the previous
        output byte for byte.
        """
        if self.json_mode:
            payload = {"event": "progress", "step": step, "message": message}
            for key, value in (extra or {}).items():
                if key not in {"event", "step", "message"} and value is not None:
                    payload[key] = value
            print(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
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


def _plain_user_message(error: InstallerError) -> str:
    """Return the small amount of context a non-technical user needs.

    The exception text and structured details remain available to the Agent for
    diagnostics; this field is deliberately safe to show in the normal flow.
    """
    code = error.error_code
    details = error.details or {}
    messages = {
        "wechat_app_not_found": "没有找到个人版微信。请确认已安装，无需登录。",
        "wechat_not_running": "微信已准备好。请打开并登录，完成后告诉我。",
        "wechat_not_adhoc_signed": (
            "下一步需要对本机微信做一次兼容设置。请先完全退出微信，完成后告诉我。"
            if details.get("wechat_running")
            else "下一步需要对本机微信做一次兼容设置，确认后可能出现一次系统确认。"
        ),
        "wechat_must_quit_for_resign": "请先完全退出微信，完成后告诉我。",
        "wechat_must_quit_for_replace": "请先完全退出微信，完成后我会继续安装。",
        "wechat_resign_confirmation_required": (
            "为了让本地消息服务正常工作，需要完成一次微信兼容设置。"
            "这只影响本机，确认后即可继续。"
        ),
        "app_management_permission_required": (
            "请在“系统设置 → 隐私与安全性 → App 管理”中，允许"
            f"“{details.get('responsible_app') or '当前安装应用'}”管理其他应用，完成后告诉我。"
        ),
        "administrator_authorization_cancelled": "系统确认未完成。请按提示允许操作，或稍后重试。",
        "wechat_process_access_failed": "暂时无法读取微信状态。请保持微信打开并已登录，然后重试。",
        "wechat_database_not_found": "暂时找不到微信数据。请确认微信已登录，然后重试。",
        "wechat_account_not_found": "未找到可用的微信账号。请重新选择账号后再继续。",
        "initialization_required": "还需要先完成微信准备，暂时无法继续。",
        "wechat_resign_failed": "微信准备未完成，请稍后重试。",
        "wechat_resign_verification_failed": "微信准备未完成，请稍后重试。",
        "version_guard_failed": "当前微信未通过兼容性检查，安装已暂停。",
        "release_artifact_integrity_mismatch": (
            "下载文件未通过完整性检查，已停止操作，当前微信没有被修改。"
        ),
        "release_artifact_not_verified": "安装包还没有准备好，请重新下载后继续。",
        "download_confirmation_required": "下载兼容版本前需要你的确认。",
        "replace_confirmation_required": "替换微信前需要你的确认。",
        "download_integrity_metadata_missing": "暂时无法安全准备安装包，请稍后重试。",
        "version_policy_unavailable": "暂时无法确认微信兼容性，安装已暂停。",
        "wechat_quit_failed": "请先完全退出微信，完成后我会继续安装。",
        "replace_wechat_failed": "微信替换没有完成，原来的微信已恢复。",
        "replace_wechat_verification_failed": "微信替换没有完成，原来的微信已恢复。",
        "unexpected_management_error": "安装暂时没有完成，请稍后重试。",
    }
    if code == "version_not_allowed":
        detected = details.get("detected_version")
        version_text = f" {detected}" if detected else ""
        return (
            f"当前微信版本{version_text}暂不支持。"
            "继续使用本地消息服务需要换成兼容版本，这会替换当前微信应用并需要重新登录。"
        )
    return messages.get(
        code,
        "安装暂时没有完成，请稍后重试；如仍失败，可查看技术诊断信息。",
    )


def default_layout(home: Path | None = None) -> InstallLayout:
    home = (home or Path.home()).expanduser().resolve()
    root = home / "Library" / "Application Support" / APP_DIR_NAME
    return InstallLayout(
        home=home,
        root=root,
        runtime_dir=root / "runtime",
        current=root / "runtime" / "current",
        data_dir=root / "data",
        state_dir=root / "state",
        activation_state=root / "state" / "activation.json",
        manifest=root / "install.json",
        bin_dir=root / "bin",
        cli=root / "bin" / "wechat-decrypt-light",
    )


def _connector_protocol(endpoint: str | None) -> dict[str, str | None]:
    """Return the canonical Desktop registration contract."""
    return {
        "name": MCP_CONNECTOR_NAME,
        "display_name": MCP_CONNECTOR_DISPLAY_NAME,
        "transport": MCP_CONNECTOR_TRANSPORT,
        "endpoint": endpoint,
    }


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


def _require_non_root_management() -> None:
    if platform.system().lower() == "darwin" and hasattr(os, "geteuid") and os.geteuid() == 0:
        raise InstallerError(
            "不要使用 sudo 运行 wechat-decrypt-light；管理 CLI 会仅为密钥扫描器请求管理员权限",
            error_code="management_cli_must_not_run_as_root",
            next_action="run_the_same_command_without_sudo",
        )


def _normalize_legacy_data_files(layout: InstallLayout) -> list[str]:
    """Repair root-owned legacy files without requesting authorization again."""
    repaired: list[str] = []
    current_uid = os.getuid()
    for path in (layout.data_dir / "config.json", layout.data_dir / "all_keys.json"):
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise InstallerError(
                f"本机数据文件类型异常，拒绝自动处理：{path.name}",
                error_code="data_file_type_invalid",
                next_action="reinstall_current_release_and_report_the_structured_error",
            )
        if metadata.st_uid == current_uid:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            continue

        # File ownership does not control replacement on Unix; a user-writable
        # data directory lets us preserve the bytes via an atomic replacement.
        try:
            payload = path.read_bytes()
            fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        except OSError as exc:
            raise InstallerError(
                f"旧版安装遗留的 {path.name} 不属于当前用户，且无法无提权修复",
                error_code="data_file_ownership_invalid",
                next_action="report_data_file_ownership_error_without_privileged_repair",
            ) from exc
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            repaired.append(path.name)
        except OSError as exc:
            raise InstallerError(
                f"旧版安装遗留的 {path.name} 不属于当前用户，且无法无提权修复",
                error_code="data_file_ownership_invalid",
                next_action="report_data_file_ownership_error_without_privileged_repair",
            ) from exc
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return repaired


def _account_id(db_dir: Path) -> str:
    return db_dir.parent.name


def _account_activity(db_dir: Path) -> float:
    target = db_dir / "message"
    try:
        return (target if target.is_dir() else db_dir).stat().st_mtime
    except OSError:
        return 0


def _discover_macos_accounts(home: Path) -> list[Path]:
    base = (
        home
        / "Library"
        / "Containers"
        / "com.tencent.xinWeChat"
        / "Data"
        / "Documents"
        / "xwechat_files"
    )
    if not base.is_dir():
        return []
    accounts = [
        path.resolve()
        for path in base.glob("*/db_storage")
        if path.is_dir() and not path.parent.name.startswith(".")
    ]
    return sorted(accounts, key=_account_activity, reverse=True)


def _read_data_config(layout: InstallLayout) -> dict:
    path = layout.data_dir / "config.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _configured_db_dir(layout: InstallLayout) -> Path | None:
    value = _read_data_config(layout).get("db_dir")
    if not isinstance(value, str) or not value.strip():
        return None
    expanded = Path(os.path.expanduser(os.path.expandvars(value)))
    if not expanded.is_absolute():
        expanded = layout.data_dir / expanded
    return expanded.resolve()


def _save_account_selection(layout: InstallLayout, db_dir: Path, source: str) -> None:
    payload = _read_data_config(layout)
    payload["db_dir"] = str(db_dir.resolve())
    payload["db_dir_selection"] = source
    _atomic_write_json(layout.data_dir / "config.json", payload)


def _public_account(account: Path, selected: Path | None = None) -> dict:
    return {
        "account_id": _account_id(account),
        "selected": selected is not None and account == selected,
    }


def _valid_key_payload(payload: object, db_dir: Path | None = None) -> bool:
    entries = [
        (key, value)
        for key, value in payload.items()
        if not key.startswith("_") and isinstance(value, dict) and value.get("enc_key")
    ] if isinstance(payload, dict) else []
    if not entries:
        return False
    if db_dir is None:
        return True
    try:
        from key_scan_common import verify_enc_key
        db_root = db_dir.resolve()
        for rel, value in entries:
            key_hex = str(value["enc_key"])
            if len(key_hex) != 64:
                return False
            rel_path = Path(rel)
            if rel_path.is_absolute():
                return False
            db_path = (db_root / rel_path).resolve()
            try:
                db_path.relative_to(db_root)
            except ValueError:
                return False
            with db_path.open("rb") as db_file:
                page1 = db_file.read(4096)
            if len(page1) != 4096 or not verify_enc_key(bytes.fromhex(key_hex), page1):
                return False
    except (OSError, ValueError, TypeError):
        return False
    return True


def _valid_key_file(path: Path, db_dir: Path | None = None) -> bool:
    try:
        with path.open(encoding="utf-8") as key_file:
            payload = json.load(key_file)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return False
    return _valid_key_payload(payload, db_dir)


def _parse_scanner_summary(output: str) -> dict[str, int]:
    """Extract non-sensitive scanner counters from stdout/stderr."""
    summary: dict[str, int] = {}
    patterns = {
        "encrypted_db_count": r"Found\s+(\d+)\s+encrypted DBs",
        "scanned_region_count": r"Scan complete:\s+\d+MB scanned,\s+(\d+)\s+regions",
        "unique_key_count": r"Scan complete:.*?,\s+(\d+)\s+unique keys",
        "matched_key_count": r"Matched\s+(\d+)/(\d+)\s+keys to known DBs",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, output, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        if key == "matched_key_count":
            summary[key] = int(match.group(1))
            summary["reported_key_count"] = int(match.group(2))
        else:
            summary[key] = int(match.group(1))
    return summary


def _empty_key_result(summary: dict[str, int]) -> tuple[str, str, str]:
    """Map scanner counters to a stable error and one actionable recovery."""
    if summary.get("encrypted_db_count") == 0:
        return (
            "wechat_database_not_found",
            "confirm_wechat_data_access_and_retry_initialize",
            "没有找到可用的微信数据。请确认微信已登录并能正常查看消息，然后重试。",
        )
    if summary.get("unique_key_count") == 0:
        return (
            "wechat_key_not_found",
            "keep_wechat_open_and_logged_in_then_retry_initialize",
            "暂时无法读取微信数据。请保持微信打开并已登录，然后重试。",
        )
    if summary.get("matched_key_count") == 0:
        return (
            "wechat_key_database_mismatch",
            "confirm_the_running_wechat_account_matches_the_detected_data_directory",
            "当前登录的微信账号与本机数据不一致。请确认账号后重试。",
        )
    return (
        "key_output_invalid",
        "retry_initialize_and_report_the_structured_diagnostics",
        "本机消息数据准备失败，请重试。",
    )


@contextlib.contextmanager
def _initialize_environment(runtime: Path, layout: InstallLayout):
    """Temporarily expose installed runtime paths to config/guard modules."""
    updates = {
        "WECHAT_DECRYPT_APP_DIR": str(runtime),
        "WECHAT_DECRYPT_DATA_DIR": str(layout.data_dir),
        "WECHAT_DECRYPT_NONINTERACTIVE": "1",
    }
    previous = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _preflight_macos_initialize(runtime: Path, layout: InstallLayout) -> dict:
    """Run all non-privileged configuration and version checks first."""
    diagnostics = io.StringIO()
    try:
        with _initialize_environment(runtime, layout), contextlib.redirect_stdout(diagnostics):
            from config import load_config
            from wechat_version_guard import check_version

            cfg = load_config()
            version_result = check_version(cfg)
    except SystemExit as exc:
        raise InstallerError(
            "未能在当前用户上下文中自动检测微信数据目录；尚未请求管理员授权",
            error_code="wechat_database_not_found",
            next_action="confirm_wechat_data_access_and_retry_initialize",
            details={"authorization_prompt_count": 0},
        ) from exc
    except (OSError, ValueError, TypeError) as exc:
        raise InstallerError(
            "初始化预检无法读取当前用户配置；尚未请求管理员授权",
            error_code="initialize_preflight_failed",
            next_action="retry_initialize_and_report_the_structured_error",
            details={"authorization_prompt_count": 0},
        ) from exc

    if not version_result.ok:
        reasons = [str(reason) for reason in version_result.reasons]
        detected = (version_result.details or {}).get("detected") or {}
        details = {
            "authorization_prompt_count": 0,
            "reasons": reasons,
            "detected_version": detected.get("short_version"),
            "detected_build": detected.get("build_version"),
            "detected_app_path": detected.get("app_path"),
        }
        if any("不在允许区间" in reason for reason in reasons):
            details["release_search"] = _release_search_guidance(
                cfg,
                detected,
            )
            supported = "、".join(details["release_search"].get("supported_versions") or [])
            support_hint = f"，当前支持 {supported}" if supported else ""
            raise InstallerError(
                f"当前微信版本 {detected.get('short_version') or '未知'} 暂不支持{support_hint}。"
                "可以为你查找可用的历史版本，但下载和替换前仍需要你确认。",
                error_code="version_not_allowed",
                next_action="search_public_sources_for_supported_release",
                details=details,
            )
        # A missing bundle is an environment prerequisite, not a policy or
        # release-integrity failure. Keep this branch machine-actionable so
        # agents do not suggest restoring a trusted release for a missing app.
        missing_app = any(
            "未配置 wechat_app_path" in reason
            or "微信安装路径不存在" in reason
            or "未能自动发现微信安装路径" in reason
            for reason in reasons
        )
        policy_failure = any(
            "allowed_version_ranges" in reason
            or "版本门禁策略" in reason
            or "可信摘要" in reason
            for reason in reasons
        )
        if missing_app and not policy_failure:
            details["requires_login"] = False
            raise InstallerError(
                "没有找到个人版微信。请先确认已安装，无需登录。",
                error_code="wechat_app_not_found",
                next_action="ensure_wechat_installed_and_retry_inspect",
                details=details,
            )
        raise InstallerError(
            "当前微信未通过安全检查，安装已暂停。",
            error_code="version_guard_failed",
            next_action="restore_the_trusted_release_and_retry_initialize",
            details=details,
        )

    raw_db_dir = str(cfg.get("db_dir") or "")
    accounts = _discover_macos_accounts(layout.home)

    # db_dir 为空或仍为模板值时，可能是微信尚未登录创建数据目录。
    # 此时仍允许流程继续到版本/签名检查，避免"登录→退出→登录"冗余循环。
    if not raw_db_dir or "your_wxid" in raw_db_dir:
        db_dir = accounts[0] if accounts else None
    else:
        db_dir = Path(raw_db_dir).expanduser().resolve()
        if not db_dir.is_dir() or not os.access(db_dir, os.R_OK | os.X_OK):
            db_dir = accounts[0] if accounts else None

    if db_dir is not None and db_dir not in accounts:
        accounts.insert(0, db_dir)
    return {
        "config": cfg,
        "db_dir": db_dir,
        "account_candidates": accounts,
        "detected": (version_result.details or {}).get("detected") or {},
    }


def _resign_wechat_app(app: Path, reporter: Reporter | None = None) -> int:
    """Re-sign WeChat during the explicit prepare-wechat stage.

    Returns authorization_prompt_count (0 = direct repair, 1 = osascript admin).
    Raises InstallerError on failure.
    """
    repair_commands = [
        ["/usr/bin/xattr", "-cr", str(app)],
        ["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(app)],
    ]

    # com.apple.provenance 等扩展属性必须在 codesign 前清理，否则即使已取得
    # 管理员授权，codesign 仍可能失败。
    if reporter:
        reporter.progress("prepare", "正在准备微信，请稍候")
    direct_succeeded = True
    for command in repair_commands:
        direct_result = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if direct_result.returncode != 0:
            direct_succeeded = False
            break
    if direct_succeeded and _is_adhoc_signed(app):
        return 0

    # 受保护的 app bundle 在同一次管理员授权中保持先清理、后签名的顺序。
    if reporter:
        reporter.progress("authorization", "请在系统提示中确认，完成后将自动继续")
    authorized_command = " && ".join(
        shlex.join(command) for command in repair_commands
    )
    result = subprocess.run(
        [
            "/usr/bin/osascript",
            "-e", "on run argv",
            "-e", "do shell script (item 1 of argv) with administrator privileges",
            "-e", "end run",
            authorized_command,
        ],
        check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        normalized = result.stderr.lower()
        cancelled = "user canceled" in normalized or "(-128)" in normalized
        app_management_denied = _is_app_management_denial(result.stderr)
        if app_management_denied and not cancelled:
            responsible_app = _responsible_app_name()
            settings_opened = _open_app_management_settings()
            app_hint = f"（{responsible_app}）" if responsible_app else ""
            raise InstallerError(
                f"macOS 阻止当前安装应用{app_hint}修改 WeChat.app；请在“系统设置 → 隐私与安全性 → App 管理”中允许该应用后重试",
                error_code="app_management_permission_required",
                next_action="enable_app_management_and_retry_prepare_wechat",
                details={
                    "authorization_prompt_count": 1,
                    "responsible_app": responsible_app,
                    "settings_opened": settings_opened,
                    "settings_pane": "Privacy_AppBundles",
                },
            )
        raise InstallerError(
            "用户取消了管理员授权" if cancelled else "WeChat 安全重签失败",
            error_code="administrator_authorization_cancelled" if cancelled else "wechat_resign_failed",
            next_action=(
                "retry_prepare_wechat_and_approve_the_macos_administrator_prompt"
                if cancelled
                else "report_wechat_resign_error"
            ),
            details={"authorization_prompt_count": 1},
        )
    if not _is_adhoc_signed(app):
        raise InstallerError(
            "系统命令已完成，但 WeChat 签名校验未通过",
            error_code="wechat_resign_verification_failed",
            next_action="report_wechat_resign_error",
            details={"authorization_prompt_count": 1},
        )
    return 1


def _is_app_management_denial(stderr: str | None) -> bool:
    """Recognize the TCC denial emitted when a host may not modify another app."""
    normalized = (stderr or "").casefold()
    return "operation not permitted" in normalized or bool(
        re.search(r"\beperm\b", normalized)
    )


def _responsible_app_name() -> str | None:
    """Return the nearest GUI app in this process's ancestry, without exposing it."""
    pid = os.getppid()
    visited: set[int] = set()
    for _ in range(12):
        if pid <= 1 or pid in visited:
            break
        visited.add(pid)
        process = subprocess.run(
            ["/bin/ps", "-ww", "-p", str(pid), "-o", "ppid=", "-o", "command="],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if process.returncode != 0 or not process.stdout.strip():
            break
        match = re.match(r"\s*(\d+)\s+(.*)", process.stdout, flags=re.DOTALL)
        if not match:
            break
        parent_pid = int(match.group(1))
        command = match.group(2).strip()
        app_match = re.search(r"(/.*?\.app)/Contents/", command)
        if app_match:
            app_bundle = Path(app_match.group(1))
            try:
                with (app_bundle / "Contents" / "Info.plist").open("rb") as info_file:
                    info = plistlib.load(info_file)
                display_name = info.get("CFBundleDisplayName") or info.get("CFBundleName")
                if isinstance(display_name, str) and display_name.strip():
                    return display_name.strip()
            except (OSError, plistlib.InvalidFileException, ValueError):
                pass
            return app_bundle.stem
        pid = parent_pid
    return None


def _open_app_management_settings() -> bool:
    """Open the user-controlled TCC pane; never attempt to change its state."""
    result = subprocess.run(
        ["/usr/bin/open", APP_MANAGEMENT_SETTINGS_URL],
        check=False,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _preflight_macos_scanner(preflight: dict) -> None:
    """Validate process and signature before the one privileged scanner call."""
    detected = preflight.get("detected") or {}
    app_path = str(detected.get("app_path") or "")

    # 先检查签名（不需要微信在运行），避免用户先登录再退出再登录的冗余流程
    if not app_path:
        raise InstallerError(
            "无法确定当前微信程序路径；未弹出管理员授权窗口",
            error_code="wechat_app_not_found",
            next_action="start_wechat_and_retry_initialize",
            details={"authorization_prompt_count": 0},
        )
    if not _is_adhoc_signed(app_path):
        cfg = preflight.get("config") or {}
        process_name = str(cfg.get("wechat_process") or "WeChat")
        probe = subprocess.run(
            ["/usr/bin/pgrep", "-x", process_name],
            check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        wechat_running = probe.returncode == 0 and bool(probe.stdout.strip())

        user_message = (
            "继续前需要准备微信。请先完全退出微信；这一步会修改微信应用，确认后可能出现系统授权。"
            if wechat_running
            else "继续前需要准备微信。这一步会修改微信应用，确认后可能出现系统授权。"
        )
        raise InstallerError(
            user_message,
            error_code="wechat_not_adhoc_signed",
            next_action=(
                "quit_wechat_confirm_and_run_prepare_wechat"
                if wechat_running
                else "confirm_and_run_prepare_wechat"
            ),
            details={
                "authorization_prompt_count": 0,
                "app_path": app_path,
                "wechat_running": wechat_running,
            },
        )

    # 签名已OK，才要求微信运行（密钥提取需要读进程内存）
    cfg = preflight.get("config") or {}
    process_name = str(cfg.get("wechat_process") or "WeChat")
    running = subprocess.run(
        ["/usr/bin/pgrep", "-x", process_name],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if running.returncode != 0 or not running.stdout.strip():
        raise InstallerError(
            "请打开并登录个人版微信，完成后再继续。",
            error_code="wechat_not_running",
            next_action="start_wechat_and_retry_initialize",
            details={"authorization_prompt_count": 0},
        )


def _is_adhoc_signed(app_path: str | Path) -> bool:
    signature = subprocess.run(
        ["/usr/bin/codesign", "-dv", "--verbose=4", app_path],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    signature_details = "\n".join((signature.stdout, signature.stderr))
    return signature.returncode == 0 and bool(re.search(
        r"(?:^Signature=adhoc$|\(adhoc(?:[,)]))",
        signature_details,
        flags=re.MULTILINE,
    ))


def _validate_wechat_bundle(app_path: str | Path, home: Path) -> Path:
    requested = Path(app_path).expanduser()
    if not requested.is_absolute():
        requested = (home / requested).absolute()
    app = requested.resolve()
    allowed = {
        Path("/Applications/WeChat.app").resolve(),
        (home / "Applications" / "WeChat.app").resolve(),
    }
    if app not in allowed or requested.is_symlink():
        raise InstallerError(
            "拒绝重签标准应用目录之外的程序",
            error_code="wechat_app_path_not_allowed",
            next_action="install_wechat_in_the_standard_applications_directory",
        )
    info_path = app / "Contents" / "Info.plist"
    try:
        with info_path.open("rb") as info_file:
            bundle_id = plistlib.load(info_file).get("CFBundleIdentifier")
    except (FileNotFoundError, OSError, plistlib.InvalidFileException) as exc:
        raise InstallerError(
            "版本门禁检测到的微信应用不是有效的 macOS app bundle",
            error_code="wechat_app_invalid",
            next_action="restore_the_official_wechat_app_and_retry",
        ) from exc
    if app.suffix != ".app" or bundle_id != "com.tencent.xinWeChat":
        raise InstallerError(
            "拒绝重签不是 WeChat 的应用",
            error_code="wechat_app_identity_mismatch",
            next_action="restore_the_official_wechat_app_and_retry",
        )
    return app


def _discover_db_salts(
    home: Path,
    db_dir: Path | None = None,
    account_dirs: list[Path] | None = None,
) -> Path | None:
    """Pre-discover encrypted DB salts from user context (has FDA).

    Returns a temp file path containing JSON array of {name, salt, page1} entries,
    or None if no encrypted databases were found.
    This allows the elevated scanner to skip filesystem access entirely.
    """
    base = db_dir or (
        home / "Library" / "Containers" / "com.tencent.xinWeChat" / "Data" / "Documents" / "xwechat_files"
    )
    if not base.is_dir():
        return None

    entries: list[dict[str, str]] = []
    sources: list[tuple[Path, Path, str]] = []
    # The configured db_dir is normally the db_storage directory.  Retain
    # support for the old xwechat_files root only when no explicit directory
    # was supplied, but never mix historical accounts into an active scan.
    if account_dirs:
        for index, storage in enumerate(account_dirs):
            prefix = f"__account_{index:03d}__/"
            sources.extend((storage, path, prefix) for path in storage.rglob("*.db"))
    elif db_dir is None:
        for account_dir in base.iterdir():
            if account_dir.name.startswith(".") or not account_dir.is_dir():
                continue
            storage = account_dir / "db_storage"
            if storage.is_dir():
                sources.extend((storage, path, "") for path in storage.rglob("*.db"))
    else:
        sources.extend((base, path, "") for path in base.rglob("*.db"))
    for db_storage, db_file, prefix in sources:
        if not db_file.is_file():
            continue
        try:
            with db_file.open("rb") as f:
                page1 = f.read(4096)
        except OSError:
            continue
        if len(page1) < 4096:
            continue
        # Skip unencrypted SQLite files
        if page1[:15] == b"SQLite format 3":
            continue
        salt_hex = page1[:16].hex()
        # Relative name from db_storage/ (matches scanner's output format)
        rel = prefix + str(db_file.relative_to(db_storage))
        entries.append({"name": rel, "salt": salt_hex, "page1": page1.hex()})

    if not entries:
        return None

    # Write to a secure temp file readable by root.  The page is public
    # ciphertext, but the file is still short-lived and is not user data.
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="hf_db_salts_", suffix=".json")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False)
        os.chmod(tmp_path, 0o600)  # root can read user-owned files without world access
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return None
    return Path(tmp_path)


def _matching_key_accounts(keys_file: Path, accounts: list[Path]) -> list[Path]:
    return [account for account in accounts if _valid_key_file(keys_file, account)]


def _normalize_account_key_output(
    keys_file: Path,
    accounts: list[Path],
    configured: Path | None,
) -> Path | None:
    try:
        payload = json.loads(keys_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    grouped: dict[int, dict] = {}
    for name, value in payload.items():
        match = re.fullmatch(r"__account_(\d{3})__/(.+)", name)
        if not match:
            continue
        index = int(match.group(1))
        if index >= len(accounts):
            continue
        grouped.setdefault(index, {})[match.group(2)] = value
    if not grouped:
        return None

    valid: list[tuple[int, dict]] = []
    for index, keys in grouped.items():
        if _valid_key_payload(keys, accounts[index]):
            valid.append((index, keys))
    if not valid:
        return None

    selected = valid[0]
    if configured is not None:
        for candidate in valid:
            if accounts[candidate[0]] == configured:
                selected = candidate
                break
    index, normalized = selected
    account = accounts[index]
    normalized["_db_dir"] = str(account)
    _atomic_write_json(keys_file, normalized)
    os.chmod(keys_file, 0o600)
    return account


def _extract_macos_keys(
    runtime: Path,
    layout: InstallLayout,
    reporter: Reporter,
    preflight: dict,
) -> bool:
    keys_file = layout.data_dir / "all_keys.json"
    configured_db_dir = preflight.get("db_dir")
    accounts = list(preflight.get("account_candidates") or [])
    matching_accounts = _matching_key_accounts(keys_file, accounts) if accounts else []
    if matching_accounts:
        selected = configured_db_dir if configured_db_dir in matching_accounts else matching_accounts[0]
        if selected != configured_db_dir:
            _save_account_selection(layout, selected, "validated_existing_keys")
            preflight["db_dir"] = selected
            preflight["account_changed"] = True
        return False
    if not accounts and _valid_key_file(keys_file, configured_db_dir):
        return False
    if keys_file.exists() or keys_file.is_symlink():
        try:
            keys_file.unlink()
        except PermissionError as exc:
            raise InstallerError(
                "现有密钥文件不属于当前用户；请勿使用 sudo 运行管理 CLI",
                error_code="key_file_ownership_invalid",
                next_action="report_key_file_ownership_error_without_privileged_repair",
            ) from exc
    scanner = runtime / "find_all_keys_macos"
    if not scanner.is_file() or not os.access(scanner, os.X_OK):
        raise InstallerError(
            "已安装的 macOS 密钥扫描器不存在或不可执行，请重新安装当前版本",
            error_code="scanner_missing",
            next_action="reinstall_current_release",
        )
    layout.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(layout.data_dir, 0o700)

    # These checks require no privileges and must happen before osascript so a
    # failed attempt never consumes an administrator prompt unnecessarily.
    _preflight_macos_scanner(preflight)

    # Pre-discover DB salts in user context (has FDA) to avoid requiring
    # Full Disk Access for the elevated scanner binary.
    db_salts_file = _discover_db_salts(
        layout.home,
        configured_db_dir,
        account_dirs=accounts if len(accounts) > 1 else None,
    )

    reporter.progress("authorization", "正在读取本机微信数据，请按系统提示确认")
    scanner_args = [
        str(scanner),
        "--output",
        str(keys_file),
        "--home",
        str(Path.home().resolve()),
        "--owner-uid",
        str(os.getuid()),
        "--owner-gid",
        str(os.getgid()),
    ]
    if db_salts_file:
        scanner_args.extend(["--db-salts", str(db_salts_file)])
    scanner_command = shlex.join(scanner_args)
    result = subprocess.run(
        [
            "/usr/bin/osascript",
            "-e",
            "on run argv",
            "-e",
            "do shell script (item 1 of argv) with administrator privileges",
            "-e",
            "end run",
            scanner_command,
        ],
        cwd=str(runtime),
        env=_runtime_env(runtime, layout),
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Clean up temporary salts file regardless of scanner outcome.
    if db_salts_file:
        try:
            db_salts_file.unlink()
        except OSError:
            pass
    if result.returncode != 0:
        # 扫描器的标准输出属于敏感操作过程信息，失败响应只使用 stderr。
        details = result.stderr.strip()
        normalized = details.lower()
        summary = _parse_scanner_summary("\n".join((result.stdout, result.stderr)))
        summary["authorization_prompt_count"] = 1
        if result.returncode == 4:
            code, action, message = _empty_key_result(summary)
            raise InstallerError(message, error_code=code, next_action=action, details=summary)
        if "wechat not running" in normalized:
            code = "wechat_not_running"
            action = "start_and_sign_in_to_wechat_then_retry_initialize"
        elif "task_for_pid failed" in normalized:
            code = "wechat_process_access_failed"
            action = "inspect_wechat_process_and_signature_before_retry"
        elif "user canceled" in normalized or "(-128)" in normalized:
            code = "administrator_authorization_cancelled"
            action = "retry_initialize_and_approve_the_macos_administrator_prompt"
        elif "authorization" in normalized or "administrator" in normalized:
            code = "administrator_authorization_required"
            action = "retry_initialize_and_approve_the_macos_administrator_prompt"
        else:
            code = "key_extraction_failed"
            action = "review_scanner_error_then_retry_initialize"
        tail = "\n".join(details.splitlines()[-12:])
        raise InstallerError(
            f"macOS 数据库密钥提取失败{': ' + tail if tail else ''}",
            error_code=code,
            next_action=action,
            details=summary,
        )
    if len(accounts) > 1:
        selected = _normalize_account_key_output(keys_file, accounts, configured_db_dir)
        if selected is not None:
            _save_account_selection(layout, selected, "matched_running_wechat")
            preflight["db_dir"] = selected
            preflight["account_changed"] = selected != configured_db_dir
            configured_db_dir = selected
    if not _valid_key_file(keys_file, configured_db_dir):
        summary = _parse_scanner_summary("\n".join((result.stdout, result.stderr)))
        summary["authorization_prompt_count"] = 1
        code, action, message = _empty_key_result(summary)
        raise InstallerError(
            message,
            error_code=code,
            next_action=action,
            details=summary,
        )
    try:
        os.chmod(keys_file, 0o600)
    except PermissionError as exc:
        raise InstallerError(
            "密钥文件所有者异常；请勿使用 sudo 运行管理 CLI",
            error_code="key_file_ownership_invalid",
            next_action="report_key_file_ownership_error_without_privileged_repair",
            details={"authorization_prompt_count": 1},
        ) from exc
    return True


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
    expected_repository: str | list[str] | tuple[str, ...],
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
    expected_repositories = (
        [expected_repository]
        if isinstance(expected_repository, str)
        else list(expected_repository)
    )
    if _repository_identity(repository) not in {
        _repository_identity(candidate) for candidate in expected_repositories
    }:
        raise InstallerError("origin 仓库不在指定的可信发布源列表中")
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


def _mark_installed_runtime(runtime: Path) -> None:
    marker = runtime / INSTALLED_RUNTIME_MARKER
    marker.write_text("installed-runtime\n", encoding="utf-8")
    marker.chmod(0o600)


def _find_uv() -> str | None:
    """Return the path to `uv` if available and functional, else None."""
    import shutil as _shutil

    uv = _shutil.which("uv")
    if not uv:
        return None
    try:
        result = subprocess.run(
            [uv, "--version"],
            capture_output=True, text=True, timeout=5,
        )
        return uv if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _create_runtime_environment(runtime: Path, python: Path) -> None:
    if sys.version_info < (3, 10) and python.resolve() == Path(sys.executable).resolve():
        raise InstallerError("需要 Python 3.10 或更高版本")
    _run(
        [str(python), "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"],
        error_context="指定的 Python 版本低于 3.10",
    )

    uv = _find_uv()
    requirements = str(runtime / "requirements.txt")
    pip_index = os.environ.get("PIP_INDEX_URL") or os.environ.get("UV_INDEX_URL")
    # Pre-cached wheels directory: agent can pre-download wheels here before
    # running install.sh to eliminate network I/O during pip install.
    find_links = os.environ.get("PIP_FIND_LINKS")

    if uv:
        # Fast path: uv venv + uv pip install (10-100x faster than pip)
        _run(
            [uv, "venv", str(runtime / ".venv"), "--python", str(python)],
            error_context="uv 创建独立 Python 环境失败",
        )
        uv_pip_args = [
            uv, "pip", "install",
            "--python", str(runtime / ".venv" / "bin" / "python3"),
            "-r", requirements,
        ]
        if find_links:
            uv_pip_args.extend(["--find-links", find_links])
        if pip_index:
            uv_pip_args.extend(["--index-url", pip_index])
        _run(uv_pip_args, error_context="uv 安装 Python 依赖失败")
    else:
        # Fallback: standard venv + pip
        _run(
            [str(python), "-m", "venv", str(runtime / ".venv")],
            error_context="创建独立 Python 环境失败",
        )
        runtime_python = runtime / ".venv" / "bin" / "python3"
        pip_args = [
            str(runtime_python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--prefer-binary",
            "--retries",
            "3",
            "--timeout",
            "20",
        ]
        if find_links:
            pip_args.extend(["--find-links", find_links])
        if pip_index:
            pip_args.extend(["--index-url", pip_index])
        pip_args.extend(["-r", requirements])
        _run(pip_args, error_context="安装 Python 依赖失败")


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


def _read_activation_state(layout: InstallLayout) -> dict:
    try:
        with layout.activation_state.open(encoding="utf-8") as state_file:
            data = json.load(state_file)
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


def _update_activation_state(layout: InstallLayout, **changes: object) -> dict:
    """Persist resumable stage markers without recording account or message data."""
    state = _read_activation_state(layout)
    state.update(changes)
    state["schema_version"] = ACTIVATION_SCHEMA_VERSION
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(layout.activation_state, state)
    return state


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


def _preflight_wechat_version(
    source: Path, layout: InstallLayout, reporter: Reporter
) -> None:
    """部署运行时前预检微信版本，避免装完才发现版本不受支持。

    预检只拦截"版本不在允许区间"这一种情况；其他检查失败（如微信未安装、
    配置读取异常）不阻塞安装，仍由 install 后的 inspect 阶段继续处理。
    """
    diagnostics = io.StringIO()
    try:
        with _initialize_environment(source, layout), contextlib.redirect_stdout(diagnostics):
            from config import load_config
            from wechat_version_guard import check_version

            cfg = load_config()
            version_result = check_version(cfg)
    except Exception:
        return
    if version_result.ok:
        return
    reasons = [str(reason) for reason in version_result.reasons]
    if not any("不在允许区间" in reason for reason in reasons):
        return
    detected = (version_result.details or {}).get("detected") or {}
    details = {
        "authorization_prompt_count": 0,
        "reasons": reasons,
        "detected_version": detected.get("short_version"),
        "detected_build": detected.get("build_version"),
        "detected_app_path": detected.get("app_path"),
    }
    details["release_search"] = _release_search_guidance(cfg, detected)
    supported = "、".join(details["release_search"].get("supported_versions") or [])
    support_hint = f"，当前支持 {supported}" if supported else ""
    raise InstallerError(
        f"当前微信版本 {detected.get('short_version') or '未知'} 暂不支持{support_hint}。"
        "可以为你查找可用的历史版本，但下载和替换前仍需要你确认。",
        error_code="version_not_allowed",
        next_action="search_public_sources_for_supported_release",
        details=details,
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
    old_manifest = _read_manifest(layout)
    old_activation = _read_activation_state(layout)
    reporter.progress("prepare", "正在检查安装文件")
    repositories = list(dict.fromkeys([args.repository, *getattr(args, "fallback_repositories", [])]))
    source_info = verify_source(
        source,
        expected_repository=repositories,
        branch=args.branch,
        expected_commit=args.expected_commit,
        expected_installer_sha256=args.expected_installer_sha256,
        allow_dirty_source=args.allow_dirty_source,
    )

    version = source_info["commit"]
    final_runtime = layout.runtime_dir / version
    if not final_runtime.exists():
        # 部署前先预检微信版本，避免装完才发现版本不受支持。
        _preflight_wechat_version(source, layout, reporter)
        layout.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        staging = layout.runtime_dir / f".{version}.{os.getpid()}.staging"
        if staging.exists():
            raise InstallerError(f"安装暂存目录已存在：{staging}")
        try:
            reporter.progress("install", "正在复制安装文件")
            copy_runtime(source, staging)
            _mark_installed_runtime(staging)
            reporter.progress("install", "正在安装所需组件，首次安装可能需要一些时间")
            # pip install and C compilation are independent — run in parallel
            # to save 2-5s (compile overlaps with the slower pip step).
            build_error: BaseException | None = None

            def _build_in_thread() -> None:
                nonlocal build_error
                try:
                    _build_macos_scanner(staging)
                except BaseException as exc:
                    build_error = exc

            build_thread = threading.Thread(target=_build_in_thread, daemon=True)
            build_thread.start()
            _create_runtime_environment(staging, Path(args.python).expanduser())
            build_thread.join()
            if build_error is not None:
                raise build_error
            os.replace(staging, final_runtime)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    else:
        if not (final_runtime / ".venv" / "bin" / "python3").is_file():
            raise InstallerError("已存在的固定版本运行时不完整，拒绝直接复用")
        _mark_installed_runtime(final_runtime)
        reporter.progress("install", "已找到现有安装，正在继续")

    reporter.progress("data", "正在保留并整理已有数据")
    migrated = migrate_existing_data(source, layout.data_dir)
    installation_id = old_manifest.get("installation_id") or str(uuid.uuid4())
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "installation_id": installation_id,
        "commit": version,
        "repository": args.repository,
        "repositories": repositories,
        "source_repository": source_info["repository"],
        "branch": source_info["branch"],
        "runtime_dir": str(final_runtime),
        "data_dir": str(layout.data_dir),
        "endpoint": f"http://{args.host}:{args.port}/mcp",
        "connector": _connector_protocol(f"http://{args.host}:{args.port}/mcp"),
        "host": args.host,
        "port": args.port,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "management_cli": str(layout.cli),
    }
    old_current = layout.current.resolve() if layout.current.exists() else None
    try:
        _atomic_symlink(final_runtime, layout.current)
        _write_management_cli(layout)
        _atomic_write_json(layout.manifest, manifest)
        activation_changes: dict[str, object] = {
            "runtime_installed": True,
            "commit": version,
        }
        if old_activation.get("commit") != version:
            activation_changes.update(service_enabled=False, query_ready=False)
        activation = _update_activation_state(layout, **activation_changes)
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
        if old_manifest:
            _atomic_write_json(layout.manifest, old_manifest)
        else:
            try:
                layout.manifest.unlink()
            except FileNotFoundError:
                pass
        raise

    old_version = old_manifest.get("commit") or old_manifest.get("version")
    installation_mode = (
        "fresh"
        if not old_version
        else "reused"
        if old_version == version
        else "upgraded"
    )
    return {
        "ok": True,
        "command": "install",
        "phase": "runtime_installed",
        "installation_mode": installation_mode,
        "installation_reused": installation_mode == "reused",
        "runtime_installed": True,
        "service_enabled": bool(activation.get("service_enabled", False)),
        "connector": _connector_protocol(manifest.get("endpoint")),
        "installation": manifest,
        "migrated": migrated,
        "next_step": "inspect",
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


def _safe_service_status(layout: InstallLayout, runtime: Path) -> dict:
    try:
        return service_status(layout, runtime)
    except (InstallerError, OSError) as exc:
        return {
            "ok": False,
            "status": "unavailable",
            "error_code": "service_status_unavailable",
            "error": str(exc),
        }


def _release_search_guidance(cfg: dict, detected: dict) -> dict:
    """Describe built-in and bounded external release discovery guidance."""
    guard = cfg.get("version_guard") or {}
    supported_versions: list[str] = []
    for item in guard.get("allowed_version_ranges") or []:
        if str(item.get("platform") or "").lower() not in {"", "darwin", "macos"}:
            continue
        exact = item.get("version") or item.get("short_version")
        minimum = item.get("min_version") or item.get("start_version")
        maximum = item.get("max_version") or item.get("end_version")
        if exact:
            supported_versions.append(str(exact))
        elif minimum and maximum and str(minimum) == str(maximum):
            supported_versions.append(str(minimum))
        elif minimum and maximum:
            supported_versions.append(f"{minimum}-{maximum}")
        elif minimum:
            supported_versions.append(f">={minimum}")
        elif maximum:
            supported_versions.append(f"<={maximum}")

    for item in guard.get("allowed_versions") or []:
        if str(item.get("platform") or "").lower() not in {"", "darwin", "macos"}:
            continue
        exact = item.get("version") or item.get("short_version")
        if exact:
            supported_versions.append(str(exact))

    supported_versions = list(dict.fromkeys(supported_versions))
    target = ", ".join(supported_versions) or "the project-verified macOS version"
    detected_version = str(detected.get("short_version") or "unknown")
    built_in_releases = _built_in_release_candidates(supported_versions)
    return {
        "available": True,
        "requires_user_confirmation": True,
        "detected_version": detected_version,
        "supported_versions": supported_versions,
        "built_in_releases": built_in_releases,
        "built_in_release_priority": "first",
        "search_queries": [
            f'WeChat macOS {target} DMG SHA-256',
            f'site:dldir1.qq.com WeChat macOS {target} DMG',
            f'site:gitee.com WeChat macOS {target} DMG',
            f'site:github.com WeChat macOS {target} DMG',
        ],
        "preferred_source_types": [
            "official_download_or_archive",
            "maintainer_declared_release_repository",
            "user_provided_local_or_private_source",
        ],
        "candidate_hosts": [
            "weixin.qq.com",
            "wechat.com",
            "dldir1.qq.com",
            "dldir1v6.qq.com",
            "gitee.com",
            "github.com",
        ],
        "verification_requirements": [
            "show_the_source_page_before_download",
            "do_not_call_a_candidate_official_without_explicit_evidence",
            "verify_download_sha256_when_published",
            "reject_candidate_when_sha256_or_size_mismatches",
            "never_offer_or_accept_a_user_override_for_integrity_mismatch",
            "verify_macos_bundle_id_com.tencent.xinWeChat",
            "verify_macos_short_version_matches_a_supported_version",
            "do_not_replace_or_re_sign_wechat_without_a_separate_user_confirmation",
        ],
        "validation_policy": {
            "runtime_compatibility_basis": "supported_short_version",
            "build_number_is_diagnostic_only": True,
            "download_integrity_basis": "maintainer_verified_sha256_and_size",
            "multiple_verified_artifacts_per_short_version_allowed": True,
            "signature_bundle_id_and_version_do_not_replace_digest_verification": True,
            "user_override_allowed": False,
            "on_integrity_mismatch": "reject_candidate_and_try_next_verified_source",
        },
        "disclaimer": (
            "A public repository or stable hosting domain does not prove official authorization, "
            "legality, or safety. Search results are candidates only."
        ),
        "network_policy": {
            "search_transport": "browser_or_web_search_first",
            "source_page_required_before_asset": True,
            "max_candidates": 3,
            "max_attempts_per_candidate": 1,
            "source_page_timeout_seconds": 15,
            "asset_probe_timeout_seconds": 10,
            "original_publisher_download": {
                "preferred_hosts": ["dldir1.qq.com", "dldir1v6.qq.com"],
                "prefer_when_release_metadata_matches": True,
                "built_in_releases_are_always_first": True,
                "required_metadata": ["version", "file_size", "published_digest"],
                "fallback_only_when_unavailable_or_mismatched": True,
                "never_construct_or_guess_a_version_url": True,
            },
            "fallback_order": [
                "project_builtin_release",
                "verified_original_publisher_download",
                "maintainer_declared_gitee_release",
                "maintainer_declared_github_release",
                "user_provided_local_or_private_source",
            ],
            "download": {
                "requires_explicit_user_confirmation": True,
                "prefer_browser_download": True,
                "connect_timeout_seconds": 10,
                "max_time_seconds": 900,
                "max_time_basis": (
                    "A verified installer is a few hundred MB. Bound the transfer in minutes, "
                    "not seconds, so a healthy link is never cut off mid-download."
                ),
                "max_attempts": 1,
                "max_attempts_basis": "restarts_from_zero_bytes",
                "resume_from_partial_file": True,
                "max_resume_attempts": 10,
                "resume_delay_seconds": 2,
                "resume_same_verified_url_only": True,
                "resume_rule": (
                    "Resuming a partial transfer on the same already-verified URL is not a "
                    "source retry. It does not consume max_candidates or "
                    "max_attempts_per_candidate, and it never relaxes digest verification: "
                    "the assembled file must still match a catalog SHA-256 and size."
                ),
                "stall_detect_bytes_per_second": 51200,
                "stall_detect_window_seconds": 30,
                "stall_rule": (
                    "Abort and resume a connection that stays below the stall threshold for "
                    "the stall window instead of waiting for the overall timeout."
                ),
                "reuse_verified_local_file": True,
                "no_unbounded_terminal_command": True,
                "do_not_git_clone_candidate_repository": True,
                "progress_reporting": {
                    "required_for_large_artifact": True,
                    "suppress_raw_progress_meter": True,
                    "report_interval_seconds": 5,
                    "report_fields": [
                        "percent",
                        "transferred_bytes",
                        "total_bytes",
                        "speed_bytes_per_second",
                        "eta_seconds",
                    ],
                    "reporting_rule": (
                        "A multi-minute download must show observable progress so the user can "
                        "tell it apart from a hang. Emit a periodic compact status instead of "
                        "streaming the raw transfer meter into the conversation."
                    ),
                    "on_resume": "state_that_the_transfer_continues_from_the_existing_bytes",
                },
            },
            "on_timeout": "stop_candidate_and_try_next_source",
            "mirror_rule": (
                "A mirror is not equivalent to the original source unless version, file size, "
                "and published SHA-256 all match."
            ),
            "integrity_mismatch_rule": (
                "Reject the candidate without changing WeChat. A valid signature, bundle ID, "
                "or compatible version is not a substitute for the catalog digest."
            ),
        },
    }


def _built_in_release_candidates(supported_versions: list[str]) -> list[dict]:
    """Return project-maintained release links matching supported versions."""
    catalog_path = Path(__file__).with_name("wechat-release-catalog.json")
    try:
        with catalog_path.open("r", encoding="utf-8") as catalog_file:
            releases = json.load(catalog_file).get("releases") or []
    except (OSError, ValueError, TypeError):
        return []

    supported = {str(version) for version in supported_versions}
    current_platform = "darwin" if sys.platform == "darwin" else "windows" if sys.platform == "win32" else sys.platform
    candidates: list[dict] = []
    for release in releases:
        if not isinstance(release, dict):
            continue
        if str(release.get("platform") or "").lower() not in {current_platform, "macos"}:
            continue
        version = str(release.get("version") or "")
        short_version = ".".join(version.split(".")[:3])
        if supported and not any(_release_version_supported(short_version, item) for item in supported):
            continue
        if not release.get("url"):
            continue
        artifacts = release.get("artifacts") or []
        declared_hashes = release.get("sha256s") or [release.get("sha256")]
        if artifacts:
            declared_hashes.extend(
                artifact.get("sha256")
                for artifact in artifacts
                if isinstance(artifact, dict)
            )
        verified_hashes = list(dict.fromkeys(
            str(value).lower()
            for value in declared_hashes
            if value and re.fullmatch(r"[0-9a-fA-F]{64}", str(value))
        ))
        if not verified_hashes:
            continue
        candidate = {
            "platform": current_platform,
            "version": version,
            "short_version": short_version,
            "url": str(release["url"]),
            "sha256": verified_hashes[0],
            "sha256s": verified_hashes,
            "artifacts": [
                {
                    "sha256": str(artifact.get("sha256")).lower(),
                    "size": int(artifact["size"]),
                }
                for artifact in artifacts
                if isinstance(artifact, dict)
                and re.fullmatch(r"[0-9a-fA-F]{64}", str(artifact.get("sha256") or ""))
                and artifact.get("size") is not None
            ],
            "source": "project_builtin",
            "requires_source_page": False,
        }
        if release.get("size") is not None and len(candidate["artifacts"]) <= 1:
            candidate["size"] = int(release["size"])
        candidates.append(candidate)
    return candidates


def _release_version_supported(version: str, supported: str) -> bool:
    """Match a catalog version against the display form of a policy range."""
    if supported == version:
        return True
    try:
        current = tuple(int(part) for part in version.split("."))
        if supported.startswith(">="):
            return current >= tuple(int(part) for part in supported[2:].split("."))
        if supported.startswith("<="):
            return current <= tuple(int(part) for part in supported[2:].split("."))
        if "-" in supported:
            lower, upper = supported.split("-", 1)
            return tuple(int(part) for part in lower.split(".")) <= current <= tuple(
                int(part) for part in upper.split(".")
            )
    except ValueError:
        return False
    return False


def inspect(args: argparse.Namespace, reporter: Reporter) -> dict:
    """Read installed state and stop at the next interaction boundary."""
    layout = default_layout(Path(args.home).expanduser() if args.home else None)
    manifest = _read_manifest(layout)
    runtime = _installed_runtime(layout, manifest)
    activation = _read_activation_state(layout)
    reporter.progress("check", "正在检查下一步")
    service_payload = _safe_service_status(layout, runtime)
    query_ready = bool(service_payload.get("query_ready"))
    initialized = bool(activation.get("initialized") or service_payload.get("initialized"))
    if "launchd_loaded" in service_payload:
        service_enabled = bool(service_payload.get("launchd_loaded"))
    else:
        service_enabled = bool(activation.get("service_enabled"))

    base = {
        "ok": True,
        "command": "inspect",
        "installation_mode": "existing",
        "installation_reused": True,
        "installation_id": manifest.get("installation_id"),
        "endpoint": manifest.get("endpoint"),
        "connector": _connector_protocol(manifest.get("endpoint")),
        "runtime_installed": True,
        "initialized": initialized,
        "service_enabled": service_enabled,
        "query_ready": query_ready,
        "service": service_payload,
    }
    if query_ready:
        return {**base, "next_step": "register_with_mcporter"}
    if initialized:
        return {**base, "next_step": "enable_service"}
    if platform.system().lower() != "darwin":
        return {**base, "next_step": "initialize"}

    try:
        preflight = _preflight_macos_initialize(runtime, layout)
        candidates = list(preflight.get("account_candidates") or [])
        configured = preflight.get("db_dir")
        if configured is not None and configured not in candidates:
            candidates.insert(0, configured)
        keys_file = layout.data_dir / "all_keys.json"
        keys_reusable = any(_valid_key_file(keys_file, candidate) for candidate in candidates)
        if keys_reusable:
            return {**base, "keys_reusable": True, "next_step": "initialize"}
        _preflight_macos_scanner(preflight)
        return {**base, "keys_reusable": False, "next_step": "initialize"}
    except InstallerError as exc:
        boundaries = {
            "wechat_not_adhoc_signed": "prepare_wechat",
            "wechat_not_running": "sign_in_to_wechat",
            "wechat_app_not_found": "ensure_wechat_installed",
            "wechat_database_not_found": "sign_in_to_wechat",
        }
        if exc.error_code not in boundaries:
            raise
        return {
            **base,
            "ready_for_initialize": False,
            "preflight_error": {
                "error_code": exc.error_code,
                "user_message": _plain_user_message(exc),
                "next_action": exc.next_action,
                "details": exc.details,
            },
            "next_step": boundaries[exc.error_code],
        }


def status(args: argparse.Namespace, reporter: Reporter) -> dict:
    layout = default_layout(Path(args.home).expanduser() if args.home else None)
    manifest = _read_manifest(layout)
    runtime = _installed_runtime(layout, manifest)
    reporter.progress("check", "正在检查运行状态")
    service_payload = service_status(layout, runtime)
    return {
        "ok": bool(service_payload.get("ok")),
        "command": "status",
        "installation_id": manifest.get("installation_id"),
        "commit": manifest.get("commit") or manifest.get("version"),
        "branch": manifest.get("branch") or manifest.get("release_branch") or "main",
        "endpoint": manifest.get("endpoint"),
        "connector": _connector_protocol(manifest.get("endpoint")),
        "runtime_dir": str(runtime),
        "data_dir": str(layout.data_dir),
        "service": service_payload,
    }


def _detect_system_proxy() -> str | None:
    """尝试从 macOS scutil --proxy 检测已启用的 HTTPS 代理。"""
    if platform.system().lower() != "darwin":
        return None
    try:
        result = subprocess.run(
            ["/usr/sbin/scutil", "--proxy"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        lines = result.stdout.splitlines()
        https_enable = False
        https_host = ""
        https_port = ""
        for line in lines:
            stripped = line.strip()
            if "HTTPSEnable" in stripped and "1" in stripped.split(":")[-1]:
                https_enable = True
            elif "HTTPSProxy" in stripped:
                https_host = stripped.split(":")[-1].strip()
            elif "HTTPSPort" in stripped:
                https_port = stripped.split(":")[-1].strip()
        if https_enable and https_host and https_port:
            return f"http://{https_host}:{https_port}"
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def _git_network_run(
    command: list[str],
    *,
    error_context: str,
    timeout: float,
    retry_cleanup: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    # 代理检测：环境变量未设置时尝试读取 macOS 系统代理
    if not env.get("https_proxy") and not env.get("HTTPS_PROXY"):
        sys_proxy = _detect_system_proxy()
        if sys_proxy:
            env["https_proxy"] = sys_proxy
            env["http_proxy"] = sys_proxy
    network_command = [
        "/usr/bin/git",
        "-c",
        "http.lowSpeedLimit=1024",
        "-c",
        "http.lowSpeedTime=15",
        *command,
    ]
    errors: list[str] = []
    for attempt in range(1, 4):
        if attempt > 1 and retry_cleanup is not None and retry_cleanup.exists():
            if retry_cleanup.is_dir() and not retry_cleanup.is_symlink():
                shutil.rmtree(retry_cleanup)
            else:
                retry_cleanup.unlink()
        try:
            result = subprocess.run(
                network_command,
                env=env,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            errors.append(f"第 {attempt} 次尝试超时")
        else:
            if result.returncode == 0:
                return result
            details = (result.stderr or result.stdout).strip().splitlines()
            errors.append(f"第 {attempt} 次尝试失败：{' | '.join(details[-3:]) or '未知 Git 错误'}")
        if attempt < 3:
            time.sleep(1)
    proxy_hint = ""
    if not env.get("https_proxy") and not env.get("HTTPS_PROXY"):
        proxy_hint = "。提示: 如使用代理工具请设置 https_proxy=http://127.0.0.1:<端口>"
    raise InstallerError(
        f"{error_context}：{'；'.join(errors)}{proxy_hint}",
        error_code="git_source_unreachable",
        next_action="retry_or_configure_an_official_fallback_repository",
    )


def _remote_branch_commit(repository: str, branch: str) -> str:
    branch = _validate_branch(branch)
    if not repository or repository.startswith("-"):
        raise InstallerError("安装清单中的发布仓库地址无效")
    result = _git_network_run(
        ["ls-remote", "--exit-code", repository, f"refs/heads/{branch}"],
        error_context=f"无法查询远端 {branch} 发布通道",
        timeout=20,
    )
    lines = [line.split() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1 or len(lines[0]) != 2 or not re.fullmatch(r"[0-9a-fA-F]{40}", lines[0][0]):
        raise InstallerError("远端发布分支返回了无法解析的提交信息")
    return lines[0][0].lower()


def _manifest_repositories(manifest: dict) -> list[str]:
    configured = manifest.get("repositories")
    values = configured if isinstance(configured, list) else [manifest.get("repository")]
    repositories = [str(value).strip() for value in values if str(value or "").strip()]
    return list(dict.fromkeys(repositories))


def _select_release_source(repositories: list[str], branch: str) -> tuple[str, str]:
    failures: list[str] = []
    for repository in repositories:
        try:
            return repository, _remote_branch_commit(repository, branch)
        except InstallerError as exc:
            failures.append(f"{repository}: {exc}")
    raise InstallerError(
        "所有可信发布源均不可达：" + "；".join(failures),
        error_code="all_git_sources_unreachable",
        next_action="retry_network_or_add_an_official_fallback_repository",
    )


def check_update(args: argparse.Namespace, reporter: Reporter) -> dict:
    layout = default_layout(Path(args.home).expanduser() if args.home else None)
    manifest = _read_manifest(layout)
    _installed_runtime(layout, manifest)
    repositories = _manifest_repositories(manifest)
    branch = str(manifest.get("branch") or manifest.get("release_branch") or "main")
    installed_commit = str(manifest.get("commit") or manifest.get("version") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{40}", installed_commit):
        raise InstallerError("安装清单缺少有效的 Git commit，请重新安装当前版本")
    reporter.progress("update", "正在检查更新")
    repository, remote_commit = _select_release_source(repositories, branch)
    return {
        "ok": True,
        "command": "check-update",
        "installed_commit": installed_commit,
        "remote_commit": remote_commit,
        "source_repository": repository,
        "branch": branch,
        "update_available": remote_commit != installed_commit,
    }


def _clone_branch(repository: str, branch: str, destination: Path) -> None:
    branch = _validate_branch(branch)
    if not repository or repository.startswith("-"):
        raise InstallerError("安装清单中的发布仓库地址无效")
    _git_network_run(
        [
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
        error_context=f"拉取远端 {branch} 发布版本失败",
        timeout=90,
        retry_cleanup=destination,
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
    repositories = _manifest_repositories(manifest)
    branch = str(manifest.get("branch") or manifest.get("release_branch") or "main")
    installed_commit = str(manifest.get("commit") or manifest.get("version") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{40}", installed_commit):
        raise InstallerError("安装清单缺少有效的 Git commit，请重新安装当前版本")

    reporter.progress("update", "正在检查更新")
    repository, remote_commit = _select_release_source(repositories, branch)
    if remote_commit == installed_commit:
        return {
            "ok": True,
            "command": "upgrade",
            "upgraded": False,
            "commit": installed_commit,
            "connector": _connector_protocol(manifest.get("endpoint")),
            "message": "当前已是最新版本",
        }

    with tempfile.TemporaryDirectory(prefix="wechat-decrypt-light-upgrade-") as temporary:
        source = Path(temporary) / "source"
        reporter.progress("download", "正在下载更新")
        _clone_branch(repository, branch, source)
        source_info = verify_source(
            source,
            expected_repository=repositories,
            branch=branch,
            expected_commit=remote_commit,
        )
        reporter.progress("install", "正在安装更新")
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
                repositories[0],
                "--branch",
                branch,
                "--expected-commit",
                source_info["commit"],
                "--host",
                str(manifest.get("host") or DEFAULT_HOST),
                "--port",
                str(manifest.get("port") or DEFAULT_PORT),
                *[
                    argument
                    for fallback in repositories[1:]
                    for argument in ("--fallback-repository", fallback)
                ],
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
        "runtime_installed": bool(install_payload.get("runtime_installed")),
        "service_enabled": bool(install_payload.get("service_enabled")),
        "connector": _connector_protocol(installation.get("endpoint")),
        "installation": installation,
        "next_step": install_payload.get("next_step") or "inspect",
    }


def repair(args: argparse.Namespace, reporter: Reporter) -> dict:
    return enable_service(args, reporter, command="repair")


def enable_service(
    args: argparse.Namespace,
    reporter: Reporter,
    *,
    command: str = "enable-service",
) -> dict:
    layout = default_layout(Path(args.home).expanduser() if args.home else None)
    manifest = _read_manifest(layout)
    runtime = _installed_runtime(layout, manifest)
    activation = _read_activation_state(layout)
    existing_service = _safe_service_status(layout, runtime)
    initialized = bool(activation.get("initialized") or existing_service.get("initialized"))
    if not initialized:
        raise InstallerError(
            "本地数据尚未初始化，暂不启用常驻服务",
            error_code="initialization_required",
            next_action="run_initialize_before_enabling_service",
        )
    host = str(manifest.get("host") or DEFAULT_HOST)
    port = int(manifest.get("port") or DEFAULT_PORT)
    reporter.progress("service", "正在完成最后配置")
    _service_command(
        runtime,
        layout,
        ["install", "--host", host, "--port", str(port)],
        error_context="LaunchAgent 启用失败；初始化结果已保留，可只重试 enable-service",
    )
    deadline = time.monotonic() + 20
    service_payload = service_status(layout, runtime)
    while not service_payload.get("query_ready") and time.monotonic() < deadline:
        time.sleep(1)
        service_payload = service_status(layout, runtime)
    query_ready = bool(service_payload.get("query_ready"))
    _update_activation_state(
        layout,
        service_enabled=True,
        query_ready=query_ready,
    )
    return {
        "ok": query_ready,
        "command": command,
        "installation_id": manifest.get("installation_id"),
        "initialized": True,
        "service_enabled": True,
        "query_ready": query_ready,
        "connector": _connector_protocol(manifest.get("endpoint")),
        "service": service_payload,
        "next_step": "register_with_mcporter" if query_ready else "retry_enable_service",
    }


def initialize(args: argparse.Namespace, reporter: Reporter) -> dict:
    """Extract keys and prepare local data without coupling service activation."""
    _require_non_root_management()
    layout = default_layout(Path(args.home).expanduser() if args.home else None)
    manifest = _read_manifest(layout)
    runtime = _installed_runtime(layout, manifest)
    runtime_python = runtime / ".venv" / "bin" / "python3"
    env = _runtime_env(runtime, layout)
    env["WECHAT_DECRYPT_SKIP_SERVICE_INSTALL"] = "1"
    repaired_files = _normalize_legacy_data_files(layout)
    if repaired_files:
        reporter.progress(
            "ownership",
            "已修复旧版安装遗留问题",
        )
    authorization_prompt_count = 0
    if platform.system().lower() == "darwin":
        preflight = _preflight_macos_initialize(runtime, layout)
        authorization_prompt_count = int(
            _extract_macos_keys(runtime, layout, reporter, preflight)
        )
    reporter.progress("initialize", "正在准备本地消息数据")
    _run(
        [str(runtime_python), str(runtime / "main.py"), "init"],
        cwd=runtime,
        env=env,
        error_context="本机消息数据初始化失败",
    )
    _update_activation_state(
        layout,
        initialized=True,
        service_enabled=False,
        query_ready=False,
    )
    return {
        "ok": True,
        "command": "initialize",
        "installation_id": manifest.get("installation_id"),
        "endpoint": manifest.get("endpoint"),
        "connector": _connector_protocol(manifest.get("endpoint")),
        "initialized": True,
        "service_enabled": False,
        "query_ready": False,
        "authorization_prompt_count": authorization_prompt_count,
        "repaired_legacy_files": repaired_files,
        "account": (
            _public_account(preflight["db_dir"])
            if platform.system().lower() == "darwin" and preflight.get("db_dir") is not None
            else None
        ),
        "account_changed": bool(preflight.get("account_changed")) if platform.system().lower() == "darwin" else False,
        "next_step": "enable_service",
    }


def prepare_wechat(args: argparse.Namespace, reporter: Reporter) -> dict:
    if platform.system().lower() != "darwin":
        raise InstallerError(
            "prepare-wechat 仅支持 macOS",
            error_code="unsupported_platform",
            next_action="run_prepare_wechat_on_macos",
        )
    if not args.confirm_resign:
        raise InstallerError(
            "准备微信会修改微信应用，需要用户明确确认",
            error_code="wechat_resign_confirmation_required",
            next_action="ask_user_to_confirm_wechat_resign",
            details={"authorization_prompt_count": 0},
        )

    layout = default_layout(Path(args.home).expanduser() if args.home else None)
    manifest = _read_manifest(layout)
    runtime = _installed_runtime(layout, manifest)
    preflight = _preflight_macos_initialize(runtime, layout)
    app = _validate_wechat_bundle(
        (preflight.get("detected") or {}).get("app_path") or "",
        layout.home,
    )

    # codesign --deep 会遍历整个 app bundle；主进程退出但 helper 仍在收尾时
    # 也不能开始重签。按可执行文件绝对路径检查 bundle 内的全部进程。
    running = subprocess.run(
        [
            "/usr/bin/pgrep",
            "-f",
            f"^{re.escape(str(app))}/Contents/",
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if running.returncode == 0 and running.stdout.strip():
        raise InstallerError(
            "微信仍在运行。请等待微信完全退出后再继续。",
            error_code="wechat_must_quit_for_resign",
            next_action="quit_wechat_and_retry_prepare_wechat",
            details={
                "authorization_prompt_count": 0,
                "running_pids": running.stdout.split(),
            },
        )
    if _is_adhoc_signed(app):
        _update_activation_state(layout, wechat_prepared=True)
        return {
            "ok": True,
            "command": "prepare-wechat",
            "already_prepared": True,
            "authorization_prompt_count": 0,
            "next_step": "open_wechat_and_initialize",
        }

    authorization_prompt_count = _resign_wechat_app(app, reporter)
    _update_activation_state(layout, wechat_prepared=True)
    subprocess.run(
        ["/usr/bin/open", str(app)],
        check=False,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {
        "ok": True,
        "command": "prepare-wechat",
        "already_prepared": False,
        "authorization_prompt_count": authorization_prompt_count,
        "next_step": "sign_in_to_wechat_then_initialize",
    }


def accounts(args: argparse.Namespace, reporter: Reporter) -> dict:
    layout = default_layout(Path(args.home).expanduser() if args.home else None)
    candidates = _discover_macos_accounts(layout.home)
    selected = _configured_db_dir(layout)
    return {
        "ok": bool(candidates),
        "command": "accounts",
        "accounts": [_public_account(account, selected) for account in candidates],
        "selected_account_id": _account_id(selected) if selected in candidates else None,
        "next_step": "select_account" if candidates and selected not in candidates else "initialize",
    }


def select_account(args: argparse.Namespace, reporter: Reporter) -> dict:
    layout = default_layout(Path(args.home).expanduser() if args.home else None)
    candidates = _discover_macos_accounts(layout.home)
    matches = [account for account in candidates if _account_id(account) == args.account]
    if len(matches) != 1:
        raise InstallerError(
            "没有找到指定的微信账号数据目录",
            error_code="wechat_account_not_found",
            next_action="list_accounts_and_select_an_available_account",
            details={"available_accounts": [_public_account(account) for account in candidates]},
        )
    selected = matches[0]
    _save_account_selection(layout, selected, "manual")
    return {
        "ok": True,
        "command": "select-account",
        "account": _public_account(selected, selected),
        "keys_reusable": _valid_key_file(layout.data_dir / "all_keys.json", selected),
        "next_step": "initialize",
    }


def uninstall(args: argparse.Namespace, reporter: Reporter) -> dict:
    layout = default_layout(Path(args.home).expanduser() if args.home else None)
    manifest = _read_manifest(layout)
    runtime = _installed_runtime(layout, manifest)
    reporter.progress("uninstall", "正在停止本地服务")
    _service_command(runtime, layout, ["uninstall"], error_context="LaunchAgent 卸载失败")
    _update_activation_state(layout, service_enabled=False, query_ready=False)
    removed_runtime = False
    if args.remove_runtime:
        reporter.progress("uninstall", "正在移除程序，保留本机数据")
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


def _parse_version(value: str) -> tuple[int, ...]:
    """把 4.1.8 形式的版本号解析为可比较的整数元组。"""
    return tuple(int(part) for part in re.split(r"[._-]", value) if part.isdigit())


def _version_in_ranges(version: str, ranges: list[tuple[str, str]]) -> bool:
    """判断版本是否落在任一 [min, max] 闭区间内。"""
    parsed = _parse_version(version)
    if not parsed:
        return False
    for minimum, maximum in ranges:
        if _parse_version(minimum) <= parsed <= _parse_version(maximum):
            return True
    return False


def _wechat_running_pids() -> list[str]:
    """返回当前运行的微信主进程 PID 列表（未运行返回空列表）。"""
    result = subprocess.run(
        ["/usr/bin/pgrep", "-x", "WeChat"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.split()
    return []


def quit_wechat(args: argparse.Namespace, reporter: Reporter) -> dict:
    """请求微信正常退出；仍未退出时停下等待用户处理。"""
    if platform.system().lower() != "darwin":
        raise InstallerError(
            "quit-wechat 仅支持 macOS",
            error_code="unsupported_platform",
            next_action="run_quit_wechat_on_macos",
        )

    if not _wechat_running_pids():
        return {
            "ok": True,
            "command": "quit-wechat",
            "quit_method": "already_quit",
            "wechat_running": False,
        }

    reporter.progress("quit-wechat", "正在请求退出微信")
    quit_method = "graceful"
    for attempt in range(3):
        subprocess.run(
            ["osascript", "-e", 'tell application "WeChat" to quit'],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(3)
        if not _wechat_running_pids():
            return {
                "ok": True,
                "command": "quit-wechat",
                "quit_method": quit_method,
                "wechat_running": False,
            }

    # Never terminate a user's process implicitly. A forced quit can lose
    # unsaved state; stop at the interaction boundary and let the user close
    # WeChat normally before retrying the replacement.
    remaining = _wechat_running_pids()
    raise InstallerError(
        "微信仍在运行，请先完全退出微信",
        error_code="wechat_quit_failed",
        next_action="close_wechat_manually_and_retry",
        details={"running_pids": remaining},
    )


def _release_catalog() -> dict:
    """读取随安装器发布的微信版本目录（wechat-release-catalog.json）。"""
    catalog_path = Path(__file__).resolve().parent / "wechat-release-catalog.json"
    if not catalog_path.is_file():
        raise InstallerError(
            "缺少发布目录文件 wechat-release-catalog.json",
            error_code="release_catalog_missing",
            next_action="reinstall_current_release_and_retry",
        )
    return json.loads(catalog_path.read_text(encoding="utf-8"))


def _catalog_release_for(version: str) -> dict:
    """按短版本号（如 4.1.8）从发布目录中选取 darwin 平台的安装包。"""
    catalog = _release_catalog()
    for release in catalog.get("releases", []):
        if release.get("platform") != "darwin":
            continue
        short = ".".join(str(release.get("version", "")).split(".")[:3])
        if short == version:
            return release
    raise InstallerError(
        f"发布目录中没有找到 macOS 微信 {version} 的安装包",
        error_code="release_not_found_in_catalog",
        next_action="check_release_catalog_or_pass_url_sha256_size",
        details={"available_darwin_versions": [
            ".".join(str(r.get("version", "")).split(".")[:3])
            for r in catalog.get("releases", [])
            if r.get("platform") == "darwin"
        ]},
    )


def _server_supports_range(url: str) -> bool:
    """探测下载服务器是否支持 HTTP Range（决定能否断点续传）。"""
    probe = subprocess.run(
        ["/usr/bin/curl", "-sI", "-H", "Range: bytes=0-0", "-o", "/dev/null",
         "-w", "%{http_code}", "--connect-timeout", "3", "--max-time", "5", url],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return probe.stdout.strip() == "206"


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_receipt_path(target: Path) -> Path:
    return target.with_name(target.name + ".verified.json")


def _write_download_receipt(target: Path, *, url: str, digest: str, size: int) -> None:
    receipt = {
        "schema_version": 1,
        "path": str(target),
        "url": url,
        "sha256": digest,
        "size": size,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }
    receipt_path = _download_receipt_path(target)
    temporary = receipt_path.with_name(receipt_path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, receipt_path)


def _read_download_receipt(target: Path) -> dict | None:
    try:
        receipt = json.loads(_download_receipt_path(target).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return receipt if receipt.get("path") == str(target) else None


def _receipt_matches_release_catalog(receipt: dict) -> bool:
    digest = str(receipt.get("sha256") or "").lower()
    size = receipt.get("size")
    try:
        releases = _release_catalog().get("releases") or []
    except InstallerError:
        return False
    for release in releases:
        if release.get("platform") != "darwin":
            continue
        for artifact in release.get("artifacts") or []:
            if str(artifact.get("sha256") or "").lower() == digest and artifact.get("size") == size:
                return True
    return False


def _download_progress_watcher(
    target: Path,
    total_bytes: int,
    reporter: Reporter,
    stop: threading.Event,
) -> None:
    """后台线程：每 5 秒把下载进度作为 JSON 事件输出，避免长下载被误判为卡死。"""
    started = time.monotonic()
    last_sample_time = started
    last_bytes = target.stat().st_size if target.exists() else 0
    while not stop.wait(5):
        try:
            current = target.stat().st_size
        except FileNotFoundError:
            current = 0
        now = time.monotonic()
        interval = max(now - last_sample_time, 1e-9)
        # A retry must never produce a negative speed or a visibly regressing
        # progress message. curl -C normally preserves the partial file, but
        # this also keeps the UI stable if a server resets the transfer.
        speed = max(0.0, (current - last_bytes) / interval)
        last_bytes, last_sample_time = current, now
        remaining = (total_bytes - current) / speed if speed > 0 and current < total_bytes else None
        reporter.progress(
            "download",
            "正在下载安装包",
            extra={
                "percent": round(min(current / total_bytes * 100, 100.0), 1) if total_bytes else None,
                "transferred_bytes": current,
                "total_bytes": total_bytes,
                "speed_bytes_per_second": round(speed),
                "eta_seconds": round(remaining) if remaining is not None else None,
            },
        )


def download_release(args: argparse.Namespace, reporter: Reporter) -> dict:
    """下载并校验微信安装包：Range 探测、断点续传、进度事件、完整性校验。"""
    if platform.system().lower() != "darwin":
        raise InstallerError(
            "download-release 仅支持 macOS",
            error_code="unsupported_platform",
            next_action="run_download_release_on_macos",
        )
    if not getattr(args, "confirm_download", False):
        raise InstallerError(
            "下载兼容版本前需要用户确认",
            error_code="download_confirmation_required",
            next_action="confirm_download_and_retry",
        )
    # Emit an immediate user-visible event before catalog, HEAD, or Range
    # probes. Those probes can each take several seconds on a slow network.
    reporter.progress("download", "正在准备下载安装包")

    if args.version:
        release = _catalog_release_for(args.version)
        url = release["url"]
        expected_sizes: dict[str, int] = {}
        for artifact in release.get("artifacts") or []:
            if artifact.get("size"):
                expected_sizes[artifact["sha256"]] = int(artifact["size"])
        expected_sha256s: list[str] = []
        for digest in release.get("sha256s") or []:
            expected_sha256s.append(digest)
        if release.get("sha256") and release["sha256"] not in expected_sha256s:
            expected_sha256s.append(release["sha256"])
        short_version = args.version
        filename = f"WeChatMac_{short_version}.dmg"
    else:
        if not args.url:
            raise InstallerError(
                "需要 --version 或 --url 指定下载来源",
                error_code="download_source_missing",
                next_action="provide_version_or_url_sha256_size",
            )
        if not args.sha256 or not args.size:
            raise InstallerError(
                "自定义下载来源必须同时提供校验信息",
                error_code="download_integrity_metadata_missing",
                next_action="provide_verified_url_sha256_size",
            )
        url = args.url
        expected_sha256s = [value for value in [args.sha256] if value]
        expected_sizes = {args.sha256: args.size} if args.sha256 and args.size else {}
        short_version = "custom"
        filename = Path(urlparse(url).path).name or "WeChatInstall.dmg"

    layout = default_layout(Path(args.home).expanduser() if args.home else None)
    download_dir = layout.data_dir / "downloads"
    download_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = Path(args.output).expanduser() if args.output else download_dir / filename
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    # Prefer the server's actual content length. A catalog may contain more
    # than one verified artifact for the same short version.
    total_bytes = None
    if len(expected_sizes) == 1:
        total_bytes = next(iter(expected_sizes.values()))
    if total_bytes is None:
        head = subprocess.run(
            ["/usr/bin/curl", "-sIL", "--connect-timeout", "3", "--max-time", "5", url],
            check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        for line in head.stdout.splitlines():
            lowered = line.lower()
            if lowered.startswith("content-length:"):
                try:
                    total_bytes = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
    if total_bytes is None and expected_sizes:
        # If the server does not expose Content-Length, retain a useful
        # estimate from the verified catalog instead of going silent.
        total_bytes = max(expected_sizes.values())

    # 本地已有完整且校验通过的文件时直接复用，避免重复下载。
    if target.is_file() and total_bytes and target.stat().st_size == total_bytes:
        digest = _sha256_of(target)
        receipt = _read_download_receipt(target)
        if digest in expected_sha256s and receipt and receipt.get("sha256") == digest and receipt.get("size") == target.stat().st_size:
            return {
                "ok": True,
                "command": "download-release",
                "path": str(target),
                "bytes": target.stat().st_size,
                "sha256": digest,
                "resume_supported": None,
                "reused": True,
                "elapsed_seconds": 0,
                "next_step": "replace-wechat",
            }

    resume_supported = _server_supports_range(url)
    partial = target.with_name(target.name + ".part")
    curl_args = [
        "/usr/bin/curl",
        "--fail",
        "--location",
        "--retry", "10",
        "--retry-all-errors",
        "--retry-delay", "2",
        "--connect-timeout", "10",
        "--max-time", "900",
        "--speed-limit", "51200",
        "--speed-time", "30",
        "-s",
        "-o", str(partial),
        url,
    ]
    if resume_supported:
        # 服务器支持 Range 时才启用断点续传，避免不支持续传时重头下载。
        curl_args[1:1] = ["-C", "-"]
    elif partial.exists():
        # A non-resumable endpoint would truncate the file. Remove the stale
        # partial first so the displayed percentage cannot jump backwards.
        partial.unlink()

    starting_bytes = partial.stat().st_size if partial.exists() else 0
    reporter.progress("download", "正在下载安装包", extra={
        "percent": round(min(starting_bytes / total_bytes * 100, 100.0), 1) if total_bytes else None,
        "transferred_bytes": starting_bytes,
        "total_bytes": total_bytes,
        "speed_bytes_per_second": 0,
        "eta_seconds": None,
    })
    stop_event = threading.Event()
    if total_bytes:
        watcher = threading.Thread(
            target=_download_progress_watcher,
            args=(partial, total_bytes, reporter, stop_event),
            daemon=True,
        )
        watcher.start()
    else:
        watcher = None

    started = time.monotonic()
    result = subprocess.run(curl_args, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if watcher is not None:
        stop_event.set()
        watcher.join(timeout=2)
    elapsed = round(time.monotonic() - started, 1)

    if result.returncode != 0:
        stderr_tail = (result.stderr or b"").decode("utf-8", "replace").strip()[-300:]
        raise InstallerError(
            f"安装包下载失败（退出码 {result.returncode}）",
            error_code="release_download_failed",
            next_action="retry_download_release",
            details={
                "path": str(partial),
                "partial_bytes": partial.stat().st_size if partial.exists() else 0,
                "resume_supported": resume_supported,
                "stderr_tail": stderr_tail or None,
            },
        )

    downloaded = partial.stat().st_size
    digest = _sha256_of(partial)
    expected_sha256s = list(dict.fromkeys(expected_sha256s))
    reporter.progress("download", "下载完成，正在校验安装包完整性")
    if expected_sha256s and digest not in expected_sha256s:
        # A complete-but-invalid artifact must not be resumed on the next
        # attempt; only interrupted transfers remain resumable.
        partial.unlink(missing_ok=True)
        raise InstallerError(
            "下载文件未通过完整性检查，已停止操作，当前微信没有被修改。",
            error_code="release_artifact_integrity_mismatch",
            next_action="try_next_verified_source_or_retry",
            details={
                "path": str(partial),
                "bytes": downloaded,
                "sha256": digest,
                "expected_sha256s": expected_sha256s,
                "expected_sizes": expected_sizes,
            },
        )
    if expected_sizes and digest not in expected_sizes:
        partial.unlink(missing_ok=True)
        raise InstallerError(
            "下载文件未通过完整性检查（缺少匹配的文件大小），已停止操作，当前微信没有被修改。",
            error_code="release_artifact_integrity_mismatch",
            next_action="try_next_verified_source_or_retry",
            details={"path": str(partial), "bytes": downloaded, "sha256": digest},
        )
    if digest in expected_sizes and expected_sizes[digest] != downloaded:
        partial.unlink(missing_ok=True)
        raise InstallerError(
            "下载文件未通过完整性检查（大小不匹配），已停止操作，当前微信没有被修改。",
            error_code="release_artifact_integrity_mismatch",
            next_action="try_next_verified_source_or_retry",
            details={
                "path": str(partial),
                "bytes": downloaded,
                "expected_bytes": expected_sizes[digest],
            },
        )

    # Publish atomically only after both digest and artifact-size checks pass.
    os.replace(partial, target)
    _write_download_receipt(target, url=url, digest=digest, size=downloaded)
    return {
        "ok": True,
        "command": "download-release",
        "path": str(target),
        "bytes": downloaded,
        "sha256": digest,
        "resume_supported": resume_supported,
        "reused": False,
        "elapsed_seconds": elapsed,
        "next_step": "replace-wechat",
    }


def _mount_dmg(dmg: Path) -> Path:
    """只读挂载 DMG，返回挂载点路径。"""
    result = subprocess.run(
        ["/usr/bin/hdiutil", "attach", "-plist", "-nobrowse", "-readonly", str(dmg)],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise InstallerError(
            f"无法挂载安装包（退出码 {result.returncode}）",
            error_code="dmg_mount_failed",
            next_action="verify_dmg_integrity_and_retry",
            details={"path": str(dmg), "stderr_tail": result.stderr.strip()[-300:] or None},
        )
    try:
        payload = plistlib.loads(result.stdout.encode("utf-8"))
    except Exception:
        payload = {}
    mount_points: list[str] = []
    for entity in payload.get("system-entities", []):
        point = entity.get("mount-point")
        if point:
            mount_points.append(point)
    if not mount_points:
        raise InstallerError(
            "挂载安装包后未找到卷宗路径",
            error_code="dmg_mount_failed",
            next_action="verify_dmg_integrity_and_retry",
        )
    return Path(mount_points[-1])


def _bundle_version(app: Path) -> tuple[str, str]:
    """读取 app 的 Bundle ID 和短版本号。"""
    info = app / "Contents" / "Info.plist"
    bundle_id = subprocess.run(
        ["/usr/libexec/PlistBuddy", "-c", "Print :CFBundleIdentifier", str(info)],
        check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()
    version = subprocess.run(
        ["/usr/libexec/PlistBuddy", "-c", "Print :CFBundleShortVersionString", str(info)],
        check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()
    return bundle_id, version


def _darwin_version_ranges() -> list[tuple[str, str]]:
    """从版本守卫策略中读取 macOS 微信支持区间。"""
    policy_path = Path(__file__).resolve().parent / "version-guard.policy.json"
    if not policy_path.is_file():
        raise InstallerError(
            "缺少微信版本兼容策略",
            error_code="version_policy_unavailable",
            next_action="reinstall_current_release_and_retry",
        )
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InstallerError(
            "微信版本兼容策略无法读取",
            error_code="version_policy_unavailable",
            next_action="reinstall_current_release_and_retry",
        ) from exc
    ranges = [
        (str(item["min_version"]), str(item["max_version"]))
        for item in policy.get("version_guard", {}).get("allowed_version_ranges", [])
        if item.get("platform") == "darwin"
    ]
    if not ranges:
        raise InstallerError(
            "微信版本兼容策略为空",
            error_code="version_policy_unavailable",
            next_action="reinstall_current_release_and_retry",
        )
    return ranges


def replace_wechat(args: argparse.Namespace, reporter: Reporter) -> dict:
    """用本地已校验的安装包替换微信：自动备份旧版，失败时回滚。"""
    if platform.system().lower() != "darwin":
        raise InstallerError(
            "replace-wechat 仅支持 macOS",
            error_code="unsupported_platform",
            next_action="run_replace_wechat_on_macos",
        )
    if not getattr(args, "confirm_replace", False):
        raise InstallerError(
            "替换微信前需要用户确认",
            error_code="replace_confirmation_required",
            next_action="confirm_replace_and_retry",
        )
    dmg = Path(args.dmg).expanduser()
    if not dmg.is_file():
        raise InstallerError(
            "找不到指定的安装包文件",
            error_code="dmg_not_found",
            next_action="run_download_release_first",
            details={"path": str(dmg)},
        )
    receipt = _read_download_receipt(dmg)
    if (
        not receipt
        or not _receipt_matches_release_catalog(receipt)
        or receipt.get("sha256") != _sha256_of(dmg)
        or receipt.get("size") != dmg.stat().st_size
    ):
        raise InstallerError(
            "安装包尚未通过完整性检查，请重新下载",
            error_code="release_artifact_not_verified",
            next_action="run_download_release_first",
        )

    running = _wechat_running_pids()
    if running:
        raise InstallerError(
            "微信仍在运行。替换微信前需要先完全退出微信。",
            error_code="wechat_must_quit_for_replace",
            next_action="quit_wechat_and_retry_replace_wechat",
            details={"authorization_prompt_count": 0, "running_pids": running},
        )

    app_path = Path("/Applications/WeChat.app")
    mount_point = _mount_dmg(dmg)
    try:
        candidate = mount_point / "WeChat.app"
        if not candidate.is_dir():
            raise InstallerError(
                "安装包内没有找到 WeChat.app",
                error_code="dmg_content_invalid",
                next_action="verify_dmg_integrity_and_retry",
                details={"mount_point": str(mount_point)},
            )
        bundle_id, new_version = _bundle_version(candidate)
        if bundle_id != "com.tencent.xinWeChat":
            raise InstallerError(
                f"安装包 Bundle ID 校验失败：{bundle_id or '未知'}",
                error_code="release_bundle_id_mismatch",
                next_action="reject_candidate_and_try_next_verified_source",
                details={"bundle_id": bundle_id, "path": str(dmg)},
            )
        supported = _darwin_version_ranges()
        if supported and not _version_in_ranges(new_version, supported):
            raise InstallerError(
                f"安装包版本 {new_version} 不在版本守卫支持区间内",
                error_code="version_not_allowed",
                next_action="search_public_sources_for_supported_release",
                details={"detected_version": new_version, "supported_versions": supported},
            )

        old_version: str | None = None
        backup_path: Path | None = None
        if app_path.exists():
            old_bundle_id, old_version = _bundle_version(app_path)
            if old_bundle_id != "com.tencent.xinWeChat":
                raise InstallerError(
                    f"/Applications/WeChat.app 的 Bundle ID 异常：{old_bundle_id or '未知'}，已停止替换",
                    error_code="existing_app_unexpected",
                    next_action="inspect_existing_app_and_retry",
                    details={"bundle_id": old_bundle_id},
                )
            backup_dir = default_layout(
                Path(args.home).expanduser() if args.home else None
            ).data_dir / "backups"
            backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_path = backup_dir / f"WeChat_{old_version}_{stamp}.app"
            reporter.progress("replace-wechat", "正在备份当前微信")
            shutil.move(str(app_path), str(backup_path))

        try:
            reporter.progress("replace-wechat", "正在安装新版本微信")
            subprocess.run(
                ["/usr/bin/ditto", str(candidate), str(app_path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except subprocess.CalledProcessError as exc:
            # ditto may leave a partial destination behind. Remove it before
            # restoring the known-good application.
            if app_path.exists():
                shutil.rmtree(app_path, ignore_errors=True)
            if backup_path is not None and backup_path.exists() and not app_path.exists():
                shutil.move(str(backup_path), str(app_path))
            raise InstallerError(
                "安装新版本微信失败，已恢复原有微信",
                error_code="replace_wechat_failed",
                next_action="retry_replace_wechat_and_report_the_error",
                details={"stderr_tail": (exc.stderr or b"").decode("utf-8", "replace").strip()[-300:] or None},
            ) from exc

        installed_bundle_id, installed_version = _bundle_version(app_path)
        if installed_bundle_id != "com.tencent.xinWeChat" or installed_version != new_version:
            if app_path.exists():
                shutil.rmtree(app_path, ignore_errors=True)
            if backup_path is not None and backup_path.exists() and not app_path.exists():
                shutil.move(str(backup_path), str(app_path))
            raise InstallerError(
                "安装后校验失败，已恢复原有微信",
                error_code="replace_wechat_verification_failed",
                next_action="retry_replace_wechat",
                details={
                    "installed_bundle_id": installed_bundle_id,
                    "installed_version": installed_version,
                    "expected_version": new_version,
                },
            )

        return {
            "ok": True,
            "command": "replace-wechat",
            "old_version": old_version,
            "new_version": new_version,
            "backup_path": str(backup_path) if backup_path else None,
            "app_path": str(app_path),
            "next_step": "prepare_wechat",
        }
    finally:
        subprocess.run(
            ["/usr/bin/hdiutil", "detach", str(mount_point)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="安装和维护本机消息 MCP")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--home", default=None, help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)

    install_parser = subparsers.add_parser("install", help="部署独立运行时")
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
        "--fallback-repository",
        dest="fallback_repositories",
        action="append",
        default=[],
        help="用户明确确认的备用发布仓库，可重复指定",
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
    inspect_parser = subparsers.add_parser("inspect", help="只读检查并返回下一安装阶段")
    inspect_parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    update_parser = subparsers.add_parser("check-update", help="检查 main 发布通道是否有新版本")
    update_parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    upgrade_parser = subparsers.add_parser("upgrade", help="经用户确认后升级到 main 最新版本")
    upgrade_parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    repair_parser = subparsers.add_parser("repair", help="按安装清单修复 LaunchAgent")
    repair_parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    enable_service_parser = subparsers.add_parser(
        "enable-service", help="初始化完成后安装并验证 LaunchAgent"
    )
    enable_service_parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    initialize_parser = subparsers.add_parser("initialize", help="经用户确认后提取密钥并预解密本机数据库")
    initialize_parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    # 兼容可能仍传入旧参数的 Agent；该参数不再授权 initialize 修改 WeChat。
    initialize_parser.add_argument("--confirm-resign", action="store_true", help=argparse.SUPPRESS)
    prepare_parser = subparsers.add_parser("prepare-wechat", help="经用户确认后安全重签 WeChat")
    prepare_parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    prepare_parser.add_argument("--confirm-resign", action="store_true", help="确认允许修改 WeChat.app 签名")
    accounts_parser = subparsers.add_parser("accounts", help="列出检测到的微信账号数据目录")
    accounts_parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    select_account_parser = subparsers.add_parser("select-account", help="选择初始化使用的微信账号")
    select_account_parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    select_account_parser.add_argument("--account", required=True, help="accounts 返回的 account_id")
    uninstall_parser = subparsers.add_parser("uninstall", help="卸载 LaunchAgent，默认保留全部数据和运行时")
    uninstall_parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    uninstall_parser.add_argument("--remove-runtime", action="store_true")
    quit_parser = subparsers.add_parser("quit-wechat", help="请求微信正常退出，未退出时等待用户处理")
    quit_parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    download_parser = subparsers.add_parser(
        "download-release",
        help="下载并校验微信安装包（Range 探测、断点续传、进度事件、完整性校验）",
    )
    download_parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    download_parser.add_argument(
        "--version", default=None, help="从发布目录选择 macOS 微信版本（如 4.1.8）"
    )
    download_parser.add_argument("--url", default=None, help="自定义下载地址（与 --version 二选一）")
    download_parser.add_argument("--sha256", default=None, help="自定义来源的期望 SHA-256")
    download_parser.add_argument("--size", type=int, default=None, help="自定义来源的期望文件大小（字节）")
    download_parser.add_argument("--output", default=None, help="下载保存路径（默认在数据目录 downloads 下）")
    download_parser.add_argument("--confirm-download", action="store_true", help="确认下载兼容版本")
    replace_parser = subparsers.add_parser(
        "replace-wechat",
        help="用本地已校验的安装包替换微信（自动备份旧版，失败回滚）",
    )
    replace_parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    replace_parser.add_argument("--dmg", required=True, help="本地微信安装包路径（download-release 的输出）")
    replace_parser.add_argument("--confirm-replace", action="store_true", help="确认替换当前微信")
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
        _require_non_root_management()
        handlers = {
            "install": install,
            "inspect": inspect,
            "status": status,
            "check-update": check_update,
            "upgrade": upgrade,
            "repair": repair,
            "enable-service": enable_service,
            "initialize": initialize,
            "prepare-wechat": prepare_wechat,
            "accounts": accounts,
            "select-account": select_account,
            "uninstall": uninstall,
            "quit-wechat": quit_wechat,
            "download-release": download_release,
            "replace-wechat": replace_wechat,
        }
        payload = handlers[args.command](args, reporter)
        reporter.result(payload)
        return 0 if payload.get("ok") else 1
    except InstallerError as exc:
        payload = {
            "ok": False,
            "command": args.command,
            "error_code": exc.error_code,
            "error": str(exc),
            "user_message": _plain_user_message(exc),
        }
        if exc.next_action:
            payload["next_action"] = exc.next_action
        if exc.details:
            payload["details"] = exc.details
            if "authorization_prompt_count" in exc.details:
                payload["authorization_prompt_count"] = exc.details[
                    "authorization_prompt_count"
                ]
            for key in ("responsible_app", "settings_opened", "settings_pane"):
                if key in exc.details:
                    payload[key] = exc.details[key]
        user_actions = {
            "wechat_app_not_found": "ensure_wechat_installed",
            "wechat_not_running": "open_and_sign_in_wechat",
            "wechat_not_adhoc_signed": "confirm_wechat_resign",
            "wechat_resign_confirmation_required": "confirm_wechat_resign",
            "wechat_must_quit_for_resign": "quit_wechat",
            "wechat_must_quit_for_replace": "quit_wechat",
            "wechat_quit_failed": "quit_wechat",
            "app_management_permission_required": "enable_app_management",
            "administrator_authorization_cancelled": "approve_administrator_prompt",
            "wechat_process_access_failed": "keep_wechat_open_and_signed_in",
            "version_not_allowed": "search_public_sources_for_supported_release",
            "wechat_account_not_found": "select_wechat_account",
            "download_confirmation_required": "confirm_download",
            "replace_confirmation_required": "confirm_replace",
        }
        if exc.error_code in user_actions:
            payload["requires_user_action"] = user_actions[exc.error_code]
        retry_commands = {
            "wechat_app_not_found": "inspect",
            "wechat_not_adhoc_signed": "prepare-wechat --confirm-resign",
            "wechat_resign_confirmation_required": "prepare-wechat --confirm-resign",
            "wechat_must_quit_for_resign": "prepare-wechat --confirm-resign",
            "wechat_must_quit_for_replace": "quit-wechat",
            "wechat_quit_failed": "quit-wechat",
            "app_management_permission_required": "prepare-wechat --confirm-resign",
            "wechat_process_access_failed": "inspect",
            "wechat_account_not_found": "accounts",
            "download_confirmation_required": "download-release --confirm-download",
            "replace_confirmation_required": "replace-wechat --confirm-replace",
        }
        if exc.error_code in retry_commands:
            payload["retry_command"] = retry_commands[exc.error_code]
        elif args.command in {
            "initialize", "prepare-wechat", "select-account", "enable-service", "repair"
        }:
            payload["retry_command"] = args.command
        reporter.result(payload)
        return 1
    except Exception as exc:
        unexpected = InstallerError(
            f"{type(exc).__name__}: {exc}",
            error_code="unexpected_management_error",
        )
        reporter.result(
            {
                "ok": False,
                "command": args.command,
                "error_code": "unexpected_management_error",
                "error": f"{type(exc).__name__}: {exc}",
                "user_message": _plain_user_message(unexpected),
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
