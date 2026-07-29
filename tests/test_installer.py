import argparse
import json
import os
import plistlib
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

    def test_discover_db_manifest_namespaces_multiple_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first" / "db_storage"
            second = root / "second" / "db_storage"
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            page1 = bytes(range(256)) * 16
            (first / "message.db").write_bytes(page1)
            (second / "message.db").write_bytes(page1)

            manifest = installer._discover_db_salts(
                root,
                first,
                account_dirs=[first, second],
            )
            self.assertIsNotNone(manifest)
            try:
                entries = json.loads(manifest.read_text(encoding="utf-8"))
            finally:
                manifest.unlink(missing_ok=True)

            self.assertEqual(
                [entry["name"] for entry in entries],
                [
                    "__account_000__/message.db",
                    "__account_001__/message.db",
                ],
            )

    def test_install_deploys_runtime_without_touching_launchagent(self):
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
                 patch.object(installer, "_service_command") as service_command, \
                 patch.object(installer, "service_status") as service_status:
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
            service_command.assert_not_called()
            service_status.assert_not_called()
            self.assertEqual(payload["phase"], "runtime_installed")
            self.assertFalse(payload["service_enabled"])
            self.assertEqual(payload["next_step"], "inspect")
            activation = json.loads(layout.activation_state.read_text(encoding="utf-8"))
            self.assertTrue(activation["runtime_installed"])

    def test_install_is_not_affected_by_service_install_failure(self):
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
                 patch.object(installer, "_service_command", side_effect=installer.InstallerError("failed")) as service_command:
                payload = installer.install(args, installer.Reporter(json_mode=True))

            service_command.assert_not_called()
            self.assertTrue(payload["ok"])
            self.assertNotEqual(layout.current.resolve(), old_runtime.resolve())
            self.assertTrue(layout.manifest.exists())


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
                        "runtime_installed": True,
                        "service_enabled": False,
                        "next_step": "inspect",
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
            self.assertTrue(payload["runtime_installed"])
            self.assertFalse(payload["service_enabled"])
            self.assertEqual(payload["next_step"], "inspect")
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

    def test_management_cli_lifts_authorization_count_from_error_details(self):
        error = installer.InstallerError(
            "preflight failed",
            error_code="wechat_not_running",
            details={"authorization_prompt_count": 0},
        )
        with patch.object(installer, "status", side_effect=error), \
             patch.object(installer.Reporter, "result") as result:
            exit_code = installer.main(["status", "--json"])

        self.assertEqual(exit_code, 1)
        payload = result.call_args.args[0]
        self.assertEqual(payload["authorization_prompt_count"], 0)

    def test_management_cli_adds_user_recovery_fields(self):
        error = installer.InstallerError(
            "微信尚未运行",
            error_code="wechat_not_running",
            next_action="start_wechat_and_retry_initialize",
        )
        with patch.object(installer, "initialize", side_effect=error), \
             patch.object(installer.Reporter, "result") as result:
            exit_code = installer.main(["initialize", "--json"])

        self.assertEqual(exit_code, 1)
        payload = result.call_args.args[0]
        self.assertEqual(payload["user_message"], "微信尚未运行")
        self.assertEqual(payload["requires_user_action"], "open_wechat")
        self.assertEqual(payload["retry_command"], "initialize")

    def test_process_access_failure_retries_read_only_inspection(self):
        error = installer.InstallerError(
            "微信进程访问失败",
            error_code="wechat_process_access_failed",
            next_action="inspect_wechat_process_and_signature_before_retry",
        )
        with patch.object(installer, "initialize", side_effect=error), \
             patch.object(installer.Reporter, "result") as result:
            exit_code = installer.main(["initialize", "--json"])

        self.assertEqual(exit_code, 1)
        payload = result.call_args.args[0]
        self.assertEqual(payload["requires_user_action"], "keep_wechat_open_and_signed_in")
        self.assertEqual(payload["retry_command"], "inspect")

    def test_legacy_initialize_resign_flag_routes_to_separate_prepare_stage(self):
        error = installer.InstallerError(
            "微信尚未完成 ad-hoc 重签名",
            error_code="wechat_not_adhoc_signed",
            next_action="confirm_and_run_prepare_wechat",
            details={"authorization_prompt_count": 0, "wechat_running": False},
        )
        with patch.object(installer, "initialize", side_effect=error) as initialize, \
             patch.object(installer.Reporter, "result") as result:
            exit_code = installer.main(
                ["initialize", "--confirm-resign", "--json"]
            )

        self.assertEqual(exit_code, 1)
        self.assertTrue(initialize.call_args.args[0].confirm_resign)
        payload = result.call_args.args[0]
        self.assertEqual(payload["retry_command"], "prepare-wechat --confirm-resign")
        self.assertEqual(payload["authorization_prompt_count"], 0)


