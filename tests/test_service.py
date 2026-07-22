import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import service
from wechat_version_guard import VersionCheckResult


class ServicePlistTests(unittest.TestCase):
    def test_build_plist_uses_absolute_project_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "wechat-decrypt-light"
            home = Path(tmp).resolve() / "home"
            paths = service.service_paths(root=root, home=home)

            plist = service.build_plist(paths, host="127.0.0.1", port=9876)

            self.assertEqual(plist["Label"], service.DEFAULT_LABEL)
            self.assertEqual(
                plist["ProgramArguments"],
                [
                    str(paths["python"]),
                    str(paths["service"]),
                    "run",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "9876",
                ],
            )
            self.assertEqual(plist["WorkingDirectory"], str(paths["root"]))
            self.assertTrue(plist["RunAtLoad"])
            self.assertTrue(plist["KeepAlive"])
            self.assertEqual(plist["EnvironmentVariables"]["WECHAT_DECRYPT_APP_DIR"], str(paths["root"]))
            self.assertEqual(plist["StandardErrorPath"], str(paths["stderr"]))

    def test_service_paths_are_independent_of_current_working_directory(self):
        original = os.getcwd()
        try:
            os.chdir("/")
            paths = service.service_paths(root=Path("/tmp/wechat-decrypt-light"), home=Path("/tmp/home"))
        finally:
            os.chdir(original)

        self.assertEqual(paths["root"], Path("/tmp/wechat-decrypt-light").resolve())
        self.assertEqual(paths["main"], Path("/tmp/wechat-decrypt-light/main.py").resolve())


class ServiceRunnerTests(unittest.TestCase):
    def test_run_service_waits_for_version_guard_before_exec(self):
        blocked = VersionCheckResult(
            enabled=True,
            ok=False,
            reasons=["微信尚未启动"],
        )
        ready = VersionCheckResult(enabled=True, ok=True)
        paths = service.service_paths(root=Path("/tmp/wechat-decrypt-light"))

        with patch.object(service, "service_paths", return_value=paths), \
             patch("config.load_config", return_value={}), \
             patch("wechat_version_guard.check_version", side_effect=[blocked, ready]), \
             patch.object(service.os, "execv") as execv, \
             patch.object(service.time, "sleep") as sleep:
            result = service.run_service(retry_interval=3)

        self.assertEqual(result, 0)
        sleep.assert_called_once_with(3)
        execv.assert_called_once_with(
            str(paths["python"]),
            [
                str(paths["python"]),
                str(paths["main"]),
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                "8765",
            ],
        )

    def test_wait_for_port_reports_timeout_without_failing_install(self):
        with patch.object(service, "_port_open", return_value=False), \
             patch.object(service.time, "sleep"):
            self.assertFalse(service._wait_for_port("127.0.0.1", 8765, timeout=0))


if __name__ == "__main__":
    unittest.main()
