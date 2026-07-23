"""WeChat client version guard.

The guard is intentionally fail-closed once enabled: an unknown version,
missing app path, or out-of-range version blocks business actions.
"""

from __future__ import annotations

import hashlib
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

# The default policy is intentionally pinned outside the policy file itself.
# A mutable policy must not be able to redefine its own trust boundary.
_DEFAULT_POLICY_NAME = "version-guard.policy.json"
_DEFAULT_POLICY_SHA256 = "f77a3fcb703a2978d2abe0ddcd16dd08b291db826ea0db9edd40bdb703edf6a5"


def _guard_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    guard = cfg.get("version_guard") or {}
    if not isinstance(guard, dict):
        guard = {}
    return guard


def is_enabled(cfg: Dict[str, Any]) -> bool:
    return bool(_guard_config(cfg).get("enabled", False))


def _policy_path(cfg: Dict[str, Any]) -> str:
    """Return the policy path recorded by config loading, if available."""
    path = cfg.get("version_guard_policy_path")
    return _expand_path(str(path)) if path else ""


def _default_policy_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), _DEFAULT_POLICY_NAME)


def _trusted_policy_sha256(policy_path: str) -> str:
    """Return the built-in trust anchor for the only accepted policy path."""
    default_path = _default_policy_path()
    if policy_path and os.path.realpath(policy_path) == os.path.realpath(default_path):
        return _DEFAULT_POLICY_SHA256
    return ""


def _canonical_policy_sha256(policy_path: str) -> str:
    """Hash policy meaning, not platform-dependent JSON whitespace or newlines."""
    with open(policy_path, encoding="utf-8") as policy_file:
        data = json.load(policy_file)
    canonical = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _check_policy_integrity(cfg: Dict[str, Any]) -> tuple[List[str], Dict[str, Any]]:
    """Fail closed when a loaded policy is missing, invalid, or unexpectedly changed.

    Direct unit callers may provide an in-memory version_guard without a policy
    path; that remains supported. Production config loading records the policy
    path, which activates this check for the real MCP workflow.
    """
    policy_path = _policy_path(cfg)
    if not policy_path:
        if cfg.get("_version_guard_policy_required"):
            return ["版本门禁策略文件不存在，拒绝执行"], {}
        return [], {}

    details: Dict[str, Any] = {"policy_path": policy_path}
    if not os.path.isfile(policy_path):
        return [f"版本门禁策略文件不存在: {policy_path}"], details

    if os.path.realpath(policy_path) != os.path.realpath(_default_policy_path()):
        return [
            "版本门禁策略路径不是受信任的默认文件，拒绝使用自定义策略"
        ], details

    expected = _trusted_policy_sha256(policy_path)
    if not expected:
        return ["版本门禁没有内置可信摘要，拒绝执行"], details

    try:
        actual = _canonical_policy_sha256(policy_path)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return [f"无法读取版本门禁策略文件: {exc}"], details

    details["policy_sha256"] = actual
    details["expected_policy_sha256"] = expected
    if actual != expected:
        return [
            "版本门禁策略完整性校验失败；文件可能已被修改，"
            "拒绝继续执行"
        ], details
    return [], details


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


def _discover_macos_app_path() -> str:
    """Find a locally installed WeChat bundle without requiring its process.

    The MCP service only needs the bundle metadata for its startup version
    gate.  Reading the bundle is safe while WeChat is closed; process memory
    remains required by the separate key-extraction path.
    """
    if _platform_key() != "darwin":
        return ""

    candidates = (
        "/Applications/WeChat.app",
        os.path.expanduser("~/Applications/WeChat.app"),
    )
    for candidate in candidates:
        bundle_path = _macos_bundle_path(candidate)
        info_path = os.path.join(bundle_path, "Contents", "Info.plist")
        if not os.path.isfile(info_path):
            continue
        try:
            with open(info_path, "rb") as info_file:
                info = plistlib.load(info_file)
        except (OSError, plistlib.InvalidFileException, ValueError):
            continue
        if info.get("CFBundleIdentifier") == "com.tencent.xinWeChat":
            return bundle_path
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
    if not app_path:
        app_path = _discover_macos_app_path()
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


