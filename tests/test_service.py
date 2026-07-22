import os
import tempfile
import unittest
from pathlib import Path

import service


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
                    str(paths["main"]),
                    "serve",
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


if __name__ == "__main__":
    unittest.main()
