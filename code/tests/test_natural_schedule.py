"""Natural scheduling and model handoff regressions, with isolated local state."""
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from personal_agent.api_model import APIModelClient
from personal_agent.webapp import Application
from personal_agent.nanobot_engine import _review_model_reply


class NaturalScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="natural-schedule-")
        self.app = Application(Path(self.temp.name))

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def chat(self, message, session_id=None):
        body = {"message": message}
        if session_id:
            body["session_id"] = session_id
        return self.app.route("POST", "/api/chat", {}, body)

    def enable_model(self):
        self.app.api_settings.save({"base_url":"https://model.example/v1", "model":"mock",
                                    "enabled":True, "expected_version":1})

    def test_reported_full_sentence_creates_learning_from_two_to_four(self):
        value = self.chat("帮我创建一下今天下午两点的日程时间两个小时，名称为学习")
        self.assertEqual(1, len(value["actions"]))
        action = value["actions"][0]
        self.assertEqual("学习", action["arguments"]["title"])
        start = datetime.fromisoformat(action["arguments"]["start"])
        end = datetime.fromisoformat(action["arguments"]["end"])
        self.assertEqual(14, start.hour)
        self.assertEqual(16, end.hour)
        self.assertEqual(timedelta(hours=2), end-start)
        self.assertEqual("pending", action["status"])
        self.assertEqual([], self.app.calendar.call("calendar_list_events", {
            "start":start.date().isoformat(), "end":(start.date()+timedelta(days=1)).isoformat()})["events"])
        saved = self.app.apply(action["id"])["result"]["event"]
        self.assertEqual("学习", saved["title"])

    def test_duration_variants_are_not_truncated(self):
        for expression, minutes in (("两个小时",120), ("两小时",120), ("2个小时",120),
                ("两个半小时",150), ("一小时半",90), ("半个小时",30),
                ("2小时30分钟",150), ("一小时二十分钟",80)):
            with self.subTest(expression=expression):
                value = self.chat("今天下午两点学习" + expression)
                self.assertEqual(1, len(value["actions"]), value)
                args = value["actions"][0]["arguments"]
                self.assertEqual("学习", args["title"])
                self.assertEqual(timedelta(minutes=minutes), datetime.fromisoformat(args["end"])-datetime.fromisoformat(args["start"]))

    def test_explicit_titles_and_duration_labels_are_kept_separate(self):
        for message, title in (("帮我创建一下今天下午两点的日程，持续两个小时，名称为学习", "学习"),
                ("今天下午两点，时长为两个小时，标题是进行研究", "进行研究"),
                ("今天下午两点，两个小时，名称为“AI，研究”", "AI，研究"),
                ("今天下午两点学习持续两个小时", "学习")):
            with self.subTest(message=message):
                value = self.chat(message)
                self.assertEqual(1, len(value["actions"]), value)
                self.assertEqual(title, value["actions"][0]["arguments"]["title"])

    def test_offline_questions_only_ask_for_missing_information(self):
        for message, question in (("帮我创建一下今天下午两点的日程，名称为学习", "要安排多久"),
                ("今天学习两个小时", "几点开始"), ("下午两点学习两个小时", "哪一天"),
                ("今天下午两点的日程时间两个小时", "叫什么")):
            with self.subTest(message=message):
                value = self.chat(message)
                self.assertEqual([], value["actions"])
                self.assertTrue(value["needs_clarification"])
                self.assertIn(question, value["reply"])

    def test_ambiguous_duration_stays_unconfirmed_offline(self):
        for message in ("今天下午两点学习一到两小时", "今天下午两点学习30到60分钟"):
            value = self.chat(message)
            self.assertEqual([], value["actions"])
            self.assertTrue(value["needs_clarification"])

    def test_decimal_duration_is_not_misread_as_its_last_digit(self):
        for value in ("1.5小时", "0.5小时"):
            result = self.chat("今天下午两点学习" + value)
            self.assertEqual([], result["actions"])
            self.assertTrue(result["needs_clarification"])

    def test_missing_duration_uses_model_and_followup_keeps_context(self):
        self.enable_model()
        original = "帮我创建一下今天下午两点的日程，名称为学习"
        now, _ = self.app.engine._now()
        start = now.replace(hour=14, minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=2)
        requests = []
        def provider(client, messages, tools, model):
            requests.append(messages)
            if len(requests) == 1:
                return {"content":"已了解你的日程安排：今天下午两点学习。学习安排多久？"}
            if len(requests) == 2:
                prompt = "\n".join(m.get("content", "") for m in messages)
                self.assertIn(original, prompt)
                self.assertIn("两个小时", prompt)
                return {"content":"", "tool_calls":[{"id":"create-followup", "function":{
                    "name":"calendar_create_event", "arguments":{"request_id":"model-followup", "title":"学习",
                        "start":start.isoformat(), "end":end.isoformat()}}}]}
            return {"content":"学习安排在14:00至16:00，请核对草案。"}
        with patch.object(APIModelClient, "chat", new=provider):
            first = self.chat(original)
            self.assertEqual("nanobot", first["mode"])
            self.assertEqual([], first["actions"])
            self.assertIn("多久", first["reply"])
            self.assertNotIn("请确认", first["reply"])
            second = self.chat("两个小时", first["session_id"])
        self.assertEqual("nanobot", second["mode"])
        self.assertEqual(3, len(requests))
        self.assertEqual(1, len(second["actions"]))
        self.assertEqual("学习", second["actions"][0]["arguments"]["title"])
        self.assertEqual(start.isoformat(), second["actions"][0]["arguments"]["start"])
        self.assertEqual(end.isoformat(), second["actions"][0]["arguments"]["end"])
        self.assertEqual("pending", second["actions"][0]["status"])

    def test_freeform_time_range_reaches_model_instead_of_format_rejection(self):
        self.enable_model()
        with patch.object(self.app.engine, "_model", return_value={"reply":"请核对学习安排。", "actions":[], "tools":[], "needs_clarification":True}) as model:
            value = self.chat("帮我安排今天下午两点到四点学习")
        model.assert_called_once()
        self.assertEqual("nanobot", value["mode"])

    def test_invalid_clock_does_not_get_reinterpreted_by_model(self):
        self.enable_model()
        with patch.object(self.app.engine, "_model") as model:
            value = self.chat("今天25:00学习两个小时")
        model.assert_not_called()
        self.assertEqual([], value["actions"])
        self.assertTrue(value["needs_clarification"])

    def test_reply_review_preserves_questions_and_draft_details(self):
        question = "已了解你的日程安排：今天下午两点学习。要持续多久？"
        self.assertEqual(question, _review_model_reply(question, False))
        draft = "已生成学习日程草案，时间为14:00至16:00，确认后保存。"
        self.assertEqual(draft, _review_model_reply(draft, True))
        mixed = _review_model_reply("已创建学习日程。还需要安排多久？", False)
        self.assertNotIn("已创建", mixed)
        self.assertIn("尚未执行", mixed)
        self.assertIn("安排多久", mixed)
        self.assertNotIn("已生成", _review_model_reply("已生成日程草案。", False))
        self.assertNotIn("已创建", _review_model_reply("已创建学习日程，但尚未通知。", False))


if __name__ == "__main__":
    unittest.main()
