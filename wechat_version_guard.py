"""WeChat client version guard.

The guard is intentionally fail-closed once enabled: an unknown version,
missing app path, or out-of-range version blocks business actions.
"""

from __future__ import annotations

import json
import os
import platform
import plistlib
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional


@dataclass
class VersionCheckResult:
    enabled: bool
    ok: bool
    reasons: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)

    @property
    def reason_text(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "未知原因"


_MCP_CACHE: Dict[str, Any] = {"expires_at": 0.0, "result": None}


def _guard_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    guard = cfg.get("version_guard") or {}
    if not isinstance(guard, dict):
        guard = {}
    return guard


def is_enabled(cfg: Dict[str, Any]) -> bool:
    return bool(_guard_config(cfg).get("enabled", False))


def _expand_path(path: Optional[str]) -> str:
    if not path:
        return ""
    return os.path.abspath(os.path.expanduser(os.path.expandvars(path)))


def _platform_key() -> str:
    system = platform.system().lower()
    if system == "darwin":
        return "darwin"
    if system == "windows":
        return "windows"
    if system == "linux":
        return "linux"
    return system


def _first_allowed_app_path(entries: Iterable[Dict[str, Any]]) -> str:
    current = _platform_key()
    for item in entries:
        if item.get("platform") and str(item.get("platform")).lower() != current:
            continue
        app_path = item.get("app_path")
        if app_path:
            return str(app_path)
    return ""


def _configured_app_path(cfg: Dict[str, Any], guard: Dict[str, Any]) -> str:
    app_path = cfg.get("wechat_app_path") or guard.get("wechat_app_path")
    if not app_path:
        app_path = _first_allowed_app_path(guard.get("allowed_version_ranges") or [])
    if not app_path:
        app_path = _first_allowed_app_path(guard.get("allowed_versions") or [])
    if not app_path:
        paths = _process_paths(cfg)
        if paths:
            app_path = paths[0]
    return _expand_path(str(app_path)) if app_path else ""


def _macos_bundle_path(path: str) -> str:
    path = _expand_path(path)
    marker = ".app"
    idx = path.find(marker)
    if idx >= 0:
        return path[: idx + len(marker)]
    return path


def _read_macos_app(app_path: str) -> Dict[str, Any]:
    bundle_path = _macos_bundle_path(app_path)
    plist_path = os.path.join(bundle_path, "Contents", "Info.plist")
    with open(plist_path, "rb") as f:
        info = plistlib.load(f)
    return {
        "platform": "darwin",
        "app_path": bundle_path,
        "bundle_id": info.get("CFBundleIdentifier", ""),
        "short_version": str(info.get("CFBundleShortVersionString", "")),
        "build_version": str(info.get("CFBundleVersion", "")),
        "plist_path": plist_path,
    }


def _macos_update_preferences_path(app_path: str) -> str:
    # Sandboxed macOS WeChat persists Sparkle update preferences here.
    home = os.path.expanduser("~")
    return os.path.join(
        home,
        "Library",
        "Containers",
        "com.tencent.xinWeChat",
        "Data",
        "Library",
        "Preferences",
        "com.tencent.xinWeChat.plist",
    )


def _read_macos_update_settings(app_path: str) -> Dict[str, Any]:
    prefs_path = _macos_update_preferences_path(app_path)
    with open(prefs_path, "rb") as f:
        prefs = plistlib.load(f)
    checks_enabled = prefs.get("SUEnableAutomaticChecks")
    auto_update_enabled = prefs.get("SUAutomaticallyUpdate")
    if not isinstance(checks_enabled, bool) or not isinstance(auto_update_enabled, bool):
        raise RuntimeError("微信自动更新偏好缺失或格式异常")
    update_disabled = (not checks_enabled) and (not auto_update_enabled)
    return {
        "prefs_path": prefs_path,
        "prefs_source": "legacy_sparkle_plist",
        "enable_automatic_checks": checks_enabled,
        "automatically_update": auto_update_enabled,
        "last_check_time": prefs.get("SULastCheckTime"),
        "skipped_version": prefs.get("SUSkippedVersion"),
        "update_status": "disabled" if update_disabled else "enabled",
        "update_disabled": update_disabled,
    }


def _macos_update_setting_supported(detected: Dict[str, Any]) -> bool:
    """Only WeChat 3.x exposes its UI update setting through the Sparkle plist."""
    version = _version_tuple(str(detected.get("short_version") or ""))
    return bool(version and version[0] < 4)


def _read_windows_app(app_path: str) -> Dict[str, Any]:
    script = (
        "$v=(Get-Item -LiteralPath $args[0]).VersionInfo; "
        "@{ProductVersion=$v.ProductVersion;FileVersion=$v.FileVersion} | ConvertTo-Json -Compress"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script, app_path],
        capture_output=True,
        text=True,
        timeout=8,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "无法读取 Windows 可执行文件版本")
    data = json.loads(proc.stdout or "{}")
    return {
        "platform": "windows",
        "app_path": app_path,
        "short_version": str(data.get("ProductVersion", "")),
        "build_version": str(data.get("FileVersion", "")),
    }


