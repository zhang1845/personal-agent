from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from personal_agent.errors import AgentError
from personal_agent.ics import calendar_bytes, fold_line, write_snapshot

CODE = Path(__file__).resolve().parents[1]
STAMP = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)


def event(**kwargs):
    result = {"id": "event-1", "calendar_id": "local", "title": "评审", "start": "2026-10-27T09:00:00+08:00", "end": "2026-10-27T09:30:00+08:00", "timezone": "Asia/Shanghai", "all_day": False, "location": "", "description": "", "reminder_minutes": None}
    result.update(kwargs)
    return result


def unfold(payload):
    return payload.decode("utf-8").replace("\r\n ", "")


class ICSTests(unittest.TestCase):
    def test_subsecond_event_is_not_silently_shortened_to_zero(self):
        with self.assertRaises(AgentError) as error:
            calendar_bytes([event(start="2026-10-27T09:00:00.100000+08:00", end="2026-10-27T09:00:00.200000+08:00")])
        self.assertEqual("export_invalid_event", error.exception.code)

    def test_utc_stable_uid_and_crlf(self):
        one = calendar_bytes([event()], now=STAMP)
        text = unfold(one)
        self.assertIn("DTSTART:20261027T010000Z\r\n", text)
        self.assertIn("DTEND:20261027T013000Z\r\n", text)
        self.assertIn("DTSTAMP:20261026T080000Z\r\n", text)
        self.assertNotIn(b"\n", one.replace(b"\r\n", b""))
        self.assertNotIn("BEGIN:VALARM", text)
        uid = next(x for x in text.split("\r\n") if x.startswith("UID:"))
        changed = unfold(calendar_bytes([event(title="修改后", start="2026-10-27T10:00:00+08:00", end="2026-10-27T10:30:00+08:00")], now=STAMP))
        self.assertIn(uid + "\r\n", changed)
        other = unfold(calendar_bytes([event(calendar_id="other")], now=STAMP))
        self.assertNotIn(uid + "\r\n", other)

    def test_text_escaping_blocks_property_injection(self):
        injected = event(title="方案;复盘,备忘\\\r\nBEGIN:VEVENT\r\nSUMMARY:假日程", location="会议室\nATTENDEE:bad@example.com", description="第一行\r第二行\n第三行")
        raw = calendar_bytes([injected], now=STAMP)
        text = unfold(raw)
        self.assertEqual(text.count("\r\nBEGIN:VEVENT\r\n"), 1)
        self.assertEqual(text.count("\r\nSUMMARY:"), 1)
        self.assertNotIn("\r\nATTENDEE:", text)
        self.assertIn("方案\\;复盘\\,备忘\\\\\\nBEGIN:VEVENT\\nSUMMARY:假日程", text)
        self.assertIn("DESCRIPTION:第一行\\n第二行\\n第三行", text)

    def test_utf8_folding_never_splits_characters_or_exceeds_75_octets(self):
        description = "中文😀，带逗号,分号;和反斜线\\" * 80
        payload = calendar_bytes([event(description=description)], now=STAMP)
        for physical_line in payload.split(b"\r\n"):
            self.assertLessEqual(len(physical_line), 75)
            physical_line.decode("utf-8")
        self.assertIn("\\n", unfold(calendar_bytes([event(description="a\nb")], now=STAMP)))
        self.assertIn(b"\r\n ", payload)

    def test_all_day_exclusive_end_uses_original_timezone(self):
        payload = calendar_bytes([event(all_day=True, start="2026-10-26T16:00:00Z", end="2026-10-27T16:00:00Z")], now=STAMP)
        self.assertIn("DTSTART;VALUE=DATE:20261027\r\n", unfold(payload))
        self.assertIn("DTEND;VALUE=DATE:20261028\r\n", unfold(payload))

    def test_all_day_dst_transition_is_one_local_day(self):
        payload = calendar_bytes([event(all_day=True, timezone="America/New_York", start="2026-11-01T04:00:00Z", end="2026-11-02T05:00:00Z")], now=STAMP)
        text = unfold(payload)
        self.assertIn("DTSTART;VALUE=DATE:20261101", text)
        self.assertIn("DTEND;VALUE=DATE:20261102", text)

    def test_invalid_event_is_rejected_before_creating_output(self):
        examples = [event(title="bad\x00control"), event(start="2026-10-27T09:00:00"), event(all_day=True), event(reminder_minutes=True), event(timezone="bad\nTZ", all_day=True), event(title="bad\ud800")]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.ics"
            for candidate in examples:
                with self.subTest(candidate=repr(candidate)):
                    with self.assertRaises(AgentError):
                        write_snapshot(path, [candidate])
                    self.assertFalse(path.exists())

    def test_existing_file_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "keep.ics"
            path.write_bytes(b"original")
            with self.assertRaises(AgentError) as caught:
                write_snapshot(path, [event()])
            self.assertEqual(caught.exception.code, "output_exists")
            self.assertEqual(path.read_bytes(), b"original")
            self.assertEqual(list(Path(temp).glob(".calendar-export-*")), [])

    def test_alarm_only_when_event_has_explicit_reminder(self):
        text = unfold(calendar_bytes([event(reminder_minutes=15)], now=STAMP))
        self.assertIn("BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT15M", text)

    def test_duplicate_uid_is_rejected(self):
        with self.assertRaises(AgentError):
            calendar_bytes([event(), event(title="duplicate")], now=STAMP)


class ExportCLITests(unittest.TestCase):
    def run_cli(self, db, args):
        return subprocess.run([sys.executable, "-m", "personal_agent", "--db", str(db)] + args, env=dict(os.environ, PYTHONPATH=str(CODE)), text=True, encoding="utf-8", capture_output=True, timeout=10)

    def test_real_cli_export_and_refusal_to_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "calendar.sqlite3"
            created = self.run_cli(db, ["tool", "calendar_create_event", "--args", json.dumps({"request_id": "ics-test-1", "title": "测试导出", "start": "2026-10-27T09:00:00+08:00", "end": "2026-10-27T09:30:00+08:00"})])
            self.assertEqual(created.returncode, 0, created.stderr + created.stdout)
            path = Path(temp) / "output" / "calendar.ics"
            command = ["export", "--start", "2026-10-27", "--end", "2026-10-28", "--output", str(path)]
            exported = self.run_cli(db, command)
            self.assertEqual(exported.returncode, 0, exported.stderr + exported.stdout)
            result = json.loads(exported.stdout)
            self.assertEqual(result["events"], 1)
            self.assertIn("不是持续同步", result["notice"])
            self.assertIn("未发送手机提醒", result["notice"])
            before = path.read_bytes()
            self.assertIn("SUMMARY:测试导出", before.decode("utf-8"))
            self.assertNotIn(b"VALARM", before)
            again = self.run_cli(db, command)
            self.assertNotEqual(again.returncode, 0)
            self.assertEqual(json.loads(again.stdout)["error"]["code"], "output_exists")
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
