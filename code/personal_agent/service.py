"""Calendar rules that run before every backend mutation, independently of an LLM."""

import hashlib
import json
import threading
from datetime import datetime, timedelta

from .backend import SQLiteBackend
from .errors import AgentError
from .timeutils import UTC, clock, interval, iso, localize, overlaps, parse_datetime, zone


DEFAULTS = {"timezone": "Asia/Shanghai", "workdays": [0, 1, 2, 3, 4],
            "work_start": "09:00", "work_end": "18:00", "lunch_start": "12:00",
            "lunch_end": "13:00", "buffer_minutes": 15}
TOOLS = {
    "calendar_status": set(), "calendar_list_calendars": set(),
    "calendar_get_preferences": set(), "calendar_set_preferences": set(DEFAULTS),
    "calendar_list_events": {"start", "end", "calendar_id"},
    "calendar_get_event": {"event_id"},
    "calendar_create_event": {"request_id", "title", "start", "end", "calendar_id", "all_day", "location", "description", "reminder_minutes", "recurrence"},
    "calendar_update_event": {"request_id", "event_id", "expected_version", "changes", "scope"},
    "calendar_delete_event": {"request_id", "event_id", "expected_version", "scope"},
    "calendar_find_free_slots": {"start", "end", "duration_minutes", "limit", "calendar_id"},
}


def text(value, field, max_length=2000, required=False):
    if not isinstance(value, str) or len(value) > max_length or (required and not value.strip()):
        raise AgentError("validation_error", field + " 无效")
    return value.strip() if required else value


