import argparse
import json
import os
import shlex
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock, patch

import installer


class RepositoryVerificationTests(unittest.TestCase):
    def test_repository_identity_accepts_https_and_ssh_for_same_repository(self):
        self.assertEqual(
            installer._repository_identity("https://github.com/example/wechat-decrypt.git"),
            installer._repository_identity("git@github.com:example/wechat-decrypt.git"),
        )

    def test_verify_source_rejects_dirty_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            (source / ".git").mkdir()
            (source / "installer.py").write_text("print('installer')\n", encoding="utf-8")
            with patch.object(
                installer,
                "_git",
                side_effect=[
                    "a" * 40,
                    "https://github.com/example/wechat-decrypt.git",
                    "a" * 40,
                    "?? unexpected.py",
                ],
            ):
                with self.assertRaisesRegex(installer.InstallerError, "不可复现版本"):
                    installer.verify_source(
                        source,
                        expected_repository="git@github.com:example/wechat-decrypt.git",
                        branch="main",
                    )

    def test_verify_source_rejects_checkout_not_at_main_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            (source / ".git").mkdir()
            (source / "installer.py").write_text("print('installer')\n", encoding="utf-8")
            with patch.object(
                installer,
                "_git",
                side_effect=[
                    "a" * 40,
                    "https://github.com/example/wechat-decrypt.git",
                    "b" * 40,
                    "",
                ],
            ):
                with self.assertRaisesRegex(installer.InstallerError, "不是 origin/main"):
                    installer.verify_source(
                        source,
                        expected_repository="https://github.com/example/wechat-decrypt.git",
                    )

    def test_verify_source_records_resolved_main_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            (source / ".git").mkdir()
            (source / "installer.py").write_text("print('installer')\n", encoding="utf-8")
            digest = installer._sha256(source / "installer.py")
            with patch.object(
                installer,
                "_git",
                side_effect=[
                    "a" * 40,
                    "https://github.com/example/wechat-decrypt.git",
                    "a" * 40,
                    "",
                ],
            ):
                source_info = installer.verify_source(
                    source,
                    expected_repository="git@github.com:example/wechat-decrypt.git",
                )

            self.assertEqual(source_info["commit"], "a" * 40)
            self.assertEqual(source_info["branch"], "main")
            self.assertEqual(source_info["installer_sha256"], digest)

    def test_verify_source_keeps_optional_fixed_commit_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            (source / ".git").mkdir()
            (source / "installer.py").write_text("print('installer')\n", encoding="utf-8")
            with patch.object(
                installer,
                "_git",
                side_effect=[
                    "a" * 40,
                    "https://github.com/example/wechat-decrypt.git",
                    "a" * 40,
                    "",
                ],
            ):
                with self.assertRaisesRegex(installer.InstallerError, "源码提交不匹配"):
                    installer.verify_source(
                        source,
                        expected_repository="https://github.com/example/wechat-decrypt.git",
                        expected_commit="b" * 40,
                    )

    def test_remote_branch_commit_parses_exact_branch_tip(self):
        remote_commit = "a" * 40
        result = CompletedProcess(
            args=[],
            returncode=0,
            stdout=f"{remote_commit}\trefs/heads/main\n",
            stderr="",
        )

        with patch.object(installer, "_git_network_run", return_value=result) as run:
            actual = installer._remote_branch_commit(
                "https://github.com/example/wechat-decrypt.git",
                "main",
            )

        self.assertEqual(actual, remote_commit)
        self.assertEqual(run.call_args_list[-1].kwargs["timeout"], 20)

    def test_git_network_run_retries_with_low_speed_limits(self):
        failed = CompletedProcess(args=[], returncode=128, stdout="", stderr="connection timed out")
        succeeded = CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")

        with patch.object(installer, "_detect_system_proxy", return_value=None), \
             patch.object(installer.subprocess, "run", side_effect=[failed, succeeded]) as run, \
             patch.object(installer.time, "sleep") as sleep:
            result = installer._git_network_run(
                ["ls-remote", "https://example.com/repo.git", "refs/heads/main"],
                error_context="query failed",
                timeout=10,
            )

        self.assertEqual(result.stdout, "ok")
        self.assertEqual(run.call_count, 2)
        command = run.call_args_list[0].args[0]
        self.assertIn("http.lowSpeedLimit=1024", command)
        self.assertIn("http.lowSpeedTime=15", command)
        sleep.assert_called_once_with(1)

    def test_git_network_run_cleans_partial_clone_before_retry(self):
        failed = CompletedProcess(args=[], returncode=128, stdout="", stderr="connection timed out")
        succeeded = CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            partial = Path(tmp) / "source"
            partial.mkdir()
            (partial / ".git.partial").write_text("partial", encoding="utf-8")
            with patch.object(installer, "_detect_system_proxy", return_value=None), \
                 patch.object(installer.subprocess, "run", side_effect=[failed, succeeded]), \
                 patch.object(installer.time, "sleep"), \
                 patch.object(installer.shutil, "rmtree", wraps=installer.shutil.rmtree) as rmtree:
                installer._git_network_run(
                    ["clone", "https://example.com/repo.git", str(partial)],
                    error_context="clone failed",
                    timeout=10,
                    retry_cleanup=partial,
                )
            rmtree.assert_called_once_with(partial)

    def test_release_source_falls_back_only_after_primary_is_unreachable(self):
        primary = "https://github.com/example/repo.git"
        mirror = "https://gitee.com/example/repo.git"
        unreachable = installer.InstallerError("timeout", error_code="git_source_unreachable")

        with patch.object(
            installer,
            "_remote_branch_commit",
            side_effect=[unreachable, "a" * 40],
        ) as remote:
            selected = installer._select_release_source([primary, mirror], "main")

        self.assertEqual(selected, (mirror, "a" * 40))
        self.assertEqual(remote.call_args_list[0].args, (primary, "main"))
        self.assertEqual(remote.call_args_list[1].args, (mirror, "main"))

    def test_release_source_reports_stable_error_when_all_sources_fail(self):
        with patch.object(
            installer,
            "_remote_branch_commit",
            side_effect=installer.InstallerError("timeout"),
        ):
            with self.assertRaises(installer.InstallerError) as raised:
                installer._select_release_source(
                    ["https://github.com/example/repo.git", "https://gitee.com/example/repo.git"],
                    "main",
                )

        self.assertEqual(raised.exception.error_code, "all_git_sources_unreachable")
        self.assertEqual(
            raised.exception.next_action,
            "retry_network_or_add_an_official_fallback_repository",
        )