class MacInitializeTests(unittest.TestCase):
    @staticmethod
    def _make_wechat_app(root: Path, bundle_id: str = "com.tencent.xinWeChat") -> Path:
        app = root / "Applications" / "WeChat.app"
        contents = app / "Contents"
        contents.mkdir(parents=True)
        with (contents / "Info.plist").open("wb") as info_file:
            plistlib.dump({"CFBundleIdentifier": bundle_id}, info_file)
        return app

    def test_prepare_wechat_requires_explicit_confirmation(self):
        args = argparse.Namespace(home=None, confirm_resign=False)
        with patch.object(installer.subprocess, "run") as run:
            with self.assertRaises(installer.InstallerError) as raised:
                installer.prepare_wechat(args, installer.Reporter(json_mode=True))

            self.assertEqual(raised.exception.error_code, "wechat_resign_confirmation_required")
            run.assert_not_called()

    def test_inspect_stops_at_prepare_boundary_without_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            layout = installer.default_layout(home)
            runtime = layout.runtime_dir / ("a" * 40)
            runtime.mkdir(parents=True)
            installer._atomic_write_json(
                layout.manifest,
                {"runtime_dir": str(runtime), "endpoint": "http://127.0.0.1:8765/mcp"},
            )
            error = installer.InstallerError(
                "微信尚未完成 ad-hoc 重签名；未弹出管理员授权窗口",
                error_code="wechat_not_adhoc_signed",
                next_action="confirm_and_run_prepare_wechat",
                details={"authorization_prompt_count": 0, "wechat_running": False},
            )
            args = argparse.Namespace(home=str(home))

            with patch.object(installer.platform, "system", return_value="Darwin"), \
                 patch.object(installer, "_safe_service_status", return_value={"status": "stopped"}), \
                 patch.object(installer, "_preflight_macos_initialize", return_value={"db_dir": None}), \
                 patch.object(installer, "_preflight_macos_scanner", side_effect=error), \
                 patch.object(installer, "_extract_macos_keys") as extract:
                payload = installer.inspect(args, installer.Reporter(json_mode=True))

            self.assertTrue(payload["ok"])
            self.assertEqual(payload["next_step"], "prepare_wechat")
            self.assertEqual(payload["preflight_error"]["error_code"], "wechat_not_adhoc_signed")
            extract.assert_not_called()

    def test_prepare_wechat_rejects_non_wechat_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            app = self._make_wechat_app(home, bundle_id="com.example.other")
            args = argparse.Namespace(home=str(home), confirm_resign=True)
            preflight = {"config": {}, "detected": {"app_path": str(app)}}

            with patch.object(installer, "_installed_runtime", return_value=home), \
                 patch.object(installer, "_preflight_macos_initialize", return_value=preflight), \
                 patch.object(installer.subprocess, "run") as run:
                with self.assertRaises(installer.InstallerError) as raised:
                    installer.prepare_wechat(args, installer.Reporter(json_mode=True))

            self.assertEqual(raised.exception.error_code, "wechat_app_identity_mismatch")
            run.assert_not_called()

    def test_prepare_wechat_rejects_wechat_bundle_outside_standard_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            outside = home / "Downloads"
            app = self._make_wechat_app(outside)
            with self.assertRaises(installer.InstallerError) as raised:
                installer._validate_wechat_bundle(app, home)

            self.assertEqual(raised.exception.error_code, "wechat_app_path_not_allowed")

    def test_prepare_wechat_rejects_symlink_at_standard_app_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            real_app = self._make_wechat_app(home / "real")
            standard = home / "Applications" / "WeChat.app"
            standard.parent.mkdir(parents=True, exist_ok=True)
            standard.symlink_to(real_app)

            with self.assertRaises(installer.InstallerError) as raised:
                installer._validate_wechat_bundle(standard, home)

            self.assertEqual(raised.exception.error_code, "wechat_app_path_not_allowed")

    def test_prepare_wechat_requires_all_bundle_processes_to_be_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            app = self._make_wechat_app(home)
            args = argparse.Namespace(home=str(home), confirm_resign=True)
            preflight = {"config": {}, "detected": {"app_path": str(app)}}
            running = CompletedProcess([], 0, "123\n", "")

            with patch.object(installer, "_installed_runtime", return_value=home), \
                 patch.object(installer, "_preflight_macos_initialize", return_value=preflight), \
                 patch.object(installer.subprocess, "run", return_value=running) as run:
                with self.assertRaises(installer.InstallerError) as raised:
                    installer.prepare_wechat(args, installer.Reporter(json_mode=True))

            self.assertEqual(raised.exception.error_code, "wechat_must_quit_for_resign")
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][1], "-f")
            self.assertIn("WeChat\.app/Contents/", run.call_args.args[0][2])
            self.assertEqual(raised.exception.details["running_pids"], ["123"])

    def test_prepare_wechat_cleans_attributes_and_signs_in_one_authorized_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            app = self._make_wechat_app(home)
            args = argparse.Namespace(home=str(home), confirm_resign=True)
            preflight = {"config": {}, "detected": {"app_path": str(app)}}
            stopped = CompletedProcess([], 1, "", "")
            official = CompletedProcess([], 0, "", "Authority=Apple Distribution\n")
            direct_xattr_failed = CompletedProcess([], 1, "", "Operation not permitted")
            authorized = CompletedProcess([], 0, "", "")
            adhoc = CompletedProcess([], 0, "", "Signature=adhoc\n")
            opened = CompletedProcess([], 0, "", "")

            with patch.object(installer, "_installed_runtime", return_value=home), \
                 patch.object(installer, "_preflight_macos_initialize", return_value=preflight), \
                 patch.object(
                     installer.subprocess,
                     "run",
                     side_effect=[
                         stopped,
                         official,
                         direct_xattr_failed,
                         authorized,
                         adhoc,
                         opened,
                     ],
                 ) as run:
                payload = installer.prepare_wechat(args, installer.Reporter(json_mode=True))

            self.assertTrue(payload["ok"])
            self.assertEqual(payload["authorization_prompt_count"], 1)
            authorization_calls = [
                call for call in run.call_args_list if call.args[0][0] == "/usr/bin/osascript"
            ]
            self.assertEqual(len(authorization_calls), 1)
            command = authorization_calls[0].args[0][-1]
            self.assertEqual(
                command,
                " && ".join(
                    [
                        shlex.join(["/usr/bin/xattr", "-cr", str(app.resolve())]),
                        shlex.join(
                            [
                                "/usr/bin/codesign",
                                "--force",
                                "--deep",
                                "--sign",
                                "-",
                                str(app.resolve()),
                            ]
                        ),
                    ]
                ),
            )

    def test_resign_cleans_attributes_before_direct_codesign(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self._make_wechat_app(Path(tmp))
            succeeded = CompletedProcess([], 0, "", "")

            with patch.object(installer.subprocess, "run", return_value=succeeded) as run, \
                 patch.object(installer, "_is_adhoc_signed", return_value=True):
                prompt_count = installer._resign_wechat_app(app)

            self.assertEqual(prompt_count, 0)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(
                run.call_args_list[0].args[0],
                ["/usr/bin/xattr", "-cr", str(app)],
            )
            self.assertEqual(
                run.call_args_list[1].args[0],
                [
                    "/usr/bin/codesign",
                    "--force",
                    "--deep",
                    "--sign",
                    "-",
                    str(app),
                ],
            )

    def test_normalize_account_key_output_keeps_only_valid_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            first = base / "first" / "db_storage"
            second = base / "second" / "db_storage"
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            keys_file = base / "all_keys.json"
            keys_file.write_text(
                json.dumps(
                    {
                        "__account_000__/message/message_0.db": {"enc_key": "a" * 64},
                        "__account_001__/message/message_0.db": {"enc_key": "b" * 64},
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(
                installer,
                "_valid_key_payload",
                side_effect=lambda _keys, db_dir: db_dir == second,
            ):
                selected = installer._normalize_account_key_output(
                    keys_file,
                    [first, second],
                    first,
                )

            self.assertEqual(selected, second)
            normalized = json.loads(keys_file.read_text(encoding="utf-8"))
            self.assertEqual(normalized["_db_dir"], str(second))
            self.assertEqual(
                normalized["message/message_0.db"]["enc_key"],
                "b" * 64,
            )
            self.assertFalse(any(key.startswith("__account_") for key in normalized))

    def test_existing_keys_correct_stale_account_without_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            layout = installer.default_layout(home)
            layout.data_dir.mkdir(parents=True)
            stale = home / "accounts" / "stale" / "db_storage"
            current = home / "accounts" / "current" / "db_storage"
            stale.mkdir(parents=True)
            current.mkdir(parents=True)
            (layout.data_dir / "config.json").write_text(
                json.dumps({"db_dir": str(stale)}),
                encoding="utf-8",
            )
            (layout.data_dir / "all_keys.json").write_text(
                json.dumps({"message.db": {"enc_key": "a" * 64}}),
                encoding="utf-8",
            )
            preflight = {
                "db_dir": stale,
                "account_candidates": [stale, current],
            }

            with patch.object(
                installer,
                "_matching_key_accounts",
                return_value=[current],
            ), patch.object(installer, "_preflight_macos_scanner") as scanner:
                prompted = installer._extract_macos_keys(
                    home,
                    layout,
                    installer.Reporter(json_mode=True),
                    preflight,
                )

            self.assertFalse(prompted)
            scanner.assert_not_called()
            self.assertEqual(preflight["db_dir"], current)
            self.assertTrue(preflight["account_changed"])
            config = json.loads((layout.data_dir / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(Path(config["db_dir"]), current.resolve())
            self.assertEqual(config["db_dir_selection"], "validated_existing_keys")

    def test_accounts_and_select_account_use_discovered_ids_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            layout = installer.default_layout(home)
            account = (
                home
                / "Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
                / "wxid_current_abcd"
                / "db_storage"
            )
            account.mkdir(parents=True)
            args = argparse.Namespace(home=str(home), account="wxid_current_abcd")

            selected = installer.select_account(args, installer.Reporter(json_mode=True))
            listed = installer.accounts(args, installer.Reporter(json_mode=True))

            self.assertTrue(selected["ok"])
            self.assertEqual(selected["account"]["account_id"], "wxid_current_abcd")
            self.assertEqual(listed["selected_account_id"], "wxid_current_abcd")
            config = json.loads((layout.data_dir / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(Path(config["db_dir"]), account.resolve())
            self.assertEqual(config["db_dir_selection"], "manual")

            args.account = "../../outside"
            with self.assertRaises(installer.InstallerError) as raised:
                installer.select_account(args, installer.Reporter(json_mode=True))
            self.assertEqual(raised.exception.error_code, "wechat_account_not_found")

    def test_legacy_root_owned_config_is_replaced_without_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = installer.default_layout(Path(tmp))
            layout.data_dir.mkdir(parents=True)
            config_file = layout.data_dir / "config.json"
            config_file.write_text('{"db_dir":"/tmp/example"}', encoding="utf-8")

            with patch.object(installer.os, "getuid", return_value=os.getuid() + 1), \
                 patch.object(installer.subprocess, "run") as run:
                repaired = installer._normalize_legacy_data_files(layout)

            self.assertEqual(repaired, ["config.json"])
            self.assertEqual(
                config_file.read_text(encoding="utf-8"),
                '{"db_dir":"/tmp/example"}',
            )
            self.assertEqual(config_file.stat().st_mode & 0o777, 0o600)
            run.assert_not_called()

    def test_version_mismatch_stops_before_any_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = base / "runtime"
            runtime.mkdir()
            layout = installer.default_layout(base / "home")
            db_dir = base / "db_storage"
            db_dir.mkdir()
            version_result = Mock(
                ok=False,
                reasons=["当前微信版本不在允许区间: 4.2.0"],
                details={
                    "detected": {
                        "short_version": "4.2.0",
                        "build_version": "123",
                        "app_path": "/Applications/WeChat.app",
                    }
                },
            )

            with patch("config.load_config", return_value={"db_dir": str(db_dir)}), \
                 patch("wechat_version_guard.check_version", return_value=version_result), \
                 patch.object(installer.subprocess, "run") as run:
                with self.assertRaises(installer.InstallerError) as raised:
                    installer._preflight_macos_initialize(runtime, layout)

            self.assertEqual(raised.exception.error_code, "version_not_allowed")
            self.assertEqual(raised.exception.details["detected_version"], "4.2.0")
            run.assert_not_called()

    def test_stale_configured_account_falls_back_to_discovered_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            runtime = home / "runtime"
            runtime.mkdir()
            layout = installer.default_layout(home)
            current = (
                home
                / "Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
                / "wxid_current_abcd"
                / "db_storage"
            )
            current.mkdir(parents=True)
            stale = home / "missing" / "db_storage"
            version_result = Mock(
                ok=True,
                reasons=[],
                details={"detected": {"app_path": "/Applications/WeChat.app"}},
            )

            with patch("config.load_config", return_value={"db_dir": str(stale)}), \
                 patch("wechat_version_guard.check_version", return_value=version_result):
                preflight = installer._preflight_macos_initialize(runtime, layout)

            self.assertEqual(preflight["db_dir"], current.resolve())
            self.assertEqual(preflight["account_candidates"], [current.resolve()])

    def test_wechat_not_running_stops_before_any_authorization(self):
        preflight = {
            "config": {"wechat_process": "WeChat"},
            "detected": {"app_path": "/Applications/WeChat.app"},
        }
        stopped = CompletedProcess([], 1, "", "")

        adhoc_signature = CompletedProcess([], 0, "", "Signature=adhoc\n")
        with patch.object(
            installer.subprocess, "run", side_effect=[adhoc_signature, stopped]
        ) as run:
            with self.assertRaises(installer.InstallerError) as raised:
                installer._preflight_macos_scanner(preflight)

        self.assertEqual(raised.exception.error_code, "wechat_not_running")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1].args[0][0], "/usr/bin/pgrep")

    def test_non_adhoc_wechat_stops_before_any_authorization(self):
        preflight = {
            "config": {"wechat_process": "WeChat"},
            "detected": {"app_path": "/Applications/WeChat.app"},
        }
        running = CompletedProcess([], 0, "123\n", "")
        official_signature = CompletedProcess(
            [], 0, "", "Authority=Apple Distribution\nflags=0x10000(runtime)\n"
        )

        with patch.object(
            installer.subprocess,
            "run",
            side_effect=[official_signature, running],
        ) as run:
            with self.assertRaises(installer.InstallerError) as raised:
                installer._preflight_macos_scanner(preflight)

        self.assertEqual(raised.exception.error_code, "wechat_not_adhoc_signed")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0][0], "/usr/bin/codesign")
        self.assertEqual(run.call_args_list[1].args[0][0], "/usr/bin/pgrep")
        self.assertEqual(raised.exception.details["wechat_running"], True)

    def test_adhoc_wechat_passes_unprivileged_scanner_preflight(self):
        preflight = {
            "config": {"wechat_process": "WeChat"},
            "detected": {"app_path": "/Applications/WeChat.app"},
        }
        running = CompletedProcess([], 0, "123\n", "")
        adhoc_signature = CompletedProcess([], 0, "", "Signature=adhoc\n")

        with patch.object(
            installer.subprocess,
            "run",
            side_effect=[adhoc_signature, running],
        ) as run:
            installer._preflight_macos_scanner(preflight)

        self.assertEqual(run.call_count, 2)
        self.assertNotIn("/usr/bin/osascript", [call.args[0][0] for call in run.call_args_list])

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

            with patch.object(installer, "_preflight_macos_scanner"), \
                 patch.object(installer.subprocess, "run", return_value=failed):
                with self.assertRaises(installer.InstallerError) as raised:
                    installer._extract_macos_keys(
                        runtime, layout, installer.Reporter(json_mode=True), {}
                    )

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

            with patch.object(installer, "_preflight_macos_scanner"), \
                 patch.object(installer.subprocess, "run", side_effect=fake_run) as run:
                prompted = installer._extract_macos_keys(
                    runtime, layout, installer.Reporter(json_mode=True), {}
                )

            self.assertTrue(prompted)
            self.assertEqual(run.call_count, 1)
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
            # 旧参数即使为 True，也不能让 initialize 进入重签逻辑。
            args = argparse.Namespace(home=str(home), confirm_resign=True)
            with patch.object(installer.platform, "system", return_value="Darwin"), \
                 patch.object(installer.os, "geteuid", return_value=501), \
                 patch.object(installer, "_preflight_macos_initialize", return_value={"db_dir": None}) as preflight, \
                 patch.object(installer, "_extract_macos_keys", return_value=True) as extract, \
                 patch.object(installer, "_run", return_value=CompletedProcess([], 0, "", "")) as run, \
                 patch.object(installer, "_service_command") as service_command, \
                 patch.object(installer, "service_status") as service_status:
                payload = installer.initialize(args, installer.Reporter(json_mode=True))

            preflight.assert_called_once_with(runtime, layout)
            extract.assert_called_once_with(runtime, layout, unittest.mock.ANY, {"db_dir": None})
            init_command = run.call_args.args[0]
            self.assertEqual(init_command, [str(runtime_python), str(runtime / "main.py"), "init"])
            self.assertNotIn("sudo", init_command)
            self.assertEqual(run.call_args.kwargs["env"]["WECHAT_DECRYPT_DATA_DIR"], str(layout.data_dir))
            service_command.assert_not_called()
            service_status.assert_not_called()
            self.assertEqual(payload["authorization_prompt_count"], 1)
            self.assertTrue(payload["initialized"])
            self.assertFalse(payload["query_ready"])
            self.assertEqual(payload["next_step"], "enable_service")
            activation = json.loads(layout.activation_state.read_text(encoding="utf-8"))
            self.assertTrue(activation["initialized"])

    def test_enable_service_is_a_separate_retryable_stage(self):
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
            installer._update_activation_state(layout, runtime_installed=True, initialized=True)
            args = argparse.Namespace(home=str(home))
            ready = {"ok": True, "status": "ready", "transport_ready": True, "initialized": True, "query_ready": True}

            with patch.object(installer, "_service_command") as service_command, \
                 patch.object(installer, "service_status", return_value=ready):
                payload = installer.enable_service(args, installer.Reporter(json_mode=True))

            service_command.assert_called_once_with(
                runtime,
                layout,
                ["install", "--host", "127.0.0.1", "--port", "8765"],
                error_context="LaunchAgent 启用失败；初始化结果已保留，可只重试 enable-service",
            )
            self.assertTrue(payload["query_ready"])
            self.assertEqual(payload["next_step"], "register_with_mcporter")

    def test_enable_service_failure_preserves_initialized_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            layout = installer.default_layout(home)
            runtime = layout.runtime_dir / ("a" * 40)
            runtime.mkdir(parents=True)
            installer._atomic_write_json(
                layout.manifest,
                {
                    "runtime_dir": str(runtime),
                    "host": "127.0.0.1",
                    "port": 8765,
                },
            )
            installer._update_activation_state(layout, runtime_installed=True, initialized=True)
            args = argparse.Namespace(home=str(home))
            stopped = {"ok": False, "status": "stopped", "initialized": True}

            with patch.object(installer, "service_status", return_value=stopped), \
                 patch.object(
                     installer,
                     "_service_command",
                     side_effect=installer.InstallerError("launchd failed"),
                 ):
                with self.assertRaises(installer.InstallerError):
                    installer.enable_service(args, installer.Reporter(json_mode=True))

            activation = installer._read_activation_state(layout)
            self.assertTrue(activation["initialized"])
            self.assertFalse(activation.get("service_enabled", False))

    def test_enable_service_accepts_initialized_legacy_install_without_stage_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            layout = installer.default_layout(home)
            runtime = layout.runtime_dir / ("a" * 40)
            runtime.mkdir(parents=True)
            installer._atomic_write_json(
                layout.manifest,
                {
                    "runtime_dir": str(runtime),
                    "host": "127.0.0.1",
                    "port": 8765,
                },
            )
            args = argparse.Namespace(home=str(home))
            ready = {"ok": True, "status": "ready", "initialized": True, "query_ready": True}

            with patch.object(installer, "service_status", return_value=ready), \
                 patch.object(installer, "_service_command"):
                payload = installer.enable_service(args, installer.Reporter(json_mode=True))

            self.assertTrue(payload["ok"])
            self.assertTrue(installer._read_activation_state(layout)["service_enabled"])

    def test_scanner_task_for_pid_failure_routes_to_process_inspection(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = base / "runtime"
            runtime.mkdir()
            scanner = runtime / "find_all_keys_macos"
            scanner.write_text("", encoding="utf-8")
            scanner.chmod(0o700)
            layout = installer.default_layout(base / "home")
            failed = CompletedProcess([], 1, "", "task_for_pid failed: 5")

            with patch.object(installer, "_preflight_macos_scanner"), \
                 patch.object(installer.subprocess, "run", return_value=failed):
                with self.assertRaises(installer.InstallerError) as raised:
                    installer._extract_macos_keys(
                        runtime, layout, installer.Reporter(json_mode=True), {}
                    )

            self.assertEqual(raised.exception.error_code, "wechat_process_access_failed")
            self.assertEqual(
                raised.exception.next_action,
                "inspect_wechat_process_and_signature_before_retry",
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

            with patch.object(installer, "_preflight_macos_scanner"), \
                 patch.object(installer.subprocess, "run", return_value=cancelled):
                with self.assertRaises(installer.InstallerError) as raised:
                    installer._extract_macos_keys(
                        runtime, layout, installer.Reporter(json_mode=True), {}
                    )

            self.assertEqual(raised.exception.error_code, "administrator_authorization_cancelled")
            self.assertEqual(
                raised.exception.next_action,
                "retry_initialize_and_approve_the_macos_administrator_prompt",
            )


if __name__ == "__main__":
    unittest.main()
