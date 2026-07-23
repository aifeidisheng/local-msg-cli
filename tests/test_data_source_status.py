import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mcp_server


class _FakeCache:
    def __init__(self, mapping=None, error=None):
        self.mapping = mapping or {}
        self.error = error

    def get(self, rel_key):
        if self.error is not None:
            raise self.error
        return self.mapping.get(rel_key)


def _create_sqlite(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE health_probe (value INTEGER)")


class DataSourceStatusTests(unittest.TestCase):
    def test_ready_status_contains_only_non_sensitive_operational_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contact_db = root / "contact.db"
            message_db = root / "message_0.db"
            _create_sqlite(contact_db)
            _create_sqlite(message_db)
            rel_key = "message/message_0.db"

            with patch("wechat_version_guard.check_or_raise"), patch.object(
                mcp_server, "_get_contact_db_path", return_value=str(contact_db)
            ), patch.object(
                mcp_server, "MSG_DB_KEYS", [rel_key]
            ), patch.object(
                mcp_server, "ALL_KEYS", {rel_key: {"enc_key": "sensitive-key"}}
            ), patch.object(
                mcp_server, "_cache", _FakeCache({rel_key: str(message_db)})
            ):
                payload = json.loads(mcp_server.data_source_status())

        self.assertEqual(
            payload,
            {
                "ok": True,
                "status": "ready",
                "initialized": True,
                "database_accessible": True,
                "contact_database_ready": True,
                "message_database_ready": True,
                "configured_message_shards": 1,
            },
        )
        serialized = json.dumps(payload)
        self.assertNotIn(str(root), serialized)
        self.assertNotIn("sensitive-key", serialized)

    def test_not_ready_status_hides_internal_cache_errors(self):
        sensitive_error = RuntimeError("failed at /private/user/account/message_0.db")
        with patch("wechat_version_guard.check_or_raise"), patch.object(
            mcp_server, "_get_contact_db_path", return_value=None
        ), patch.object(
            mcp_server, "MSG_DB_KEYS", ["message/message_0.db"]
        ), patch.object(
            mcp_server, "ALL_KEYS", {}
        ), patch.object(
            mcp_server, "_cache", _FakeCache(error=sensitive_error)
        ):
            result = mcp_server.data_source_status()
            payload = json.loads(result)

        self.assertEqual(payload["status"], "not_ready")
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["initialized"])
        self.assertFalse(payload["database_accessible"])
        self.assertNotIn("/private/user", result)
        self.assertNotIn("message_0.db", result)

    def test_sqlite_probe_does_not_create_a_missing_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.db"

            self.assertFalse(mcp_server._sqlite_schema_readable(missing))
            self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