def integer(value, field, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise AgentError("validation_error", field + " 超出范围或不是整数")
    return value


class CalendarService:
    def __init__(self, db_path, backend=None):
        self.storage = SQLiteBackend(db_path)
        self.backend = backend or self.storage
        self.lock = threading.RLock()
        profile = self.backend.kind + ":" + str(getattr(self.backend, "profile_id", self.backend.kind))
        with self.storage.connection(immediate=True) as conn:
            row = conn.execute("SELECT value FROM metadata WHERE key='backend_profile'").fetchone()
            if row and row["value"] != profile:
                raise AgentError("backend_profile_mismatch", "此数据库属于其他后端或账号，请为当前日历使用独立数据库")
            if not row:
                existing = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] + conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
                if existing and self.backend is not self.storage:
                    raise AgentError("backend_profile_mismatch", "已有数据未绑定外部账号，请使用新的独立数据库")
                conn.execute("INSERT INTO metadata VALUES('backend_profile',?)", (profile,))

        from .recurrence import Recurrence
        self._recurrence = Recurrence(self)

    @property
    def recurrence(self):
        if not hasattr(self,"_recurrence"):
            from .recurrence import Recurrence
            self._recurrence=Recurrence(self)
        return self._recurrence

    def close(self):
        if self.backend is not self.storage and hasattr(self.backend, "close"):
            self.backend.close()

    def _preferences(self, conn=None):
        if conn is None:
            with self.storage.connection() as conn:
                return self._preferences(conn)
        row = conn.execute("SELECT body FROM preferences WHERE id=1").fetchone()
        return json.loads(row["body"]) if row else dict(DEFAULTS, workdays=list(DEFAULTS["workdays"]))

    def _call_backend(self, method, *args, conn=None):
        function = getattr(self.backend, method)
        if self.backend is self.storage:
            return function(*args, connection=conn)
        return function(*args)

    def _calendar(self, calendar_id=None):
        calendars = self.backend.list_calendars()
        if not isinstance(calendars, list) or not calendars:
            raise AgentError("backend_unavailable", "连接器未返回有效日历列表")
        for c in calendars:
            if not isinstance(c, dict) or not isinstance(c.get("id"), str) or not c["id"] or type(c.get("writable")) is not bool:
                raise AgentError("backend_unavailable", "连接器日历身份或权限不完整")
        if len({c["id"] for c in calendars}) != len(calendars):
            raise AgentError("backend_unavailable", "连接器返回重复日历身份")
        if calendar_id is None:
            if len(calendars) == 1:
                return calendars[0]
            writable = [c for c in calendars if c.get("writable") is True]
            if len(writable) != 1:
                raise AgentError("validation_error", "需要明确选择一个日历", {"calendars": calendars})
            return writable[0]
        text(calendar_id, "calendar_id", 512, True)
        matches = [c for c in calendars if c.get("id") == calendar_id]
        if len(matches) != 1:
            raise AgentError("not_found", "找不到所选日历")
        return matches[0]

    def _event(self, event_id, conn=None):
        text(event_id, "event_id", 1024, True)
        if event_id.startswith("series:") and self.backend is self.storage:
            return self.recurrence.get(event_id,conn)
        event = self._checked_event(self._call_backend("get_event", event_id, conn=conn))
        if event["id"] != event_id:
            raise AgentError("backend_unavailable", "连接器返回了其他事件，拒绝操作")
        return event

    @staticmethod
    def _checked_event(event):
        try:
            if not isinstance(event, dict):
                raise ValueError()
            for key in ("id", "title", "calendar_id", "version"):
                if not isinstance(event.get(key), str) or not event[key].strip():
                    raise ValueError()
            for key in ("all_day", "recurring"):
                if type(event.get(key)) is not bool:
                    raise ValueError()
            for key in ("location", "description"):
                if not isinstance(event.get(key), str):
                    raise ValueError()
            if "reminder_minutes" not in event:
                raise ValueError()
            if event["reminder_minutes"] is not None:
                integer(event["reminder_minutes"], "reminder_minutes", 0, 40320)
            for key in ("start", "end"):
                value = event.get(key)
                if not isinstance(value, str):
                    raise ValueError()
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    raise ValueError()
            interval(event["start"], event["end"], zone("UTC"), bounded=False)
        except (ValueError, TypeError, AgentError):
            raise AgentError("backend_unavailable", "连接器事件缺少明确身份、偏移、版本或完整字段")
        return event

    def _events(self, a, b, calendar_id, conn=None):
        events = self._call_backend("list_events", iso(a), iso(b), calendar_id, conn=conn)
        if self.backend is self.storage:
            events += self.recurrence.list_events(a,b,calendar_id,conn)
        if not isinstance(events, list):
            raise AgentError("backend_unavailable", "日程列表格式无效")
        if len(events) > 200:
            raise AgentError("validation_error", "范围内日程超过 200 条，请缩小查询范围")
        checked = []
        for e in events:
            e = self._checked_event(e)
            c, d = interval(e["start"], e["end"], zone("UTC"), bounded=False)
            if e["calendar_id"] != calendar_id:
                raise AgentError("backend_unavailable", "连接器混入了其他日历数据")
            if overlaps(a, b, c, d):
                checked.append(e)
        if len({e["id"] for e in checked}) != len(checked):
            raise AgentError("backend_unavailable", "连接器事件实例身份重复")
        return sorted(checked, key=lambda e: parse_datetime(e["start"], zone("UTC")))

    def _normalize(self, raw, preferences):
        tz_name = raw.get("timezone", preferences["timezone"])
        tz = zone(tz_name)
        title = text(raw.get("title"), "title", 200, True)
        all_day = raw.get("all_day", False)
        if type(all_day) is not bool:
            raise AgentError("validation_error", "all_day 必须是布尔值")
        a, b = interval(raw.get("start"), raw.get("end"), tz)
        if all_day:
            for value in (a, b):
                local = value.astimezone(tz)
                if (local.hour, local.minute, local.second, local.microsecond) != (0, 0, 0, 0):
                    raise AgentError("validation_error", "全天日程使用本地午夜，结束日期不包含在日程内")
        reminder = raw.get("reminder_minutes")
        if reminder is not None:
            integer(reminder, "reminder_minutes", 0, 40320)
            if not self.backend.capabilities.get("supports_reminders", False):
                raise AgentError("unsupported_capability", "此后端不发送手机提醒；请接入支持提醒的真实日历")
        return {"title": title, "start": iso(a), "end": iso(b),
                "timezone": tz_name, "calendar_id": raw["calendar_id"], "all_day": all_day,
                "location": text(raw.get("location", ""), "location", 1000),
                "description": text(raw.get("description", ""), "description", 5000),
                "reminder_minutes": reminder, "recurring": False}

    def _check_conflicts(self, event, exclude=None, conn=None):
        a, b = interval(event["start"], event["end"], zone("UTC"))
        conflicts = [e for e in self._events(a, b, event["calendar_id"], conn) if e["id"] != exclude]
        if conflicts:
            raise AgentError("conflict", "该时段与已有日程冲突，尚未写入", {
                "conflicts": [{k: e[k] for k in ("id", "title", "start", "end")} for e in conflicts],
            })

    def _prepare_mutation(self, name, args, preferences, conn):
        if name == "calendar_create_event":
            calendar = self._calendar(args.get("calendar_id"))
            if calendar.get("writable") is not True:
                raise AgentError("unsupported_capability", "日历不可写")
            event = self._normalize(dict(args, calendar_id=calendar["id"]), preferences)
            self._check_conflicts(event, conn=conn)
            return None, event
        current = self._event(args.get("event_id"), conn)
        if current.get("external",{}).get("readonly"):
            raise AgentError("unsupported_capability","该 CalDAV 日程当前为只读，请启用写入并同步后再编辑；远端重复实例请在原生日历处理")
        if current.get("recurring"):
            raise AgentError("unsupported_capability", "第一版不修改重复系列，请在原生日历中处理")
        if current.get("version") != text(args.get("expected_version"), "expected_version", 1024, True):
            raise AgentError("version_conflict", "日程已变化，请重新读取再操作", {"current_version": current.get("version")})
        calendar = self._calendar(current["calendar_id"])
        if calendar.get("writable") is not True:
            raise AgentError("unsupported_capability", "日历不可写")
        if name == "calendar_delete_event":
            return current, None
        changes = args.get("changes")
        allowed = {"title", "start", "end", "location", "description", "all_day", "reminder_minutes"}
        if not isinstance(changes, dict) or not changes or set(changes) - allowed:
            raise AgentError("validation_error", "changes 必须只包含支持的可修改字段")
        event = self._normalize(dict(current, **changes), preferences)
        self._check_conflicts(event, exclude=current["id"], conn=conn)
        return current, event

    def _perform(self, name, args, event, conn=None):
        if name == "calendar_create_event":
            return {"event": self._call_backend("create_event", event, args["request_id"], conn=conn)}
        if name == "calendar_update_event":
            return {"event": self._call_backend("update_event", args["event_id"], event,
                    args["expected_version"], args["request_id"], conn=conn)}
        result = self._call_backend("delete_event", args["event_id"], args["expected_version"], args["request_id"], conn=conn)
        if result.get("deleted") is not True or result.get("event_id") != args["event_id"]:
            raise AgentError("unknown_outcome", "连接器未确认目标日程已删除")
        return result

    def _validate_write_result(self, result, expected, expected_id=None):
        if expected is None:
            return
        saved = result.get("event")
        if not isinstance(saved, dict) or not isinstance(saved.get("id"), str) or not saved["id"] or not isinstance(saved.get("version"), str) or not saved["version"]:
            raise AgentError("unknown_outcome", "连接器缺少可核查的事件 ID 或版本")
        try:
            self._checked_event(saved)
        except AgentError:
            raise AgentError("unknown_outcome", "连接器写入回执字段不完整，无法核查")
        if saved["recurring"] is not False:
            raise AgentError("unknown_outcome", "连接器意外返回重复系列，需核查原生日历")
        if expected_id is not None and saved["id"] != expected_id:
            raise AgentError("unknown_outcome", "连接器返回的事件 ID 与修改目标不同")
        for key in ("title", "calendar_id", "all_day", "location", "description", "reminder_minutes"):
            if saved.get(key) != expected[key]:
                raise AgentError("unknown_outcome", "连接器返回的日程与请求不一致")
        for key in ("start", "end"):
            if parse_datetime(saved.get(key), zone("UTC")) != parse_datetime(expected[key], zone("UTC")):
                raise AgentError("unknown_outcome", "连接器返回时间与请求不一致")

    def _mutate(self, name, args):
        request_id = text(args.get("request_id"), "request_id", 128, True)
        try:
            fingerprint = hashlib.sha256(json.dumps({"name": name, "arguments": args},
                                      sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        except (ValueError, TypeError):
            raise AgentError("validation_error", "参数必须是有限的 JSON 值")
        remote = self.backend is not self.storage
        with self.storage.connection(immediate=True) as conn:
            old = conn.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise AgentError("request_id_conflict", "同一请求编号不能用于不同参数")
                if old["status"] != "done":
                    raise AgentError("unknown_outcome", "此前写入结果未知，请先在原生日历核查；不会重发", {"request_id": request_id})
                return dict(json.loads(old["result"]), replayed=True)
            if self.backend.capabilities.get("writes_enabled", True) is not True:
                raise AgentError("unsupported_capability", "外部日历写入尚未明确启用，未发送操作")
            preferences = self._preferences(conn)
            _, event = self._prepare_mutation(name, args, preferences, conn)
            conn.execute("INSERT INTO requests(request_id,fingerprint,status) VALUES(?,?,'pending')", (request_id, fingerprint))
            if not remote:
                result = self._perform(name, args, event, conn)
                self._validate_write_result(result, event, args.get("event_id"))
                conn.execute("UPDATE requests SET status='done',result=? WHERE request_id=?",
                             (json.dumps(result, ensure_ascii=False), request_id))
                return dict(result, replayed=False)
        # The remote service is not part of our database transaction. Once reserved,
        # every unresolved result is held for human reconciliation, never auto-retried.
        try:
            result = self._perform(name, args, event)
            self._validate_write_result(result, event, args.get("event_id"))
        except Exception:
            with self.storage.connection(immediate=True) as conn:
                conn.execute("UPDATE requests SET status='unknown' WHERE request_id=?", (request_id,))
            raise AgentError("unknown_outcome", "外部写入未获得可核查结果，请核查日历后人工处理，不会自动重试", {"request_id": request_id})
        with self.storage.connection(immediate=True) as conn:
            conn.execute("UPDATE requests SET status='done',result=? WHERE request_id=?",
                         (json.dumps(result, ensure_ascii=False), request_id))
        return dict(result, replayed=False)

    def _set_preferences(self, args):
        with self.storage.connection(immediate=True) as conn:
            prefs = dict(self._preferences(conn), **args)
            zone(prefs["timezone"])
            days = prefs["workdays"]
            if not isinstance(days, list) or not days or any(type(x) is not int or x not in range(7) for x in days) or len(set(days)) != len(days):
                raise AgentError("validation_error", "workdays 为不重复的 0 至 6，0 表示周一")
            start, end, lunch_a, lunch_b = [clock(prefs[k]) for k in ("work_start", "work_end", "lunch_start", "lunch_end")]
            if not start < end or not start <= lunch_a <= lunch_b <= end:
                raise AgentError("validation_error", "工作和午休时段顺序无效；午休起止相同表示关闭")
            integer(prefs["buffer_minutes"], "buffer_minutes", 0, 120)
            conn.execute("INSERT OR REPLACE INTO preferences VALUES(1,?)", (json.dumps(prefs),))
            return {"preferences": prefs}

    def _free_slots(self, args, prefs):
        tz = zone(prefs["timezone"])
        a, b = interval(args.get("start"), args.get("end"), tz)
        duration = timedelta(minutes=integer(args.get("duration_minutes"), "duration_minutes", 5, 1440))
        limit = integer(args.get("limit", 5), "limit", 1, 20)
        cal = self._calendar(args.get("calendar_id"))
        buffer = timedelta(minutes=prefs["buffer_minutes"])
        # Widen the read window so a meeting just outside the query still contributes its buffer.
        events = self._events(a - buffer, b + buffer, cal["id"])
        blocks = [(parse_datetime(e["start"], tz) - buffer, parse_datetime(e["end"], tz) + buffer) for e in events]
        slots = []
        day = a.astimezone(tz).date()
        last = b.astimezone(tz).date()
        while day <= last:
            if day.weekday() in prefs["workdays"]:
                def at(value):
                    return localize(datetime.combine(day, clock(value)), tz).astimezone(UTC)
                left, right = max(a, at(prefs["work_start"])), min(b, at(prefs["work_end"]))
                lunch_a, lunch_b = at(prefs["lunch_start"]), at(prefs["lunch_end"])
                busy = blocks + ([(lunch_a, lunch_b)] if lunch_b > lunch_a else [])
                gaps = [(left, right)] if left < right else []
                for c, d in sorted(busy):
                    updated = []
                    for x, y in gaps:
                        if not overlaps(x, y, c, d):
                            updated.append((x, y))
                        else:
                            if x < c: updated.append((x, min(c, y)))
                            if d < y: updated.append((max(d, x), y))
                    gaps = updated
                for x, y in gaps:
                    # Return one earliest candidate per free gap, rather than filling
                    # the first morning with many adjacent alternatives.
                    if y - x >= duration:
                        slots.append({"start": x.astimezone(tz).isoformat(),
                                      "end": (x + duration).astimezone(tz).isoformat()})
                        if len(slots) >= limit:
                            return {"slots": slots, "timezone": prefs["timezone"]}
            day += timedelta(days=1)
        return {"slots": slots, "timezone": prefs["timezone"]}

    def call(self, name, arguments):
        if name not in TOOLS:
            raise AgentError("unknown_tool", "未知工具")
        if not isinstance(arguments, dict) or set(arguments) - TOOLS[name]:
            raise AgentError("validation_error", "参数包含不支持的字段")
        with self.lock:
            if (name == "calendar_create_event" and arguments.get("recurrence")) or (name in ("calendar_update_event","calendar_delete_event") and str(arguments.get("event_id","")).startswith("series:")):
                return self.recurrence.mutate(name,arguments)
            if arguments.get("scope") == "occurrence":
                # An explicit single occurrence also means just this ordinary event.
                # Normalize before fingerprinting so an omitted default replays safely.
                arguments = {key: value for key, value in arguments.items() if key != "scope"}
            elif arguments.get("scope") is not None:
                raise AgentError("validation_error","普通日程无需指定重复范围")
            prefs = self._preferences()
            if name == "calendar_status":
                return {"backend": self.backend.kind, "timezone": prefs["timezone"],
                        "current_time": datetime.now(zone(prefs["timezone"])).isoformat(timespec="seconds"),
                        "capabilities": dict(self.backend.capabilities), "limits": {"range_days": 31, "events": 200},
                        "notice": "本地开发日历，未同步真实账号，不发送手机提醒" if self.backend is self.storage else "已配置外部连接器；真实账号和提醒仍需验证"}
            if name == "calendar_list_calendars":
                return {"calendars": self.backend.list_calendars()}
            if name == "calendar_get_preferences":
                return {"preferences": prefs}
            if name == "calendar_set_preferences":
                return self._set_preferences(arguments)
            if name == "calendar_list_events":
                a, b = interval(arguments.get("start"), arguments.get("end"), zone(prefs["timezone"]))
                cal = self._calendar(arguments.get("calendar_id"))
                return {"events": self._events(a, b, cal["id"]), "timezone": prefs["timezone"], "complete": True}
            if name == "calendar_get_event":
                return {"event": self._event(arguments.get("event_id"))}
            if name == "calendar_find_free_slots":
                return self._free_slots(arguments, prefs)
            return self._mutate(name, arguments)
