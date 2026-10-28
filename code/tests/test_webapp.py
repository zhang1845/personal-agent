"""Real HTTP tests for the loopback workbench, using isolated local data only."""

import http.client
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import quote, urlencode
from unittest.mock import patch

from personal_agent.errors import AgentError


class OfflineModel:
    def list_models(self):
        return {"available": False, "models": [], "error": "本地模型未运行"}

    def chat(self, *args, **kwargs):
        raise AgentError("model_unavailable", "测试不访问任何真实模型")


class WebappTests(unittest.TestCase):
    def setUp(self):
        from personal_agent.webapp import make_server
        self.temp = tempfile.TemporaryDirectory(prefix="agent-web-test-")
        self.server = make_server(Path(self.temp.name), port=0, model_client=OfflineModel())
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.origin = "http://127.0.0.1:%s" % self.port
        status, _, payload = self.request("GET", "/api/bootstrap")
        self.assertEqual(200, status, payload)
        self.token = payload["token"]

    def tearDown(self):
        if hasattr(self, "server"):
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=3)
            close = getattr(self.server.application, "close", None)
            if close:
                close()
        self.temp.cleanup()

    def request(self, method, path, payload=None, headers=None, raw=None):
        supplied = headers or {}
        values = {}
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            values.update({"Origin": self.origin,
                           "X-Agent-Token": getattr(self, "token", "")})
        values.update(supplied)
        values = {key: value for key, value in values.items() if value is not None}
        if raw is not None:
            body = raw
            values.setdefault("Content-Type", "application/json")
        elif payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            values.setdefault("Content-Type", "application/json")
        else:
            body = None
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            client.request(method, path, body=body, headers=values)
            response = client.getresponse()
            status, response_headers = response.status, dict(response.getheaders())
            content = response.read()
            content_type = response.getheader("Content-Type", "")
            if "application/json" in content_type:
                content = json.loads(content.decode("utf-8"))
            return status, response_headers, content
        finally:
            client.close()

    @staticmethod
    def create_arguments(request_id="web-create-once"):
        return {"request_id": request_id, "title": "测试评审",
                "start": "2026-10-27T15:00:00+08:00",
                "end": "2026-10-27T16:00:00+08:00"}

    def create_event(self, request_id="web-create-once"):
        status, _, result = self.request("POST", "/api/events", self.create_arguments(request_id))
        self.assertEqual(200, status, result)
        return result["event"]

    def events(self):
        query = urlencode({"start": "2026-10-27", "end": "2026-10-28"})
        status, _, result = self.request("GET", "/api/events?" + query)
        self.assertEqual(200, status, result)
        self.assertTrue(result["complete"])
        return result["events"]

    def assert_error(self, result):
        self.assertEqual({"code", "message", "details"}, set(result["error"]))
        self.assertIsInstance(result["error"]["code"], str)
        self.assertIsInstance(result["error"]["message"], str)
        self.assertIsInstance(result["error"]["details"], dict)

    def test_bootstrap_and_read_routes_have_no_sample_data(self):
        status, _, result = self.request("GET", "/api/bootstrap")
        self.assertEqual(200, status)
        self.assertEqual("local_development", result["status"]["backend"])
        self.assertFalse(result["status"]["capabilities"]["supports_reminders"])
        self.assertEqual("Asia/Shanghai", result["preferences"]["timezone"])
        for route, key in [("/api/tasks", "tasks"), ("/api/memories", "memories"),
                           ("/api/sessions", "sessions")]:
            with self.subTest(route=route):
                code, _, data = self.request("GET", route)
                self.assertEqual(200, code, data)
                self.assertEqual([], data[key])
        self.assertEqual([], self.events())

    def test_mutations_require_token_and_same_origin(self):
        variants = [{"X-Agent-Token": None}, {"X-Agent-Token": "wrong"},
                    {"Origin": None}, {"Origin": "https://attacker.example"},
                    {"Origin": "http://127.0.0.1:1"}]
        for headers in variants:
            with self.subTest(headers=headers):
                code, _, data = self.request("POST", "/api/events", self.create_arguments(), headers=headers)
                self.assertEqual(403, code, data)
                self.assert_error(data)
        self.assertEqual([], self.events())

    def test_csrf_is_required_for_every_mutating_method(self):
        operations = [("POST", "/api/tasks", {"title": "不应写入"}),
                      ("PUT", "/api/settings", {"model": ""}),
                      ("PATCH", "/api/tasks/missing", {"completed": True}),
                      ("DELETE", "/api/memories/missing", {})]
        for method, path, data in operations:
            with self.subTest(method=method):
                code, _, result = self.request(method, path, data, headers={"X-Agent-Token": None})
                self.assertEqual(403, code, result)
                self.assert_error(result)
        self.assertEqual([], self.request("GET", "/api/tasks")[2]["tasks"])

    def test_rebinding_host_is_rejected_even_on_get(self):
        code, _, data = self.request("GET", "/api/bootstrap", headers={"Host": "attacker.example"})
        self.assertEqual(403, code, data)
        self.assert_error(data)
        code, _, data = self.request("POST", "/api/events", self.create_arguments(),
                                     headers={"Host": "attacker.example"})
        self.assertEqual(403, code, data)
        self.assertEqual([], self.events())

    def test_body_limit_and_malformed_json_do_not_mutate(self):
        code, _, data = self.request("POST", "/api/chat", {"message": "x" * 70000})
        self.assertEqual(413, code, data)
        self.assert_error(data)
        for raw in [b"not json", b"\xff", b"[]", b"null"]:
            with self.subTest(raw=raw):
                code, _, data = self.request("POST", "/api/events", raw=raw)
                self.assertEqual(400, code, data)
                self.assertIn("error", data)
        self.assertEqual([], self.events())

    def test_static_assets_are_local_and_have_security_headers(self):
        code, headers, body = self.request("GET", "/")
        self.assertEqual(200, code)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn(b"<html", body.lower())
        self.assertIn("Content-Security-Policy", headers)
        self.assertIn("nosniff", headers.get("X-Content-Type-Options", ""))
        self.assertNotEqual("*", headers.get("Access-Control-Allow-Origin"))
        for path in ["/../README.md", "/%2e%2e/README.md", "/assets/../../README.md"]:
            with self.subTest(path=path):
                status, _, response = self.request("GET", path)
                self.assertIn(status, (400, 403, 404), response)

    def test_create_retry_conflict_and_versioned_edit(self):
        event = self.create_event()
        code, _, repeated = self.request("POST", "/api/events", self.create_arguments())
        self.assertEqual(200, code, repeated)
        self.assertEqual(event["id"], repeated["event"]["id"])
        self.assertEqual(1, len(self.events()))
        code, _, failed = self.request("POST", "/api/events", self.create_arguments("different-request"))
        self.assertEqual(409, code, failed)
        self.assertEqual("conflict", failed["error"]["code"])
        path = "/api/events/" + quote(event["id"], safe="")
        change = {"request_id": "web-change", "expected_version": event["version"],
                  "changes": {"title": "修改后的评审"}}
        code, _, updated = self.request("PATCH", path, change)
        self.assertEqual(200, code, updated)
        self.assertEqual("修改后的评审", updated["event"]["title"])
        code, _, stale = self.request("PATCH", path, dict(change, request_id="stale-update"))
        self.assertEqual(409, code, stale)
        self.assertEqual("version_conflict", stale["error"]["code"])
        delete = {"request_id": "web-delete", "expected_version": updated["event"]["version"]}
        code, _, removed = self.request("DELETE", path, delete)
        self.assertEqual(200, code, removed)
        self.assertTrue(removed["deleted"])
        self.assertEqual([], self.events())

    def test_unknown_fields_are_not_accepted_as_ignored_writes(self):
        args = dict(self.create_arguments(), attendees=["unrequested@example.com"])
        code, _, result = self.request("POST", "/api/events", args)
        self.assertEqual(400, code, result)
        self.assertEqual([], self.events())

    def test_invalid_types_duplicate_query_and_request_id_reuse_fail_safely(self):
        invalid = [("POST", "/api/tasks", {"title": "", "due_date": "2026-10-28"}),
                   ("POST", "/api/tasks", {"title": "日期错误", "due_date": "2026-02-30"}),
                   ("PATCH", "/api/tasks/missing", {"completed": "true"}),
                   ("POST", "/api/memories", {"content": ["不是字符串"]}),
                   ("POST", "/api/chat", {"message": "待办：x", "session_id": 4}),
                   ("GET", "/api/events?start=2026-10-27&start=2026-10-28&end=2026-10-29", None)]
        for method, path, args in invalid:
            with self.subTest(path=path, args=args):
                code, _, result = self.request(method, path, args)
                self.assertEqual(400, code, result)
                self.assert_error(result)
        event = self.create_event()
        code, _, result = self.request("POST", "/api/events", dict(self.create_arguments(), title="相同ID不同内容"))
        self.assertEqual(409, code, result)
        self.assertEqual("request_id_conflict", result["error"]["code"])
        self.assertEqual([event["id"]], [item["id"] for item in self.events()])

    def test_tasks_and_memories_are_real_persisted_records(self):
        code, _, data = self.request("POST", "/api/tasks", {"title": "完成个人 Agent", "due_date": "2026-10-28"})
        self.assertEqual(200, code, data)
        code, _, listed = self.request("GET", "/api/tasks")
        self.assertEqual(200, code)
        self.assertEqual(1, len(listed["tasks"]))
        task = listed["tasks"][0]
        self.assertFalse(task["completed"])
        code, _, changed = self.request("PATCH", "/api/tasks/" + task["id"], {"completed": True})
        self.assertEqual(200, code, changed)
        self.assertTrue(self.request("GET", "/api/tasks")[2]["tasks"][0]["completed"])
        content = "偏好不在午休时段安排会议"
        code, _, saved = self.request("POST", "/api/memories", {"content": content})
        self.assertEqual(200, code, saved)
        memories = self.request("GET", "/api/memories")[2]["memories"]
        self.assertEqual([content], [m["content"] for m in memories])
        code, _, removed = self.request("DELETE", "/api/memories/" + memories[0]["id"], {})
        self.assertEqual(200, code, removed)
        self.assertEqual([], self.request("GET", "/api/memories")[2]["memories"])

    def test_preferences_and_overview_use_personal_timezone(self):
        code, _, saved = self.request("PUT", "/api/preferences", {"work_start": "10:00", "buffer_minutes": 20})
        self.assertEqual(200, code, saved)
        prefs = self.request("GET", "/api/preferences")[2]["preferences"]
        self.assertEqual("10:00", prefs["work_start"])
        self.assertEqual(20, prefs["buffer_minutes"])
        self.create_event()
        code, _, overview = self.request("GET", "/api/overview?date=2026-10-27")
        self.assertEqual(200, code, overview)
        self.assertEqual("2026-10-27", overview["date"])
        self.assertEqual("Asia/Shanghai", overview["timezone"])
        self.assertEqual(1, len(overview["events"]))
        self.assertIn("counts", overview)

    def test_export_is_a_snapshot_and_does_not_take_an_output_path(self):
        self.create_event()
        query = urlencode({"start": "2026-10-27", "end": "2026-10-28"})
        code, headers, content = self.request("GET", "/api/export?" + query)
        self.assertEqual(200, code, content)
        self.assertIn("text/calendar", headers.get("Content-Type", ""))
        self.assertIn(b"BEGIN:VCALENDAR", content)
        self.assertIn(b"BEGIN:VEVENT", content)
        self.assertNotIn(b"VALARM", content)
        code, _, response = self.request("GET", "/api/export?" + query + "&output=/tmp/should-not-write.ics")
        self.assertEqual(400, code, response)

    def test_free_slots_read_route_preserves_duration(self):
        self.create_event()
        query = urlencode({"start": "2026-10-27", "end": "2026-10-28", "duration_minutes": "60"})
        code, _, result = self.request("GET", "/api/free-slots?" + query)
        self.assertEqual(200, code, result)
        self.assertTrue(result["slots"])
        self.assertEqual("Asia/Shanghai", result["timezone"])
        for slot in result["slots"]:
            self.assertEqual(timedelta(minutes=60),
                             datetime.fromisoformat(slot["end"]) - datetime.fromisoformat(slot["start"]))
        self.assertEqual(1, len(self.events()))

    def test_apply_uses_stored_action_and_retries_do_not_duplicate(self):
        store = self.server.application.store
        session = store.new_session("HTTP action test")
        action = store.create_action(session["id"], "calendar_create_event",
                                     self.create_arguments("action-create-once"), title="创建评审")
        path = "/api/actions/" + action["id"] + "/apply"
        code, _, rejected = self.request("POST", path, {"tool": "calendar_delete_event", "arguments": {}})
        self.assertEqual(400, code, rejected)
        self.assertEqual([], self.events())
        code, _, result = self.request("POST", path, {})
        self.assertEqual(200, code, result)
        self.assertEqual("applied", result["action"]["status"])
        self.assertEqual(1, len(self.events()))
        code, _, repeat = self.request("POST", path, {})
        self.assertEqual(200, code, repeat)
        self.assertEqual(result["result"]["event"]["id"], repeat["result"]["event"]["id"])
        self.assertEqual(1, len(self.events()))

    def test_unsupported_stored_action_cannot_be_executed(self):
        store = self.server.application.store
        session = store.new_session("Unsupported action")
        action = store.create_action(session["id"], "run_shell", {"command": "echo should-not-run"})
        code, _, result = self.request("POST", "/api/actions/" + action["id"] + "/apply", {})
        self.assertIn(code, (400, 403), result)
        self.assertIn("error", result)
        self.assertEqual([], self.events())

    def test_concurrent_task_action_apply_is_not_duplicated(self):
        store = self.server.application.store
        session = store.new_session("Concurrent action")
        action = store.create_action(session["id"], "assistant_add_task", {"title": "只创建一个待办"})
        path = "/api/actions/" + action["id"] + "/apply"
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(lambda _: self.request("POST", path, {}), range(4)))
        self.assertTrue(all(code == 200 for code, _, _ in replies), replies)
        tasks = self.request("GET", "/api/tasks")[2]["tasks"]
        self.assertEqual(["只创建一个待办"], [task["title"] for task in tasks])

    def test_get_cannot_apply_a_stored_write(self):
        store = self.server.application.store
        session = store.new_session("Read-only HTTP")
        action = store.create_action(session["id"], "assistant_add_task", {"title": "不能被GET创建"})
        code, _, result = self.request("GET", "/api/actions/" + action["id"] + "/apply")
        self.assertIn(code, (404, 405), result)
        self.assertEqual("pending", store.get_action(action["id"])["status"])
        self.assertEqual([], self.request("GET", "/api/tasks")[2]["tasks"])

    def test_unknown_write_outcome_is_not_automatically_repeated(self):
        app = self.server.application
        session = app.store.new_session("Uncertain result")
        action = app.store.create_action(session["id"], "calendar_create_event", self.create_arguments())
        original = app.calendar.call
        attempts = []

        def uncertain(name, arguments):
            if name == "calendar_create_event":
                attempts.append(arguments)
                raise AgentError("write_outcome_unknown", "测试模拟网络中断后结果待核实")
            return original(name, arguments)

        with patch.object(app.calendar, "call", side_effect=uncertain):
            path = "/api/actions/" + action["id"] + "/apply"
            code, _, failed = self.request("POST", path, {})
            self.assertIn(code, (409, 503), failed)
            self.assertEqual("unknown", app.store.get_action(action["id"])["status"])
            code, _, blocked = self.request("POST", path, {})
            self.assertEqual(409, code, blocked)
        self.assertEqual(1, len(attempts))
        self.assertEqual([], self.events())

    def test_chat_history_is_persisted_without_auto_applying_writes(self):
        code, _, result = self.request("POST", "/api/chat", {"message": "待办：检查 Agent 日程"})
        self.assertEqual(200, code, result)
        self.assertEqual("rules", result["mode"])
        self.assertTrue(result["actions"])
        self.assertEqual([], self.request("GET", "/api/tasks")[2]["tasks"])
        session_id = result["session_id"]
        code, _, history = self.request("GET", "/api/sessions/" + session_id)
        self.assertEqual(200, code, history)
        self.assertTrue(any(m["role"] == "user" and "检查 Agent 日程" in m["content"] for m in history["messages"]))
        code, _, listed = self.request("GET", "/api/sessions")
        self.assertEqual(200, code)
        self.assertIn(session_id, [s["id"] for s in listed["sessions"]])

    def test_missing_objects_and_unknown_api_paths_are_json_errors(self):
        for method, path, args in [
            ("GET", "/api/sessions/not-found", None),
            ("PATCH", "/api/tasks/not-found", {"completed": True}),
            ("DELETE", "/api/memories/not-found", {}),
            ("POST", "/api/actions/not-found/apply", {}),
            ("GET", "/api/not-a-route", None),
        ]:
            with self.subTest(path=path):
                code, _, result = self.request(method, path, args)
                self.assertEqual(404, code, result)
                self.assertIn("error", result)

    def test_model_offline_and_settings_do_not_enable_paid_endpoints(self):
        code, _, result = self.request("GET", "/api/models")
        self.assertEqual(200, code, result)
        self.assertFalse(result["available"])
        self.assertEqual([], result["models"])
        code, _, result = self.request("GET", "/api/settings")
        self.assertEqual(200, code, result)
        self.assertEqual("", result["settings"]["model"])
        for args in [{"model": "qwen3:cloud"},
                     {"endpoint": "https://paid.example/api", "model": "qwen3:8b"}]:
            with self.subTest(args=args):
                code, _, failed = self.request("PUT", "/api/settings", args)
                self.assertEqual(400, code, failed)
                self.assertIn("error", failed)

    def test_http_data_and_pending_actions_survive_server_restart(self):
        from personal_agent.webapp import make_server
        event = self.create_event()
        self.request("POST", "/api/tasks", {"title": "重启后的待办"})
        self.request("POST", "/api/memories", {"content": "重启后的记忆"})
        code, _, conversation = self.request("POST", "/api/chat", {"message": "待办：重启后待确认"})
        self.assertEqual(200, code, conversation)
        old_token = self.token
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.server.application.close()
        self.server = make_server(Path(self.temp.name), port=0, model_client=OfflineModel())
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.origin = "http://127.0.0.1:%s" % self.port
        code, _, result = self.request("GET", "/api/bootstrap")
        self.assertEqual(200, code, result)
        self.token = result["token"]
        self.assertNotEqual(old_token, self.token)
        self.assertEqual([event["id"]], [item["id"] for item in self.events()])
        self.assertEqual(["重启后的待办"], [t["title"] for t in self.request("GET", "/api/tasks")[2]["tasks"]])
        self.assertEqual(["重启后的记忆"], [m["content"] for m in self.request("GET", "/api/memories")[2]["memories"]])
        history = self.request("GET", "/api/sessions/" + conversation["session_id"])[2]
        self.assertEqual("pending", history["actions"][0]["status"])
        self.assertTrue(any(m["role"] == "user" and "重启后待确认" in m["content"] for m in history["messages"]))


if __name__ == "__main__":
    unittest.main()
