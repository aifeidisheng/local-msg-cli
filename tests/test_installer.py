import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
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
            digest = installer._sha256(source / "installer.py")
            with patch.object(
                installer,
                "_git",
                side_effect=[
                    "a" * 40,
                    "https://github.com/example/wechat-decrypt.git",
                    "?? unexpected.py",
                ],
            ):
                with self.assertRaisesRegex(installer.InstallerError, "不可复现版本"):
                    installer.verify_source(
                        source,
                        expected_commit="a" * 40,
                        expected_repository="git@github.com:example/wechat-decrypt.git",
                        expected_installer_sha256=digest,
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
            expected_commit="a" * 40,
            expected_repository="https://github.com/example/wechat-decrypt.git",
            expected_installer_sha256="b" * 64,
            allow_dirty_source=False,
            python="/usr/bin/python3",
            host="127.0.0.1",
            port=8765,
        )

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
            self.assertEqual(manifest["version"], "a" * 40)
            self.assertEqual(layout.current.resolve(), Path(manifest["runtime_dir"]))
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
    def test_json_flag_is_accepted_after_subcommand(self):
        with patch.object(installer, "status", return_value={"ok": True, "command": "status"}):
            with patch.object(installer.Reporter, "result") as result:
                exit_code = installer.main(["status", "--json"])

        self.assertEqual(exit_code, 0)
        result.assert_called_once_with({"ok": True, "command": "status"})


if __name__ == "__main__":
    unittest.main()
