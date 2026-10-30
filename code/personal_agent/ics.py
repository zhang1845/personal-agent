"""RFC 5545 calendar snapshot export, not a synchronization transport."""

from datetime import datetime, timezone
import json
import os
from .platform_utils import IS_WINDOWS
from pathlib import Path
import tempfile
import uuid
from .timeutils import zone

from .errors import AgentError


def escape_text(value):
    if not isinstance(value, str) or any(ord(c) < 32 and c not in "\r\n\t" for c in value):
        raise AgentError("export_invalid_event", "日程文本包含不支持的控制字符")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")


def fold_line(line):
    """Fold at 75 UTF-8 octets, including the continuation space."""
    if "\r" in line or "\n" in line:
        raise AgentError("export_invalid_event", "属性行包含未经转义的换行")
    chunks, current, size = [], "", 0
    for char in line:
        try:
            length = len(char.encode("utf-8"))
        except UnicodeEncodeError:
            raise AgentError("export_invalid_event", "日程文本不是有效的 Unicode")
        if size + length > 75:
            chunks.append(current)
            current, size = " ", 1
        current += char
        size += length
    chunks.append(current)
    return "\r\n".join(chunks)


def _instant(value):
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError()
        return instant
    except (AttributeError, TypeError, ValueError):
        raise AgentError("export_invalid_event", "导出要求有明确 UTC 偏移的日程起止时间")


def _date_value(instant, zone_name):
    try:
        local = instant.astimezone(zone(zone_name))
    except (AgentError, OSError, TypeError, ValueError, OverflowError):
        raise AgentError("export_invalid_event", "全天日程缺少有效时区") from None
    if (local.hour, local.minute, local.second, local.microsecond) != (0, 0, 0, 0):
        raise AgentError("export_invalid_event", "全天日程必须使用对应时区的午夜边界")
    return local.strftime("%Y%m%d")


def calendar_bytes(events, default_timezone="Asia/Shanghai", now=None):
    """Return UTF-8 bytes with stable UIDs, CRLF and bounded physical lines."""
    if not isinstance(events, list):
        raise AgentError("export_invalid_event", "日程列表格式无效")
    timestamp = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        raise AgentError("export_invalid_event", "导出时间需要 UTC 偏移")
    stamp = timestamp.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Daynest//Calendar snapshot//ZH-CN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
    seen = set()
    for event in events:
        if not isinstance(event, dict) or not all(isinstance(event.get(k), str) and event[k] for k in ("id", "calendar_id", "title", "start", "end")):
            raise AgentError("export_invalid_event", "日程缺少标识或起止时间")
        start, end = _instant(event["start"]), _instant(event["end"])
        if end <= start:
            raise AgentError("export_invalid_event", "日程结束时间必须晚于开始时间")
        identity = json.dumps([event["calendar_id"], event["id"]], ensure_ascii=False, separators=(",", ":"))
        uid = str(uuid.uuid5(uuid.NAMESPACE_URL, identity)) + "@personal-agent.local"
        if uid in seen:
            raise AgentError("export_invalid_event", "日程列表含重复事件标识")
        seen.add(uid)
        lines.extend(["BEGIN:VEVENT", "UID:" + uid, "DTSTAMP:" + stamp])
        all_day = event.get("all_day", False)
        if type(all_day) is not bool:
            raise AgentError("export_invalid_event", "全天标记必须是布尔值")
        if all_day:
            zone_name = event.get("timezone", default_timezone)
            start_date, end_date = _date_value(start, zone_name), _date_value(end, zone_name)
            if end_date <= start_date:
                raise AgentError("export_invalid_event", "全天日程结束日期必须晚于开始日期")
            lines.extend(["DTSTART;VALUE=DATE:" + start_date, "DTEND;VALUE=DATE:" + end_date])
        else:
            if start.microsecond or end.microsecond:
                raise AgentError("export_invalid_event", "ICS 快照仅支持秒级时间，请先明确调整含小数秒的日程")
            lines.extend(["DTSTART:" + start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), "DTEND:" + end.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")])
        lines.append("SUMMARY:" + escape_text(event["title"]))
        for key in ("location", "description"):
            value = event.get(key, "")
            if value:
                lines.append(key.upper() + ":" + escape_text(value))
        reminder = event.get("reminder_minutes")
        if reminder is not None:
            if type(reminder) is not int or not 0 <= reminder <= 40320:
                raise AgentError("export_invalid_event", "提醒时长无效")
            lines.extend(["BEGIN:VALARM", "ACTION:DISPLAY", "TRIGGER:-PT%dM" % reminder, "DESCRIPTION:" + escape_text(event["title"]), "END:VALARM"])
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return ("\r\n".join(fold_line(line) for line in lines) + "\r\n").encode("utf-8")


def write_snapshot(path, events, default_timezone="Asia/Shanghai"):
    """Install a finished file atomically without replacing any existing path."""
    payload = calendar_bytes(events, default_timezone)
    destination = Path(path).expanduser().absolute()
    temp_path = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".calendar-export-", suffix=".tmp", dir=str(destination.parent), delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # Install without overwriting an existing path or following a symlink.
        # POSIX hard links and Windows rename both provide exclusive insertion.
        if IS_WINDOWS:
            # Windows rename is exclusive even on volumes without hard links.
            os.rename(str(temp_path), str(destination))
        else:
            os.link(str(temp_path), str(destination))
    except FileExistsError:
        raise AgentError("output_exists", "导出目标已存在，请选择新的文件名；原文件未修改")
    except OSError:
        raise AgentError("export_failed", "日历文件未能导出，请检查输出目录和文件系统权限")
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
    return {"output": str(destination), "events": len(events), "notice": "这是离线 ICS 快照，不是持续同步。导入与通知权限由日历客户端处理，未发送手机提醒。"}
