import subprocess
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
        self.assertIn("independent runtime", result.stdout)
        self.assertIn("setup.sh --development", result.stdout)

    def test_install_rejects_unknown_arguments_before_network_access(self):
        result = self.run_script("install.sh", "--unsupported")

        self.assertEqual(result.returncode, 2)
        self.assertIn("Unknown option", result.stderr)

    def test_setup_refuses_to_act_as_an_end_user_installer(self):
        result = self.run_script("setup.sh")

        self.assertEqual(result.returncode, 2)
        self.assertIn("./install.sh", result.stderr)
        self.assertIn("./setup.sh --development", result.stderr)


if __name__ == "__main__":
    unittest.main()