def _read_linux_app(cfg: Dict[str, Any], app_path: str) -> Dict[str, Any]:
    manual = cfg.get("wechat_version") or _guard_config(cfg).get("wechat_version") or {}
    if isinstance(manual, str):
        manual = {"short_version": manual}
    if not isinstance(manual, dict):
        manual = {}
    return {
        "platform": "linux",
        "app_path": app_path,
        "short_version": str(manual.get("short_version", "")),
        "build_version": str(manual.get("build_version", "")),
    }


def read_installed_version(cfg: Dict[str, Any]) -> Dict[str, Any]:
    guard = _guard_config(cfg)
    app_path = _configured_app_path(cfg, guard)
    if not app_path:
        raise RuntimeError("未配置 wechat_app_path，且未能从运行中的微信进程自动发现安装路径")

    current = _platform_key()
    if current == "darwin":
        if not os.path.exists(_macos_bundle_path(app_path)):
            raise RuntimeError(f"微信安装路径不存在: {app_path}")
        return _read_macos_app(app_path)
    if not os.path.exists(app_path):
        raise RuntimeError(f"微信安装路径不存在: {app_path}")
    if current == "windows":
        return _read_windows_app(app_path)
    if current == "linux":
        return _read_linux_app(cfg, app_path)
    raise RuntimeError(f"暂不支持的平台: {current}")


def _process_paths_macos(process_name: str) -> List[str]:
    proc = subprocess.run(["pgrep", "-x", process_name], capture_output=True, text=True)
    if proc.returncode != 0:
        return []
    paths = []
    for pid in proc.stdout.split():
        ps = subprocess.run(["ps", "-p", pid, "-o", "command="], capture_output=True, text=True)
        command = (ps.stdout or "").strip()
        if command:
            paths.append(command.split()[0])
    return paths


def _process_paths(cfg: Dict[str, Any]) -> List[str]:
    if _platform_key() == "darwin":
        return _process_paths_macos(str(cfg.get("wechat_process") or "WeChat"))
    return []


def _same_app_path(expected: str, actual: str) -> bool:
    expected_bundle = _macos_bundle_path(expected) if _platform_key() == "darwin" else expected
    actual_bundle = _macos_bundle_path(actual) if _platform_key() == "darwin" else actual
    return os.path.realpath(expected_bundle) == os.path.realpath(actual_bundle)


