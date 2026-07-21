import hashlib
import os
import plistlib
import tempfile
import unittest
from unittest.mock import patch

import wechat_version_guard as guard


def _make_macos_app(root, short_version="4.0.18", build_version="23110"):
    app_path = os.path.join(root, "WeChat.app")
    contents = os.path.join(app_path, "Contents")
    os.makedirs(contents)
    with open(os.path.join(contents, "Info.plist"), "wb") as f:
        plistlib.dump(
            {
                "CFBundleIdentifier": "com.tencent.xinWeChat",
                "CFBundleShortVersionString": short_version,
                "CFBundleVersion": build_version,
            },
            f,
        )
    return app_path


class WechatVersionGuardTests(unittest.TestCase):
    def test_default_policy_uses_pinned_trust_anchor(self):
        policy_path = os.path.join(os.path.dirname(guard.__file__), "version-guard.policy.json")

        with patch.dict(
            os.environ,
            {guard._POLICY_SHA256_ENV: "0" * 64},
            clear=False,
        ):
            reasons, details = guard._check_policy_integrity(
                {"version_guard_policy_path": policy_path}
            )

        self.assertEqual(reasons, [])
        self.assertEqual(details["policy_sha256"], guard._DEFAULT_POLICY_SHA256)

    def test_policy_integrity_rejects_modified_policy(self):
        with tempfile.TemporaryDirectory() as td:
            policy_path = os.path.join(td, "version-guard.policy.json")
            with open(policy_path, "wb") as f:
                f.write(b'{"version_guard":{"enabled":true}}')
            with open(policy_path, "rb") as f:
                expected = hashlib.sha256(f.read()).hexdigest()
            with open(policy_path, "wb") as f:
                f.write(b'{"version_guard":{"enabled":false}}')

            with patch.dict(
                os.environ,
                {guard._POLICY_SHA256_ENV: expected},
                clear=False,
            ):
                reasons, details = guard._check_policy_integrity(
                    {"version_guard_policy_path": policy_path}
                )

        self.assertTrue(reasons)
        self.assertIn("完整性校验失败", reasons[0])
        self.assertNotEqual(details["policy_sha256"], expected)

    def test_custom_policy_requires_external_trust_anchor(self):
        with tempfile.TemporaryDirectory() as td:
            policy_path = os.path.join(td, "custom-policy.json")
            with open(policy_path, "w", encoding="utf-8") as f:
                f.write('{"version_guard":{"enabled":true}}')

            with patch.dict(os.environ, {guard._POLICY_SHA256_ENV: ""}, clear=False):
                reasons, _ = guard._check_policy_integrity(
                    {"version_guard_policy_path": policy_path}
                )

        self.assertTrue(reasons)
        self.assertIn("未配置可信 SHA-256 摘要", reasons[0])

    def test_policy_integrity_blocks_before_version_probe(self):
        with tempfile.TemporaryDirectory() as td:
            policy_path = os.path.join(td, "custom-policy.json")
            with open(policy_path, "w", encoding="utf-8") as f:
                f.write('{"version_guard":{"enabled":true}}')

            cfg = {
                "version_guard_policy_path": policy_path,
                "version_guard": {
                    "enabled": True,
                    "allowed_version_ranges": [
                        {"platform": "darwin", "version": "4.1.8"}
                    ],
                },
            }
            with patch.dict(os.environ, {guard._POLICY_SHA256_ENV: ""}, clear=False), \
                 patch.object(guard, "read_installed_version") as read_version:
                result = guard.check_version(cfg)

        self.assertFalse(result.ok)
        self.assertIn("可信 SHA-256 摘要", result.reason_text)
        read_version.assert_not_called()

    def test_policy_integrity_failure_does_not_display_untrusted_range(self):
        with tempfile.TemporaryDirectory() as td:
            policy_path = os.path.join(td, "custom-policy.json")
            with open(policy_path, "w", encoding="utf-8") as f:
                f.write(
                    '{"version_guard":{"enabled":true,"allowed_version_ranges":'
                    '[{"platform":"darwin","version":"4.1.11"}]}}'
                )

            cfg = {
                "version_guard_policy_path": policy_path,
                "version_guard": {
                    "enabled": True,
                    "allowed_version_ranges": [
                        {"platform": "darwin", "version": "4.1.11"}
                    ],
                },
            }
            with patch.dict(os.environ, {guard._POLICY_SHA256_ENV: ""}, clear=False):
                result = guard.check_version(cfg)
                report = guard.format_report(result, cfg)

        self.assertFalse(result.ok)
        self.assertIn("策略完整性校验未通过", report)
        self.assertNotIn("当前允许：macOS 4.1.11", report)

    def test_mcp_rechecks_policy_integrity_around_version_cache(self):
        with tempfile.TemporaryDirectory() as td:
            policy_path = os.path.join(td, "custom-policy.json")
            with open(policy_path, "wb") as f:
                f.write(b"trusted-policy")
            with open(policy_path, "rb") as f:
                expected = hashlib.sha256(f.read()).hexdigest()

            cfg = {
                "version_guard_policy_path": policy_path,
                "version_guard": {"enabled": True},
            }
            previous_cache = dict(guard._MCP_CACHE)
            try:
                with patch.dict(
                    os.environ,
                    {guard._POLICY_SHA256_ENV: expected},
                    clear=False,
                ), patch.object(
                    guard,
                    "check_version",
                    return_value=guard.VersionCheckResult(enabled=True, ok=True),
                ) as check_version:
                    guard._MCP_CACHE.update({"result": None, "expires_at": 0.0})
                    guard.check_or_raise(cfg, ttl_seconds=300, action="测试 MCP 工具")
                    check_version.assert_called_once()

                    with open(policy_path, "wb") as f:
                        f.write(b"modified-policy")

                    with self.assertRaisesRegex(RuntimeError, "策略文件内容已变化"):
                        guard.check_or_raise(cfg, ttl_seconds=300, action="测试 MCP 工具")
            finally:
                guard._MCP_CACHE.clear()
                guard._MCP_CACHE.update(previous_cache)

    def test_disabled_guard_allows_legacy_config(self):
        result = guard.check_version({})

        self.assertFalse(result.enabled)
        self.assertTrue(result.ok)

    def test_macos_allowed_version_range_passes(self):
        with tempfile.TemporaryDirectory() as td, \
             patch.object(guard.platform, "system", return_value="Darwin"), \
             patch.object(guard, "_process_paths", return_value=[]):
            app_path = _make_macos_app(td)
            cfg = {
                "wechat_app_path": app_path,
                "version_guard": {
                    "enabled": True,
                    "allowed_version_ranges": [
                        {
                            "platform": "darwin",
                            "bundle_id": "com.tencent.xinWeChat",
                            "min_version": "4.0.18",
                            "max_version": "4.0.18",
                        }
                    ],
                },
            }

            result = guard.check_version(cfg)

        self.assertTrue(result.enabled)
        self.assertTrue(result.ok, result.reason_text)

    def test_macos_version_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as td, \
             patch.object(guard.platform, "system", return_value="Darwin"), \
             patch.object(guard, "_process_paths", return_value=[]):
            app_path = _make_macos_app(td, short_version="4.0.20", build_version="23897")
            cfg = {
                "wechat_app_path": app_path,
                "version_guard": {
                    "enabled": True,
                    "allowed_version_ranges": [
                        {
                            "platform": "darwin",
                            "bundle_id": "com.tencent.xinWeChat",
                            "min_version": "4.0.18",
                            "max_version": "4.0.19",
                        }
                    ],
                },
            }

            result = guard.check_version(cfg)

        self.assertFalse(result.ok)
        self.assertIn("不在允许区间", result.reason_text)

    def test_enabled_without_allowed_version_ranges_fails_closed(self):
        with tempfile.TemporaryDirectory() as td, \
             patch.object(guard.platform, "system", return_value="Darwin"), \
             patch.object(guard, "_process_paths", return_value=[]):
            app_path = _make_macos_app(td)
            result = guard.check_version(
                {
                    "wechat_app_path": app_path,
                    "version_guard": {"enabled": True, "allowed_version_ranges": []},
                }
            )

        self.assertFalse(result.ok)
        self.assertIn("未配置 allowed_version_ranges", result.reason_text)

    def test_missing_app_path_fails_closed(self):
        with patch.object(guard.platform, "system", return_value="Darwin"), \
             patch.object(guard, "_process_paths", return_value=[]):
            result = guard.check_version(
                {
                    "version_guard": {
                        "enabled": True,
                        "allowed_version_ranges": [
                            {"platform": "darwin", "min_version": "4.0.18", "max_version": "4.0.18"}
                        ],
                    }
                }
            )

        self.assertFalse(result.ok)
        self.assertIn("未配置 wechat_app_path", result.reason_text)

    def test_macos_can_discover_running_process_path_when_unconfigured(self):
        with tempfile.TemporaryDirectory() as td, patch.object(guard.platform, "system", return_value="Darwin"):
            app_path = _make_macos_app(td)
            process_path = os.path.join(app_path, "Contents", "MacOS", "WeChat")
            cfg = {
                "version_guard": {
                    "enabled": True,
                    "allowed_version_ranges": [
                        {
                            "platform": "darwin",
                            "bundle_id": "com.tencent.xinWeChat",
                            "min_version": "4.0.18",
                            "max_version": "4.0.18",
                        }
                    ],
                },
            }

            with patch.object(guard, "_process_paths", return_value=[process_path]):
                result = guard.check_version(cfg)

        self.assertTrue(result.ok, result.reason_text)
        self.assertEqual(result.details["detected"]["app_path"], app_path)

    def test_exact_allowed_version_remains_backward_compatible(self):
        with tempfile.TemporaryDirectory() as td, \
             patch.object(guard.platform, "system", return_value="Darwin"), \
             patch.object(guard, "_process_paths", return_value=[]):
            app_path = _make_macos_app(td)
            cfg = {
                "wechat_app_path": app_path,
                "version_guard": {
                    "enabled": True,
                    "allowed_versions": [
                        {
                            "platform": "darwin",
                            "bundle_id": "com.tencent.xinWeChat",
                            "short_version": "4.0.18",
                        }
                    ],
                },
            }

            result = guard.check_version(cfg)

        self.assertTrue(result.ok, result.reason_text)

    def test_macos_update_disabled_requirement_passes_when_both_flags_false(self):
        with tempfile.TemporaryDirectory() as td, \
             patch.object(guard.platform, "system", return_value="Darwin"), \
             patch.object(guard, "_process_paths", return_value=[]), \
             patch.object(
                 guard,
                 "_read_macos_update_settings",
                 return_value={
                     "prefs_path": "/tmp/com.tencent.xinWeChat.plist",
                     "enable_automatic_checks": False,
                     "automatically_update": False,
                     "update_disabled": True,
                 },
             ):
            app_path = _make_macos_app(td, short_version="3.8.9")
            cfg = {
                "wechat_app_path": app_path,
                "version_guard": {
                    "enabled": True,
                    "require_update_disabled": True,
                    "allowed_version_ranges": [
                        {
                            "platform": "darwin",
                            "min_version": "3.8.9",
                            "max_version": "3.8.9",
                        }
                    ],
                },
            }

            result = guard.check_version(cfg)

        self.assertTrue(result.ok, result.reason_text)

    def test_macos_update_disabled_requirement_fails_when_auto_update_enabled(self):
        with tempfile.TemporaryDirectory() as td, \
             patch.object(guard.platform, "system", return_value="Darwin"), \
             patch.object(guard, "_process_paths", return_value=[]), \
             patch.object(
                 guard,
                 "_read_macos_update_settings",
                 return_value={
                     "prefs_path": "/tmp/com.tencent.xinWeChat.plist",
                     "enable_automatic_checks": True,
                     "automatically_update": True,
                     "update_disabled": False,
                 },
             ):
            app_path = _make_macos_app(td, short_version="3.8.9")
            cfg = {
                "wechat_app_path": app_path,
                "version_guard": {
                    "enabled": True,
                    "require_update_disabled": True,
                    "allowed_version_ranges": [
                        {
                            "platform": "darwin",
                            "min_version": "3.8.9",
                            "max_version": "3.8.9",
                        }
                    ],
                },
            }

            result = guard.check_version(cfg)

        self.assertFalse(result.ok)
        self.assertIn("Sparkle 自动更新未关闭", result.reason_text)

    def test_macos_update_settings_reads_diagnostic_fields_from_plist_file(self):
        with tempfile.TemporaryDirectory() as td, \
             patch.object(guard, "_macos_update_preferences_path", return_value=os.path.join(td, "prefs.plist")):
            prefs_path = os.path.join(td, "prefs.plist")
            with open(prefs_path, "wb") as f:
                plistlib.dump(
                    {
                        "SUEnableAutomaticChecks": True,
                        "SUAutomaticallyUpdate": True,
                        "SUSkippedVersion": "269110",
                        "SULastCheckTime": "2026-07-10 18:50:54 +0000",
                    },
                    f,
                )

            settings = guard._read_macos_update_settings("/Applications/WeChat.app")

        self.assertEqual(settings["prefs_source"], "legacy_sparkle_plist")
        self.assertEqual(settings["skipped_version"], "269110")
        self.assertEqual(settings["last_check_time"], "2026-07-10 18:50:54 +0000")

    def test_macos_4x_update_requirement_fails_when_setting_cannot_be_read(self):
        with tempfile.TemporaryDirectory() as td, \
             patch.object(guard.platform, "system", return_value="Darwin"), \
             patch.object(guard, "_process_paths", return_value=[]), \
             patch.object(guard, "_read_macos_update_settings") as read_settings:
            app_path = _make_macos_app(td, short_version="4.1.8")
            cfg = {
                "wechat_app_path": app_path,
                "version_guard": {
                    "enabled": True,
                    "require_update_disabled": True,
                    "allowed_version_ranges": [
                        {
                            "platform": "darwin",
                            "min_version": "4.1.8",
                            "max_version": "4.1.8",
                        }
                    ],
                },
            }

            result = guard.check_version(cfg)

        self.assertFalse(result.ok)
        self.assertIn("微信 4.x 自动升级开关无法可靠检测", result.reason_text)
        self.assertIn("手动关闭", result.details["update_notice"])
        read_settings.assert_not_called()

    def test_non_macos_update_disabled_requirement_fails_closed(self):
        cfg = {
            "wechat_app_path": r"C:\Program Files\Tencent\Weixin\Weixin.exe",
            "version_guard": {
                "enabled": True,
                "require_update_disabled": True,
                "allowed_version_ranges": [
                    {
                        "platform": "windows",
                        "min_version": "4.0.18",
                        "max_version": "4.0.18",
                    }
                ],
            },
        }

        with patch.object(guard.platform, "system", return_value="Windows"), \
             patch.object(
                 guard,
                 "_read_windows_app",
                 return_value={
                     "platform": "windows",
                     "app_path": r"C:\Program Files\Tencent\Weixin\Weixin.exe",
                     "short_version": "4.0.18",
                     "build_version": "23110",
                 },
             ), \
             patch.object(os.path, "exists", return_value=True):
            result = guard.check_version(cfg)

        self.assertFalse(result.ok)
        self.assertIn("未实现自动升级状态检测", result.reason_text)


if __name__ == "__main__":
    unittest.main()