class DataMigrationTests(unittest.TestCase):
    def test_migration_never_overwrites_existing_sensitive_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            data = Path(tmp) / "data"
            source.mkdir()
            data.mkdir()
            (source / "config.json").write_text('{"source": true}', encoding="utf-8")
            (source / "all_keys.json").write_text('{"key": "source"}', encoding="utf-8")
            (data / "all_keys.json").write_text('{"key": "installed"}', encoding="utf-8")

            migrated = installer.migrate_existing_data(source, data)

            self.assertEqual(json.loads((data / "all_keys.json").read_text()), {"key": "installed"})
            self.assertEqual(json.loads((data / "config.json").read_text()), {"source": True})
            self.assertEqual(migrated, ["config.json"])
            self.assertEqual((data / "config.json").stat().st_mode & 0o777, 0o600)


class InstallerFlowTests(unittest.TestCase):
    def _args(self, source: Path, home: Path) -> argparse.Namespace:
        return argparse.Namespace(
            source=str(source),
            home=str(home),
            repository="https://github.com/example/wechat-decrypt.git",
            branch="main",
            expected_commit=None,
            expected_installer_sha256=None,
            allow_dirty_source=False,
            python="/usr/bin/python3",
            host="127.0.0.1",
            port=8765,
        )

    def test_discover_db_manifest_is_limited_to_configured_directory_and_contains_page1(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = root / "current"; current.mkdir()
            historical = root / "historical"; historical.mkdir()
            page1 = bytes(range(256)) * 16
            (current / "message.db").write_bytes(page1)
            (historical / "old.db").write_bytes(page1)

            manifest = installer._discover_db_salts(root, current)
            self.assertIsNotNone(manifest)
            try:
                entries = json.loads(manifest.read_text(encoding="utf-8"))
            finally:
                manifest.unlink(missing_ok=True)

            self.assertEqual([entry["name"] for entry in entries], ["message.db"])
            self.assertEqual(entries[0]["salt"], page1[:16].hex())
            self.assertEqual(entries[0]["page1"], page1.hex())

    def test_install_uses_versioned_runtime_and_writes_manifest_after_service_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "source"
            home = base / "home"
            source.mkdir()
            (source / "config.json").write_text('{"db_dir": "/tmp/db"}', encoding="utf-8")
            args = self._args(source, home)

            def fake_copy(_source, destination):
                destination.mkdir(parents=True)
                (destination / "service.py").write_text("", encoding="utf-8")

            def fake_environment(runtime, _python):
                python = runtime / ".venv" / "bin" / "python3"
                python.parent.mkdir(parents=True)
                python.write_text("", encoding="utf-8")

            ready = {
                "ok": True,
                "status": "ready",
                "launchd_pid": 123,
                "port_pids": [123],
            }
            with patch.object(installer.platform, "system", return_value="Darwin"), \
                 patch.object(
                     installer,
                     "verify_source",
                     return_value={
                         "commit": "a" * 40,
                         "repository": "https://github.com/example/wechat-decrypt.git",
                         "branch": "main",
                         "installer_sha256": "b" * 64,
                     },
                 ), \
                 patch.object(installer, "copy_runtime", side_effect=fake_copy), \
                 patch.object(installer, "_create_runtime_environment", side_effect=fake_environment), \
                 patch.object(installer, "_build_macos_scanner"), \
                 patch.object(installer, "_service_command", return_value=Mock(returncode=0)), \
                 patch.object(installer, "service_status", return_value=ready):
                payload = installer.install(args, installer.Reporter(json_mode=True))

            layout = installer.default_layout(home)
            manifest = json.loads(layout.manifest.read_text(encoding="utf-8"))
            self.assertTrue(payload["ok"])
            self.assertEqual(manifest["commit"], "a" * 40)
            self.assertEqual(manifest["branch"], "main")
            self.assertEqual(
                manifest["repositories"],
                ["https://github.com/example/wechat-decrypt.git"],
            )
            self.assertEqual(
                manifest["source_repository"],
                "https://github.com/example/wechat-decrypt.git",
            )
            self.assertEqual(layout.current.resolve(), Path(manifest["runtime_dir"]))
            self.assertTrue(
                (Path(manifest["runtime_dir"]) / installer.INSTALLED_RUNTIME_MARKER).is_file()
            )
            self.assertEqual(manifest["data_dir"], str(layout.data_dir))
            self.assertTrue(os.access(layout.cli, os.X_OK))
            self.assertIn("runtime/current/installer.py", layout.cli.read_text(encoding="utf-8"))
            self.assertEqual(payload["next_step"], "run_init_with_user_confirmation")

    def test_install_rolls_back_current_pointer_when_service_install_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "source"
            home = base / "home"
            source.mkdir()
            args = self._args(source, home)
            layout = installer.default_layout(home)
            old_runtime = layout.runtime_dir / "old"
            old_runtime.mkdir(parents=True)
            installer._atomic_symlink(old_runtime, layout.current)

            def fake_copy(_source, destination):
                destination.mkdir(parents=True)

            def fake_environment(runtime, _python):
                python = runtime / ".venv" / "bin" / "python3"
                python.parent.mkdir(parents=True)
                python.write_text("", encoding="utf-8")

            with patch.object(installer.platform, "system", return_value="Darwin"), \
                 patch.object(
                     installer,
                     "verify_source",
                     return_value={
                         "commit": "a" * 40,
                         "repository": "https://github.com/example/wechat-decrypt.git",
                         "branch": "main",
                         "installer_sha256": "b" * 64,
                     },
                 ), \
                 patch.object(installer, "copy_runtime", side_effect=fake_copy), \
                 patch.object(installer, "_create_runtime_environment", side_effect=fake_environment), \
                 patch.object(installer, "_build_macos_scanner"), \
                 patch.object(installer, "_service_command", side_effect=installer.InstallerError("failed")):
                with self.assertRaises(installer.InstallerError):
                    installer.install(args, installer.Reporter(json_mode=True))

            self.assertEqual(layout.current.resolve(), old_runtime.resolve())
            self.assertFalse(layout.manifest.exists())


class JsonCliTests(unittest.TestCase):
    def test_install_parser_defaults_to_main_channel(self):
        args = installer.build_parser().parse_args(
            ["install", "--repository", "https://github.com/example/wechat-decrypt.git"]
        )

        self.assertEqual(args.repository, "https://github.com/example/wechat-decrypt.git")
        self.assertEqual(args.fallback_repositories, [])
        self.assertEqual(args.branch, "main")
        self.assertIsNone(args.expected_commit)
        self.assertIsNone(args.expected_installer_sha256)

    def test_install_parser_accepts_multiple_confirmed_fallback_repositories(self):
        args = installer.build_parser().parse_args(
            [
                "install",
                "--repository",
                "https://github.com/example/wechat-decrypt.git",
                "--fallback-repository",
                "https://gitee.com/example/wechat-decrypt.git",
                "--fallback-repository",
                "https://gitcode.com/example/wechat-decrypt.git",
            ]
        )

        self.assertEqual(
            args.fallback_repositories,
            [
                "https://gitee.com/example/wechat-decrypt.git",
                "https://gitcode.com/example/wechat-decrypt.git",
            ],
        )

    def test_legacy_repository_option_remains_accepted(self):
        args = installer.build_parser().parse_args(
            ["install", "--expected-repository", "git@github.com:example/wechat-decrypt.git"]
        )

        self.assertEqual(args.repository, "git@github.com:example/wechat-decrypt.git")

    def test_check_update_compares_installed_commit_with_main_tip(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            layout = installer.default_layout(home)
            runtime = layout.runtime_dir / ("a" * 40)
            runtime.mkdir(parents=True)
            installer._atomic_write_json(
                layout.manifest,
                {
                    "commit": "a" * 40,
                    "repository": "https://github.com/example/wechat-decrypt.git",
                    "branch": "main",
                    "runtime_dir": str(runtime),
                },
            )
            args = argparse.Namespace(home=str(home))

            with patch.object(installer, "_remote_branch_commit", return_value="b" * 40):
                payload = installer.check_update(args, installer.Reporter(json_mode=True))

            self.assertTrue(payload["update_available"])
            self.assertEqual(payload["installed_commit"], "a" * 40)
            self.assertEqual(payload["remote_commit"], "b" * 40)

    def test_upgrade_returns_without_clone_when_commit_is_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            layout = installer.default_layout(home)
            runtime = layout.runtime_dir / ("a" * 40)
            runtime.mkdir(parents=True)
            installer._atomic_write_json(
                layout.manifest,
                {
                    "commit": "a" * 40,
                    "repository": "https://github.com/example/wechat-decrypt.git",
                    "branch": "main",
                    "runtime_dir": str(runtime),
                },
            )
            args = argparse.Namespace(home=str(home))

            with patch.object(installer, "_remote_branch_commit", return_value="a" * 40), \
                 patch.object(installer, "_clone_branch") as clone:
                payload = installer.upgrade(args, installer.Reporter(json_mode=True))

            self.assertFalse(payload["upgraded"])
            self.assertEqual(payload["commit"], "a" * 40)
            clone.assert_not_called()

    def test_upgrade_runs_downloaded_installer_for_new_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            layout = installer.default_layout(home)
            runtime = layout.runtime_dir / ("a" * 40)
            runtime.mkdir(parents=True)
            installer._atomic_write_json(
                layout.manifest,
                {
                    "commit": "a" * 40,
                    "repository": "https://github.com/example/wechat-decrypt.git",
                    "branch": "main",
                    "runtime_dir": str(runtime),
                    "host": "127.0.0.1",
                    "port": 8765,
                },
            )
            args = argparse.Namespace(home=str(home))
            install_result = CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(
                    {
                        "ok": True,
                        "installation": {"commit": "b" * 40, "branch": "main"},
                        "service": {"ok": True, "status": "ready"},
                    }
                ),
                stderr="",
            )

            with patch.object(installer, "_remote_branch_commit", return_value="b" * 40), \
                 patch.object(installer, "_clone_branch") as clone, \
                 patch.object(
                     installer,
                     "verify_source",
                     return_value={
                         "commit": "b" * 40,
                         "repository": "https://github.com/example/wechat-decrypt.git",
                         "branch": "main",
                     },
                 ), \
                 patch.object(installer, "_run", return_value=install_result) as run:
                payload = installer.upgrade(args, installer.Reporter(json_mode=True))

            self.assertTrue(payload["upgraded"])
            self.assertEqual(payload["from_commit"], "a" * 40)
            self.assertEqual(payload["to_commit"], "b" * 40)
            clone.assert_called_once()
            command = run.call_args.args[0]
            self.assertIn("--expected-commit", command)
            self.assertEqual(command[command.index("--expected-commit") + 1], "b" * 40)
            self.assertEqual(command[command.index("--branch") + 1], "main")
            self.assertNotIn("--expected-installer-sha256", command)

    def test_json_flag_is_accepted_after_subcommand(self):
        with patch.object(installer, "status", return_value={"ok": True, "command": "status"}):
            with patch.object(installer.Reporter, "result") as result:
                exit_code = installer.main(["status", "--json"])

        self.assertEqual(exit_code, 0)
        result.assert_called_once_with({"ok": True, "command": "status"})

    def test_management_cli_rejects_sudo_and_returns_machine_readable_recovery(self):
        with patch.object(installer.platform, "system", return_value="Darwin"), \
             patch.object(installer.os, "geteuid", return_value=0), \
             patch.object(installer.Reporter, "result") as result:
            exit_code = installer.main(["status", "--json"])

        self.assertEqual(exit_code, 1)
        payload = result.call_args.args[0]
        self.assertEqual(payload["error_code"], "management_cli_must_not_run_as_root")
        self.assertEqual(payload["next_action"], "run_the_same_command_without_sudo")


class MacInitializeTests(unittest.TestCase):
    def test_scanner_summary_does_not_include_key_material(self):
        summary = installer._parse_scanner_summary(
            "Found 20 encrypted DBs\n"
            "Scan complete: 5375MB scanned, 655 regions, 20 unique keys\n"
            "Matched 17/20 keys to known DBs\n"
            "x'" + "a" * 96 + "'\n"
        )

        self.assertEqual(
            summary,
            {
                "encrypted_db_count": 20,
                "scanned_region_count": 655,
                "unique_key_count": 20,
                "matched_key_count": 17,
                "reported_key_count": 20,
            },
        )

    def test_empty_scanner_result_reports_database_access_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = base / "runtime"
            runtime.mkdir()
            scanner = runtime / "find_all_keys_macos"
            scanner.write_text("", encoding="utf-8")
            scanner.chmod(0o700)
            layout = installer.default_layout(base / "home")
            output = (
                "Found 20 encrypted DBs\n"
                "Scan complete: 5375MB scanned, 655 regions, 20 unique keys\n"
                "Matched 0/20 keys to known DBs\n"
            )
            failed = CompletedProcess([], 0, output, "")

            with patch.object(installer.subprocess, "run", return_value=failed):
                with self.assertRaises(installer.InstallerError) as raised:
                    installer._extract_macos_keys(runtime, layout, installer.Reporter(json_mode=True))

        self.assertEqual(raised.exception.error_code, "wechat_key_database_mismatch")
        self.assertEqual(
            raised.exception.next_action,
            "confirm_the_running_wechat_account_matches_the_detected_data_directory",
        )
        self.assertEqual(raised.exception.details["encrypted_db_count"], 20)
        self.assertEqual(raised.exception.details["matched_key_count"], 0)

    def test_scanner_uses_macos_authorization_and_writes_to_data_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = base / "runtime"
            runtime.mkdir()
            scanner = runtime / "find_all_keys_macos"
            scanner.write_text("", encoding="utf-8")
            scanner.chmod(0o700)
            layout = installer.default_layout(base / "home")

            def fake_run(command, **_kwargs):
                output = layout.data_dir / "all_keys.json"
                output.write_text(
                    json.dumps({"message/message_0.db": {"enc_key": "a" * 64}}),
                    encoding="utf-8",
                )
                return CompletedProcess(command, 0, "Saved\n", "")

            with patch.object(installer.subprocess, "run", side_effect=fake_run) as run:
                installer._extract_macos_keys(runtime, layout, installer.Reporter(json_mode=True))

            command = run.call_args.args[0]
            self.assertEqual(command[0], "/usr/bin/osascript")
            self.assertTrue(any("with administrator privileges" in argument for argument in command))
            authorized_command = command[-1]
            self.assertIn(shlex.quote(str(scanner)), authorized_command)
            self.assertIn(shlex.quote(str(layout.data_dir / "all_keys.json")), authorized_command)
            self.assertIn("--owner-uid", authorized_command)
            self.assertIn("--owner-gid", authorized_command)
            self.assertEqual((layout.data_dir / "all_keys.json").stat().st_mode & 0o777, 0o600)

    def test_initialize_extracts_keys_before_running_unprivileged_init(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            layout = installer.default_layout(home)
            runtime = layout.runtime_dir / ("a" * 40)
            runtime_python = runtime / ".venv" / "bin" / "python3"
            runtime_python.parent.mkdir(parents=True)
            runtime_python.write_text("", encoding="utf-8")
            installer._atomic_write_json(
                layout.manifest,
                {
                    "runtime_dir": str(runtime),
                    "host": "127.0.0.1",
                    "port": 8765,
                    "endpoint": "http://127.0.0.1:8765/mcp",
                },
            )
            args = argparse.Namespace(home=str(home))
            ready = {"ok": True, "status": "ready", "query_ready": True}

            with patch.object(installer.platform, "system", return_value="Darwin"), \
                 patch.object(installer.os, "geteuid", return_value=501), \
                 patch.object(installer, "_extract_macos_keys") as extract, \
                 patch.object(installer, "_run", return_value=CompletedProcess([], 0, "", "")) as run, \
                 patch.object(installer, "_service_command") as service_command, \
                 patch.object(installer, "service_status", return_value=ready):
                payload = installer.initialize(args, installer.Reporter(json_mode=True))

            extract.assert_called_once_with(runtime, layout, unittest.mock.ANY)
            init_command = run.call_args.args[0]
            self.assertEqual(init_command, [str(runtime_python), str(runtime / "main.py"), "init"])
            self.assertNotIn("sudo", init_command)
            self.assertEqual(run.call_args.kwargs["env"]["WECHAT_DECRYPT_DATA_DIR"], str(layout.data_dir))
            service_command.assert_not_called()
            self.assertEqual(payload["next_step"], "register_with_mcporter")

    def test_initialize_starts_existing_service_without_reinstalling_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            layout = installer.default_layout(home)
            runtime = layout.runtime_dir / ("a" * 40)
            runtime_python = runtime / ".venv" / "bin" / "python3"
            runtime_python.parent.mkdir(parents=True)
            runtime_python.write_text("", encoding="utf-8")
            installer._atomic_write_json(
                layout.manifest,
                {
                    "runtime_dir": str(runtime),
                    "host": "127.0.0.1",
                    "port": 8765,
                    "endpoint": "http://127.0.0.1:8765/mcp",
                },
            )
            args = argparse.Namespace(home=str(home))
            stopped = {"ok": False, "status": "stopped", "transport_ready": False}
            ready = {"ok": True, "status": "ready", "transport_ready": True, "initialized": True, "query_ready": True}

            with patch.object(installer.platform, "system", return_value="Darwin"), \
                 patch.object(installer.os, "geteuid", return_value=501), \
                 patch.object(installer, "_extract_macos_keys"), \
                 patch.object(installer, "_run", return_value=CompletedProcess([], 0, "", "")), \
                 patch.object(installer, "_service_command") as service_command, \
                 patch.object(installer, "service_status", side_effect=[stopped, ready]):
                payload = installer.initialize(args, installer.Reporter(json_mode=True))

            service_command.assert_called_once_with(
                runtime,
                layout,
                ["start"],
                error_context="初始化完成，但 LaunchAgent 启动失败",
            )
            self.assertTrue(payload["query_ready"])

    def test_scanner_failure_returns_stable_wechat_resign_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = base / "runtime"
            runtime.mkdir()
            scanner = runtime / "find_all_keys_macos"
            scanner.write_text("", encoding="utf-8")
            scanner.chmod(0o700)
            layout = installer.default_layout(base / "home")
            failed = CompletedProcess([], 1, "", "task_for_pid failed: 5")

            with patch.object(installer.subprocess, "run", return_value=failed):
                with self.assertRaises(installer.InstallerError) as raised:
                    installer._extract_macos_keys(runtime, layout, installer.Reporter(json_mode=True))

            self.assertEqual(raised.exception.error_code, "wechat_resign_required")
            self.assertEqual(
                raised.exception.next_action,
                "quit_and_adhoc_resign_wechat_then_reopen_and_retry_initialize",
            )

    def test_cancelled_macos_authorization_has_a_retryable_error_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = base / "runtime"
            runtime.mkdir()
            scanner = runtime / "find_all_keys_macos"
            scanner.write_text("", encoding="utf-8")
            scanner.chmod(0o700)
            layout = installer.default_layout(base / "home")
            cancelled = CompletedProcess([], 1, "", "execution error: User canceled. (-128)")

            with patch.object(installer.subprocess, "run", return_value=cancelled):
                with self.assertRaises(installer.InstallerError) as raised:
                    installer._extract_macos_keys(runtime, layout, installer.Reporter(json_mode=True))

            self.assertEqual(raised.exception.error_code, "administrator_authorization_cancelled")
            self.assertEqual(
                raised.exception.next_action,
                "retry_initialize_and_approve_the_macos_administrator_prompt",
            )


if __name__ == "__main__":
    unittest.main()
