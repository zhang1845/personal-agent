import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from personal_agent.cli import build_config
from personal_agent.errors import AgentError
from personal_agent.mcp_server import MAX_MESSAGE_BYTES, MCPServer, TOOLS, serve

CODE = Path(__file__).resolve().parents[1]


class FakeService:
    def __init__(self):
        self.calls = []

    def call(self, name, args):
        self.calls.append((name, args))
        if args.get("failure"):
            raise AgentError("conflict", "时间冲突", {"changed": False})
        return {"name": name, "arguments": args, "mode": "test"}


def initialize(server, version="2025-06-18"):
    return server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "unittest", "version": "1"}}})


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeService()
        self.server = MCPServer(self.service)

    def test_handshake_versions_and_fallback(self):
        for requested, expected in [("2024-11-05", "2024-11-05"), ("2025-06-18", "2025-06-18"), ("future-version", "2025-06-18")]:
            result = initialize(MCPServer(FakeService()), requested)
            self.assertEqual(result["result"]["protocolVersion"], expected)

    def test_initialization_and_tools(self):
        before = self.server.dispatch({"jsonrpc": "2.0", "id": 0, "method": "tools/list"})
        self.assertEqual(before["error"]["code"], -32002)
        result = initialize(self.server)
        self.assertEqual(result["result"]["capabilities"], {"tools": {"listChanged": False}})
        tools = self.server.dispatch({"jsonrpc": "2.0", "id": "tools", "method": "tools/list"})["result"]["tools"]
        self.assertEqual(len(tools), 10)
        version = next(x for x in tools if x["name"] == "calendar_delete_event")["inputSchema"]["properties"]["expected_version"]
        self.assertEqual(version["type"], "string")
        slots = next(x for x in tools if x["name"] == "calendar_find_free_slots")["inputSchema"]["properties"]
        self.assertEqual(slots["duration_minutes"]["minimum"], 5)
        self.assertEqual(slots["duration_minutes"]["maximum"], 1440)
        prefs = next(x for x in tools if x["name"] == "calendar_set_preferences")["inputSchema"]["properties"]
        self.assertEqual(prefs["lunch_start"]["type"], "string")
        self.assertEqual(prefs["lunch_end"]["type"], "string")
        self.assertEqual(prefs["buffer_minutes"]["maximum"], 120)
        self.assertEqual(prefs["workdays"]["minItems"], 1)

    def test_notification_never_executes_write(self):
        initialize(self.server)
        for method in ["notifications/initialized", "tools/call", "unknown"]:
            self.assertIsNone(self.server.dispatch({"jsonrpc": "2.0", "method": method, "params": {"name": "calendar_delete_event", "arguments": {"event_id": "x"}}}))
        self.assertEqual(self.service.calls, [])

    def test_tool_result_and_domain_error(self):
        initialize(self.server)
        for args, failed in [({}, False), ({"failure": True}, True)]:
            reply = self.server.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "calendar_status", "arguments": args}})
            result = reply["result"]
            self.assertEqual(result["isError"], failed)
            self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])
        self.assertEqual(self.service.calls[-1][0], "calendar_status")

    def test_invalid_requests_do_not_crash(self):
        initialize(self.server)
        cases = [
            ([], -32600),
            ({"jsonrpc": "1.0", "id": 2, "method": "ping"}, -32600),
            ({"jsonrpc": "2.0", "id": True, "method": "ping"}, -32600),
            ({"jsonrpc": "2.0", "id": None, "method": "ping"}, -32600),
            ({"jsonrpc": "2.0", "id": 2, "method": "unknown"}, -32601),
            ({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": []}, -32602),
            ({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": []}}, -32602),
            ({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "missing"}}, -32602),
            ({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "calendar_status", "arguments": []}}, -32602),
        ]
        for request, code in cases:
            with self.subTest(request=request):
                self.assertEqual(self.server.dispatch(request)["error"]["code"], code)
        self.assertEqual(self.service.calls, [])

    def test_stream_recovers_after_bad_utf8_and_oversized_message(self):
        valid = json.dumps({"jsonrpc": "2.0", "id": 12, "method": "ping"}).encode() + b"\n"
        payload = b"not-json\n\xff\n" + b"x" * (MAX_MESSAGE_BYTES + 10) + b"\n" + valid
        output = io.StringIO()
        serve(self.service, io.BytesIO(payload), output)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([r.get("error", {}).get("code") for r in replies[:3]], [-32700, -32700, -32600])
        self.assertEqual(replies[3], {"jsonrpc": "2.0", "id": 12, "result": {}})

    def test_unexpected_error_does_not_leak_exception(self):
        def broken(*args):
            raise RuntimeError("secret-token-value")
        self.service.call = broken
        initialize(self.server)
        reply = self.server.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "calendar_status"}})
        self.assertTrue(reply["result"]["isError"])
        self.assertNotIn("secret-token-value", json.dumps(reply))


