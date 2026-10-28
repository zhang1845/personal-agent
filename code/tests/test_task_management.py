"""Task migration, reversible deletion and durable local task scheduling."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from personal_agent.personal_store import PersonalStore
from personal_agent.errors import AgentError
import test_webapp


class TaskStoreTests(unittest.TestCase):
    def test_concurrent_initialization_migrates_once(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "concurrent.sqlite3"
            with ThreadPoolExecutor(max_workers=3) as pool:
                stores = list(pool.map(lambda _: PersonalStore(path), range(3)))
            self.assertEqual([], stores[0].list_tasks())
            with sqlite3.connect(str(path)) as c:
                names = [r[1] for r in c.execute("PRAGMA table_info(tasks)")]
            self.assertEqual(1, names.count("version"))
            self.assertEqual(1, names.count("deleted_at"))

    def test_old_database_migrates_without_losing_data_or_resurrecting_deleted_actions(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "old.sqlite3"
            with sqlite3.connect(str(path)) as c:
                c.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY,title TEXT NOT NULL,due_date TEXT,completed INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,source_action_id TEXT UNIQUE)")
                c.execute("INSERT INTO tasks VALUES('old','原有任务','2026-10-28',0,'2026-10-26','source')")
            store = PersonalStore(path)
            task = store.get_task("old")
            self.assertEqual(("原有任务", 1, None), (task["title"], task["version"], task["deleted_at"]))
            task = store.update_task("old", {"title": "修改", "due_date": None}, 1)
            deleted = store.set_task_deleted("old", True, task["version"])
            self.assertEqual([], store.list_tasks())
            self.assertTrue(store.add_task("不会复活", source_action_id="source")["deleted_at"])
            store = PersonalStore(path)
            self.assertEqual(1, len(store.list_tasks(deleted=True)))
            restored = store.set_task_deleted("old", False, deleted["version"])
            self.assertEqual(("修改", None, False), (restored["title"], restored["due_date"], restored["completed"]))

    def test_validation_and_stale_version_never_partially_update(self):
        with tempfile.TemporaryDirectory() as folder:
            store = PersonalStore(Path(folder) / "tasks.sqlite3")
            task = store.add_task("保留", "2026-10-28")
            for changes in ({"title": "变化", "due_date": "2026-02-30"}, {"completed": "true"}, {"title": ""}):
                with self.assertRaises(AgentError):
                    store.update_task(task["id"], changes, task["version"])
                self.assertEqual("保留", store.get_task(task["id"])["title"])
            store.update_task(task["id"], {"title": "新标题"}, 1)
            with self.assertRaises(AgentError) as error:
                store.set_task_deleted(task["id"], True, 1)
            self.assertEqual("version_conflict", error.exception.code)
            self.assertFalse(store.get_task(task["id"])["deleted_at"])


class TaskHTTPTests(unittest.TestCase):
    setUp = test_webapp.WebappTests.setUp
    tearDown = test_webapp.WebappTests.tearDown
    request = test_webapp.WebappTests.request
    events = test_webapp.WebappTests.events

    def task(self, title="任务排程", due_date="2026-10-28"):
        code, _, result = self.request("POST", "/api/tasks", {"title": title, "due_date": due_date})
        self.assertEqual(200, code, result)
        return result["task"]

    def schedule(self, task, request_id="plan-one", hour=15, **extra):
        payload = {"request_id": request_id, "expected_version": task["version"],
                   "start": "2026-10-27T%02d:00:00" % hour, "end": "2026-10-27T%02d:30:00" % hour}
        payload.update(extra)
        return self.request("POST", "/api/tasks/" + task["id"] + "/schedule", payload)

    def test_edit_trash_restore_and_overview_counts(self):
        task = self.task()
        path = "/api/tasks/" + task["id"]
        code, _, result = self.request("PATCH", path, {"title": "已编辑", "due_date": None, "expected_version": 1})
        self.assertEqual(200, code, result)
        self.assertIsNone(result["task"]["due_date"])
        code, _, stale = self.request("PATCH", path, {"title": "旧窗口", "expected_version": 1})
        self.assertEqual(409, code, stale)
        code, _, deleted = self.request("DELETE", path, {"expected_version": result["task"]["version"]})
        self.assertEqual(200, code, deleted)
        self.assertEqual([], self.request("GET", "/api/tasks")[2]["tasks"])
        self.assertEqual(1, len(self.request("GET", "/api/tasks?deleted=true")[2]["tasks"]))
        overview = self.request("GET", "/api/overview?date=2026-10-27")[2]
        self.assertEqual(0, overview["counts"]["tasks"])
        self.assertEqual(1, len(overview["deleted_tasks"]))
        code, _, restored = self.request("POST", path + "/restore", {"expected_version": deleted["task"]["version"]})
        self.assertEqual(200, code, restored)
        self.assertEqual("已编辑", restored["task"]["title"])
        self.assertFalse(restored["task"]["completed"])

    def test_schedule_requires_confirm_and_replays_without_duplicate(self):
        task = self.task()
        self.request("GET", "/api/free-slots?start=2026-10-27&end=2026-10-28&duration_minutes=30")
        self.assertEqual([], self.events())
        first = self.schedule(task)
        self.assertEqual(200, first[0], first)
        second = self.schedule(task)
        self.assertEqual(200, second[0], second)
        self.assertEqual(first[2]["event"]["id"], second[2]["event"]["id"])
        self.assertEqual(1, len(self.events()))
        self.assertFalse(second[2]["task"]["completed"])
        self.assertEqual(2, second[2]["task"]["version"])
        changed = self.schedule(task, hour=16)
        self.assertEqual(409, changed[0], changed)
        self.assertEqual("request_id_conflict", changed[2]["error"]["code"])

    def test_parallel_different_requests_cannot_schedule_one_task_twice(self):
        task = self.task()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.schedule, task, "parallel-%s" % i, 15 + i) for i in range(2)]
            results = [f.result() for f in futures]
        self.assertEqual([200, 409], sorted(r[0] for r in results), results)
        self.assertEqual(1, len(self.events()))

    def test_conflict_releases_reservation_and_does_not_complete_task(self):
        self.request("POST", "/api/events", {"request_id": "busy", "title": "占用", "start": "2026-10-27T15:00", "end": "2026-10-27T16:00"})
        task = self.task()
        failed = self.schedule(task)
        self.assertEqual(409, failed[0], failed)
        self.assertEqual("conflict", failed[2]["error"]["code"])
        self.assertEqual("failed", self.server.application.store.get_task(task["id"])["schedule"]["status"])
        success = self.schedule(task, "new-slot", 16)
        self.assertEqual(200, success[0], success)
        self.assertFalse(success[2]["task"]["completed"])

    def test_deleted_completed_and_stale_tasks_do_not_create_events(self):
        for operation in ("delete", "complete", "edit"):
            task = self.task(operation)
            path = "/api/tasks/" + task["id"]
            if operation == "delete":
                self.request("DELETE", path, {"expected_version": task["version"]})
            else:
                self.request("PATCH", path, {"completed": True} if operation == "complete" else {"title": "变更", "expected_version": task["version"]})
            result = self.schedule(task, operation)
            self.assertIn(result[0], (404, 409), result)
        self.assertEqual([], self.events())

    def test_crash_after_calendar_commit_is_reconciled_without_writing_again(self):
        task = self.task()
        store = self.server.application.store
        with patch.object(store, "finish_plan", side_effect=OSError("simulated interrupted task link")):
            result = self.schedule(task)
        self.assertEqual(503, result[0], result)
        self.assertEqual(1, len(self.events()))
        cal = self.server.application.calendar
        with patch.object(cal, "call", wraps=cal.call) as calls:
            code, _, result = self.request("GET", "/api/tasks/" + task["id"] + "/schedule")
        self.assertEqual(200, code, result)
        self.assertEqual("applied", result["schedule"]["status"])
        self.assertFalse(any(call.args[0] == "calendar_create_event" for call in calls.call_args_list))
        self.assertEqual(1, len(self.events()))

    def test_pending_resumes_exact_request_and_frozen_timezone(self):
        task = self.task()
        cal = self.server.application.calendar
        original = cal.call
        def interrupted(name, args):
            if name == "calendar_create_event":
                raise OSError("before calendar write")
            return original(name, args)
        with patch.object(cal, "call", side_effect=interrupted):
            self.assertEqual(503, self.schedule(task)[0])
        path = "/api/tasks/" + task["id"]
        self.assertEqual(409, self.request("DELETE", path, {"expected_version": task["version"]})[0])
        self.request("PUT", "/api/preferences", {"timezone": "UTC"})
        code, _, resumed = self.request("POST", path + "/schedule/retry", {})
        self.assertEqual(200, code, resumed)
        self.assertEqual("2026-10-27T07:00:00Z", resumed["event"]["start"])
        self.assertEqual(1, len(self.events()))

    def test_cancel_calendar_event_allows_new_plan_and_task_delete_keeps_event(self):
        task = self.task()
        code, _, first = self.schedule(task)
        self.assertEqual(200, code, first)
        event = first["event"]
        self.request("DELETE", "/api/events/" + event["id"], {"request_id": "cancel-linked", "expected_version": event["version"]})
        second = self.schedule(first["task"], "after-cancel", 16)
        self.assertEqual(200, second[0], second)
        self.request("DELETE", "/api/tasks/" + task["id"], {"expected_version": second[2]["task"]["version"]})
        self.assertEqual(1, len(self.events()))

    def test_new_routes_keep_csrf_validation_and_reject_unrequested_fields(self):
        task = self.task()
        path = "/api/tasks/" + task["id"]
        for method, suffix, payload in (("DELETE", "", {}), ("POST", "/restore", {}), ("POST", "/schedule/retry", {})):
            self.assertEqual(403, self.request(method, path + suffix, payload, headers={"X-Agent-Token": None})[0])
        self.assertEqual(400, self.schedule(task, title="不能伪造另一个标题")[0])
        self.assertEqual(400, self.request("GET", "/api/tasks?deleted=yes")[0])
        self.assertEqual([], self.events())

    def test_new_task_edits_and_trash_require_a_version(self):
        task = self.task()
        path = "/api/tasks/" + task["id"]
        for body in ({"title": "未带版本"}, {"title": "错误版本", "expected_version": True}):
            self.assertEqual(400, self.request("PATCH", path, body)[0])
        self.assertEqual(400, self.request("DELETE", path, {})[0])
        self.assertEqual(200, self.request("PATCH", path, {"completed": True})[0])

    def test_task_schedule_does_not_claim_external_backend_support(self):
        task = self.task()
        with patch.object(self.server.application.calendar, "backend", object()):
            code, _, result = self.schedule(task)
        self.assertEqual(400, code, result)
        self.assertEqual("unsupported_capability", result["error"]["code"])
        self.assertEqual([], self.events())

    def test_restart_recovers_calendar_commit_from_persisted_request(self):
        task = self.task()
        with patch.object(self.server.application.store, "finish_plan", side_effect=OSError("crash")):
            self.assertEqual(503, self.schedule(task)[0])
        from personal_agent.webapp import Application
        restarted = Application(self.temp.name, model_client=test_webapp.OfflineModel())
        try:
            result = restarted.task_planner.reconcile(task["id"])
            self.assertEqual("applied", result["schedule"]["status"])
            self.assertEqual(2, result["task"]["version"])
            self.assertEqual(1, len(self.events()))
        finally:
            restarted.close()
