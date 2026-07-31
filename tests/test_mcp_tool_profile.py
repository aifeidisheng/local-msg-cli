import unittest
from unittest.mock import patch

import mcp_server


class McpToolProfileTests(unittest.TestCase):
    def test_register_tool_supports_direct_call_api(self):
        registered = []

        def direct(name_or_fn=None, **kwargs):
            registered.append((name_or_fn, kwargs))

        def tool():
            return None

        with patch.object(mcp_server, "_mcp_tool", direct):
            mcp_server._register_mcp_tool(tool, {"name": "sample"})

        self.assertEqual(registered, [(tool, {"name": "sample"})])

    def test_register_tool_supports_decorator_factory_api(self):
        registered = []

        def decorator_factory(*, name=None):
            def decorator(fn):
                registered.append((fn, name))
                return fn
            return decorator

        def tool():
            return None

        with patch.object(mcp_server, "_mcp_tool", decorator_factory):
            mcp_server._register_mcp_tool(tool, {"name": "sample"})

        self.assertEqual(registered, [(tool, "sample")])

    def test_core_profile_only_enables_analysis_surface(self):
        expected = {
            "data_source_status",
            "get_recent_sessions",
            "query_messages",
            "search_messages",
            "list_contacts",
            "get_contact_info",
            "get_new_messages",
        }

        self.assertEqual(mcp_server._CORE_MCP_TOOLS, expected)
        for tool_name in expected:
            self.assertTrue(mcp_server._mcp_tool_enabled(tool_name, "core"))
        self.assertFalse(mcp_server._mcp_tool_enabled("get_chat_history", "core"))
        self.assertFalse(mcp_server._mcp_tool_enabled("decode_image", "core"))

    def test_extended_profile_enables_compatibility_and_decode_tools(self):
        self.assertTrue(mcp_server._mcp_tool_enabled("get_chat_history", "extended"))
        self.assertTrue(mcp_server._mcp_tool_enabled("decode_image", "extended"))

    def test_unknown_profile_fails_closed_to_core(self):
        self.assertEqual(mcp_server._normalize_mcp_tool_profile("unexpected"), "core")
        self.assertFalse(mcp_server._mcp_tool_enabled("decode_image", "unexpected"))


if __name__ == "__main__":
    unittest.main()
