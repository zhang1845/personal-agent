"""Scope regressions use temporary calendars and never contact a real account."""
from pathlib import Path
import tempfile
import unittest

from personal_agent.errors import AgentError
from personal_agent.service import CalendarService
from test_service import FakeRemoteBackend
import test_webapp as http_helpers


class OrdinaryEventScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ordinary-scope-")
        self.path = Path(self.temp.name) / "calendar.sqlite3"
        self.calendar = CalendarService(self.path)
        self.event = self.create("target", "10:00", "11:00")
        self.other = self.create("untouched", "14:00", "15:00")

    def tearDown(self):
        self.calendar.close()
        self.temp.cleanup()

    def create(self, request, start, end):
        return self.calendar.call("calendar_create_event", {
            "request_id": request, "title": request,
            "start": "2030-01-07T" + start + ":00+08:00",
            "end": "2030-01-07T" + end + ":00+08:00",
        })["event"]

    def args(self, request, **extra):
        return dict(request_id=request, event_id=self.event["id"],
                    expected_version=self.event["version"], scope="occurrence", **extra)

    def test_delete_explicit_occurrence_replays_omitted_default_after_restart(self):
        args = self.args("cancel-one")
        result = self.calendar.call("calendar_delete_event", args)
        self.assertTrue(result["deleted"])
        self.assertFalse(result["replayed"])
        self.assertEqual("occurrence", args["scope"])
        self.calendar.close()
        self.calendar = CalendarService(self.path)
        replay = dict(args)
        replay.pop("scope")
        self.assertTrue(self.calendar.call("calendar_delete_event", replay)["replayed"])
        self.assertTrue(self.calendar.call("calendar_delete_event", args)["replayed"])
        self.assertEqual(self.other, self.calendar.call("calendar_get_event", {"event_id": self.other["id"]})["event"])
        with self.assertRaises(AgentError) as caught:
            self.calendar.call("calendar_get_event", {"event_id": self.event["id"]})
        self.assertEqual("not_found", caught.exception.code)

    def test_update_accepts_default_without_changing_identity_or_other_event(self):
        args = self.args("move-one", changes={"start": "2030-01-07T11:00:00+08:00", "end": "2030-01-07T12:00:00+08:00"})
        result = self.calendar.call("calendar_update_event", args)
        self.assertEqual(self.event["id"], result["event"]["id"])
        self.assertEqual("2030-01-07T03:00:00Z", result["event"]["start"])
        self.assertEqual("occurrence", args["scope"])
        replay = dict(args)
        replay.pop("scope")
        self.assertTrue(self.calendar.call("calendar_update_event", replay)["replayed"])
        self.assertEqual(self.other, self.calendar.call("calendar_get_event", {"event_id": self.other["id"]})["event"])

    def test_series_and_invalid_scopes_still_reject_without_mutation(self):
        for name in ("calendar_update_event", "calendar_delete_event"):
            for scope in ("series", "all", "", False, 1, [], {}):
                with self.subTest(tool=name, scope=scope):
                    args = self.args("bad-scope")
                    args["scope"] = scope
                    if name == "calendar_update_event":
                        args["changes"] = {"title": "must not change"}
                    with self.assertRaises(AgentError) as caught:
                        self.calendar.call(name, args)
                    self.assertEqual("validation_error", caught.exception.code)
        self.assertEqual(self.event, self.calendar.call("calendar_get_event", {"event_id": self.event["id"]})["event"])

    def test_explicit_occurrence_does_not_bypass_latest_version_check(self):
        self.calendar.call("calendar_update_event", self.args("edit-first", changes={"title": "new version"}))
        with self.assertRaises(AgentError) as caught:
            self.calendar.call("calendar_delete_event", self.args("stale-delete"))
        self.assertEqual("version_conflict", caught.exception.code)
        self.assertEqual("new version", self.calendar.call("calendar_get_event", {"event_id": self.event["id"]})["event"]["title"])

    def test_remote_delete_occurrence_is_sent_once_across_default_variants(self):
        backend = FakeRemoteBackend()
        remote = CalendarService(Path(self.temp.name) / "remote.sqlite3", backend=backend)
        try:
            event = remote.call("calendar_create_event", {"request_id": "remote-create", "title": "remote test",
                "start": "2030-01-07T10:00:00+08:00", "end": "2030-01-07T11:00:00+08:00"})["event"]
            args = dict(request_id="remote-delete", event_id=event["id"], expected_version=event["version"])
            self.assertTrue(remote.call("calendar_delete_event", args)["deleted"])
            self.assertTrue(remote.call("calendar_delete_event", dict(args, scope="occurrence"))["replayed"])
            self.assertEqual(1, backend.delete_calls)
        finally:
            remote.close()

    def test_unknown_remote_delete_is_not_retried_with_explicit_default(self):
        class LostDeleteResponse(FakeRemoteBackend):
            def delete_event(self, *args):
                super().delete_event(*args)
                raise AgentError("unknown_outcome", "Response lost after delete")
        backend = LostDeleteResponse()
        remote = CalendarService(Path(self.temp.name) / "unknown.sqlite3", backend=backend)
        try:
            event = remote.call("calendar_create_event", {"request_id": "remote-create", "title": "remote test",
                "start": "2030-01-07T10:00:00+08:00", "end": "2030-01-07T11:00:00+08:00"})["event"]
            args = dict(request_id="remote-delete", event_id=event["id"], expected_version=event["version"])
            for variant in (dict(args, scope="occurrence"), args):
                with self.assertRaises(AgentError) as caught:
                    remote.call("calendar_delete_event", variant)
                self.assertEqual("unknown_outcome", caught.exception.code)
            self.assertEqual(1, backend.delete_calls)
        finally:
            remote.close()


class OrdinaryScopeHTTPTests(unittest.TestCase):
    setUp = http_helpers.WebappTests.setUp
    tearDown = http_helpers.WebappTests.tearDown
    request = http_helpers.WebappTests.request

    def test_real_http_delete_accepts_occurrence_and_replays_without_scope(self):
        code, _, created = self.request("POST", "/api/events", http_helpers.WebappTests.create_arguments())
        self.assertEqual(200, code, created)
        event = created["event"]
        path = "/api/events/" + event["id"]
        args = {"request_id": "delete-default", "expected_version": event["version"], "scope": "occurrence"}
        code, _, result = self.request("DELETE", path, args)
        self.assertEqual(200, code, result)
        self.assertTrue(result["deleted"])
        args.pop("scope")
        code, _, replayed = self.request("DELETE", path, args)
        self.assertEqual(200, code, replayed)
        self.assertTrue(replayed["replayed"])


if __name__ == "__main__":
    unittest.main()
