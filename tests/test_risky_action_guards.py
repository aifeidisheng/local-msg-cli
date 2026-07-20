import os
import unittest
from unittest.mock import patch

import wechat_version_guard as guard


class RiskyActionGuardTests(unittest.TestCase):
    def test_risky_action_requires_enabled_guard(self):
        result = guard.check_risky_action({}, action="获取微信密钥")

        self.assertFalse(result.ok)
        self.assertIn("要求 version_guard.enabled=true", result.reason_text)

    def test_risky_action_checks_exact_process_executable(self):
        cfg = {
            "version_guard": {
                "enabled": True,
                "allowed_version_ranges": [
                    {
                        "platform": "windows",
                        "min_version": "4.1.9",
                        "max_version": "4.1.9",
                    }
                ],
            }
        }
        process_path = r"C:\Program Files\Tencent\Weixin\Weixin.exe"

        with patch.object(guard.platform, "system", return_value="Windows"), \
             patch.object(guard, "_process_path_for_pid", return_value=process_path), \
             patch.object(os.path, "exists", return_value=True), \
             patch.object(
                 guard,
                 "_read_windows_app",
                 return_value={
                     "platform": "windows",
                     "app_path": process_path,
                     "short_version": "4.1.10",
                     "build_version": "99999",
                 },
             ):
            result = guard.check_risky_action(
                cfg,
                action="读取微信进程内存获取密钥",
                pids=[1234],
            )

        self.assertFalse(result.ok)
        self.assertIn("PID=1234", result.reason_text)
        self.assertIn("4.1.10", result.reason_text)

    def test_risky_action_fails_when_process_path_is_unknown(self):
        cfg = {
            "version_guard": {
                "enabled": True,
                "allowed_version_ranges": [
                    {"platform": "darwin", "max_version": "4.1.8"}
                ],
            }
        }

        with patch.object(
            guard,
            "_process_path_for_pid",
            side_effect=RuntimeError("进程已退出"),
        ):
            result = guard.check_risky_action(
                cfg,
                action="读取微信进程内存获取密钥",
                pids=[9333],
            )

        self.assertFalse(result.ok)
        self.assertIn("进程已退出", result.reason_text)


class NativeScannerGuardTests(unittest.TestCase):
    def test_database_scanner_guards_before_task_for_pid(self):
        with open("find_all_keys_macos.c", encoding="utf-8") as source_file:
            source = source_file.read()

        self.assertLess(
            source.index("enforce_wechat_pid_version_guard(&pid, 1)"),
            source.index("task_for_pid(mach_task_self(), pid, &task)"),
        )

    def test_image_scanner_guards_each_discovered_pid_batch(self):
        with open("find_image_key.c", encoding="utf-8") as source_file:
            source = source_file.read()

        self.assertIn("enforce_wechat_pid_version_guard(pids, npids)", source)


if __name__ == "__main__":
    unittest.main()
