"""Inspect real SDK requests with private markers and an offline provider."""

import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from personal_agent.api_model import APIModelClient
from personal_agent.assistant import ALL_TOOLS
from personal_agent.model_context import select_context
from personal_agent.webapp import Application


class ModelContextTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = Application(Path(self.directory.name))
        self.addCleanup(self.app.close)
        self.app.api_settings.save({"base_url": "https://model.example/v1", "model": "offline",
                                    "enabled": True, "expected_version": 1})
        self.sid = self.app.store.new_session("路由验证")["id"]
        self.app.store.save_memory("旧记忆标记")
        self.app.store.add_message(self.sid, "user", "总结知识库")
        self.app.store.add_message(self.sid, "assistant", "旧回答标记")
        self.app.knowledge.set_workstyle("旧工作方式标记", 1)
        self.app.knowledge.save_source({"title": "迁移笔记", "kind": "material",
                                        "content": "周五下午的安排是先切换再迁移。资料正文标记。"})
        upload = self.app.chat_integrations.upload("旧附件标记.txt", base64.b64encode(b"test").decode())
        self.app.chat_integrations.bind_attachments([upload["id"]], self.sid)

    def request(self, message, **kwargs):
        with patch.object(APIModelClient, "chat", autospec=True, return_value={"content": "收到"}) as model:
            self.app.route("POST", "/api/chat", {}, {"message": message, "session_id": self.sid, **kwargs})
        model.assert_called_once()
        _, messages, tools, _ = model.call_args.args
        return json.dumps(messages, ensure_ascii=False), {tool["function"]["name"] for tool in tools}

    def test_calendar_turn_omits_unrelated_evidence_and_tools(self):
        for message in ("请先查周五下午有没有空，然后列出时段", "请安排资料评审会，然后列出日程"):
            with self.subTest(message=message):
                payload, names = self.request(message)
                for marker in ("资料正文标记", "旧记忆标记", "旧回答标记", "旧附件标记", "旧工作方式标记"):
                    self.assertNotIn(marker, payload)
                self.assertIn("calendar_find_free_slots", names)
                self.assertTrue(all(name.startswith("calendar_") for name in names))

    def test_knowledge_and_compound_requests_choose_their_own_tools(self):
        for message, needs_task in (("读取迁移笔记，然后解释资料结论", False),
                                    ("根据迁移笔记分析切换顺序，然后生成待办", True)):
            with self.subTest(message=message):
                payload, names = self.request(message)
                self.assertIn("资料正文标记", payload)
                self.assertIn("knowledge_get_source", names)
                self.assertEqual(needs_task, "assistant_add_task" in names)
                self.assertNotIn("assistant_read_attachment", names)
                self.assertNotIn("旧记忆标记", payload)
                self.assertNotIn("旧回答标记", payload)

    def test_sent_attachment_is_available_even_when_intent_does_not_name_it(self):
        fresh = self.app.chat_integrations.upload("本轮标记.txt", base64.b64encode(b"text").decode())
        payload, names = self.request("请分析这个内容，然后给我一个结论", attachment_ids=[fresh["id"]])
        self.assertIn("本轮标记.txt", payload)
        self.assertNotIn("旧附件标记", payload)
        self.assertIn("assistant_read_attachment", names)

    def test_followup_retains_calendar_fields_but_stops_at_previous_topic(self):
        turns = [{"role": "user", "content": "阅读资料"}, {"role": "assistant", "content": "旧内容"},
                 {"role": "user", "content": "明天下午三点安排评审会"},
                 {"role": "assistant", "content": "评审会需要多久？"}]
        domains, history, _ = select_context("一个半小时", turns, ALL_TOOLS)
        self.assertEqual({"calendar"}, domains)
        self.assertEqual(turns[-2:], history)
        _, unrelated, _ = select_context("把那场会议改到四点", turns[:2], ALL_TOOLS)
        self.assertEqual([], unrelated)
        _, prior, tools = select_context("根据上述内容查找笔记并生成待办", turns, ALL_TOOLS)
        self.assertIn("assistant_add_task", tools)
        self.assertIn("knowledge_search", tools)
        self.assertTrue(prior)
