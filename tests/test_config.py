import json
import os
import tempfile
import unittest
from unittest.mock import patch

import config


class SaveConfigUpdatesTests(unittest.TestCase):
    def test_save_config_updates_keeps_existing_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            initial = {
                "db_dir": "/tmp/db_storage",
                "keys_file": "all_keys.json",
            }
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(initial, f, ensure_ascii=False, indent=2)

            with patch.dict(os.environ, {"WECHAT_DECRYPT_APP_DIR": tmp}, clear=False):
                written = config.save_config_updates({"image_aes_key": "abc123def4567890"})

            self.assertEqual(written, cfg_path)
            with open(cfg_path, encoding="utf-8") as f:
                saved = json.load(f)

            self.assertEqual(
                saved,
                {
                    "db_dir": "/tmp/db_storage",
                    "keys_file": "all_keys.json",
                    "image_aes_key": "abc123def4567890",
                },
            )
            self.assertNotIn("version_guard", saved)
            self.assertNotIn("wechat_app_path", saved)


class VersionGuardPolicyTests(unittest.TestCase):
    def test_load_config_merges_version_guard_policy_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            policy_path = os.path.join(tmp, "version-guard.policy.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({"db_dir": "/tmp/db_storage"}, f, ensure_ascii=False, indent=2)
            with open(policy_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "version_guard": {
                            "enabled": True,
                            "allowed_version_ranges": [
                                {
                                    "platform": "darwin",
                                    "min_version": "4.1.8",
                                    "max_version": "4.1.8",
                                }
                            ],
                        }
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )

            with patch.dict(os.environ, {"WECHAT_DECRYPT_APP_DIR": tmp}, clear=False):
                loaded = config.load_config()

            self.assertTrue(loaded["version_guard"]["enabled"])
            self.assertEqual(
                loaded["version_guard"]["allowed_version_ranges"][0]["min_version"],
                "4.1.8",
            )
            self.assertEqual(loaded["version_guard_policy_path"], policy_path)


if __name__ == "__main__":
    unittest.main()
