import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import runtime_guard


class RuntimeGuardTests(unittest.TestCase):
    def test_macos_source_checkout_without_marker_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                runtime_guard.require_macos_execution_mode(
                    "main.py init",
                    root=Path(tmp),
                    system="Darwin",
                )

        self.assertEqual(raised.exception.code, 2)
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload["error_code"], "end_user_must_use_installer")
        self.assertEqual(payload["command"], "main.py init")
        self.assertEqual(payload["canonical_command"], "./install.sh --initialize")

    def test_development_marker_allows_source_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / runtime_guard.DEVELOPMENT_MARKER).write_text("source-development\n", encoding="utf-8")

            runtime_guard.require_macos_execution_mode("main.py init", root=root, system="Darwin")

    def test_installed_runtime_marker_allows_runtime_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / runtime_guard.INSTALLED_RUNTIME_MARKER).write_text("installed-runtime\n", encoding="utf-8")

            runtime_guard.require_macos_execution_mode("service.py run", root=root, system="Darwin")

    def test_non_macos_platform_is_not_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime_guard.require_macos_execution_mode("main.py init", root=Path(tmp), system="Linux")


if __name__ == "__main__":
    unittest.main()
