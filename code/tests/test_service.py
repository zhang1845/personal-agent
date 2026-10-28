"""Behavioral tests for the deterministic calendar boundary.

All state lives in temporary SQLite databases. No test starts a real calendar
connector, uses credentials, or contacts a model service.
"""

import copy
import multiprocessing
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from personal_agent.errors import AgentError
from personal_agent.service import CalendarService


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def simultaneous_create(db_path, number, ready, proceed, results):
    """Separate processes must compete on the database, not a Python lock."""
    service = CalendarService(db_path)
    ready.put(number)
    if not proceed.wait(15):
        results.put(("worker_timeout", number))
        return
    try:
        result = service.call("calendar_create_event", {
            "request_id": "worker-{}".format(number),
            "title": "Concurrent booking {}".format(number),
            "start": "2026-10-27T10:00:00+08:00",
            "end": "2026-10-27T11:00:00+08:00",
        })
        results.put(("created", result["event"]["id"]))
    except AgentError as exc:
        results.put((exc.code, number))
    except Exception as exc:
        results.put(("unexpected", type(exc).__name__))


class FakeRemoteBackend:
    kind = "fake_remote"
    capabilities = {
        "remote": True,
        "supports_reminders": True,
        "supports_recurrence": False,
    }

    def __init__(self, outcome="success"):
        self.outcome = outcome
        self.events = {}
        self.create_calls = 0
        self.update_calls = 0
        self.delete_calls = 0
        self.list_error = None

    def list_calendars(self):
        return [{"id": "remote", "name": "Fake remote", "writable": True}]

    def list_events(self, start, end, calendar_id):
        if self.list_error:
            raise self.list_error
        return [copy.deepcopy(event) for event in self.events.values()
                if event["calendar_id"] == calendar_id
                and instant(event["start"]) < instant(end)
                and instant(start) < instant(event["end"])]

    def get_event(self, event_id):
        if event_id not in self.events:
            raise AgentError("not_found", "Not found")
        return copy.deepcopy(self.events[event_id])

    def create_event(self, event, request_id):
        self.create_calls += 1
        stored = copy.deepcopy(event)
        stored.update(id="remote-{}".format(self.create_calls), version="1")
        self.events[stored["id"]] = stored
        if self.outcome == "unknown":
            # The provider changed state, but its response was lost.
            raise AgentError("unknown_outcome", "Connection closed after send")
        return copy.deepcopy(stored)

    def update_event(self, event_id, event, expected_version, request_id):
        self.update_calls += 1
        current = self.get_event(event_id)
        if current["version"] != expected_version:
            raise AgentError("version_conflict", "Concurrent change")
        current.update(copy.deepcopy(event))
        current["id"] = event_id
        current["version"] = str(int(expected_version) + 1)
        self.events[event_id] = current
        return copy.deepcopy(current)

    def delete_event(self, event_id, expected_version, request_id):
        self.delete_calls += 1
        current = self.get_event(event_id)
        if current["version"] != expected_version:
            raise AgentError("version_conflict", "Concurrent change")
        del self.events[event_id]
        return {"deleted": True, "event_id": event_id}

    def close(self):
        pass


class CalendarServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="calendar-test-")
        self.db = str(Path(self.tmp.name) / "calendar.sqlite3")
        self.service = CalendarService(self.db)
        self.counter = 0

    def tearDown(self):
        close = getattr(self.service, "close", None)
        if close:
            close()
        self.tmp.cleanup()

    def call(self, name, **arguments):
        return self.service.call(name, arguments)

    def create(self, **overrides):
        self.counter += 1
        values = {
            "request_id": "test-{}".format(self.counter),
            "title": "方案评审",
            "start": "2026-10-27T10:00:00+08:00",
            "end": "2026-10-27T11:00:00+08:00",
        }
        values.update(overrides)
        return self.service.call("calendar_create_event", values)

    def expect_error(self, code, name, **arguments):
        with self.assertRaises(AgentError) as caught:
            self.service.call(name, arguments)
        self.assertEqual(code, caught.exception.code)
        return caught.exception

    def events(self, start="2026-10-27T00:00:00+08:00",
               end="2026-10-28T00:00:00+08:00"):
        return self.call("calendar_list_events", start=start, end=end)["events"]

    def test_status_does_not_claim_remote_calendar_or_phone_reminders(self):
        status = self.call("calendar_status")
        self.assertIsInstance(status, dict)
        self.assertNotEqual(status.get("backend"), "icloud")
        self.assertIn("local", str(status).lower())
        self.assertIn("local", [c["id"] for c in self.call("calendar_list_calendars")["calendars"]])

    def test_create_read_and_persist_across_service_instances(self):
        event = self.create()["event"]
        self.assertTrue(event["id"])
        self.assertIsInstance(event["version"], str)
        self.assertEqual("local", event["calendar_id"])
        self.assertEqual(instant("2026-10-27T02:00:00Z"), instant(event["start"]))
        reopened = CalendarService(self.db)
        self.assertEqual(event, reopened.call("calendar_get_event", {"event_id": event["id"]})["event"])

    def test_half_open_query_excludes_touching_event(self):
        self.create()
        self.assertEqual([], self.events("2026-10-27T11:00:00+08:00", "2026-10-27T12:00:00+08:00"))
        self.assertEqual([], self.events("2026-10-27T09:00:00+08:00", "2026-10-27T10:00:00+08:00"))
        self.assertEqual(1, len(self.events("2026-10-27T10:59:00+08:00", "2026-10-27T11:01:00+08:00")))

    def test_microsecond_query_does_not_drop_partially_overlapping_event(self):
        event = self.create(start="2026-10-27T10:00:01+08:00", end="2026-10-27T10:00:02+08:00")["event"]
        found = self.events("2026-10-27T10:00:00.500000+08:00", "2026-10-27T10:00:01.500000+08:00")
        self.assertEqual([event["id"]], [item["id"] for item in found])

    def test_subsecond_event_preserves_positive_duration(self):
        event = self.create(start="2026-10-27T10:00:00.100000+08:00", end="2026-10-27T10:00:00.200000+08:00")["event"]
        self.assertEqual(timedelta(microseconds=100000), instant(event["end"]) - instant(event["start"]))
        found = self.events("2026-10-27T10:00:00.150000+08:00", "2026-10-27T10:00:00.250000+08:00")
        self.assertEqual([event["id"]], [item["id"] for item in found])

    def test_adjacent_events_are_allowed(self):
        self.create()
        self.create(start="2026-10-27T11:00:00+08:00", end="2026-10-27T12:00:00+08:00")
        self.assertEqual(2, len(self.events()))

    def test_overlap_is_rejected_without_creating_an_event(self):
        self.create()
        self.expect_error("conflict", "calendar_create_event", request_id="overlap",
                          title="冲突", start="2026-10-27T10:30:00+08:00", end="2026-10-27T11:30:00+08:00")
        self.assertEqual(1, len(self.events()))

    def test_conflict_detection_compares_instants_not_offset_strings(self):
        self.create()
        self.expect_error("conflict", "calendar_create_event", request_id="utc-overlap",
                          title="UTC冲突", start="2026-10-27T02:30:00Z", end="2026-10-27T03:30:00Z")

    def test_idempotent_create_survives_restart(self):
        args = {"request_id": "same-request", "title": "去重",
                "start": "2026-10-27T10:00:00+08:00", "end": "2026-10-27T11:00:00+08:00"}
        first = self.service.call("calendar_create_event", args)
        reopened = CalendarService(self.db)
        second = reopened.call("calendar_create_event", args)
        self.assertEqual(first["event"], second["event"])
        self.assertEqual(1, len(self.events()))

    def test_same_request_with_changed_payload_is_rejected(self):
        self.create(request_id="dedup")
        self.expect_error("request_id_conflict", "calendar_create_event", request_id="dedup",
                          title="不同标题", start="2026-10-27T10:00:00+08:00", end="2026-10-27T11:00:00+08:00")
        self.assertEqual("方案评审", self.events()[0]["title"])

    def test_request_id_cannot_be_reused_for_another_operation(self):
        event = self.create(request_id="cross-operation")["event"]
        self.expect_error("request_id_conflict", "calendar_delete_event", request_id="cross-operation",
                          event_id=event["id"], expected_version=event["version"])
        self.assertEqual(1, len(self.events()))

    def test_update_changes_version_and_keeps_event_identity(self):
        old = self.create()["event"]
        updated = self.call("calendar_update_event", request_id="update-1", event_id=old["id"],
                            expected_version=old["version"], changes={"title": "新的标题"})["event"]
        self.assertEqual(old["id"], updated["id"])
        self.assertNotEqual(old["version"], updated["version"])
        self.assertEqual("新的标题", updated["title"])

    def test_stale_version_cannot_overwrite_newer_changes(self):
        old = self.create()["event"]
        self.call("calendar_update_event", request_id="update-first", event_id=old["id"],
                  expected_version=old["version"], changes={"title": "第一个修改"})
        self.expect_error("version_conflict", "calendar_update_event", request_id="update-stale",
                          event_id=old["id"], expected_version=old["version"], changes={"title": "过期修改"})
        self.assertEqual("第一个修改", self.events()[0]["title"])

    def test_update_excludes_itself_from_conflicts(self):
        old = self.create()["event"]
        changed = self.call("calendar_update_event", request_id="same-slot", event_id=old["id"],
                            expected_version=old["version"], changes={"location": "会议室A"})["event"]
        self.assertEqual("会议室A", changed["location"])

    def test_reschedule_to_conflicting_slot_preserves_original(self):
        old = self.create()["event"]
        self.create(start="2026-10-27T14:00:00+08:00", end="2026-10-27T15:00:00+08:00")
        self.expect_error("conflict", "calendar_update_event", request_id="move-conflict", event_id=old["id"],
                          expected_version=old["version"], changes={"start": "2026-10-27T14:30:00+08:00", "end": "2026-10-27T15:30:00+08:00"})
        actual = self.call("calendar_get_event", event_id=old["id"])["event"]
        self.assertEqual(instant(old["start"]), instant(actual["start"]))
        self.assertEqual(old["version"], actual["version"])

    def test_delete_is_idempotent_but_unknown_id_is_not_success(self):
        event = self.create()["event"]
        args = dict(request_id="delete-once", event_id=event["id"], expected_version=event["version"])
        self.assertTrue(self.call("calendar_delete_event", **args)["deleted"])
        self.assertTrue(self.call("calendar_delete_event", **args)["deleted"])
        self.assertEqual([], self.events())
        self.expect_error("not_found", "calendar_get_event", event_id=event["id"])
        self.expect_error("not_found", "calendar_delete_event", request_id="unknown-delete",
                          event_id="not-an-event", expected_version="1")

    def test_relative_or_invalid_time_is_rejected(self):
        for start, end in [("明天下午", "明天晚上"), ("2026-02-30T10:00", "2026-02-30T11:00"),
                           ("2026-10-27T11:00", "2026-10-27T10:00"),
                           ("2026-10-27T10:00", "2026-10-27T10:00")]:
            with self.subTest(start=start, end=end):
                with self.assertRaises(AgentError) as caught:
                    self.create(start=start, end=end)
                self.assertEqual("validation_error", caught.exception.code)
        self.assertEqual([], self.events())

    def test_naive_datetime_uses_personal_timezone(self):
        event = self.create(start="2026-10-27T10:00", end="2026-10-27T11:00")["event"]
        self.assertEqual(instant("2026-10-27T02:00:00Z"), instant(event["start"]))

    def test_all_day_event_end_is_exclusive(self):
        event = self.create(start="2026-10-27", end="2026-10-28", all_day=True)["event"]
        self.assertTrue(event["all_day"])
        self.assertEqual(timedelta(days=1), instant(event["end"]) - instant(event["start"]))
        self.assertEqual([], self.events("2026-10-28T00:00:00+08:00", "2026-10-29T00:00:00+08:00"))

    def test_local_reminder_is_explicitly_unsupported(self):
        with self.assertRaises(AgentError) as caught:
            self.create(reminder_minutes=15)
        self.assertEqual("unsupported_capability", caught.exception.code)
        self.assertEqual([], self.events())

    def test_empty_title_and_missing_request_id_do_not_write(self):
        for overrides in [{"title": "  "}, {"request_id": ""}]:
            with self.subTest(overrides=overrides):
                with self.assertRaises(AgentError) as caught:
                    self.create(**overrides)
                self.assertEqual("validation_error", caught.exception.code)
        self.assertEqual([], self.events())

    def test_unknown_calendar_and_missing_event_fail(self):
        with self.assertRaises(AgentError):
            self.create(calendar_id="unconfigured")
        self.expect_error("not_found", "calendar_get_event", event_id="missing")

    def test_large_query_is_rejected(self):
        with self.assertRaises(AgentError):
            self.call("calendar_list_events", start="2026-01-01T00:00:00Z", end="2026-03-01T00:00:00Z")

    def test_preferences_validate_and_persist(self):
        prefs = self.call("calendar_get_preferences")["preferences"]
        self.assertEqual("Asia/Shanghai", prefs["timezone"])
        new = self.call("calendar_set_preferences", work_start="10:00", buffer_minutes=20)["preferences"]
        self.assertEqual("10:00", new["work_start"])
        self.assertEqual(20, new["buffer_minutes"])
        reopened = CalendarService(self.db)
        self.assertEqual(new, reopened.call("calendar_get_preferences", {})["preferences"])
        for change in [{"timezone": "Mars/Olympus"}, {"workdays": [7]},
                       {"buffer_minutes": -1}, {"work_start": "19:00", "work_end": "18:00"}]:
            with self.subTest(change=change):
                with self.assertRaises(AgentError):
                    self.call("calendar_set_preferences", **change)
        self.assertEqual(new, self.call("calendar_get_preferences")["preferences"])

    def test_dst_gap_and_fold_require_an_explicit_offset(self):
        self.call("calendar_set_preferences", timezone="America/New_York")
        for start, end in [("2026-03-08T02:15", "2026-03-08T03:15"),
                           ("2026-11-01T01:15", "2026-11-01T02:15")]:
            with self.subTest(start=start):
                with self.assertRaises(AgentError) as caught:
                    self.create(start=start, end=end)
                self.assertEqual("validation_error", caught.exception.code)
        event = self.create(start="2026-11-01T01:15:00-04:00", end="2026-11-01T01:45:00-04:00")["event"]
        self.assertEqual(instant("2026-11-01T05:15:00Z"), instant(event["start"]))

    def test_free_slots_respect_workday_lunch_and_buffer(self):
        self.create()
        slots = self.call("calendar_find_free_slots", start="2026-10-27T00:00:00+08:00",
                          end="2026-10-28T00:00:00+08:00", duration_minutes=60, limit=20)["slots"]
        self.assertTrue(slots)
        for slot in slots:
            start, end = instant(slot["start"]), instant(slot["end"])
            self.assertEqual(timedelta(hours=1), end - start)
            self.assertGreaterEqual(start, instant("2026-10-27T09:00:00+08:00"))
            self.assertLessEqual(end, instant("2026-10-27T18:00:00+08:00"))
            self.assertFalse(start < instant("2026-10-27T13:00:00+08:00") and instant("2026-10-27T12:00:00+08:00") < end)
            self.assertFalse(start < instant("2026-10-27T11:15:00+08:00") and instant("2026-10-27T09:45:00+08:00") < end)

    def test_weekend_and_too_short_window_have_no_slots(self):
        result = self.call("calendar_find_free_slots", start="2026-10-31T00:00:00+08:00",
                           end="2026-11-01T00:00:00+08:00", duration_minutes=30)
        self.assertEqual([], result["slots"])
        result = self.call("calendar_find_free_slots", start="2026-10-27T09:00:00+08:00",
                           end="2026-10-27T09:20:00+08:00", duration_minutes=30)
        self.assertEqual([], result["slots"])

    def test_free_slot_does_not_round_outside_microsecond_query(self):
        start, end = "2026-10-27T09:00:00.500000+08:00", "2026-10-27T09:05:00.500000+08:00"
        result = self.call("calendar_find_free_slots", start=start, end=end, duration_minutes=5)
        self.assertEqual(1, len(result["slots"]))
        slot = result["slots"][0]
        self.assertEqual(instant(start), instant(slot["start"]))
        self.assertEqual(instant(end), instant(slot["end"]))

    def test_invalid_duration_is_not_silently_coerced(self):
        for value in [0, -5, True, 1.5, "30"]:
            with self.subTest(value=value):
                with self.assertRaises(AgentError):
                    self.call("calendar_find_free_slots", start="2026-10-27T09:00:00+08:00",
                              end="2026-10-27T18:00:00+08:00", duration_minutes=value)

    def test_unknown_tool_fails_instead_of_returning_empty_data(self):
        with self.assertRaises(AgentError):
            self.call("not_a_tool")

    def test_parallel_processes_cannot_double_book(self):
        context = multiprocessing.get_context("spawn")
        ready, results = context.Queue(), context.Queue()
        proceed = context.Event()
        workers = [context.Process(target=simultaneous_create,
                                   args=(self.db, i, ready, proceed, results)) for i in range(2)]
        try:
            for worker in workers:
                worker.start()
            for _ in workers:
                ready.get(timeout=15)
            proceed.set()
            outcomes = [results.get(timeout=15)[0] for _ in workers]
            self.assertEqual(["conflict", "created"], sorted(outcomes))
        finally:
            for worker in workers:
                worker.join(timeout=5)
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=5)
        self.assertEqual(1, len(self.events()))


class RemoteOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="remote-outcome-test-")
        self.db = str(Path(self.tmp.name) / "calendar.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def create_arguments(request_id="remote-once"):
        return {"request_id": request_id, "title": "Remote event", "calendar_id": "remote",
                "start": "2026-10-27T10:00:00+08:00", "end": "2026-10-27T11:00:00+08:00"}

    def test_unknown_remote_write_is_not_reissued_after_restart(self):
        backend = FakeRemoteBackend(outcome="unknown")
        service = CalendarService(self.db, backend=backend)
        args = self.create_arguments()
        with self.assertRaises(AgentError) as caught:
            service.call("calendar_create_event", args)
        self.assertEqual("unknown_outcome", caught.exception.code)
        reopened = CalendarService(self.db, backend=backend)
        with self.assertRaises(AgentError) as caught:
            reopened.call("calendar_create_event", args)
        self.assertEqual("unknown_outcome", caught.exception.code)
        self.assertEqual(1, backend.create_calls)
        self.assertEqual(1, len(backend.events))

    def test_conflict_read_failure_does_not_become_an_empty_calendar(self):
        backend = FakeRemoteBackend()
        backend.list_error = AgentError("backend_unavailable", "Cannot read current events")
        service = CalendarService(self.db, backend=backend)
        with self.assertRaises(AgentError):
            service.call("calendar_create_event", self.create_arguments())
        self.assertEqual(0, backend.create_calls)

    def test_event_limit_must_not_silently_hide_busy_time(self):
        backend = FakeRemoteBackend()
        base = datetime(2026, 10, 27, 1, tzinfo=timezone.utc)
        for index in range(201):
            start = base + timedelta(minutes=index)
            backend.events[str(index)] = {
                "id": str(index), "version": "1", "title": "Busy",
                "start": start.isoformat(), "end": (start + timedelta(minutes=1)).isoformat(),
                "calendar_id": "remote", "all_day": False, "recurring": False,
                "location": "", "description": "", "reminder_minutes": None,
            }
        service = CalendarService(self.db, backend=backend)
        with self.assertRaises(AgentError):
            service.call("calendar_list_events", {
                "start": "2026-10-27T00:00:00Z", "end": "2026-10-28T00:00:00Z",
                "calendar_id": "remote",
            })
        with self.assertRaises(AgentError):
            service.call("calendar_find_free_slots", {
                "start": "2026-10-27T00:00:00Z", "end": "2026-10-28T00:00:00Z",
                "duration_minutes": 30,
            })

    def test_recurring_remote_event_is_not_changed_as_a_series(self):
        backend = FakeRemoteBackend()
        event = dict(self.create_arguments())
        event.pop("request_id")
        event.update(id="series-1", version="1", recurring=True, all_day=False,
                     location="", description="", reminder_minutes=None)
        backend.events[event["id"]] = event
        service = CalendarService(self.db, backend=backend)
        with self.assertRaises(AgentError) as caught:
            service.call("calendar_update_event", {"request_id": "series-change", "event_id": "series-1",
                                                     "expected_version": "1", "changes": {"title": "Do not edit all"}})
        self.assertEqual("unsupported_capability", caught.exception.code)
        self.assertEqual(0, backend.update_calls)

    def test_changed_location_or_reminder_is_not_reported_as_success(self):
        for field, requested, returned in [("location", "Room A", "Room B"),
                                           ("reminder_minutes", 15, 30),
                                           ("description", "Bring report", "")]:
            with self.subTest(field=field):
                backend = FakeRemoteBackend()
                original = backend.create_event

                def mismatched_response(event, request_id):
                    result = original(event, request_id)
                    result[field] = returned
                    return result

                backend.create_event = mismatched_response
                args = self.create_arguments("mismatch-" + field)
                args[field] = requested
                service = CalendarService(self.db, backend=backend)
                with self.assertRaises(AgentError) as caught:
                    service.call("calendar_create_event", args)
                self.assertEqual("unknown_outcome", caught.exception.code)
                with self.assertRaises(AgentError) as retry:
                    service.call("calendar_create_event", args)
                self.assertEqual("unknown_outcome", retry.exception.code)
                self.assertEqual(1, backend.create_calls)

    def test_missing_write_fields_and_empty_version_are_unknown(self):
        for field in ["location", "description", "reminder_minutes", "version"]:
            with self.subTest(field=field):
                backend = FakeRemoteBackend()
                original = backend.create_event

                def incomplete_response(event, request_id):
                    result = original(event, request_id)
                    if field == "version":
                        result["version"] = ""
                    else:
                        result.pop(field, None)
                    return result

                backend.create_event = incomplete_response
                args = self.create_arguments("missing-" + field)
                args.update(location="Room A", description="Bring report", reminder_minutes=15)
                service = CalendarService(self.db, backend=backend)
                for _ in range(2):
                    with self.assertRaises(AgentError) as caught:
                        service.call("calendar_create_event", args)
                    self.assertEqual("unknown_outcome", caught.exception.code)
                self.assertEqual(1, backend.create_calls)

    def test_read_only_rejection_does_not_reserve_request(self):
        backend = FakeRemoteBackend()
        backend.capabilities = dict(backend.capabilities, writes_enabled=False)
        service = CalendarService(self.db, backend=backend)
        args = self.create_arguments("read-only-first")
        with self.assertRaises(AgentError) as caught:
            service.call("calendar_create_event", args)
        self.assertEqual("unsupported_capability", caught.exception.code)
        self.assertEqual(0, backend.create_calls)
        backend.capabilities["writes_enabled"] = True
        result = service.call("calendar_create_event", args)
        self.assertEqual("remote-1", result["event"]["id"])
        self.assertEqual(1, backend.create_calls)

    def test_update_response_with_another_event_id_is_not_success(self):
        backend = FakeRemoteBackend()
        service = CalendarService(self.db, backend=backend)
        event = service.call("calendar_create_event", self.create_arguments())["event"]
        original = backend.update_event

        def mismatched_response(event_id, event, expected_version, request_id):
            result = original(event_id, event, expected_version, request_id)
            result["id"] = "a-different-event"
            return result

        backend.update_event = mismatched_response
        with self.assertRaises(AgentError) as caught:
            service.call("calendar_update_event", {
                "request_id": "update-wrong-response", "event_id": event["id"],
                "expected_version": event["version"], "changes": {"title": "Changed title"},
            })
        self.assertEqual("unknown_outcome", caught.exception.code)
        with self.assertRaises(AgentError) as retry:
            service.call("calendar_update_event", {
                "request_id": "update-wrong-response", "event_id": event["id"],
                "expected_version": event["version"], "changes": {"title": "Changed title"},
            })
        self.assertEqual("unknown_outcome", retry.exception.code)
        self.assertEqual(1, backend.update_calls)

    def test_wrong_get_event_identity_is_rejected_before_update(self):
        backend = FakeRemoteBackend()
        base = {"version": "1", "title": "Existing", "calendar_id": "remote", "all_day": False,
                "recurring": False, "location": "", "description": "", "reminder_minutes": None}
        backend.events = {
            "A": dict(base, id="A", start="2026-10-27T09:00:00+08:00", end="2026-10-27T10:00:00+08:00"),
            "B": dict(base, id="B", start="2026-10-27T14:00:00+08:00", end="2026-10-27T15:00:00+08:00"),
        }
        original = backend.get_event
        backend.get_event = lambda event_id: original("B" if event_id == "A" else event_id)
        service = CalendarService(self.db, backend=backend)
        with self.assertRaises(AgentError) as caught:
            service.call("calendar_update_event", {
                "request_id": "update-wrong-read", "event_id": "A", "expected_version": "1",
                "changes": {"location": "New location"},
            })
        self.assertEqual("backend_unavailable", caught.exception.code)
        self.assertEqual(0, backend.update_calls)
        self.assertEqual("2026-10-27T09:00:00+08:00", backend.events["A"]["start"])

    def test_event_order_uses_instants_when_offsets_differ(self):
        backend = FakeRemoteBackend()
        base = {"version": "1", "title": "Event", "calendar_id": "remote", "all_day": False,
                "recurring": False, "location": "", "description": "", "reminder_minutes": None}
        backend.events = {
            "early": dict(base, id="early", start="2026-10-27T08:00:00+08:00", end="2026-10-27T09:00:00+08:00"),
            "late": dict(base, id="late", start="2026-10-27T02:00:00Z", end="2026-10-27T03:00:00Z"),
        }
        service = CalendarService(self.db, backend=backend)
        actual = service.call("calendar_list_events", {
            "start": "2026-10-27T00:00:00Z", "end": "2026-10-27T04:00:00Z", "calendar_id": "remote",
        })["events"]
        self.assertEqual(["early", "late"], [event["id"] for event in actual])


if __name__ == "__main__":
    unittest.main()
