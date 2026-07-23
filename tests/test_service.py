import os
import plistlib
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

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
            self.assertEqual(plist["EnvironmentVariables"]["WECHAT_DECRYPT_DATA_DIR"], str(paths["data_dir"]))
            self.assertEqual(plist["StandardErrorPath"], str(paths["stderr"]))

            self._write_plist(paths, plist)
            self.assertEqual(service._configured_endpoint(paths), ("127.0.0.1", 9876))

    @staticmethod
    def _write_plist(paths, plist):
        paths["plist"].parent.mkdir(parents=True, exist_ok=True)
        with paths["plist"].open("wb") as plist_file:
            plistlib.dump(plist, plist_file)

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
             patch.object(service, "acquire_instance_lock", return_value=42) as acquire_lock, \
             patch("config.load_config", return_value={}), \
             patch("wechat_version_guard.check_version", side_effect=[blocked, ready]), \
             patch.object(service.os, "execv") as execv, \
             patch.object(service.os, "close") as close, \
             patch.dict(service.os.environ, {}, clear=False), \
             patch.object(service.time, "sleep") as sleep:
            result = service.run_service(retry_interval=3)

        self.assertEqual(result, 0)
        acquire_lock.assert_called_once_with(paths, blocking=True)
        close.assert_called_once_with(42)
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

    def test_instance_lock_rejects_second_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = service.service_paths(root=Path(tmp) / "app", home=Path(tmp) / "home")
            first_fd = service.acquire_instance_lock(paths)
            try:
                with self.assertRaises(service.ServiceAlreadyRunningError):
                    service.acquire_instance_lock(paths)
            finally:
                os.close(first_fd)


