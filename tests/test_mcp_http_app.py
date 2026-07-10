import unittest
from unittest.mock import patch

import mcp_server


class McpHttpAppTests(unittest.TestCase):
    def test_build_mcp_http_app_prefers_http_app(self):
        class FakeMcp:
            def http_app(self):
                return "http-app"

            def streamable_http_app(self):
                raise AssertionError("streamable_http_app should not be used when http_app exists")

        with patch.object(mcp_server, "mcp", FakeMcp()):
            self.assertEqual(mcp_server._build_mcp_http_app(), "http-app")

    def test_build_mcp_http_app_falls_back_to_streamable_http_app(self):
        class FakeMcp:
            def streamable_http_app(self):
                return "streamable-http-app"

        with patch.object(mcp_server, "mcp", FakeMcp()):
            self.assertEqual(mcp_server._build_mcp_http_app(), "streamable-http-app")

    def test_build_mcp_http_app_reports_unsupported_fastmcp_api(self):
        with patch.object(mcp_server, "mcp", object()):
            with self.assertRaisesRegex(RuntimeError, "http_app\\(\\) or streamable_http_app\\(\\)"):
                mcp_server._build_mcp_http_app()


if __name__ == "__main__":
    unittest.main()
