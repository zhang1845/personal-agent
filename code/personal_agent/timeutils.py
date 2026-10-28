"""Strict, timezone-aware intervals shared by all calendar backends."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import AgentError

UTC = timezone.utc
MAX_RANGE_DAYS = 31


def zone(name):
    if (not isinstance(name, str) or not name or len(name) > 100
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise AgentError("validation_error", "时区需要有效的 IANA 名称")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise AgentError("validation_error", "未知时区", {"timezone": name})


def localize(naive, tz):
    candidates = []
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=tz, fold=fold)
        if candidate.astimezone(UTC).astimezone(tz).replace(tzinfo=None) == naive:
            if not any(x.utcoffset() == candidate.utcoffset() for x in candidates):
                candidates.append(candidate)
    if len(candidates) != 1:
        raise AgentError(
            "validation_error", "本地时间处于夏令时跳空或重复区间，请提供明确 UTC 偏移",
            {"time": naive.isoformat(), "timezone": str(tz)},
        )
    return candidates[0]


def parse_datetime(value, tz):
    if not isinstance(value, str) or not value or len(value) > 64:
        raise AgentError("validation_error", "时间需要 ISO 日期或日期时间字符串")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise AgentError("validation_error", "无法识别时间，请使用 ISO 日期和时间")
    if parsed.tzinfo is None:
        parsed = localize(parsed, tz)
    return parsed.astimezone(UTC)


def iso(value):
    value = value.astimezone(UTC)
    return value.isoformat(timespec="microseconds" if value.microsecond else "seconds").replace("+00:00", "Z")


def interval(start, end, tz, bounded=True):
    a, b = parse_datetime(start, tz), parse_datetime(end, tz)
    if b <= a:
        raise AgentError("validation_error", "结束时间必须晚于开始时间")
    if bounded and b - a > timedelta(days=MAX_RANGE_DAYS):
        raise AgentError("validation_error", "单次时间范围不能超过 31 天")
    return a, b


def clock(value):
    if not isinstance(value, str) or len(value) != 5:
        raise AgentError("validation_error", "工作和午休时间应使用 HH:MM")
    try:
        parsed = time.fromisoformat(value)
    except ValueError:
        raise AgentError("validation_error", "无效的工作或午休时间")
    if parsed.second or parsed.tzinfo:
        raise AgentError("validation_error", "工作时间不能包含秒或时区")
    return parsed


def overlaps(a, b, c, d):
    return a < d and c < b
