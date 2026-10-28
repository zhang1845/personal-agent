import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from personal_agent.bridge import MCPBridge
from personal_agent.errors import AgentError


FAKE_SERVER = r'''
import json, os, sys, time
mode, log_path = sys.argv[1:3]
event = {"id":"event-1","title":"评审","start":"2026-10-26T09:00:00+08:00",
         "end":"2026-10-26T10:00:00+08:00","calendar_id":"work","version":"v1",
         "all_day":False,"recurring":False,"location":"","description":""}
for line in sys.stdin:
    req = json.loads(line)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(req, ensure_ascii=False)+"\n")
    if "id" not in req: continue
    if req["method"] == "initialize":
        if mode == "bad_init": result = {"protocolVersion":"1900-01-01","capabilities":{"tools":{}}}
        else: result = {"protocolVersion":"2025-06-18","capabilities":{"tools":{}}}
    else:
        if mode == "timeout": time.sleep(3)
        if mode == "disconnect": sys.exit(0)
        if mode == "stdout_log":
            print("secret-token-must-not-leak", flush=True)
            continue
        if mode == "oversized":
            print("x" * 5000, flush=True)
            continue
        args = req["params"]["arguments"]
        tool = req["params"]["name"]
        if mode == "mapping":
            payload={"data":{"events":[dict(event, id="mapped-1")]},"finished":True}
            payload["data"]["events"][0]["etag"] = payload["data"]["events"][0].pop("version")
        elif tool == "list_calendars":
            payload={"calendars":[{"id":"work","name":"Work","writable":True}],"complete":True}
        elif tool == "list_events":
            payload={"events":[event],"complete":mode != "incomplete"}
            if mode == "paged": payload["nextCursor"]="more"
            if mode == "naive": event["start"]="2026-10-26T09:00:00"
            if mode == "no_version": event.pop("version")
            if mode == "duplicates": payload["events"].append(dict(event))
        elif tool == "delete_event":
            payload={"deleted":True,"event_id":args["event_id"]}
        else:
            payload={"event":event}
            if mode == "wrong_id": event["id"]="different"
            if mode == "invalid_write": event.pop("version")
        if mode == "tool_error":
            result={"isError":True,"content":[{"type":"text","text":"secret-token-must-not-leak"}]}
        elif mode == "text":
            result={"content":[{"type":"text","text":json.dumps(payload)}]}
        else:
            result={"structuredContent":payload,"content":[]}
        if mode == "notification":
            print(json.dumps({"jsonrpc":"2.0","method":"notifications/progress","params":{}}),flush=True)
    print(json.dumps({"jsonrpc":"2.0","id":req["id"],"result":result}),flush=True)
'''