def _process_path_for_pid(pid: int) -> str:
    """Resolve the executable that will be touched by a risky action."""
    current = _platform_key()
    if current == "darwin":
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        command = (proc.stdout or "").strip()
        if proc.returncode != 0 or not command:
            raise RuntimeError(f"无法读取微信进程 PID={pid} 的可执行文件路径")
        return _expand_path(command.split()[0])
    if current == "linux":
        try:
            return os.path.realpath(os.readlink(f"/proc/{pid}/exe"))
        except OSError as exc:
            raise RuntimeError(f"无法读取微信进程 PID={pid} 的可执行文件路径: {exc}") from exc
    if current == "windows":
        script = (
            "$p=Get-CimInstance Win32_Process -Filter \"ProcessId=$($args[0])\"; "
            "if ($null -ne $p) { $p.ExecutablePath }"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script, str(pid)],
            capture_output=True,
            text=True,
            timeout=8,
        )
        path = (proc.stdout or "").strip()
        if proc.returncode != 0 or not path:
            raise RuntimeError(
                (proc.stderr or "").strip()
                or f"无法读取微信进程 PID={pid} 的可执行文件路径"
            )
        return _expand_path(path.splitlines()[0])
    raise RuntimeError(f"暂不支持校验进程版本的平台: {current}")


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
    integrity_reasons, integrity_details = _check_policy_integrity(cfg)
    if integrity_reasons:
        return VersionCheckResult(
            enabled=True,
            ok=False,
            reasons=integrity_reasons,
            details={"policy_integrity": integrity_details},
        )

    if not is_enabled(cfg):
        return VersionCheckResult(enabled=False, ok=True, details={"status": "disabled"})

    reasons: List[str] = []
    details: Dict[str, Any] = {}
    if integrity_details:
        details["policy_integrity"] = integrity_details

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


def _allowed_version_summary(cfg: Dict[str, Any]) -> str:
    guard = _guard_config(cfg)
    current = _platform_key()
    platform_name = {
        "darwin": "macOS",
        "windows": "Windows",
        "linux": "Linux",
    }.get(current, current)
    versions: List[str] = []

    for item in _allowed_version_ranges(guard):
        if item.get("platform") and str(item["platform"]).lower() != current:
            continue
        exact = item.get("version") or item.get("short_version")
        minimum = item.get("min_version") or item.get("start_version")
        maximum = item.get("max_version") or item.get("end_version")
        if exact or (minimum and maximum and str(minimum) == str(maximum)):
            versions.append(str(exact or minimum))
        elif minimum and maximum:
            versions.append(f"{minimum} 至 {maximum}")
        elif minimum:
            versions.append(f"{minimum} 及以上")
        elif maximum:
            versions.append(f"{maximum} 及以下")

    for item in _allowed_versions(guard):
        if item.get("platform") and str(item["platform"]).lower() != current:
            continue
        version = item.get("short_version") or item.get("version")
        if version:
            versions.append(str(version))

    unique_versions = list(dict.fromkeys(versions))
    return f"{platform_name} {'、'.join(unique_versions)}" if unique_versions else f"{platform_name}（未配置）"


def _doctor_command() -> str:
    return "python main.py doctor" if _platform_key() == "windows" else "python3 main.py doctor"


