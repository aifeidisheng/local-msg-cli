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
