import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import windows_service


class WindowsServiceTests(unittest.TestCase):
    def _paths(self, root: Path) -> dict[str, Path]:
        return {
            "root": root,
            "python": root / ".venv" / "Scripts" / "python.exe",
            "task_python": root / ".venv" / "Scripts" / "pythonw.exe",
            "main": root / "main.py",
            "service": root / "windows_service.py",
            "log_dir": root / "logs",
            "stdout": root / "logs" / "stdout.log",
            "stderr": root / "logs" / "stderr.log",
        }

    def test_task_xml_uses_current_user_least_privilege_and_no_time_limit(self):
        paths = self._paths(Path(r"C:\Users\Test User\MCP & Tools"))

        xml = windows_service._task_xml(
            paths, "127.0.0.1", 8765, user=r"DESKTOP\Test User"
        )
        root = ET.fromstring(xml)
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}

        self.assertEqual(root.findtext(".//t:LogonType", namespaces=ns), "InteractiveToken")
        self.assertEqual(root.findtext(".//t:RunLevel", namespaces=ns), "LeastPrivilege")
        self.assertEqual(root.findtext(".//t:ExecutionTimeLimit", namespaces=ns), "PT0S")
        self.assertEqual(root.findtext(".//t:MultipleInstancesPolicy", namespaces=ns), "IgnoreNew")
        self.assertEqual(root.findtext(".//t:DisallowStartIfOnBatteries", namespaces=ns), "false")
        self.assertIn(str(paths["service"]), root.findtext(".//t:Arguments", namespaces=ns))
        self.assertEqual(root.findtext(".//t:WorkingDirectory", namespaces=ns), str(paths["root"]))
        settings = root.find(".//t:Settings", namespaces=ns)
        order = [element.tag.rsplit("}", 1)[-1] for element in settings]
        self.assertLess(order.index("AllowHardTerminate"), order.index("StartWhenAvailable"))
        self.assertLess(order.index("Enabled"), order.index("ExecutionTimeLimit"))

    def test_service_paths_keep_logs_outside_source_tree(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as local:
            with patch.dict(os.environ, {"LOCALAPPDATA": local}, clear=False):
                paths = windows_service.service_paths(Path(root))

        self.assertEqual(paths["log_dir"], Path(local) / "WeChatDecryptLight" / "logs")

    def test_task_match_parses_utf16_xml_with_non_ascii_paths(self):
        paths = self._paths(Path(r"C:\Users\测试用户\local-msg-cli"))
        xml = windows_service._task_xml(
            paths, "127.0.0.1", 8765, user=r"DESKTOP\测试用户"
        )
        completed = SimpleNamespace(
            returncode=0,
            stdout=xml,
            stderr="",
        )

        with patch.object(windows_service, "_run_schtasks", return_value=completed):
            self.assertTrue(windows_service._task_matches(paths, "127.0.0.1", 8765))

    def test_schtasks_output_decoder_handles_utf16_bom(self):
        payload = "计划任务".encode("utf-16")

        self.assertEqual(windows_service._decode_windows_output(payload), "计划任务")

    def test_port_owner_must_be_expected_python_and_main(self):
        paths = self._paths(Path(r"C:\Users\Test User\local-msg-cli"))
        expected = {
            "Pid": 123,
            "ExecutablePath": str(paths["python"]),
            "CommandLine": f'"{paths["python"]}" "{paths["main"]}" serve --port 8765',
        }

        with patch.object(windows_service, "_port_owner_details", return_value=[expected]):
            self.assertTrue(windows_service._service_owns_port(paths, "127.0.0.1", 8765))
        with patch.object(
            windows_service,
            "_port_owner_details",
            return_value=[{"ExecutablePath": r"C:\Python\python.exe", "CommandLine": "other.py"}],
        ):
            self.assertFalse(windows_service._service_owns_port(paths, "127.0.0.1", 8765))

    def test_status_reports_port_conflict_instead_of_ready(self):
        paths = self._paths(Path(r"C:\Users\Test User\local-msg-cli"))
        output = []
        with patch.object(windows_service, "_require_windows"), \
             patch.object(windows_service, "service_paths", return_value=paths), \
             patch.object(windows_service, "task_exists", return_value=True), \
             patch.object(windows_service, "_task_matches", return_value=True), \
             patch.object(windows_service, "_port_open", return_value=True), \
             patch.object(windows_service, "_service_owns_port", return_value=False), \
             patch("builtins.print", side_effect=lambda value: output.append(value)):
            result = windows_service.status_service(json_mode=True)

        payload = json.loads(output[0])
        self.assertEqual(result, 1)
        self.assertEqual(payload["status"], "port_conflict")
        self.assertFalse(payload["transport_ready"])

    def test_start_is_idempotent_when_service_already_owns_port(self):
        paths = self._paths(Path(r"C:\Users\Test User\local-msg-cli"))
        with patch.object(windows_service, "_require_windows"), \
             patch.object(windows_service, "service_paths", return_value=paths), \
             patch.object(windows_service, "task_exists", return_value=True), \
             patch.object(windows_service, "_service_owns_port", return_value=True), \
             patch.object(windows_service, "_run_schtasks") as schtasks:
            result = windows_service.start_service()

        self.assertEqual(result, 0)
        schtasks.assert_not_called()

    def test_stop_is_idempotent_when_port_is_closed(self):
        with patch.object(windows_service, "_require_windows"), \
             patch.object(windows_service, "service_paths"), \
             patch.object(windows_service, "task_exists", return_value=True), \
             patch.object(windows_service, "_port_open", return_value=False), \
             patch.object(windows_service, "_run_schtasks") as schtasks:
            result = windows_service.stop_service()

        self.assertEqual(result, 0)
        schtasks.assert_not_called()

    def test_install_refuses_unknown_existing_port_without_changing_task(self):
        paths = self._paths(Path(r"C:\Users\Test User\local-msg-cli"))

        with patch.object(windows_service, "_require_windows"), \
             patch.object(windows_service, "service_paths", return_value=paths), \
             patch.object(Path, "is_file", return_value=True), \
             patch.object(windows_service, "task_exists", return_value=False), \
             patch.object(windows_service, "_port_open", return_value=True), \
             patch.object(windows_service, "_run_schtasks") as schtasks:
            result = windows_service.install_service()

        self.assertEqual(result, 1)
        schtasks.assert_not_called()


if __name__ == "__main__":
    unittest.main()
