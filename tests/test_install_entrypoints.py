import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InstallEntrypointTests(unittest.TestCase):
    def run_script(self, script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/bash", str(ROOT / script), *arguments],
            cwd=ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_install_script_has_valid_bash_syntax(self):
        result = subprocess.run(
            ["/bin/bash", "-n", str(ROOT / "install.sh")],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_install_help_identifies_canonical_end_user_entrypoint(self):
        result = self.run_script("install.sh", "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Usage: ./install.sh --initialize", result.stdout)
        self.assertIn("independent runtime", result.stdout)
        self.assertIn("setup.sh --development", result.stdout)

    def test_install_rejects_unknown_arguments_before_network_access(self):
        result = self.run_script("install.sh", "--unsupported")

        self.assertEqual(result.returncode, 2)
        self.assertIn("Unknown option", result.stderr)

    def test_setup_refuses_to_act_as_an_end_user_installer(self):
        result = self.run_script("setup.sh")

        self.assertEqual(result.returncode, 2)
        self.assertIn("./install.sh --initialize", result.stderr)
        self.assertIn("./setup.sh --development", result.stderr)

    def test_end_user_docs_use_the_canonical_macos_command(self):
        canonical = "./install.sh --initialize"
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        usage = (ROOT / "USAGE.md").read_text(encoding="utf-8")
        macos_install = readme.split("## macOS 正式安装", 1)[1].split("## 源码开发安装", 1)[0]

        self.assertIn(canonical, agents)
        self.assertIn(canonical, macos_install)
        self.assertNotIn("\n./install.sh\n", macos_install)
        self.assertIn(canonical, usage)

    def test_initialize_failure_still_returns_combined_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repository = base / "release"
            repository.mkdir()
            (repository / "installer.py").write_text(
                "import json\n"
                "print(json.dumps({'ok': True, 'installation': "
                "{'endpoint': 'http://127.0.0.1:8765/mcp'}}))\n",
                encoding="utf-8",
            )
            subprocess.run(["/usr/bin/git", "init"], cwd=repository, check=True, capture_output=True)
            subprocess.run(["/usr/bin/git", "checkout", "-b", "main"], cwd=repository, check=True, capture_output=True)
            subprocess.run(["/usr/bin/git", "add", "installer.py"], cwd=repository, check=True, capture_output=True)
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-c",
                    "user.name=tests",
                    "-c",
                    "user.email=tests@example.invalid",
                    "commit",
                    "-m",
                    "fixture",
                ],
                cwd=repository,
                check=True,
                capture_output=True,
            )

            home = base / "home"
            management_cli = home / "Library/Application Support/WeChatDecryptLight/bin/wechat-decrypt-light"
            management_cli.parent.mkdir(parents=True)
            management_cli.write_text(
                "#!/bin/bash\n"
                "echo '{\"ok\":false,\"error_code\":\"wechat_not_running\","
                "\"error\":\"WeChat is not running\","
                "\"next_action\":\"start_wechat_and_retry_initialize\"}'\n"
                "exit 1\n",
                encoding="utf-8",
            )
            management_cli.chmod(0o700)

            fake_bin = base / "bin"
            fake_bin.mkdir()
            fake_uname = fake_bin / "uname"
            fake_uname.write_text("#!/bin/bash\necho Darwin\n", encoding="utf-8")
            fake_uname.chmod(0o700)

            env = dict(os.environ)
            env["HOME"] = str(home)
            env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
            result = subprocess.run(
                [
                    "/bin/bash",
                    str(ROOT / "install.sh"),
                    "--initialize",
                    "--repository",
                    str(repository),
                    "--python",
                    sys.executable,
                ],
                cwd=ROOT,
                env=env,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        self.assertEqual(result.returncode, 1, result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "wechat_not_running")
        self.assertEqual(payload["next_action"], "start_wechat_and_retry_initialize")
        self.assertEqual(payload["initialize"]["error_code"], "wechat_not_running")


if __name__ == "__main__":
    unittest.main()
