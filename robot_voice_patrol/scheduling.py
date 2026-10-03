"""Validated UTC scheduling, daily local times and explicit missed-run policy."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import math
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .contracts import CommandError


def parse_instant(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise CommandError("时间必须为带时区的 ISO 8601 字符串")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise CommandError("时间必须为带时区的 ISO 8601 字符串") from exc


def timezone_for(name):
    if not isinstance(name, str) or len(name) > 64:
        raise CommandError("时区名称无效")
    if name in {"UTC", "Etc/UTC"}:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        if name in {"Asia/Hong_Kong", "Asia/Shanghai"}:
            return timezone(timedelta(hours=8), name)
        raise CommandError("时区不可用，请安装 tzdata 或使用 UTC / Asia/Hong_Kong")


def validate_repeat(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise CommandError("重复计划格式错误")
    if set(value) == {"interval_seconds"}:
        seconds = value["interval_seconds"]
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 1 <= seconds <= 31536000:
            raise CommandError("重复间隔必须在 1 秒到 365 天之间")
        return {"interval_seconds": float(seconds)}
    if set(value) == {"daily_at", "timezone"}:
        if not isinstance(value["daily_at"], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value["daily_at"]):
            raise CommandError("每日时间须为 HH:MM")
        timezone_for(value["timezone"])
        return dict(value)
    raise CommandError("重复计划只支持 interval_seconds 或 daily_at + timezone")


def next_occurrence(repeat, after, *, anchor=None):
    """Return a strictly future occurrence; never replay all missed occurrences."""
    repeat = validate_repeat(repeat)
    if repeat is None:
        return None
    if "interval_seconds" in repeat:
        interval = repeat["interval_seconds"]
        base = anchor or after
        steps = max(1, math.floor((after - base).total_seconds() / interval) + 1)
        return base + timedelta(seconds=interval * steps)
    zone = timezone_for(repeat["timezone"])
    local = after.astimezone(zone)
    hour, minute = map(int, repeat["daily_at"].split(":"))
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0, fold=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    # Nonexistent DST local time moves to the next valid round-trip time;
    # ambiguous time uses fold=0 and executes once, not once per UTC offset.
    candidate = candidate.astimezone(timezone.utc).astimezone(zone)
    return candidate.astimezone(timezone.utc)
