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

    def test_macos_allowed_version_passes(self):
        with tempfile.TemporaryDirectory() as td, patch.object(guard.platform, "system", return_value="Darwin"):
            app_path = _make_macos_app(td)
            cfg = {
                "wechat_app_path": app_path,
                "version_guard": {
                    "enabled": True,
                    "allowed_versions": [
                        {
                            "platform": "darwin",
                            "app_path": app_path,
                            "bundle_id": "com.tencent.xinWeChat",
                            "short_version": "4.0.18",
                            "build_version": "23110",
                        }
                    ],
                },
            }

            result = guard.check_version(cfg)

        self.assertTrue(result.enabled)
        self.assertTrue(result.ok, result.reason_text)

    def test_macos_version_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as td, patch.object(guard.platform, "system", return_value="Darwin"):
            app_path = _make_macos_app(td, short_version="4.0.20", build_version="23897")
            cfg = {
                "wechat_app_path": app_path,
                "version_guard": {
                    "enabled": True,
                    "allowed_versions": [
                        {
                            "platform": "darwin",
                            "app_path": app_path,
                            "bundle_id": "com.tencent.xinWeChat",
                            "short_version": "4.0.18",
                            "build_version": "23110",
                        }
                    ],
                },
            }

            result = guard.check_version(cfg)

        self.assertFalse(result.ok)
        self.assertIn("不在白名单", result.reason_text)

    def test_enabled_without_allowed_versions_fails_closed(self):
        with tempfile.TemporaryDirectory() as td, patch.object(guard.platform, "system", return_value="Darwin"):
            app_path = _make_macos_app(td)
            result = guard.check_version(
                {
                    "wechat_app_path": app_path,
                    "version_guard": {"enabled": True, "allowed_versions": []},
                }
            )

        self.assertFalse(result.ok)
        self.assertIn("未配置 allowed_versions", result.reason_text)

    def test_missing_app_path_fails_closed(self):
        with patch.object(guard.platform, "system", return_value="Darwin"):
            result = guard.check_version(
                {
                    "version_guard": {
                        "enabled": True,
                        "allowed_versions": [
                            {"platform": "darwin", "short_version": "4.0.18", "build_version": "23110"}
                        ],
                    }
                }
            )

        self.assertFalse(result.ok)
        self.assertIn("未配置 wechat_app_path", result.reason_text)


if __name__ == "__main__":
    unittest.main()
