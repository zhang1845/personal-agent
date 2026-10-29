"""Deterministic assistant tests: temporary state and injected models only."""

import tempfile
import unittest
from pathlib import Path

from personal_agent.assistant import AgentEngine
from personal_agent.errors import AgentError
from personal_agent.service import CalendarService


class FakeStore:
    def __init__(self):
        self.sessions = {}
        self.messages = {}
        self.actions = []
        self.tasks = []
        self.memories = []

    def new_session(self, title):
        session = {"id": "session-" + str(len(self.sessions) + 1), "title": title}
        self.sessions[session["id"]] = session
        self.messages[session["id"]] = []
        return session

    def get_session(self, sid):
        if sid not in self.sessions:
            raise AgentError("not_found", "会话不存在")
        return self.sessions[sid]

    def add_message(self, sid, role, content):
        self.messages[sid].append({"role": role, "content": content})

    def list_messages(self, sid, limit=20):
        return self.messages[sid][-limit:]

    def create_action(self, sid, tool, arguments, title=None):
        action = {"id": "action-" + str(len(self.actions) + 1), "session_id": sid,
                  "tool": tool, "arguments": arguments, "title": title, "status": "pending"}
        self.actions.append(action)
        return action

    def list_tasks(self):
        return self.tasks

    def list_memories(self):
        return self.memories


class ReadOnlyCalendar:
    def __init__(self, core):
        self.core = core
        self.calls = []
        # A deliberately fixed instant: relative days must use this, not host time.
        self.current_time = "2030-01-01T18:00:00Z"
        self.timezone = "Asia/Shanghai"

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name in ("calendar_create_event", "calendar_update_event", "calendar_delete_event", "calendar_set_preferences"):
            raise AssertionError("The engine must never execute a write")
        if name == "calendar_status":
            return {"timezone": self.timezone, "current_time": self.current_time, "backend": "local"}
        return self.core.call(name, arguments)


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, messages, tools, model):
        self.calls.append((list(messages), tools, model))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def tool(name, arguments):
    return {"content": "", "tool_calls": [{"function": {"name": name, "arguments": arguments}}]}


class AssistantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="assistant-test-")
        self.core = CalendarService(str(Path(self.tmp.name) / "calendar.sqlite3"))
        self.calendar = ReadOnlyCalendar(self.core)
        self.store = FakeStore()
        self.model = FakeModel([])
        self.engine = AgentEngine(self.calendar, self.store, self.model)

    def tearDown(self):
        self.core.close()
        self.tmp.cleanup()

    def seed_event(self, **overrides):
        values = {"request_id": "seed", "title": "已有评审", "start": "2030-01-03T15:00:00+08:00",
                  "end": "2030-01-03T15:30:00+08:00"}
        values.update(overrides)
        return self.core.call("calendar_create_event", values)["event"]

    def assert_no_write(self, result):
        self.assertEqual([], result["actions"])
        self.assertEqual([], self.store.actions)

    def test_relative_query_uses_status_time_and_personal_timezone(self):
        self.seed_event()
        result = self.engine.respond("明天有什么安排")
        # UTC Jan 1 18:00 is Jan 2 in Shanghai, so tomorrow is Jan 3.
        self.assertEqual("rules", result["mode"])
        self.assertIn("2030-01-03", result["reply"])
        self.assertIn("已有评审", result["reply"])
        self.assertIn("15:00", result["reply"])
        self.assertEqual("2030-01-03", result["tools"][0]["result"]["events"][0]["start"][:10])
        self.assert_no_write(result)

    def test_today_day_after_and_explicit_date_queries(self):
        for expression, expected in (("今天的日程", "2030-01-02"), ("后天的安排", "2030-01-04"),
                                     ("查看2026-12-30的日程", "2026-12-30")):
            with self.subTest(expression=expression):
                result = self.engine.respond(expression)
                self.assertIn(expected, result["reply"])
                self.assert_no_write(result)

    def test_clear_chinese_and_numeric_create_only_make_drafts(self):
        for expression in ("明天下午3点安排半小时评审", "明天15:00 评审30分钟", "明天下午3:00 评审30分钟", "明天下午15:00 评审30分钟"):
            with self.subTest(expression=expression):
                result = self.engine.respond(expression, model="selected-local")
                self.assertEqual("rules", result["mode"])
                self.assertFalse(result["needs_clarification"])
                action = result["actions"][0]
                self.assertEqual("calendar_create_event", action["tool"])
                self.assertEqual("pending", action["status"])
                args = action["arguments"]
                self.assertEqual("评审", args["title"])
                self.assertEqual("2030-01-03T15:00:00", args["start"])
                self.assertEqual("2030-01-03T15:30:00", args["end"])
                self.assertTrue(args["request_id"])
        self.assertEqual([], self.core.call("calendar_list_events", {"start": "2030-01-03", "end": "2030-01-04"})["events"])
        self.assertEqual([], self.model.calls)

    def test_creation_supports_explicit_date_and_chinese_numbers(self):
        result = self.engine.respond("2026-12-30 下午三点半 方案评审二十分钟")
        args = result["actions"][0]["arguments"]
        self.assertEqual("2026-12-30T15:30:00", args["start"])
        self.assertEqual("2026-12-30T15:50:00", args["end"])
        self.assertEqual("方案评审", args["title"])

    def test_missing_or_ambiguous_fields_without_model_do_not_guess(self):
        for expression in ("明天15:00评审", "明天评审30分钟", "明天安排评审", "15:00评审30分钟",
                           "明天15:00 30分钟", "明天3点评审30分钟", "明天25:00评审30分钟",
                           "明天15:00评审0分钟", "今天和明天15:00评审30分钟", "2026-02-30 15:00评审30分钟"):
            with self.subTest(expression=expression):
                result = self.engine.respond(expression)
                self.assertTrue(result["needs_clarification"])
                self.assert_no_write(result)
        self.assertEqual([], self.model.calls)

    def test_duration_ranges_are_not_silently_shortened(self):
        for message in ("明天15:00 评审30到60分钟", "明天15:00 评审一到两小时"):
            result = self.engine.respond(message)
            self.assert_no_write(result)
            self.assertTrue(result["needs_clarification"])

    def test_title_keeps_english_letters_and_ordinary_chinese(self):
        for title in ("TEST", "到店拜访"):
            result = self.engine.respond("明天15:00 " + title + "30分钟")
            self.assertEqual(title, result["actions"][0]["arguments"]["title"])

    def test_query_containing_time_is_never_treated_as_create(self):
        result = self.engine.respond("查看明天15:00的日程30分钟")
        self.assertTrue(result["needs_clarification"])
        self.assert_no_write(result)

    def test_ambiguous_cancellation_does_not_call_model(self):
        result = self.engine.respond("帮我取消那个会议", model="local")
        self.assertTrue(result["needs_clarification"])
        self.assertIn("不会猜测", result["reply"])
        self.assert_no_write(result)
        self.assertEqual([], self.model.calls)

    def test_memory_and_task_are_pending_not_implicitly_saved(self):
        memory = self.engine.respond("记住我喜欢下午开会")
        task = self.engine.respond("待办：整理个人Agent需求")
        self.assertEqual("assistant_save_memory", memory["actions"][0]["tool"])
        self.assertEqual("我喜欢下午开会", memory["actions"][0]["arguments"]["content"])
        self.assertEqual("assistant_add_task", task["actions"][0]["tool"])
        self.assertEqual([], self.store.tasks)
        self.assertEqual([], self.store.memories)

    def test_read_saved_tasks_and_memories(self):
        self.store.tasks = [{"id": "t", "title": "写报告", "due_date": None, "completed": False},
                            {"id": "done", "title": "已完成项目", "due_date": None, "completed": True}]
        self.store.memories = [{"id": "m", "content": "偏好下午开会"}]
        result = self.engine.respond("我的待办")
        self.assertIn("写报告", result["reply"])
        self.assertNotIn("已完成项目", result["reply"])
        self.assertIn("偏好下午开会", self.engine.respond("个人记忆")["reply"])

    def test_model_read_loop_then_write_only_proposes(self):
        self.model.responses = [tool("calendar_list_events", {"start": "2030-01-03", "end": "2030-01-04"}),
                                tool("calendar_create_event", {"title": "讨论", "start": "2030-01-03T14:00:00", "end": "2030-01-03T14:30:00"})]
        result = self.engine.respond("请给项目讨论选一个明确可用的时间", model="local-installed")
        self.assertEqual("ollama", result["mode"])
        self.assertEqual(2, len(self.model.calls))
        self.assertEqual("calendar_list_events", result["tools"][0]["name"])
        self.assertEqual("calendar_create_event", result["actions"][0]["tool"])
        self.assertIn("request_id", result["actions"][0]["arguments"])
        self.assertEqual("tool", self.model.calls[1][0][-1]["role"])
        self.assertTrue(all(t["function"]["name"] not in ("shell", "exec", "read_file") for t in self.model.calls[0][1]))

    def test_unknown_tool_prevents_any_pending_action_in_same_response(self):
        response = tool("assistant_add_task", {"title": "ok"})
        response["tool_calls"].append({"function": {"name": "shell", "arguments": {"cmd": "echo bad"}}})
        self.model.responses = [response]
        result = self.engine.respond("规划我的项目", model="local")
        self.assertEqual("unsupported_tool", result["error"]["code"])
        self.assert_no_write(result)

    def test_model_cannot_save_unsolicited_memory(self):
        self.model.responses = [tool("assistant_save_memory", {"content": "用户喜欢工作"})]
        result = self.engine.respond("帮我分析工作计划", model="local")
        self.assert_no_write(result)
        self.assertTrue(result["needs_clarification"])

    def test_bad_model_write_types_are_rejected_without_drafts(self):
        self.model.responses = [tool("calendar_create_event", {"title": "评审", "start": "2030-01-03T14:00:00", "end": "2030-01-03T14:30:00", "all_day": "false"})]
        result = self.engine.respond("请规划项目交流", model="local")
        self.assert_no_write(result)
        self.assertEqual("validation_error", result["error"]["code"])

    def test_malformed_model_tool_function_fails_closed(self):
        for function in (None, [], "shell", {"name": ["shell"]}):
            self.model.responses = [{"content": "", "tool_calls": [{"function": function}]}]
            result = self.engine.respond("请规划项目交流", model="local")
            self.assert_no_write(result)
            self.assertTrue(result["needs_clarification"])

    def test_update_requires_existing_id_and_current_version(self):
        event = self.seed_event()
        args = {"event_id": event["id"], "expected_version": "outdated", "changes": {"title": "新标题"}}
        self.model.responses = [tool("calendar_update_event", args)]
        result = self.engine.respond("把已有评审改成新标题", model="local")
        self.assertEqual("version_conflict", result["error"]["code"])
        self.assert_no_write(result)
        args["expected_version"] = event["version"]
        self.model.responses = [tool("calendar_update_event", args)]
        result = self.engine.respond("把已有评审改成新标题", model="local")
        self.assertEqual("calendar_update_event", result["actions"][0]["tool"])
        self.assertEqual("已有评审", self.core.call("calendar_get_event", {"event_id": event["id"]})["event"]["title"])

    def test_model_has_three_read_rounds_maximum(self):
        self.model.responses = [tool("calendar_status", {})] * 4
        result = self.engine.respond("详细研究我的行程", model="local")
        self.assertEqual(3, len(self.model.calls))
        self.assertEqual(3, len(result["tools"]))
        self.assertTrue(result["needs_clarification"])
        self.assert_no_write(result)

    def test_model_unavailable_does_not_disable_rules(self):
        self.model.responses = [AgentError("model_unavailable", "本机模型尚未运行")]
        failed = self.engine.respond("帮我思考工作重点", model="local")
        self.assertEqual("model_unavailable", failed["error"]["code"])
        result = self.engine.respond("明天15:00 评审30分钟", model="local")
        self.assertEqual("rules", result["mode"])
        self.assertEqual(1, len(result["actions"]))

    def test_model_text_cannot_claim_write_success_without_tool(self):
        claims = ("我已经帮你安排好了会议", "已经帮你记住了，以后不会在午休安排会议。",
                  "已为你记下这个偏好", "工作时间已设定为九点", "缓冲时间调整好了", "已创建会议",
                  "I added the event.", "Your preference has been saved.")
        for claim in claims:
            with self.subTest(claim=claim):
                self.model.responses = [{"content": claim}]
                result = self.engine.respond("帮我考虑会议规划", model="local")
                self.assertIn("未做任何写入", result["reply"])
                self.assertNotIn(claim, result["reply"])
                self.assertIn("本轮未执行写入；写入须生成并应用草案。", result["reply"])
                self.assert_no_write(result)

    def test_model_advice_without_draft_always_states_actual_write_status(self):
        self.model.responses = [{"content": "可以把项目讨论放在上午，具体时间请再确认。"}]
        result = self.engine.respond("帮我考虑会议规划", model="local")
        self.assertIn("可以把项目讨论放在上午", result["reply"])
        self.assertIn("本轮未执行写入；写入须生成并应用草案。", result["reply"])
        self.assert_no_write(result)

    def test_rules_mode_truthfully_requires_model_api_for_complex_expressions(self):
        result = self.engine.respond("帮我结合最近习惯做一份复杂周计划")
        self.assertEqual("rules", result["mode"])
        self.assertIn("智能对话", result["reply"])
        self.assert_no_write(result)

    def test_session_roundtrip_and_input_validation(self):
        first = self.engine.respond("我的待办")
        second = self.engine.respond("个人记忆", session_id=first["session_id"])
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertEqual(4, len(self.store.list_messages(first["session_id"])))
        for message in ("", "  ", "x" * 8001, None):
            with self.assertRaises(AgentError):
                self.engine.respond(message)
        with self.assertRaises(AgentError):
            self.engine.respond("今天的日程", session_id="missing")

    def test_unknown_clock_timezone_does_not_use_host_clock(self):
        self.calendar.current_time = "2030-01-01T18:00:00"
        result = self.engine.respond("明天15:00 评审30分钟")
        self.assert_no_write(result)
        self.assertTrue(result["needs_clarification"])


if __name__ == "__main__":
    unittest.main()