def _humanize_reason(reason: str) -> str:
    reason = str(reason or "未知原因")
    if "version_guard.enabled=true 但未配置 allowed_version_ranges" in reason:
        return "版本门禁已经启用，但没有配置当前平台允许使用的微信版本。"
    if "版本门禁策略文件不存在" in reason:
        return "版本门禁策略文件不存在，无法确认允许的微信版本。"
    if "版本门禁策略路径不是受信任的默认文件" in reason:
        return "检测到自定义版本门禁策略；生产流程只接受受信任的默认策略文件。"
    if "没有内置可信摘要" in reason:
        return "版本门禁没有内置可信摘要，已按安全策略停止。"
    if "无法读取版本门禁策略文件" in reason:
        return "版本门禁策略文件无法读取，已按安全策略停止。"
    if "版本门禁策略完整性校验失败" in reason:
        return "版本门禁策略文件内容已变化，不能通过修改策略来放行当前版本。"
    if "未配置 wechat_app_path" in reason:
        return (
            "没有找到微信安装位置。请先启动微信，或在 config.json 中设置 "
            "wechat_app_path。"
        )
    if "当前微信版本不在允许区间" in reason:
        version = reason.rsplit(":", 1)[-1].strip() if "：" not in reason else reason.rsplit("：", 1)[-1].strip()
        return f"检测到的微信版本 {version} 不在本项目已经验证的安全范围内。"
    if "要求 version_guard.enabled=true" in reason:
        return "风险操作必须启用版本门禁；当前门禁未启用，因此已按安全策略停止。"
    if "未提供可校验的微信进程 PID" in reason:
        return "没有找到可以校验版本的微信进程，无法安全执行后续操作。"
    if "当前平台未实现自动升级状态检测" in reason:
        return "当前平台无法可靠确认微信自动更新是否关闭。"
    if "微信 4.x 自动升级开关无法可靠检测" in reason:
        return "微信 4.x 的自动更新开关无法可靠读取，请在微信设置中手动关闭。"
    if "旧版微信 Sparkle 自动更新未关闭" in reason:
        return "检测到微信自动更新仍然开启，请先关闭自动更新。"
    return reason


def _detected_version_lines(detected: Dict[str, Any], label: str = "当前微信") -> List[str]:
    if not detected:
        return []
    lines = [
        f"检测到：{label}，版本 {detected.get('short_version') or '未知'}"
        f"（build {detected.get('build_version') or '未知'}）"
    ]
    if detected.get("app_path"):
        lines.append(f"程序路径：{detected['app_path']}")
    return lines


def _failure_guidance(cfg: Dict[str, Any], result: VersionCheckResult) -> List[str]:
    reasons = [_humanize_reason(reason) for reason in result.reasons]
    lines = ["失败原因："]
    lines.extend(f"  - {reason}" for reason in reasons)
    lines.extend(["", "处理建议："])

    raw_reasons = "；".join(result.reasons)
    if (
        "版本门禁策略" in raw_reasons
        or "版本门禁没有内置可信摘要" in raw_reasons
    ):
        lines.extend(
            [
                "  1. 恢复受信任的 version-guard.policy.json，不要通过修改策略放行新版本。",
                "  2. 如果需要支持新版本，应发布包含新策略和新内置摘要的受信任版本。",
                f"  3. 运行 `{_doctor_command()}` 重新检查。",
            ]
        )
    elif "要求 version_guard.enabled=true" in raw_reasons:
        lines.extend(
            [
                "  1. 确认程序目录中存在 version-guard.policy.json。",
                "  2. 确认其中的 version_guard.enabled 为 true，并配置当前平台允许版本。",
                f"  3. 运行 `{_doctor_command()}` 重新检查。",
            ]
        )
    elif "未配置 allowed_version_ranges" in raw_reasons:
        lines.extend(
            [
                "  1. 检查 version-guard.policy.json 是否随程序一起部署。",
                "  2. 为当前平台配置经过验证的微信版本。",
                f"  3. 运行 `{_doctor_command()}` 重新检查。",
            ]
        )
    elif (
        "未提供可校验的微信进程 PID" in raw_reasons
        or "无法读取微信进程 PID" in raw_reasons
        or "进程已退出" in raw_reasons
    ):
        lines.extend(
            [
                "  1. 确认微信已经启动并完成登录。",
                "  2. 如果微信刚刚更新或重启，请等待进程稳定后再试。",
                f"  3. 运行 `{_doctor_command()}` 确认版本通过，再重新执行。",
            ]
        )
    elif "未配置 wechat_app_path" in raw_reasons or "安装路径不存在" in raw_reasons:
        lines.extend(
            [
                "  1. 启动并登录微信。",
                "  2. 检查 config.json 中的 wechat_app_path 是否指向实际微信程序。",
                f"  3. 运行 `{_doctor_command()}` 重新检查。",
            ]
        )
    elif "不在允许区间" in raw_reasons:
        lines.extend(
            [
                "  1. 退出当前微信。",
                f"  2. 安装当前允许的微信版本：{_allowed_version_summary(cfg)}。",
                "  3. 在微信设置中关闭自动更新，避免再次后台升级。",
                "  4. 重新启动并登录微信。",
                f"  5. 运行 `{_doctor_command()}`，看到“检查通过”后再重试。",
                "",
                "不要直接修改 version-guard.policy.json 放行未验证的新版本。",
            ]
        )
    else:
        lines.extend(
            [
                "  1. 按上面的失败原因修正微信版本或门禁配置。",
                f"  2. 运行 `{_doctor_command()}`，看到“检查通过”后再重试。",
            ]
        )
    return lines


