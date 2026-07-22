import contextlib
import io
import sys
import unittest
from unittest.mock import patch

import main
from wechat_version_guard import VersionCheckResult


class MainDoctorTests(unittest.TestCase):
    def test_doctor_is_diagnostic_only_when_version_is_blocked(self):
        output = io.StringIO()
        blocked = VersionCheckResult(
            enabled=True,
            ok=False,
            reasons=["当前微信版本不在允许区间: 4.1.11"],
        )

        with patch.object(sys, "argv", ["main.py", "doctor"]), \
             patch("config.load_config", return_value={}), \
             patch("wechat_version_guard.check_version", return_value=blocked), \
             patch("wechat_version_guard.format_report", return_value="[版本门禁] 检查失败"), \
             contextlib.redirect_stdout(output):
            main.main()

        self.assertIn("[版本门禁] 检查失败", output.getvalue())
        self.assertIn("诊断模式", output.getvalue())
        self.assertIn("不会执行密钥提取、解密或消息查询", output.getvalue())


class MacServiceInstallHookTests(unittest.TestCase):
    def test_macos_service_install_hook_is_opt_out(self):
        with patch("main.platform.system", return_value="Darwin"), \
             patch.dict("os.environ", {"WECHAT_DECRYPT_SKIP_SERVICE_INSTALL": "1"}, clear=False), \
             patch("service.install_service") as install_service:
            main._maybe_install_macos_service()

        install_service.assert_not_called()

    def test_macos_service_install_hook_calls_service_installer(self):
        with patch("main.platform.system", return_value="Darwin"), \
             patch.dict("os.environ", {}, clear=True), \
             patch("service.install_service", return_value=0) as install_service:
            main._maybe_install_macos_service()

        install_service.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
