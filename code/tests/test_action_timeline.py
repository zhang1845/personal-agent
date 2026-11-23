"""Model drafts retain their final business outcome across confirmations and restarts."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from personal_agent.api_model import APIModelClient
from personal_agent.errors import AgentError
from personal_agent.personal_store import PersonalStore
from personal_agent.webapp import Application


class ActionTimelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Application(Path(self.temp.name))
        self.addCleanup(self.app.close)
        self.store = self.app.store
        self.app.api_settings.save({"base_url": "https://model.example/v1", "model": "mock",
                                    "enabled": True, "expected_version": 1})
        self.event = {"title": "评审", "start": "2030-01-07T10:00:00+08:00",
                      "end": "2030-01-07T10:30:00+08:00", "location": "A 座"}

    def runs(self):
        return self.app.route("GET", "/api/agent-runs", {}, {})["runs"]

    def test_model_draft_conflict_can_be_revised_and_confirmed_once(self):
        calls = iter([
            {"content": "", "tool_calls": [{"id": "first", "function": {"name": "calendar_create_event",
                "arguments": dict(self.event, request_id="from-model")}}]},
            {"content": "请核对时间。"}
        ])
        with patch.object(APIModelClient, "chat", side_effect=lambda *_: next(calls)):
            reply = self.app.route("POST", "/api/chat", {}, {"message": "请先看工作安排，然后创建一项评审日程草案"})
        run = self.runs()[0]
        action = reply["actions"][0]
        self.assertEqual("pending", run["actions"][0]["status"])
        self.assertEqual((reply["run_id"], run["id"]), (action["run_id"], action["run_id"]))
        history = self.app.route("GET", "/api/sessions/" + reply["session_id"], {}, {})
        self.assertEqual(action["run_id"], history["messages"][-1]["run_id"])

        self.app.calendar.call("calendar_create_event", dict(self.event, request_id="occupied", title="保留的安排"))
        with self.assertRaises(AgentError) as failure:
            self.app.apply(action["id"])
        self.assertEqual("conflict", failure.exception.code)
        first = self.runs()[0]["actions"][0]
        self.assertEqual(("failed", "conflict"), (first["status"], first["error"]["code"]))
        self.assertNotIn("details", first["error"])
        self.assertNotIn("arguments", first)
        self.assertTrue(first["resolved_at"])

        times = {"start": "2030-01-07T14:00:00+08:00", "end": "2030-01-07T14:30:00+08:00"}
        path = "/api/actions/" + action["id"] + "/revise-time"
        replacement = self.app.route("POST", path, {}, times)["action"]
        self.assertEqual("pending", replacement["status"])
        self.assertEqual(action["id"], replacement["replaces_id"])
        self.assertEqual(replacement, self.app.route("POST", path, {}, times)["action"])
        with self.assertRaises(AgentError):
            self.app.route("POST", path, {}, dict(times, end="2030-01-07T15:00:00+08:00"))

        events = lambda: self.app.calendar.call("calendar_list_events", {"start": "2030-01-07", "end": "2030-01-08"})["events"]
        self.assertEqual(1, len(events()))
        applied = self.app.apply(replacement["id"])
        self.assertEqual(applied, self.app.apply(replacement["id"]))
        self.assertEqual("A 座", applied["result"]["event"]["location"])
        self.assertEqual(2, len(events()))
        self.assertEqual(["failed", "applied"], [a["status"] for a in self.runs()[0]["actions"]])

    def test_revising_update_retains_target_and_rejects_stale_version(self):
        target = self.app.calendar.call("calendar_create_event", dict(self.event, request_id="target"))["event"]
        occupied = {"start": "2030-01-07T12:00:00+08:00", "end": "2030-01-07T12:30:00+08:00"}
        self.app.calendar.call("calendar_create_event", dict(self.event, **occupied, request_id="busy"))
        session = self.store.new_session("改期")
        action = self.store.create_action(session["id"], "calendar_update_event", {
            "event_id": target["id"], "expected_version": target["version"],
            "changes": dict(occupied, location="B 座")
        })
        with self.assertRaises(AgentError) as exc:
            self.app.apply(action["id"])
        self.assertEqual("conflict", exc.exception.code)
        times = {"start": "2030-01-07T14:00:00+08:00", "end": "2030-01-07T14:30:00+08:00"}
        revised = self.app.revise_conflict(action["id"], times)["action"]
        self.assertEqual(target["id"], revised["arguments"]["event_id"])
        self.assertEqual(dict(times, location="B 座"), revised["arguments"]["changes"])
        self.app.calendar.call("calendar_update_event", {"event_id": target["id"], "expected_version": target["version"],
            "changes": {"title": "已由用户修改"}, "request_id": "manual-change"})
        with self.assertRaises(AgentError) as exc:
            self.app.apply(revised["id"])
        self.assertEqual("version_conflict", exc.exception.code)

    def test_unknown_result_cannot_be_resent_or_revised(self):
        session = self.store.new_session("超时")
        run = self.app.execution.start(session["id"], "nanobot-sdk", "mock")
        action = self.store.create_action(session["id"], "calendar_create_event", self.event, run_id=run)
        with patch.object(self.app.calendar, "call", side_effect=AgentError("unknown_outcome", "请核查实际日历")) as remote:
            with self.assertRaises(AgentError):
                self.app.apply(action["id"])
            with self.assertRaises(AgentError):
                self.app.apply(action["id"])
            with self.assertRaises(AgentError):
                self.app.revise_conflict(action["id"], {"start": self.event["start"], "end": self.event["end"]})
            self.assertEqual(1, remote.call_count)
        self.assertEqual("unknown", self.runs()[0]["actions"][0]["status"])

    def test_upgrade_preserves_unlinked_drafts(self):
        path = Path(self.temp.name) / "legacy.sqlite3"
        with sqlite3.connect(path) as c:
            c.execute("CREATE TABLE actions(id TEXT PRIMARY KEY, session_id TEXT, tool TEXT, arguments TEXT, title TEXT, status TEXT, result TEXT, created_at TEXT)")
            c.execute("INSERT INTO actions VALUES ('older','s','assistant_add_task','{}','旧草案','pending',NULL,'2025-01-01')")
        store = PersonalStore(path)
        self.assertIsNone(store.get_action("older")["run_id"])
        self.assertEqual({}, store.outcomes_by_run(["unrelated"]))