def _allowed_summary_for_result(cfg: Dict[str, Any], result: VersionCheckResult) -> str:
    """Never display an untrusted policy's version range as authoritative."""
    if any("版本门禁策略" in str(reason) for reason in result.reasons):
        return "未读取（策略完整性校验未通过）"
    return _allowed_version_summary(cfg)


def format_report(
    result: VersionCheckResult,
    cfg: Optional[Dict[str, Any]] = None,
) -> str:
    """Human-readable diagnostic report used by status and doctor."""
    cfg = cfg or {}
    if not result.enabled:
        return "\n".join(
            [
                "[版本门禁] 未启用",
                "安全提示：普通离线操作可以继续，但获取密钥等风险操作仍会要求启用门禁。",
                "处理建议：检查 version-guard.policy.json 是否存在并已启用。",
            ]
        )

    title = "[版本门禁] 检查通过" if result.ok else "[版本门禁] 检查失败"
    lines = [title]
    lines.extend(_detected_version_lines(result.details.get("detected") or {}))
    lines.append(f"当前允许：{_allowed_summary_for_result(cfg, result)}")

    if result.ok:
        lines.append("检查结果：当前微信版本符合安全策略。")
    else:
        lines.extend(_failure_guidance(cfg, result))

    if result.details.get("update_notice"):
        lines.append(f"自动更新提示：{result.details['update_notice']}")
    update_settings = result.details.get("update_settings") or {}
    if update_settings:
        status = "已关闭" if update_settings.get("update_disabled") else "仍开启"
        lines.append(f"自动更新状态：{status}")
        if update_settings.get("prefs_path"):
            lines.append(f"设置来源：{update_settings['prefs_path']}")
    return "\n".join(lines)


def format_block_report(
    cfg: Dict[str, Any],
    result: VersionCheckResult,
    *,
    action: str,
) -> str:
    lines = [
        "",
        "============================================================",
        f"[安全拦截] 已停止：{action}",
        "============================================================",
    ]
    lines.extend(_detected_version_lines(result.details.get("detected") or {}))
    lines.append(f"当前允许：{_allowed_summary_for_result(cfg, result)}")
    lines.extend(_failure_guidance(cfg, result))
    lines.extend(
        [
            "",
            f"安全状态：版本检查未通过，尚未执行“{action}”。",
        ]
    )
    return "\n".join(lines)


def format_risky_action_report(
    cfg: Dict[str, Any],
    result: VersionCheckResult,
) -> str:
    action = str(result.details.get("action") or "风险操作")
    lines = [
        "",
        "============================================================",
        f"[安全拦截] 已停止：{action}" if not result.ok else f"[安全检查] 可以执行：{action}",
        "============================================================",
    ]

    targets = result.details.get("targets") or []
    detected_any = False
    for target in targets:
        detected = ((target.get("result") or {}).get("detected") or {})
        if not detected:
            continue
        detected_any = True
        pid = target.get("pid")
        label = f"PID {pid}" if pid is not None else "目标微信"
        lines.append(
            f"检测到：{label}，微信 {detected.get('short_version') or '未知版本'}"
            f"（build {detected.get('build_version') or '未知'}）"
        )
        if detected.get("app_path"):
            lines.append(f"程序路径：{detected['app_path']}")

    lines.append(f"当前允许：{_allowed_summary_for_result(cfg, result)}")

    if result.ok:
        lines.append("检查结果：版本符合安全策略，继续执行。")
        return "\n".join(lines)

    lines.extend(_failure_guidance(cfg, result))
    if "进程内存" in action or "密钥" in action:
        lines.extend(
            [
                "",
                "安全状态：门禁未放行；本次检查后没有调用 task_for_pid/OpenProcess，"
                "没有继续读取微信进程内存，也没有提取密钥。",
            ]
        )
    else:
        lines.extend(["", "安全状态：该操作尚未执行，请先处理上述版本问题。"])
    return "\n".join(lines)


