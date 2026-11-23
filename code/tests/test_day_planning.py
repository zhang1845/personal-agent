"""Day planning through the same chat and confirmation endpoints as the workbench."""
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from personal_agent.errors import AgentError
from personal_agent.webapp import Application


class DayPlanningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Application(Path(self.temp.name))
        self.addCleanup(self.app.close)
        self.tz = ZoneInfo("Asia/Shanghai")
        self.now = datetime(2030, 1, 2, 10, 15, tzinfo=self.tz)  # Wednesday
        self.app.engine._now = lambda: (self.now, self.tz)
        self.sid = None

    def chat(self, text):
        body = {"message": text}
        if self.sid:
            body["session_id"] = self.sid
        result = self.app.route("POST", "/api/chat", {}, body)
        self.sid = result["session_id"]
        return result

    def seed(self, title, start, end):
        return self.app.calendar.call("calendar_create_event", {"request_id": title, "title": title,
                      "start": "2030-01-02T" + start, "end": "2030-01-02T" + end})

    def apply(self, action):
        return self.app.route("POST", f"/api/actions/{action['id']}/apply", {}, {})

    def test_select_batch_allow_partial_confirmation_and_replay(self):
        for title in ("准备报告", "写邮件", "资料归档"):
            self.app.store.add_task(title)
        self.seed("会议", "13:30", "14:15")
        initial = self.chat("把这三件事排进今天下午")
        self.assertIn("准备报告", initial["reply"])
        self.assertFalse(initial["actions"])
        missing = self.chat("1、2、3")
        self.assertTrue(missing["needs_clarification"])
        draft = self.chat("1 45分钟，2 15分钟，3 六小时")
        self.assertIn("资料归档", draft["reply"])
        self.assertEqual(2, len(draft["actions"]), draft)
        first, second = draft["actions"]
        self.assertIn("T14:30:", first["arguments"]["start"])
        self.assertIn("T13:00:", second["arguments"]["start"])
        applied = self.apply(second)
        self.assertEqual(applied["result"]["event"]["id"], self.apply(second)["result"]["event"]["id"])
        self.assertEqual("pending", self.app.store.get_action(first["id"])["status"])
        events = self.app.calendar.call("calendar_list_events", {"start":"2030-01-02", "end":"2030-01-03"})["events"]
        self.assertEqual({"会议", "写邮件"}, {e["title"] for e in events})
        self.assertFalse(any(t["completed"] for t in self.app.store.list_tasks()))

    def test_check_conflict_and_changed_version_on_confirmation(self):
        first = self.app.store.add_task("交稿")
        self.app.store.add_task("评审")
        self.chat("帮我规划今天")
        a, b = self.chat("交稿 30分钟，评审 半小时")["actions"]
        self.app.store.update_task(first["id"], {"title":"修改后的交稿"}, first["version"])
        with self.assertRaises(AgentError) as stale:
            self.apply(a)
        self.assertEqual("version_conflict", stale.exception.code)
        self.app.calendar.call("calendar_create_event", {"request_id":"new", "title":"新会议",
                               "start": b["arguments"]["start"], "end": b["arguments"]["end"]})
        with self.assertRaises(AgentError) as busy:
            self.apply(b)
        self.assertEqual("conflict", busy.exception.code)

    def test_named_estimates_in_initial_message_and_only_missing_estimate_followup(self):
        self.app.store.add_task("交稿")
        self.app.store.add_task("评审")
        result = self.chat("帮我规划今天下午：交稿半小时，评审")
        self.assertTrue(result["needs_clarification"])
        self.assertIn("2", result["reply"])
        draft = self.chat("四十五分钟")
        self.assertEqual(["交稿", "评审"], [a["arguments"]["title"] for a in draft["actions"]])
        self.assertEqual("2030-01-02T13:45:00+08:00", draft["actions"][1]["arguments"]["start"])

    def test_no_tasks_no_time_and_session_bound_selection(self):
        self.assertIn("暂无", self.chat("帮我规划今天")["reply"])
        self.app.store.add_task("写邮件")
        self.chat("帮我规划今天")
        saved_sid = self.sid
        self.sid = None
        self.assertFalse(self.chat("1 30分钟")["actions"])
        self.sid = saved_sid
        self.now = self.now.replace(hour=20)
        result = self.chat("1 30分钟")
        self.assertFalse(result["actions"])
        self.assertIn("未排入日历", result["reply"])

    def test_dates_cancellation_and_duplicate_title(self):
        self.app.store.add_task("报告")
        self.app.store.add_task("报告")
        self.chat("帮我规划今天")
        self.assertFalse(self.chat("报告 30分钟")["actions"])
        self.chat("帮我规划今天下午")
        self.assertIn("结束", self.chat("取消规划")["reply"])
        self.assertFalse(self.chat("1 20分钟")["actions"])
        self.chat("帮我规划今天")
        self.now = self.now.replace(day=3)
        self.assertIn("日期", self.chat("1 20分钟")["reply"])