class ServiceInspectionTests(unittest.TestCase):
    def _write_plist(self, paths, plist):
        paths["plist"].parent.mkdir(parents=True, exist_ok=True)
        with paths["plist"].open("wb") as plist_file:
            plistlib.dump(plist, plist_file)

    def test_inspection_reports_ready_only_when_launchd_pid_owns_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = service.service_paths(root=Path(tmp) / "app", home=Path(tmp) / "home")
            self._write_plist(paths, service.build_plist(paths))
            job = service.LaunchJobInfo(
                loaded=True,
                state="running",
                pid=123,
                program=str(paths["python"]),
            )

            with patch.object(service, "_job_info", return_value=job), \
                 patch.object(service, "_port_owner_pids", return_value={123}), \
                 patch.object(service, "_process_command", return_value=str(paths["main"])):
                inspection = service.inspect_service(paths)

            self.assertEqual(inspection.status, service.STATUS_READY)

    def test_inspection_reports_port_conflict_for_other_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = service.service_paths(root=Path(tmp) / "app", home=Path(tmp) / "home")
            self._write_plist(paths, service.build_plist(paths))
            job = service.LaunchJobInfo(
                loaded=True,
                state="running",
                pid=123,
                program=str(paths["python"]),
            )

            with patch.object(service, "_job_info", return_value=job), \
                 patch.object(service, "_port_owner_pids", return_value={456}), \
                 patch.object(service, "_process_command", return_value=str(paths["main"])):
                inspection = service.inspect_service(paths)

            self.assertEqual(inspection.status, service.STATUS_CONFLICT)

    def test_inspection_reports_stale_project_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = service.service_paths(root=Path(tmp) / "current", home=Path(tmp) / "home")
            old_paths = service.service_paths(root=Path(tmp) / "old", home=Path(tmp) / "home")
            self._write_plist(paths, service.build_plist(old_paths))
            job = service.LaunchJobInfo(
                loaded=True,
                state="running",
                pid=123,
                program=str(old_paths["python"]),
            )

            with patch.object(service, "_job_info", return_value=job), \
                 patch.object(service, "_port_owner_pids", return_value={123}), \
                 patch.object(service, "_process_command", return_value=str(old_paths["main"])):
                inspection = service.inspect_service(paths)

            self.assertEqual(inspection.status, service.STATUS_STALE)
            self.assertEqual(inspection.configured_root, str(old_paths["root"]))

    def test_inspection_treats_waiting_for_wechat_as_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = service.service_paths(root=Path(tmp) / "app", home=Path(tmp) / "home")
            self._write_plist(paths, service.build_plist(paths))
            job = service.LaunchJobInfo(
                loaded=True,
                state="running",
                pid=123,
                program=str(paths["python"]),
            )
            command = f"{paths['python']} {paths['service']} run --port 8765"

            with patch.object(service, "_job_info", return_value=job), \
                 patch.object(service, "_port_owner_pids", return_value=set()), \
                 patch.object(service, "_process_command", return_value=command):
                inspection = service.inspect_service(paths)

            self.assertEqual(inspection.status, service.STATUS_WAITING)

    def test_status_returns_success_while_waiting_for_wechat(self):
        inspection = service.ServiceInspection(
            status=service.STATUS_WAITING,
            job=service.LaunchJobInfo(loaded=True, state="running", pid=123),
            port_pids=frozenset(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            paths = service.service_paths(root=Path(tmp) / "app", home=Path(tmp) / "home")
            self._write_plist(paths, service.build_plist(paths))
            output = StringIO()
            with patch.object(service, "_require_macos"), \
                 patch.object(service, "service_paths", return_value=paths), \
                 patch.object(service, "inspect_service", return_value=inspection), \
                 redirect_stdout(output):
                result = service.status_service()

        self.assertEqual(result, 0)
        self.assertIn("等待微信和版本门禁", output.getvalue())

    def test_json_status_contains_only_operational_metadata(self):
        inspection = service.ServiceInspection(
            status=service.STATUS_READY,
            job=service.LaunchJobInfo(loaded=True, state="running", pid=123),
            port_pids=frozenset({123}),
        )
        with tempfile.TemporaryDirectory() as tmp:
            paths = service.service_paths(root=Path(tmp) / "app", home=Path(tmp) / "home")
            self._write_plist(paths, service.build_plist(paths))
            paths["data_dir"].mkdir(parents=True)
            (paths["data_dir"] / "all_keys.json").write_text(
                '{"message/message_0.db":{"enc_key":"' + "a" * 64 + '"}}',
                encoding="utf-8",
            )
            output = StringIO()
            with patch.object(service, "_require_macos"), \
                 patch.object(service, "service_paths", return_value=paths), \
                 patch.object(service, "inspect_service", return_value=inspection), \
                 redirect_stdout(output):
                result = service.status_service_json()

        self.assertEqual(result, 0)
        payload = __import__("json").loads(output.getvalue())
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["transport_ready"])
        self.assertTrue(payload["initialized"])
        self.assertTrue(payload["query_ready"])
        self.assertEqual(payload["launchd_pid"], 123)
        self.assertNotIn("keys", payload)
        self.assertNotIn("messages", payload)

    def test_json_status_distinguishes_transport_from_query_readiness(self):
        inspection = service.ServiceInspection(
            status=service.STATUS_READY,
            job=service.LaunchJobInfo(loaded=True, state="running", pid=123),
            port_pids=frozenset({123}),
        )
        with tempfile.TemporaryDirectory() as tmp:
            paths = service.service_paths(root=Path(tmp) / "app", home=Path(tmp) / "home")
            payload = service.service_status_payload(paths, inspection, "127.0.0.1", 8765)

        self.assertTrue(payload["transport_ready"])
        self.assertFalse(payload["initialized"])
        self.assertFalse(payload["query_ready"])


class ServiceInstallTests(unittest.TestCase):
    @staticmethod
    def _prepare_paths(tmp):
        paths = service.service_paths(root=Path(tmp) / "app", home=Path(tmp) / "home")
        paths["python"].parent.mkdir(parents=True, exist_ok=True)
        paths["python"].write_text("#!/bin/sh\n", encoding="utf-8")
        paths["python"].chmod(0o700)
        paths["main"].write_text("", encoding="utf-8")
        paths["service"].write_text("", encoding="utf-8")
        return paths

    def test_install_refuses_port_owned_by_non_launchd_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._prepare_paths(tmp)
            job = service.LaunchJobInfo(loaded=True, state="running", pid=123)

            with patch.object(service, "_require_macos"), \
                 patch.object(service, "service_paths", return_value=paths), \
                 patch.object(service, "_job_info", return_value=job), \
                 patch.object(service, "_port_owner_pids", return_value={456}), \
                 patch.object(service, "_process_command", return_value="other-server"), \
                 patch.object(service, "_run_launchctl") as launchctl:
                result = service.install_service()

            self.assertEqual(result, 1)
            launchctl.assert_not_called()

    def test_install_accepts_healthy_waiting_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._prepare_paths(tmp)
            waiting = service.ServiceInspection(
                status=service.STATUS_WAITING,
                job=service.LaunchJobInfo(loaded=True, state="running", pid=123),
                port_pids=frozenset(),
            )
            launch_result = Mock(returncode=0, stdout="", stderr="")

            with patch.object(service, "_require_macos"), \
                 patch.object(service, "service_paths", return_value=paths), \
                 patch.object(service, "_job_info", return_value=service.LaunchJobInfo(loaded=False)), \
                 patch.object(service, "_port_owner_pids", return_value=set()), \
                 patch.object(service, "_bootout_loaded_service", return_value=None), \
                 patch.object(service, "_run_launchctl", return_value=launch_result), \
                 patch.object(service, "_wait_for_service_inspection", return_value=waiting):
                result = service.install_service()

            self.assertEqual(result, 0)
            self.assertTrue(service._plist_matches_current(paths))

    def test_install_restores_previous_plist_when_bootstrap_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._prepare_paths(tmp)
            old_paths = service.service_paths(root=Path(tmp) / "old", home=Path(tmp) / "home")
            paths["plist"].parent.mkdir(parents=True, exist_ok=True)
            with paths["plist"].open("wb") as plist_file:
                plistlib.dump(service.build_plist(old_paths), plist_file)
            old_content = paths["plist"].read_bytes()
            previous_job = service.LaunchJobInfo(loaded=True, state="running", pid=123)
            failed = Mock(returncode=5, stdout="", stderr="bootstrap failed")
            restored = Mock(returncode=0, stdout="", stderr="")

            with patch.object(service, "_require_macos"), \
                 patch.object(service, "service_paths", return_value=paths), \
                 patch.object(service, "_job_info", return_value=previous_job), \
                 patch.object(service, "_port_owner_pids", return_value=set()), \
                 patch.object(service, "_bootout_loaded_service", return_value=Mock(returncode=0)), \
                 patch.object(service, "_run_launchctl", side_effect=[failed, restored]):
                result = service.install_service()

            self.assertEqual(result, 5)
            self.assertEqual(paths["plist"].read_bytes(), old_content)


if __name__ == "__main__":
    unittest.main()