def enforce_or_exit(cfg: Dict[str, Any], *, action: str = "继续执行当前操作") -> None:
    result = check_version(cfg)
    if result.enabled and not result.ok:
        print(format_block_report(cfg, result, action=action), flush=True)
        sys.exit(2)
    if result.enabled:
        print(format_report(result, cfg), flush=True)


def check_risky_action(
    cfg: Dict[str, Any],
    *,
    action: str,
    pids: Optional[Iterable[int]] = None,
    app_path: Optional[str] = None,
) -> VersionCheckResult:
    """Fail-closed version check for signing and key extraction.

    Unlike MCP checks, this never uses a cached result. When PIDs are supplied,
    the executable behind every PID is checked instead of trusting a configured
    installation path that may have become stale after a background update.
    """
    integrity_reasons, integrity_details = _check_policy_integrity(cfg)
    if integrity_reasons:
        return VersionCheckResult(
            enabled=True,
            ok=False,
            reasons=integrity_reasons,
            details={"policy_integrity": integrity_details, "action": action},
        )

    if not is_enabled(cfg):
        return VersionCheckResult(
            enabled=True,
            ok=False,
            reasons=[f"风险动作“{action}”要求 version_guard.enabled=true"],
            details={"action": action, "status": "guard_required"},
        )

    targets: List[Dict[str, Any]] = []
    if pids is not None:
        pid_list = [int(pid) for pid in pids]
        if not pid_list:
            return VersionCheckResult(
                enabled=True,
                ok=False,
                reasons=[f"风险动作“{action}”未提供可校验的微信进程 PID"],
                details={"action": action, "status": "missing_pid"},
            )
        for pid in pid_list:
            try:
                target_path = _process_path_for_pid(pid)
            except Exception as exc:
                targets.append({"pid": pid, "error": str(exc)})
                continue
            targets.append({"pid": pid, "app_path": target_path})
    elif app_path:
        targets.append({"app_path": _expand_path(app_path)})
    else:
        targets.append({"app_path": None})

    reasons: List[str] = []
    checked_targets: List[Dict[str, Any]] = []
    for target in targets:
        label = f"PID={target['pid']}" if target.get("pid") is not None else "目标应用"
        if target.get("error"):
            reasons.append(f"{label}: {target['error']}")
            checked_targets.append(target)
            continue

        target_cfg = dict(cfg)
        if target.get("app_path"):
            target_cfg["wechat_app_path"] = target["app_path"]
        result = check_version(target_cfg)
        checked = dict(target)
        checked["result"] = result.details
        checked_targets.append(checked)
        if not result.ok:
            reasons.extend(f"{label}: {reason}" for reason in result.reasons)

    return VersionCheckResult(
        enabled=True,
        ok=not reasons,
        reasons=reasons,
        details={"action": action, "targets": checked_targets},
    )


def enforce_risky_action_or_exit(
    cfg: Dict[str, Any],
    *,
    action: str,
    pids: Optional[Iterable[int]] = None,
    app_path: Optional[str] = None,
) -> None:
    result = check_risky_action(cfg, action=action, pids=pids, app_path=app_path)
    print(format_risky_action_report(cfg, result), flush=True)
    if not result.ok:
        sys.exit(2)


def check_or_raise(
    cfg: Dict[str, Any],
    ttl_seconds: int = 30,
    *,
    action: str = "调用 MCP 工具",
) -> None:
    # Do not let the normal version-result TTL cache hide a policy mutation.
    integrity_reasons, integrity_details = _check_policy_integrity(cfg)
    if integrity_reasons:
        result = VersionCheckResult(
            enabled=True,
            ok=False,
            reasons=integrity_reasons,
            details={"policy_integrity": integrity_details},
        )
        raise RuntimeError(format_block_report(cfg, result, action=action))

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
        raise RuntimeError(format_block_report(cfg, result, action=action))
