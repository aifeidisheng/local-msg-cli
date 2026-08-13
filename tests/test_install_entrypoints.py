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

    def test_install_defaults_to_the_project_gitee_source(self):
        script = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn(
            'readonly DEFAULT_REPOSITORY="https://gitee.com/feipig_up_tree/local-msg-cli.git"',
            script,
        )
        self.assertNotIn("--fallback-repository URL", self.run_script("install.sh", "--help").stdout)

    def test_install_rejects_unknown_arguments_before_network_access(self):
        result = self.run_script("install.sh", "--unsupported")

        self.assertEqual(result.returncode, 2)
        self.assertIn("Unknown option", result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error_code"], "invalid_arguments")
        self.assertEqual(payload["phase"], "arguments")
        self.assertEqual(payload["user_message"], "安装参数不完整，请稍后重试。")
        self.assertNotIn("--unsupported", payload["user_message"])

    def test_setup_refuses_to_act_as_an_end_user_installer(self):
        result = self.run_script("setup.sh")

        self.assertEqual(result.returncode, 2)
        self.assertIn("./install.sh --initialize", result.stderr)
        self.assertIn("./setup.sh --development", result.stderr)

    def test_end_user_docs_use_the_canonical_macos_command(self):
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        usage = (ROOT / "USAGE.md").read_text(encoding="utf-8")
        macos_install = readme.split("## macOS 正式安装", 1)[1].split("## 源码开发安装", 1)[0]

        self.assertIn("install.sh", agents)
        self.assertIn("--repository '<user-provided-repository-url>'", agents)
        self.assertIn("--repository 'https://gitee.com/feipig_up_tree/local-msg-cli.git'", macos_install)
        self.assertNotIn("--fallback-repository", macos_install)
        self.assertNotIn("Git 网络操作会重试", macos_install)
        self.assertIn("install.sh --initialize", usage)

    def test_end_user_docs_require_plain_language_status_updates(self):
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("User-facing communication (plain-language mode)", agents)
        self.assertIn("Do not produce a running installation diary", agents)
        self.assertIn("### 普通用户会经历什么", readme)
        self.assertIn("用户不需要打开终端或输入命令", readme)
        self.assertIn("不展示完整 JSON", readme)
        self.assertIn("do not display a", agents)
        self.assertIn("detected account ID", agents)
        self.assertIn("账号标识", readme)
        self.assertIn("不逐步复述执行结果", readme)

    def test_bootstrap_uses_quiet_plain_language_progress(self):
        script = (ROOT / "install.sh").read_text(encoding="utf-8")

        self.assertIn("clone --quiet --depth 1 --no-tags", script)
        self.assertIn("[准备] 正在下载安装文件", script)
        self.assertIn("[安装] 正在完成安装", script)
        self.assertIn("[检查] 正在确认下一步", script)
        self.assertNotIn("Cloning confirmed main release", script)
        self.assertNotIn("Deploying verified commit", script)
        self.assertNotIn('echo "  export https_proxy', script)

    def test_development_setup_never_regenerates_version_policy(self):
        script = (ROOT / "setup.sh").read_text(encoding="utf-8")

        self.assertIn("版本策略文件已就绪（不可在本地修改）", script)
        self.assertNotIn("生成 version-guard.policy.json 模板", script)
        self.assertNotIn("编辑 version-guard.policy.json", script)

    def test_installer_progress_avoids_internal_implementation_terms(self):
        source = (ROOT / "installer.py").read_text(encoding="utf-8")
        progress_lines = "\n".join(
            line for line in source.splitlines() if "reporter.progress" in line
        )

        for internal_term in ("Git", "commit", "Python", "LaunchAgent", "PID", "端口", "扫描器", "预解密"):
            self.assertNotIn(internal_term, progress_lines)

    def test_inspect_failure_still_returns_combined_json(self):
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
                "\"user_message\":\"请打开并登录微信\","
                "\"requires_user_action\":\"open_and_sign_in_wechat\","
                "\"retry_command\":\"inspect\","
                "\"authorization_prompt_count\":0,"
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
        self.assertTrue(payload["install_complete"])
        self.assertFalse(payload["initialize_complete"])
        self.assertEqual(payload["phase"], "inspect")
        self.assertEqual(payload["error_code"], "wechat_not_running")
        self.assertEqual(payload["next_action"], "start_wechat_and_retry_initialize")
        self.assertEqual(payload["authorization_prompt_count"], 0)
        self.assertEqual(payload["user_message"], "请打开并登录微信")
        self.assertEqual(payload["requires_user_action"], "open_and_sign_in_wechat")
        self.assertEqual(payload["retry_command"], "inspect")
        self.assertEqual(payload["inspect"]["error_code"], "wechat_not_running")

    def test_inspect_invalid_stdout_returns_structured_phase_error(self):
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
            management_cli.write_text("#!/bin/bash\necho 'not-json'\n", encoding="utf-8")
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
        self.assertTrue(payload["install_complete"])
        self.assertFalse(payload["initialize_complete"])
        self.assertEqual(payload["phase"], "inspect")
        self.assertEqual(payload["error_code"], "inspect_output_invalid")


if __name__ == "__main__":
    unittest.main()
