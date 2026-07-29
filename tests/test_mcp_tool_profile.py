import unittest

import mcp_server


class McpToolProfileTests(unittest.TestCase):
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