class CLITests(unittest.TestCase):
    def run_cli(self, args, stdin=None):
        env = dict(os.environ, PYTHONPATH=str(CODE))
        return subprocess.run([sys.executable, "-m", "personal_agent"] + args, input=stdin, text=True, encoding="utf-8", capture_output=True, env=env, timeout=10)

    def test_config_is_standalone_and_local_only(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "unused.sqlite3"
            result = self.run_cli(["--db", str(db), "config"])
            self.assertEqual(result.returncode, 0, result.stderr)
            config = json.loads(result.stdout)
            self.assertEqual(config["agents"]["defaults"]["fallbackModels"], [])
            self.assertEqual(config["providers"]["ollama"]["apiBase"], "http://127.0.0.1:11434/v1")
            self.assertEqual(len(config["tools"]["mcpServers"]["personal-calendar"]["enabledTools"]), len(TOOLS))
            self.assertFalse(db.exists())

    def test_actual_cli_status_and_invalid_json(self):
        with tempfile.TemporaryDirectory() as temp:
            args = ["--db", str(Path(temp) / "calendar.sqlite3"), "tool", "calendar_status"]
            result = self.run_cli(args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsInstance(json.loads(result.stdout), dict)
            bad = self.run_cli(args + ["--args", "[1]"])
            self.assertNotEqual(bad.returncode, 0)
            self.assertEqual(json.loads(bad.stdout)["error"]["code"], "invalid_arguments")

    def test_bridge_config_export_and_invalid_file(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "bridge.json"
            db = Path(temp) / "calendar.sqlite3"
            config.write_text('{"command":"unused-command","tools":{}}', encoding="utf-8")
            result = self.run_cli(["--db", str(db), "--bridge-config", str(config), "config"])
            self.assertEqual(result.returncode, 0, result.stderr)
            arguments = json.loads(result.stdout)["tools"]["mcpServers"]["personal-calendar"]["args"]
            self.assertIn("--bridge-config", arguments)
            self.assertIn(str(config.resolve()), arguments)
            self.assertFalse(db.exists())
            config.write_text("{broken", encoding="utf-8")
            bad = self.run_cli(["--db", str(db), "--bridge-config", str(config), "tool", "calendar_status"])
            self.assertNotEqual(bad.returncode, 0)
            self.assertEqual(json.loads(bad.stdout)["error"]["code"], "invalid_config")

    def test_actual_stdio_subprocess(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "calendar_status", "arguments": {}}},
        ]
        with tempfile.TemporaryDirectory() as temp:
            result = self.run_cli(["--db", str(Path(temp) / "calendar.sqlite3"), "mcp"], "".join(json.dumps(x) + "\n" for x in messages))
            self.assertEqual(result.returncode, 0, result.stderr)
            replies = [json.loads(x) for x in result.stdout.splitlines()]
            self.assertEqual([x["id"] for x in replies], [1, 2, 3])
            self.assertFalse(replies[2]["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
