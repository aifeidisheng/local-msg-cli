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

    def test_risky_action_report_explains_block_and_next_steps(self):
        cfg = {
            "version_guard": {
                "enabled": True,
                "allowed_version_ranges": [
                    {
                        "platform": "darwin",
                        "min_version": "4.1.8",
                        "max_version": "4.1.8",
                    }
                ],
            }
        }
        result = guard.VersionCheckResult(
            enabled=True,
            ok=False,
            reasons=["PID=9333: 当前微信版本不在允许区间: 4.1.11"],
            details={
                "action": "读取微信进程内存获取密钥",
                "targets": [
                    {
                        "pid": 9333,
                        "result": {
                            "detected": {
                                "app_path": "/Applications/WeChat.app",
                                "short_version": "4.1.11",
                                "build_version": "269111",
                            }
                        },
                    }
                ],
            },
        )

        with patch.object(guard.platform, "system", return_value="Darwin"):
            report = guard.format_risky_action_report(cfg, result)

        self.assertIn("[安全拦截] 已停止：读取微信进程内存获取密钥", report)
        self.assertIn("检测到：PID 9333，微信 4.1.11（build 269111）", report)
        self.assertIn("当前允许：macOS 4.1.8", report)
        self.assertIn("本次检查后没有调用 task_for_pid/OpenProcess", report)
        self.assertIn("python3 main.py doctor", report)
        self.assertIn("不要直接修改 version-guard.policy.json", report)
        self.assertEqual(report.count("处理建议："), 1)

    def test_standard_block_report_names_action_and_uses_plain_language(self):
        cfg = {
            "version_guard": {
                "enabled": True,
                "allowed_version_ranges": [
                    {"platform": "darwin", "version": "4.1.8"}
                ],
            }
        }
        result = guard.VersionCheckResult(
            enabled=True,
            ok=False,
            reasons=["当前微信版本不在允许区间: 4.1.11"],
            details={
                "detected": {
                    "app_path": "/Applications/WeChat.app",
                    "short_version": "4.1.11",
                    "build_version": "269111",
                }
            },
        )

        with patch.object(guard.platform, "system", return_value="Darwin"):
            report = guard.format_block_report(cfg, result, action="启动 MCP Server")

        self.assertIn("[安全拦截] 已停止：启动 MCP Server", report)
        self.assertIn("当前允许：macOS 4.1.8", report)
        self.assertIn("尚未执行“启动 MCP Server”", report)
        self.assertNotIn("reasons", report)
        self.assertNotIn("FAIL", report)

    def test_doctor_report_explains_disabled_guard(self):
        report = guard.format_report(
            guard.VersionCheckResult(
                enabled=False,
                ok=True,
                details={"status": "disabled"},
            ),
            {},
        )

        self.assertIn("[版本门禁] 未启用", report)
        self.assertIn("version-guard.policy.json", report)
        self.assertNotIn("DISABLED", report)

    def test_mcp_exception_says_tool_was_not_executed(self):
        cfg = {
            "version_guard": {
                "enabled": True,
                "allowed_version_ranges": [
                    {"platform": "darwin", "version": "4.1.8"}
                ],
            }
        }
        result = guard.VersionCheckResult(
            enabled=True,
            ok=False,
            reasons=["当前微信版本不在允许区间: 4.1.11"],
            details={"detected": {"short_version": "4.1.11"}},
        )

        with patch.object(guard.platform, "system", return_value="Darwin"), \
             patch.object(guard, "check_version", return_value=result):
            guard._MCP_CACHE["result"] = None
            with self.assertRaises(RuntimeError) as caught:
                guard.check_or_raise(
                    cfg,
                    ttl_seconds=0,
                    action="调用 MCP 工具 search_messages",
                )

        message = str(caught.exception)
        self.assertIn("已停止：调用 MCP 工具 search_messages", message)
        self.assertIn("尚未执行“调用 MCP 工具 search_messages”", message)


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
