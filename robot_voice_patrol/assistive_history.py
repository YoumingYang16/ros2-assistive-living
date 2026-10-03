"""Read-only, bounded history queries over the local living-assistance journal.

History records are user reports and software state, not independently verified
physical outcomes. No query advances a reminder or changes any record.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import sqlite3

from .contracts import CommandError


KINDS = {
    "contact": "联系人", "reminder": "提醒", "assistance": "人工协助",
    "checklist": "日常清单", "need": "需求", "checkin": "本人状态记录",
    "incident": "生活异常", "equipment": "设备台账", "wellbeing": "主动定时确认",
}
STATES = {
    "active": "进行中", "pending": "等待提醒", "due": "提醒已到时",
    "missed": "提醒待确认", "acknowledged": "已知晓／已记录响应",
    "cancelled": "已取消", "created": "已建立", "delivered": "已记录送达",
    "escalated": "等待人工核实", "resolved": "本人记录已处理",
    "completed": "已完成本地记录", "removed": "已移除", "open": "待处理",
    "recorded": "已记录", "waiting": "等待本人确认", "overdue": "逾期待核实",
}
KIND_STATES = {
    "contact": ("active", "removed"),
    "reminder": ("pending", "due", "missed", "acknowledged", "cancelled"),
    "assistance": ("created", "delivered", "acknowledged", "escalated", "resolved", "cancelled"),
    "checklist": ("active", "completed"), "need": ("open", "completed", "removed"),
    "checkin": ("recorded",), "incident": ("open", "acknowledged", "resolved"),
    "equipment": ("active", "completed"),
    "wellbeing": ("waiting", "overdue", "completed", "cancelled"),
}
MAX_OFFSET = 1_000_000
SEARCH_FIELDS = ("title", "detail", "note", "name", "report_note")
# Service timestamps are always UTC ISO, with either zero or six fractional
# digits. Padding the zero-fraction form keeps inclusive microsecond boundaries
# exact; SQLite julianday() would round sub-millisecond values.
_TIME_KEY = "(CASE WHEN substr(updated_at,20,1)='.' THEN updated_at ELSE substr(updated_at,1,19)||'.000000+00:00' END)"
_PRIVATE_KEYS = {
    "contact_hint", "contact_address", "phone", "phone_number", "email",
    "address", "api_key", "key", "secret", "token", "signature", "signed_receipt",
    "raw_receipt", "receipt", "receipt_body", "credentials", "authorization",
    "access_token", "refresh_token", "password", "private_key", "signing_key",
}


def history_metadata():
    """Return fresh UI labels and constraints; callers cannot mutate constants."""
    return {
        "kinds": deepcopy(KINDS), "states": deepcopy(STATES),
        "kind_states": {kind: list(states) for kind, states in KIND_STATES.items()},
        "source": "local_records_not_independently_verified", "date_field": "updated_at",
        "date_boundaries": "inclusive", "includes_removed": True,
        "sort": ["updated_at_desc", "id_asc"], "search_fields": list(SEARCH_FIELDS),
        "max_limit": 100, "max_offset": MAX_OFFSET,
        "privacy": "联系方式提示、凭据及原始签名回执不在历史查询中返回。",
        "message": "历史来源于本机记录；状态变化不等于外部联系、实际照护或机械动作已经完成。",
    }


def _integer(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise CommandError(f"{name} 必须是 {low} 到 {high} 之间的整数")
    return value


def _text(value, name, maximum, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise CommandError(f"{name} 必须是长度不超过 {maximum} 的文本")
    if any(ord(char) < 32 for char in value):
        raise CommandError(f"{name} 不得包含控制字符")
    return value.strip()


def _date(value, name):
    if value is None:
        return None
    value = _text(value, name, 60)
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None or date.utcoffset() is None:
            raise ValueError("timezone missing")
        return date.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, OverflowError) as exc:
        raise CommandError(f"{name} 必须是带时区的 ISO 时间") from exc


def _public(value):
    """Remove structured transport secrets at any nesting level, preserving data."""
    if isinstance(value, list):
        return [_public(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _public(item) for key, item in value.items()
            if key.lower() not in _PRIVATE_KEYS
            and not key.lower().endswith(("_secret", "_token", "_password", "_signature", "_credential", "_credentials", "_api_key"))
        }
    return value


def _record(snapshot):
    record = _public(json.loads(snapshot))
    # V6 reminders had no revision; expose the same read-only compatibility
    # default as AssistiveService, so history and the dashboard agree.
    if record.get("kind") == "reminder":
        record.setdefault("revision", 1)
    return record


def query_history(service, *, kind=None, state=None, query="", since=None, until=None, limit=25, offset=0):
    """Query every saved record, including removed entries, without mutations.

    Dates filter updated_at, include both boundaries, and require an explicit
    offset or Z. Text is a literal case-insensitive substring of human-facing
    fields only; SQL wildcards and identifiers have no special meaning.
    """
    limit = _integer(limit, "limit", 1, 100)
    offset = _integer(offset, "offset", 0, MAX_OFFSET)
    query = _text(query, "query", 200, empty=True)
    if kind is not None and (not isinstance(kind, str) or kind not in KINDS):
        raise CommandError("kind 不在生活记录类型中")
    if state is not None and (not isinstance(state, str) or state not in STATES):
        raise CommandError("state 不在生活记录状态中")
    if kind is not None and state is not None and state not in KIND_STATES[kind]:
        raise CommandError("state 与 kind 不匹配")
    since, until = _date(since, "since"), _date(until, "until")
    if since is not None and until is not None and since > until:
        raise CommandError("since 不能晚于 until")
    clauses, params = [], []
    # Ignore unsupported future/corrupt kinds rather than leaking unrelated data.
    clauses.append("kind IN (" + ",".join("?" for _ in KINDS) + ")")
    params.extend(KINDS)
    for name, value in (("kind", kind), ("state", state)):
        if value is not None:
            clauses.append(name + "=?")
            params.append(value)
    if query:
        clauses.append("(" + " OR ".join("instr(lower(COALESCE(json_extract(snapshot,'$." + field + "'),'')),lower(?))>0" for field in SEARCH_FIELDS) + ")")
        params.extend([query] * len(SEARCH_FIELDS))
    for operator, value in ((">=", since), ("<=", until)):
        if value is not None:
            clauses.append(_TIME_KEY + operator + "?")
            params.append(value)
    where = " AND ".join(clauses)
    try:
        with service.store._lock:
            connection = service.store._connection
            total = connection.execute("SELECT COUNT(*) FROM assistive_records WHERE " + where, params).fetchone()[0]
            rows = connection.execute("SELECT snapshot FROM assistive_records WHERE " + where + " ORDER BY updated_at DESC,id ASC LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
            records = [_record(row[0]) for row in rows]
    except CommandError:
        raise
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise CommandError("生活历史暂时无法读取，请检查本机数据服务") from exc
    return {"records": records, "total": total, "limit": limit, "offset": offset,
            "has_more": offset + len(records) < total, "meta": history_metadata()}


def record_detail(service, record_id, *, event_limit=50, event_offset=0):
    """Read a record and a bounded audit page; does not load transport receipts."""
    record_id = _text(record_id, "id", 100)
    event_limit = _integer(event_limit, "event_limit", 1, 100)
    event_offset = _integer(event_offset, "event_offset", 0, MAX_OFFSET)
    try:
        with service.store._lock:
            connection = service.store._connection
            row = connection.execute("SELECT snapshot,kind FROM assistive_records WHERE id=?", (record_id,)).fetchone()
            if row is None or row["kind"] not in KINDS:
                raise CommandError("生活辅助记录不存在")
            record = _record(row["snapshot"])
            total = connection.execute("SELECT COUNT(*) FROM assistive_events WHERE record_id=?", (record_id,)).fetchone()[0]
            rows = connection.execute("SELECT id,record_id,action,time,data FROM assistive_events WHERE record_id=? ORDER BY id DESC LIMIT ? OFFSET ?", (record_id, event_limit, event_offset)).fetchall()
            events = [{**dict(item), "data": _public(json.loads(item["data"]))} for item in rows]
    except CommandError:
        raise
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise CommandError("生活历史暂时无法读取，请检查本机数据服务") from exc
    return {"record": record, "events": events, "event_total": total,
            "event_limit": event_limit, "event_offset": event_offset,
            "events_has_more": event_offset + len(events) < total, "meta": history_metadata()}
