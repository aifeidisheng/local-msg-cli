import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import macos_raw_key_capture as capture


class RawKeyCaptureTests(unittest.TestCase):
    @staticmethod
    def _macho_with_calls(call_count):
        signature = capture._KEY_CALL_SIGNATURES[0]
        blob = bytearray(1024)
        struct.pack_into("<I", blob, 0, capture._MH_MAGIC_64)
        struct.pack_into("<I", blob, 4, capture._CPU_TYPE_ARM64)
        struct.pack_into("<I", blob, 16, 1)
        struct.pack_into("<II", blob, 32, capture._LC_SEGMENT_64, 72)
        struct.pack_into("<Q", blob, 32 + 24, 0)
        struct.pack_into("<Q", blob, 32 + 32, len(blob))
        struct.pack_into("<Q", blob, 32 + 40, 0)
        struct.pack_into("<Q", blob, 32 + 48, len(blob))
        for index in range(call_count):
            signature_offset = 160 + index * 256
            blob[signature_offset : signature_offset + len(signature)] = signature
            call_offset = signature_offset + len(signature)
            struct.pack_into("<I", blob, call_offset, 0x94000001 + index)
        return blob

    def test_only_current_official_4112_build_is_selected(self):
        self.assertTrue(capture.supports_build("4.1.12", "269341"))
        self.assertTrue(capture.supports_build("4.1.12.29", "269341"))
        self.assertFalse(capture.supports_build("4.1.12", "269342"))
        self.assertFalse(capture.supports_build("4.1.13", "269341"))

    def test_decode_known_arm64_branch(self):
        self.assertEqual(capture._decode_arm64_bl(0x94000001, 0x1000), 0x1004)

    def test_locator_requires_one_signature_target(self):
        blob = self._macho_with_calls(1)
        call_offset = 160 + len(capture._KEY_CALL_SIGNATURES[0])

        with tempfile.TemporaryDirectory() as directory:
            dylib = Path(directory) / "wechat.dylib"
            dylib.write_bytes(blob)
            self.assertEqual(capture.locate_key_hook(dylib), call_offset + 4)

    def test_locator_fails_closed_for_zero_or_multiple_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            dylib = Path(directory) / "wechat.dylib"
            dylib.write_bytes(self._macho_with_calls(0))
            with self.assertRaises(capture.RawKeyCaptureError):
                capture.locate_key_hook(dylib)

            dylib.write_bytes(self._macho_with_calls(2))
            with self.assertRaises(capture.RawKeyCaptureError):
                capture.locate_key_hook(dylib)

    def test_unverified_candidate_is_never_persisted(self):
        entry = capture.DatabaseEntry(
            "contact/contact.db", "/tmp/contact.db", 4096, "00" * 16, b"x" * 4096, 0
        )
        with tempfile.TemporaryDirectory() as directory, patch.object(
            capture, "verify_enc_key", return_value=False
        ):
            output = Path(directory) / "all_keys.json"
            self.assertEqual(capture._save_verified_candidate([entry], b"a" * 32, output), set())
            self.assertFalse(output.exists())

    def test_verified_candidate_is_saved_with_private_permissions(self):
        entry = capture.DatabaseEntry(
            "contact/contact.db", "/tmp/contact.db", 4096, "11" * 16, b"x" * 4096, 0
        )
        with tempfile.TemporaryDirectory() as directory, patch.object(
            capture, "verify_enc_key", return_value=True
        ):
            output = Path(directory) / "private" / "all_keys.json"
            matched = capture._save_verified_candidate([entry], b"a" * 32, output)
            payload = json.loads(output.read_text(encoding="utf-8"))

            self.assertEqual(matched, {"contact/contact.db"})
            self.assertEqual(payload["contact/contact.db"]["enc_key"], (b"a" * 32).hex())
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(output.parent.stat().st_mode & 0o777, 0o700)

    def test_capture_completes_only_after_contact_and_message_keys_match(self):
        entries = [
            capture.DatabaseEntry("contact/contact.db", "", 0, "", b"", 0),
            capture.DatabaseEntry("message/message_0.db", "", 0, "", b"", 0),
            capture.DatabaseEntry("session/session.db", "", 0, "", b"", 0),
        ]
        self.assertIsNone(capture._account_complete(entries, {"contact/contact.db"}))
        self.assertEqual(
            capture._account_complete(
                entries, {"contact/contact.db", "message/message_0.db"}
            ),
            0,
        )

    def test_capture_completion_keeps_multi_account_namespace(self):
        entries = [
            capture.DatabaseEntry(
                "__account_001__/contact/contact.db", "", 0, "", b"", 1
            ),
            capture.DatabaseEntry(
                "__account_001__/message/message_0.db", "", 0, "", b"", 1
            ),
        ]
        self.assertEqual(
            capture._account_complete(
                entries,
                {
                    "__account_001__/contact/contact.db",
                    "__account_001__/message/message_0.db",
                },
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