def _allowed_version_ranges(guard: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = guard.get("allowed_version_ranges") or []
    return [item for item in raw if isinstance(item, dict)]


def _allowed_versions(guard: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = guard.get("allowed_versions") or []
    return [item for item in raw if isinstance(item, dict)]


def _version_tuple(version: str) -> Optional[tuple]:
    parts = re.findall(r"\d+", str(version or ""))
    if not parts:
        return None
    return tuple(int(part) for part in parts)


def _compare_versions(left: str, right: str) -> Optional[int]:
    left_tuple = _version_tuple(left)
    right_tuple = _version_tuple(right)
    if left_tuple is None or right_tuple is None:
        return None
    width = max(len(left_tuple), len(right_tuple))
    left_norm = left_tuple + (0,) * (width - len(left_tuple))
    right_norm = right_tuple + (0,) * (width - len(right_tuple))
    if left_norm < right_norm:
        return -1
    if left_norm > right_norm:
        return 1
    return 0


def _matches_allowed(detected: Dict[str, Any], allowed: Dict[str, Any]) -> bool:
    current = _platform_key()
    if allowed.get("platform") and str(allowed.get("platform")).lower() != current:
        return False
    for key in ("bundle_id", "short_version", "build_version"):
        expected = allowed.get(key)
        if expected and str(detected.get(key, "")) != str(expected):
            return False
    app_path = allowed.get("app_path")
    if app_path and not _same_app_path(_expand_path(str(app_path)), str(detected.get("app_path") or "")):
        return False
    return bool(allowed.get("short_version") or allowed.get("build_version"))


def _matches_allowed_range(detected: Dict[str, Any], allowed_range: Dict[str, Any]) -> bool:
    current = _platform_key()
    if allowed_range.get("platform") and str(allowed_range.get("platform")).lower() != current:
        return False
    bundle_id = allowed_range.get("bundle_id")
    if bundle_id and str(detected.get("bundle_id", "")) != str(bundle_id):
        return False
    app_path = allowed_range.get("app_path")
    if app_path and not _same_app_path(_expand_path(str(app_path)), str(detected.get("app_path") or "")):
        return False

    detected_version = str(detected.get("short_version") or "")
    if not detected_version:
        return False

    min_version = allowed_range.get("min_version") or allowed_range.get("start_version")
    max_version = allowed_range.get("max_version") or allowed_range.get("end_version")
    exact_version = allowed_range.get("version") or allowed_range.get("short_version")

    if exact_version:
        cmp_exact = _compare_versions(detected_version, str(exact_version))
        return cmp_exact == 0
    if min_version:
        cmp_min = _compare_versions(detected_version, str(min_version))
        if cmp_min is None or cmp_min < 0:
            return False
    if max_version:
        cmp_max = _compare_versions(detected_version, str(max_version))
        if cmp_max is None or cmp_max > 0:
            return False
    return bool(min_version or max_version)


def check_version(cfg: Dict[str, Any]) -> VersionCheckResult:
    guard = _guard_config(cfg)
    if not is_enabled(cfg):
        return VersionCheckResult(enabled=False, ok=True, details={"status": "disabled"})

    reasons: List[str] = []
    details: Dict[str, Any] = {}
    ranges = _allowed_version_ranges(guard)
    allowed = _allowed_versions(guard)
    if not ranges and not allowed:
        reasons.append("version_guard.enabled=true 但未配置 allowed_version_ranges")

    try:
        detected = read_installed_version(cfg)
        details["detected"] = detected
    except Exception as exc:
        detected = {}
        details["error"] = str(exc)
        reasons.append(str(exc))

    range_match = bool(detected and ranges and any(_matches_allowed_range(detected, item) for item in ranges))
    exact_match = bool(detected and allowed and any(_matches_allowed(detected, item) for item in allowed))
    if detected and (ranges or allowed) and not (range_match or exact_match):
        reasons.append(
            "当前微信版本不在允许区间: "
            f"{detected.get('short_version') or '?'}"
        )

    if detected and _platform_key() == "darwin" and not _macos_update_setting_supported(detected):
        details["update_notice"] = (
            "微信 4.x 自动升级开关无法可靠读取；请在微信“设置 > 通用”中手动关闭。"
        )

    if guard.get("require_update_disabled", False):
        current = _platform_key()
        if current != "darwin":
            reasons.append(f"当前平台未实现自动升级状态检测: {current}")
        elif detected and not _macos_update_setting_supported(detected):
            reasons.append("微信 4.x 自动升级开关无法可靠检测")
        elif detected:
            try:
                update_settings = _read_macos_update_settings(str(detected.get("app_path") or ""))
                details["update_settings"] = update_settings
                if not update_settings.get("update_disabled", False):
                    reasons.append("旧版微信 Sparkle 自动更新未关闭")
            except Exception as exc:
                details["update_settings_error"] = str(exc)
                reasons.append(f"读取微信自动更新状态失败: {exc}")

    return VersionCheckResult(enabled=True, ok=not reasons, reasons=reasons, details=details)


def format_report(result: VersionCheckResult) -> str:
    if not result.enabled:
        return "[version] 微信版本门禁: DISABLED"
    lines = [f"[version] 微信版本门禁: {'PASS' if result.ok else 'FAIL'}"]
    detected = result.details.get("detected") or {}
    if detected:
        lines.extend(
            [
                f"  app_path      = {detected.get('app_path') or '?'}",
                f"  bundle_id     = {detected.get('bundle_id') or '?'}",
                f"  version       = {detected.get('short_version') or '?'}",
                f"  build         = {detected.get('build_version') or '?'}",
            ]
        )
    if result.reasons:
        lines.append("  reasons       = " + "；".join(result.reasons))
    if result.details.get("update_notice"):
        lines.append("  update_check  = unsupported (WeChat 4.x)")
        lines.append(f"  update_action = {result.details['update_notice']}")
    update_settings = result.details.get("update_settings") or {}
    if update_settings:
        lines.append(
            "  auto_update   = "
            + (
                "disabled"
                if update_settings.get("update_disabled")
                else "enabled"
            )
        )
        lines.append(f"  prefs_source  = {update_settings.get('prefs_source') or '?'}")
        lines.append(f"  prefs_path    = {update_settings.get('prefs_path') or '?'}")
        if update_settings.get("update_status"):
            lines.append(f"  update_status = {update_settings.get('update_status')}")
        if update_settings.get("last_check_time"):
            lines.append(f"  last_check    = {update_settings.get('last_check_time')}")
        if update_settings.get("skipped_version"):
            lines.append(f"  skipped_ver   = {update_settings.get('skipped_version')}")
    return "\n".join(lines)


def enforce_or_exit(cfg: Dict[str, Any]) -> None:
    result = check_version(cfg)
    if result.enabled:
        print(format_report(result), flush=True)
    if result.enabled and not result.ok:
        print("[!] 微信安全门禁拒绝执行，请根据 reasons 处理后重试。", flush=True)
        sys.exit(2)


def check_or_raise(cfg: Dict[str, Any], ttl_seconds: int = 30) -> None:
    if not is_enabled(cfg):
        return
    now = time.time()
    cached = _MCP_CACHE.get("result")
    if cached is not None and now < float(_MCP_CACHE.get("expires_at") or 0):
        result = cached
    else:
        result = check_version(cfg)
        _MCP_CACHE["result"] = result
        _MCP_CACHE["expires_at"] = now + ttl_seconds
    if not result.ok:
        raise RuntimeError(f"微信版本门禁拒绝执行: {result.reason_text}")