class BridgeTests(unittest.TestCase):
    def setUp(self):
        project = Path(__file__).resolve().parents[2]
        temp_root = project / "output" / "runtime"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=str(temp_root))
        self.root = Path(self.temp.name)
        self.script = self.root / "fake.py"
        self.script.write_text(FAKE_SERVER, encoding="utf-8")
        self.log = self.root / "calls.jsonl"
        self.bridges = []

    def tearDown(self):
        for bridge in self.bridges:
            bridge.close()
        self.temp.cleanup()

    def make(self, mode="normal", **extra):
        config = {
            "profile_id": "test-account-main",
            "command": sys.executable,
            "args": [str(self.script), mode, str(self.log)],
            "timeout_seconds": 1,
            "tools": {name:{"name":name} for name in (
                "list_calendars", "list_events", "get_event", "create_event",
                "update_event", "delete_event",
            )},
        }
        config.update(extra)
        bridge=MCPBridge(config)
        self.bridges.append(bridge)
        return bridge

    def events(self, bridge):
        return bridge.list_events("2026-10-26T00:00:00+08:00", "2026-10-27T00:00:00+08:00", "work")

    def assert_error(self, code, call):
        with self.assertRaises(AgentError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn("secret-token", str(caught.exception.as_dict()))

    def test_lazy_initialization_and_handshake(self):
        bridge=self.make()
        self.assertEqual(bridge.profile_id, "test-account-main")
        self.assertFalse(self.log.exists())
        self.assertEqual(bridge.list_calendars()[0]["id"], "work")
        calls=[json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([c["method"] for c in calls], ["initialize", "notifications/initialized", "tools/call"])

    def test_profile_identity_must_be_explicit_nonempty_and_stable(self):
        for profile in (None, "", " ", " account-main", "account-main ", 123, "x" * 129):
            with self.subTest(profile=profile):
                self.assert_error("invalid_config", lambda: self.make(profile_id=profile))
        self.assert_error("invalid_config", lambda: MCPBridge({"command":sys.executable}))
        self.assertFalse(self.log.exists())

    def test_complete_event_list_and_json_text_fallback(self):
        for mode in ("normal", "text", "notification"):
            with self.subTest(mode=mode):
                self.assertEqual(self.events(self.make(mode))[0]["version"], "v1")

    def test_incomplete_paged_duplicate_or_malformed_events_fail_closed(self):
        for mode in ("incomplete", "paged", "duplicates", "naive", "no_version"):
            with self.subTest(mode=mode):
                self.assert_error("backend_unavailable", lambda: self.events(self.make(mode)))

    def test_bad_initialization_fails_before_calendar_tool(self):
        self.assert_error("backend_unavailable", lambda: self.events(self.make("bad_init")))
        calls=[json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([c["method"] for c in calls], ["initialize"])

    def test_invalid_input_range_does_not_start_child(self):
        bridge=self.make()
        self.assert_error("invalid_range", lambda: bridge.list_events("2026-10-26", "2026-10-27", "work"))
        self.assert_error("invalid_range", lambda: bridge.list_events("2026-10-26T00:00:00Z", "2027-10-27T00:00:00Z", "work"))
        self.assertFalse(self.log.exists())

    def test_explicit_argument_and_response_field_mapping(self):
        bridge=self.make("mapping")
        bridge.config["tools"]["list_events"].update({
            "name":"mapped_list", "argument_map":{"calendar_id":"calendarId"},
            "response_path":"data.events", "complete_path":"finished",
            "event_fields":{"version":"etag"},
        })
        events=self.events(bridge)
        self.assertEqual(events[0]["id"], "mapped-1")
        self.assertEqual(events[0]["version"], "v1")
        call=json.loads(self.log.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(call["params"]["arguments"]["calendarId"], "work")
        self.assertNotIn("calendar_id", call["params"]["arguments"])

    def test_writes_disabled_without_spawn(self):
        bridge=self.make()
        self.assert_error("writes_disabled", lambda: bridge.create_event({}, "request-1"))
        self.assertFalse(self.log.exists())

    def test_write_success_and_delete_result(self):
        bridge=self.make(writes_enabled=True)
        self.assertEqual(bridge.create_event({"title":"评审"}, "request-1")["id"], "event-1")
        self.assertEqual(bridge.update_event("event-1", {"title":"评审"}, "v1", "request-2")["version"], "v1")
        self.assertTrue(bridge.delete_event("event-1", "v1", "request-3")["deleted"])

    def test_ambiguous_writes_are_unknown_without_automatic_retry(self):
        for mode in ("timeout", "disconnect", "invalid_write", "tool_error", "stdout_log"):
            with self.subTest(mode=mode):
                self.log.unlink(missing_ok=True)
                bridge=self.make(mode,writes_enabled=True,timeout_seconds=.15)
                self.assert_error("unknown_outcome", lambda: bridge.create_event({}, "request-1"))
                calls=[json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(sum(c["method"]=="tools/call" for c in calls),1)
                bridge.close()

    def test_read_failures_and_output_limit_are_sanitized(self):
        for mode in ("timeout", "disconnect", "tool_error", "stdout_log", "oversized"):
            with self.subTest(mode=mode):
                self.assert_error("backend_unavailable", lambda: self.events(self.make(mode,timeout_seconds=.15,max_response_bytes=1024)))

    def test_mismatched_identity_is_never_accepted(self):
        bridge=self.make("wrong_id", writes_enabled=True)
        self.assert_error("backend_unavailable", lambda: bridge.get_event("event-1"))
        self.assert_error("unknown_outcome", lambda: bridge.update_event("event-1", {}, "v1", "request-1"))

    def test_unsupported_reminder_and_recurrence_do_not_spawn(self):
        bridge=self.make(writes_enabled=True)
        self.assert_error("unsupported_reminder", lambda: bridge.create_event({"reminder_minutes":15}, "request-1"))
        self.assert_error("unsupported_recurrence", lambda: bridge.create_event({"recurring":True}, "request-1"))
        self.assertFalse(self.log.exists())

    def test_close_reaps_child(self):
        bridge=self.make()
        bridge.list_calendars()
        process=bridge._process
        bridge.close()
        self.assertIsNotNone(process.poll())
        self.assertIsNone(bridge._process)


if __name__ == "__main__":
    unittest.main()
