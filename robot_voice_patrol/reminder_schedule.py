"""Bounded local-calendar reminders, independent of robot motion and networks.

Nonexistent wall times are skipped. An ambiguous wall time selects its first
UTC occurrence only; the repeated hour never produces a second reminder.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import CommandError


def calendar_timezone(name):
    """Resolve IANA data; only UTC and modern Hong Kong have fixed fallbacks."""
    if not isinstance(name, str) or not 1 <= len(name) <= 100:
        raise CommandError("calendar.timezone 必须是可用的 IANA 时区名称")
    if name in {"UTC", "Etc/UTC"}:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        if name == "Asia/Hong_Kong":
            return timezone(timedelta(hours=8), "Asia/Hong_Kong")
        raise CommandError(f"时区 {name} 不可用；请安装时区数据或选择 UTC / Asia/Hong_Kong，未安排提醒") from None


def validate_calendar(value):
    """Return a normalized weekly calendar; no unknown or coercible fields."""
    if not isinstance(value, dict) or set(value) != {"weekdays", "local_time", "timezone"}:
        raise CommandError("calendar 只接受 weekdays、local_time 和 timezone，三项均必需")
    days = value["weekdays"]
    if (not isinstance(days, list) or not 1 <= len(days) <= 7
            or any(type(day) is not int or not 1 <= day <= 7 for day in days)
            or len(set(days)) != len(days)):
        raise CommandError("calendar.weekdays 必须是不同的 ISO 星期数 1..7，1 为周一")
    at = value["local_time"]
    if not isinstance(at, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", at):
        raise CommandError("calendar.local_time 必须是 HH:MM，例如 08:30")
    calendar_timezone(value["timezone"])
    return {"weekdays": sorted(days), "local_time": at, "timezone": value["timezone"]}


def _aware(value, name):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CommandError(f"{name} 必须包含时区")
    return value.astimezone(timezone.utc)


def next_calendar_occurrence(calendar, after, *, end_at=None):
    """Return the first UTC instant strictly after `after`, or None at cutoff.

    end_at is an aware datetime and is inclusive. Iterate local dates, never
    add fixed 86400-second intervals across a daylight-saving transition.
    """
    calendar = validate_calendar(calendar)
    after = _aware(after, "after")
    cutoff = _aware(end_at, "end_at") if end_at is not None else None
    if cutoff is not None and after >= cutoff:
        return None
    zone = calendar_timezone(calendar["timezone"])
    try:
        first_date = after.astimezone(zone).date()
        hour, minute = map(int, calendar["local_time"].split(":"))
        # One missing date/hour may skip a scheduled week. This is a finite
        # guard against pathological timezone data, not an unbounded search.
        for offset in range(370):
            date = first_date + timedelta(days=offset)
            if date.isoweekday() not in calendar["weekdays"]:
                continue
            wall = datetime(date.year, date.month, date.day, hour, minute)
            instants = set()
            for fold in (0, 1):
                utc = wall.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
                if utc.astimezone(zone).replace(tzinfo=None) == wall:
                    instants.add(utc)
            if not instants:  # Spring-forward gap: skip, do not shift the time.
                continue
            candidate = min(instants)  # Fall-back overlap: the first occurrence.
            if candidate <= after:
                continue
            return candidate if cutoff is None or candidate <= cutoff else None
    except (OverflowError, ValueError) as exc:
        raise CommandError("日历时间超出支持范围，未安排提醒") from exc
    raise CommandError("在有限搜索范围内没有有效的本地日历时间")


def parse_clock_text(value):
    """Parse a bounded Chinese/decimal clock expression into HH:MM."""
    if not isinstance(value, str) or value.endswith(":"):
        return None
    match = re.fullmatch(r"(上午|早上|下午|晚上|中午)?([0-9]{1,2}|[零一二两三四五六七八九十]{1,3})(?:点|:)(?:([0-9]{1,2}|[零一二两三四五六七八九十]{1,3})(?:分)?)?", value)
    if not match:
        return None

    def number(text):
        if not text:
            return 0
        if text.isascii() and text.isdigit():
            return int(text)
        digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
                  "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        if text in digits:
            return digits[text]
        if re.fullmatch(r"[一二三四五六七八九]?十[零一二三四五六七八九]?", text):
            left, right = text.split("十")
            return digits.get(left, 1) * 10 + digits.get(right, 0)
        raise CommandError("提醒时间格式无效")

    hour, minute = number(match[2]), number(match[3])
    if match[1]:
        if not 1 <= hour <= 12:
            raise CommandError("带上午/下午的小时必须在 1 到 12 之间")
        hour = hour % 12 + (12 if match[1] in {"下午", "晚上", "中午"} else 0)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise CommandError("提醒时分超出有效范围")
    return f"{hour:02d}:{minute:02d}"


def parse_weekly_reminder(text, timezone_name="Asia/Hong_Kong"):
    """Return a strict reminder.create proposal or None; never persist it."""
    if not isinstance(text, str):
        return None
    match = re.fullmatch(r"(?:每周|每星期)([一二三四五六日天](?:(?:[、,，和]\s*)?(?:周|星期)?[一二三四五六日天])*?)\s*((?:上午|早上|下午|晚上|中午)?(?:[0-9]{1,2}|[零一二两三四五六七八九十]{1,3})(?:点|:)(?:(?:[0-9]{1,2}|[零一二两三四五六七八九十]{1,3})(?:分)?)?)提醒我(.{1,160})", text)
    if not match:
        return None
    raw_days = re.sub(r"周|星期|[、,，和\s]", "", match[1])
    mapping = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
    days = [mapping[day] for day in raw_days]
    local_time = parse_clock_text(match[2])
    if local_time is None:
        return None
    calendar = validate_calendar({"weekdays": days, "local_time": local_time, "timezone": timezone_name})
    return {"op": "reminder.create", "title": match[3].strip(), "calendar": calendar}
