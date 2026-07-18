import importlib.util
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


SERVER_PATH = Path(__file__).resolve().parents[1] / "so_i_can_see_you_mcp.py"
NEW_TOOLS = {
    "entrust_eyes",
    "describe_the_eyes",
    "look_now",
    "share_the_view",
    "turn_the_gaze",
}
OLD_TOOLS = {
    "set_camera_credentials",
    "camera_info",
    "snapshot",
    "rtsp_url",
    "ptz_move",
    "find_person",
    "patrol_detect",
    "seek_a_person",
    "search_the_view",
}


def load_server(environment=None):
    spec = importlib.util.spec_from_file_location("so_i_can_see_you_mcp_test", SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, environment or {}, clear=True):
        assert spec.loader is not None
        spec.loader.exec_module(module)
    return module


class PublicIdentityTests(unittest.TestCase):
    def test_new_environment_variables_are_used(self):
        server = load_server(
            {
                "SEEYOU_HOST": "new-address",
                "SEEYOU_USERNAME": "new-user",
                "SEEYOU_PASSWORD": "new-password",
                "CAMERA_HOST": "legacy-address",
                "CAMERA_PASSWORD": "legacy-password",
            }
        )
        self.assertEqual(server.HOST, "new-address")
        self.assertEqual(server.USERNAME, "new-user")
        self.assertEqual(server.PASSWORD, "new-password")

    def test_legacy_environment_variables_are_not_fallbacks(self):
        server = load_server(
            {"CAMERA_HOST": "legacy-address", "CAMERA_PASSWORD": "legacy-password"}
        )
        self.assertEqual(server.HOST, "")
        self.assertEqual(server.PASSWORD, "")

    def test_only_romantic_tool_names_are_exposed(self):
        server = load_server()
        names = {tool["name"] for tool in server.TOOLS}
        self.assertEqual(names, NEW_TOOLS)
        self.assertTrue(names.isdisjoint(OLD_TOOLS))

    def test_initialize_exposes_new_server_identity(self):
        server = load_server()
        output = io.StringIO()
        with redirect_stdout(output):
            server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"},
                }
            )
        response = json.loads(output.getvalue())
        self.assertEqual(response["result"]["serverInfo"], {"name": "so-i-can-see-you", "version": "2.1.0"})

    def test_entrust_eyes_never_echoes_password(self):
        server = load_server()
        result = server.call_tool(
            "entrust_eyes",
            {"host": "192.0.2.10", "username": "admin", "password": "private-secret"},
        )
        rendered = json.dumps(result)
        self.assertNotIn("private-secret", rendered)
        self.assertEqual(server.PASSWORD, "private-secret")

    def test_tool_call_emits_progress_before_final_response(self):
        server = load_server()
        server.capture_rtsp_frame = lambda quality: b"jpeg"
        output = io.StringIO()
        with redirect_stdout(output):
            server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {
                        "name": "look_now",
                        "arguments": {"quality": "sub"},
                        "_meta": {"progressToken": "look-7"},
                    },
                }
            )
        messages = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(messages[0]["method"], "notifications/progress")
        self.assertEqual(
            messages[0]["params"],
            {
                "progressToken": "look-7",
                "progress": 1,
                "message": "Capturing a camera frame",
            },
        )
        self.assertEqual(messages[-1]["id"], 7)
        self.assertIn("result", messages[-1])

    def test_progress_values_increase_for_one_request(self):
        server = load_server()
        output = io.StringIO()
        with redirect_stdout(output):
            notify = server.progress_notifier(42)
            notify("first")
            notify("second")
        messages = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([item["params"]["progress"] for item in messages], [1, 2])
        self.assertTrue(all(item["params"]["progressToken"] == 42 for item in messages))

    def test_no_progress_token_keeps_single_response_behavior(self):
        server = load_server()
        server.capture_rtsp_frame = lambda quality: b"jpeg"
        output = io.StringIO()
        with redirect_stdout(output):
            server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 8,
                    "method": "tools/call",
                    "params": {"name": "look_now", "arguments": {}},
                }
            )
        messages = output.getvalue().splitlines()
        self.assertEqual(len(messages), 1)
        self.assertEqual(json.loads(messages[0])["id"], 8)

    def test_legacy_tool_names_are_rejected(self):
        server = load_server()
        for tool_name in OLD_TOOLS:
            with self.subTest(tool_name=tool_name):
                with self.assertRaisesRegex(ValueError, "unknown tool"):
                    server.call_tool(tool_name, {})


if __name__ == "__main__":
    unittest.main()
