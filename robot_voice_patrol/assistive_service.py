"""Durable, local-first daily living assistance independent of motion jobs.

Every mutation and its idempotency receipt share a SQLite transaction. The
background worker only advances reminder/request state; it never drives a
robot, contacts a person, makes a clinical decision, or claims physical care.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
import threading
import uuid

from .assistive_catalog import DEFAULT_PROFILE, HELP_CATEGORIES, REMINDER_CATEGORIES, ROUTINES
from .assistive_catalog import catalog as coverage_catalog
from .contracts import CommandError
from .reminder_schedule import next_calendar_occurrence, parse_weekly_reminder, validate_calendar
from .store import encode


MAX_RECORDS = 1000
MAX_RECEIPTS = 10000
FINAL = {"acknowledged", "cancelled", "resolved", "removed", "completed"}


def _text(value, name, limit=500, *, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise CommandError(f"{name} 必须是 1 到 {limit} 字符的文本")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise CommandError(f"{name} 不得包含控制字符")
    return value.strip()


def _number(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise CommandError(f"{name} 必须在 {low} 到 {high} 之间")
    return value


def _choice(value, choices, name):
    if not isinstance(value, str) or value not in choices:
        raise CommandError(f"{name} 不在支持的选项中")
    return value


def _boolean(value, name):
    if not isinstance(value, bool):
        raise CommandError(f"{name} 必须为布尔值")
    return value


def _iso(value):
    return value.astimezone(timezone.utc).isoformat()


def _date(value, name="due_at"):
    if not isinstance(value, str) or len(value) > 60:
        raise CommandError(f"{name} 必须是带时区的 ISO 时间")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone required")
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise CommandError(f"{name} 必须是带时区的 ISO 时间") from exc


class AssistiveService:
    """One service per MissionStore; close it before closing the store.

    ``clock`` may return a timezone-aware datetime or epoch seconds for tests.
    Call ``tick()`` explicitly when start=False. No task is replayed on restart.
    """

    def __init__(self, store, start=True, clock=None):
        self.store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._stop = threading.Event()
        self._thread = None
        self._last_error = None
        with store._lock, store._connection:
            store._connection.executescript("""
                CREATE TABLE IF NOT EXISTS assistive_records (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL,
                    due_at TEXT, snapshot TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS assistive_kind_state ON assistive_records(kind,state);
                CREATE INDEX IF NOT EXISTS assistive_due ON assistive_records(due_at,state);
                CREATE TABLE IF NOT EXISTS assistive_receipts (
                    request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    response TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS assistive_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT,
                    action TEXT NOT NULL, time TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS assistive_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS assistive_delivery_receipts (
                    provider TEXT NOT NULL, receipt_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, response TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(provider,receipt_id));
            """)
            store._connection.execute("INSERT OR IGNORE INTO assistive_meta VALUES('version','1')")
            version = store._connection.execute("SELECT value FROM assistive_meta WHERE key='version'").fetchone()[0]
            if version != "1":
                raise RuntimeError("生活辅助数据库版本不兼容")
            store._connection.execute("INSERT OR IGNORE INTO assistive_meta VALUES('profile',?)", (encode(DEFAULT_PROFILE),))
        self.tick()
        if start:
            self._thread = threading.Thread(target=self._loop, name="assistive-reminders", daemon=True)
            self._thread.start()

    def _now(self):
        result = self._clock()
        if isinstance(result, (float, int)) and not isinstance(result, bool):
            result = datetime.fromtimestamp(result, timezone.utc)
        if not isinstance(result, datetime) or result.tzinfo is None:
            raise ValueError("assistive clock must return aware datetime or timestamp")
        return result.astimezone(timezone.utc)

    def _loop(self):
        while not self._stop.wait(0.5):
            try:
                self.tick()
                self._last_error = None
            except Exception as exc:  # health exposes the error; do not silently stop reminders
                self._last_error = type(exc).__name__

    def close(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)

    def catalog(self):
        return coverage_catalog()

    def _load(self, record_id, kind=None):
        record_id = _text(record_id, "id", 100)
        row = self.store._connection.execute("SELECT snapshot FROM assistive_records WHERE id=?", (record_id,)).fetchone()
        if not row:
            raise CommandError("生活辅助记录不存在")
        record = json.loads(row[0])
        if record["kind"] == "reminder":
            record.setdefault("revision", 1)
        if kind and record["kind"] != kind:
            raise CommandError("记录类型与操作不匹配")
        return record

    def _save(self, record):
        if record["kind"] == "reminder":
            previous = self.store._connection.execute("SELECT snapshot FROM assistive_records WHERE id=?", (record["id"],)).fetchone()
            record["revision"] = json.loads(previous[0]).get("revision", 1) + 1 if previous else 1
        record["updated_at"] = _iso(self._now())
        self.store._connection.execute("""INSERT INTO assistive_records VALUES(?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET state=excluded.state,due_at=excluded.due_at,
            snapshot=excluded.snapshot,updated_at=excluded.updated_at""", (
                record["id"], record["kind"], record["state"], record.get("due_at"),
                encode(record), record["updated_at"]))
        return record

    def _new(self, kind, state, **fields):
        count = self.store._connection.execute("""SELECT COUNT(*) FROM assistive_records WHERE kind=?
            AND state NOT IN ('removed','cancelled','resolved','completed')
            AND NOT (kind='reminder' AND state='acknowledged')""", (kind,)).fetchone()[0]
        if count >= MAX_RECORDS:
            raise CommandError("此类未完成记录已达上限，请先整理已有记录")
        return {"id": uuid.uuid4().hex, "kind": kind, "state": state, "created_at": _iso(self._now()), **fields}

    def _audit(self, action, record=None, **data):
        self.store._connection.execute("INSERT INTO assistive_events(record_id,action,time,data) VALUES(?,?,?,?)",
                                      ((record or {}).get("id"), action, _iso(self._now()), encode(data)))

    def snapshot(self):
        with self.store._lock:
            profile = json.loads(self.store._connection.execute("SELECT value FROM assistive_meta WHERE key='profile'").fetchone()[0])
            result = {"profile": profile, "contacts": [], "reminders": [], "assistance": [],
                      "checklists": [], "needs": [], "checkins": [], "incidents": [], "equipment": [], "wellbeing": [], "counts": {}}
            names = {"contact": "contacts", "reminder": "reminders", "assistance": "assistance",
                     "checklist": "checklists", "need": "needs", "checkin": "checkins", "incident": "incidents", "equipment": "equipment", "wellbeing": "wellbeing"}
            # Active records first so historical activity cannot hide a due reminder.
            for kind, name in names.items():
                rows = self.store._connection.execute("""SELECT snapshot FROM assistive_records WHERE kind=? AND state!='removed'
                    ORDER BY CASE WHEN state IN ('pending','due','missed','created','escalated','open','active','waiting','overdue')
                        OR (state='acknowledged' AND kind IN ('incident','assistance')) THEN 0 ELSE 1 END,
                    updated_at DESC LIMIT 250""", (kind,)).fetchall()
                result[name] = [json.loads(row[0]) for row in rows]
                if kind == "reminder":
                    for record in result[name]:
                        record.setdefault("revision", 1)
            counts = self.store._connection.execute("SELECT kind,state,COUNT(*) AS n FROM assistive_records GROUP BY kind,state").fetchall()
            for row in counts:
                result["counts"].setdefault(row["kind"], {})[row["state"]] = row["n"]
            events = self.store._connection.execute("SELECT id,record_id,action,time,data FROM assistive_events ORDER BY id DESC LIMIT 40").fetchall()
            result["events"] = [{**dict(row), "data": json.loads(row["data"])} for row in events]
            authenticated_receipts = self.store._connection.execute("SELECT COUNT(*) FROM assistive_delivery_receipts").fetchone()[0]
            result["delivery"] = {"mode": "local_only", "external_messages_sent": 0,
                                  "authenticated_receipts": authenticated_receipts,
                                  "message": "默认未接入通信渠道；本程序不发送消息。有配置的宿主可提交经签名验证的提供方回执。"}
            result["health"] = {"running": bool(self._thread and self._thread.is_alive()),
                                "error": self._last_error, "clock": _iso(self._now())}
            return result

    def tick(self):
        """Advance only actual transitions, preserving notifications across restart."""
        now = self._now()
        changed = 0
        with self.store._lock, self.store._connection:
            rows = self.store._connection.execute("SELECT snapshot FROM assistive_records WHERE (kind='reminder' AND state IN ('pending','due')) OR (kind='assistance' AND state IN ('created','delivered'))").fetchall()
            for row in rows:
                record = json.loads(row[0])
                if record["kind"] == "reminder":
                    due = _date(record["due_at"])
                    if now < due:
                        continue
                    state = "missed" if now >= due + timedelta(seconds=record["grace_seconds"]) else "due"
                    if record["state"] == state:
                        continue
                    record.update(state=state, notification={"channel": "local_dashboard", "presented_at": _iso(now),
                                                           "audible_confirmed": False, "user_acknowledged": False})
                else:
                    if now < _date(record["escalate_at"]):
                        continue
                    record.update(state="escalated", escalated_at=_iso(now), escalation_reason="等待本人记录人工响应超时")
                self._save(record)
                self._audit("automatic." + record["state"], record, delivery="local_only")
                changed += 1
            from .living_operations import advance_wellbeing
            changed += advance_wellbeing(self, now)
        return changed

    def delivery_envelope(self, request_id):
        """Prepare a consented request for a trusted host; performs no dispatch."""
        with self.store._lock:
            request = self._load(request_id, "assistance")
            if request["state"] in {"resolved", "cancelled"}:
                raise CommandError("已结束的请求不能发送")
            if not request["consent"] or not request["contact_id"]:
                raise CommandError("需要明确的联系人和本人联系同意")
            contact = self._load(request["contact_id"], "contact")
            if contact["state"] == "removed":
                raise CommandError("联系人已移除")
            return {"version": 1, "request_id": request["id"], "contact_id": contact["id"],
                    "contact": {key: contact[key] for key in ("name", "relationship", "contact_hint")},
                    "category": request["category"], "title": request["title"], "detail": request["detail"],
                    "urgency": request["urgency"], "created_at": request["created_at"],
                    "consent": True, "idempotency_key": "assistive:" + request["id"]}

    def record_delivery_receipt(self, receipt, verifier):
        """Trusted-host only; deliberately not exposed as an unauthenticated API.

        A verified provider receipt and the changed state commit together.
        Recipient acknowledgment is still a provider report, not proof of care.
        """
        from .assistive_connectors import DeliveryReceiptVerifier
        if not isinstance(verifier, DeliveryReceiptVerifier):
            raise CommandError("通信回执需要显式配置的签名验证器")
        body = verifier.verify(receipt)
        fingerprint = hashlib.sha256(json.dumps(receipt, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
        with self.store._lock, self.store._connection:
            previous = self.store._connection.execute("SELECT fingerprint,response FROM assistive_delivery_receipts WHERE provider=? AND receipt_id=?",
                                                      (body["provider"], body["receipt_id"])).fetchone()
            if previous:
                if previous[0] != fingerprint:
                    raise CommandError("通信回执标识已被用于不同内容")
                result = json.loads(previous[1])
                result["deduplicated"] = True
                return result
            count = self.store._connection.execute("SELECT COUNT(*) FROM assistive_delivery_receipts").fetchone()[0]
            if count >= MAX_RECEIPTS:
                raise CommandError("通信回执记录已达上限，请导出并整理数据库")
            request = self._load(body["request_id"], "assistance")
            self.delivery_envelope(request["id"])
            if request["contact_id"] != body["contact_id"]:
                raise CommandError("通信回执联系人与请求不匹配")
            if body["status"] == "failed" and request["external_delivery_confirmed"]:
                raise CommandError("失败回执不能覆盖已确认的送达回执")
            if body["status"] in {"delivered", "acknowledged"}:
                request.update(delivery_status="delivered", external_delivery_confirmed=True,
                               delivery_evidence={"provider": body["provider"], "delivery_id": body["delivery_id"],
                                                  "occurred_at": body["occurred_at"], "authenticated": True})
                if body["status"] == "acknowledged":
                    request.update(state="acknowledged", acknowledgement_source="authenticated_provider_report")
                elif request["state"] in {"created", "escalated"}:
                    request["state"] = "delivered"
            else:
                request.update(delivery_status="failed", state="escalated", escalation_reason="通信提供方报告发送失败")
            self._save(request)
            self._audit("connector." + body["status"], request, provider=body["provider"], receipt_id=body["receipt_id"])
            result = self._response("已记录经签名验证的通信提供方回执；不代表实际照护已完成。", request)
            self.store._connection.execute("INSERT INTO assistive_delivery_receipts VALUES(?,?,?,?,?,?)",
                                          (body["provider"], body["receipt_id"], request["id"], fingerprint, encode(result), _iso(self._now())))
            return result

    def action(self, body):
        if self._stop.is_set():
            raise CommandError("生活辅助服务已关闭")
        if not isinstance(body, dict):
            raise CommandError("生活辅助操作必须是对象")
        # Serialization preflight rejects non-finite/unserializable nested payloads.
        try:
            encoded = encode(body)
        except (ValueError, TypeError, RecursionError) as exc:
            raise CommandError("操作必须是有限 JSON 数据") from exc
        if len(encoded.encode("utf-8")) > 24000:
            raise CommandError("生活辅助操作内容过大")
        request_id = body.get("request_id")
        if request_id is not None:
            request_id = _text(request_id, "request_id", 256)
        payload = {key: value for key, value in body.items() if key != "request_id"}
        if any(not isinstance(key, str) for key in payload):
            raise CommandError("操作字段名必须为字符串")
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
        with self.store._lock, self.store._connection:
            if self._stop.is_set():
                raise CommandError("生活辅助服务已关闭")
            if request_id:
                receipt = self.store._connection.execute("SELECT fingerprint,response FROM assistive_receipts WHERE request_id=?", (request_id,)).fetchone()
                if receipt:
                    if receipt[0] != fingerprint:
                        raise CommandError("request_id 已用于不同的生活辅助请求")
                    response = json.loads(receipt[1])
                    response["deduplicated"] = True
                    return response
                count = self.store._connection.execute("SELECT COUNT(*) FROM assistive_receipts").fetchone()[0]
                if count >= MAX_RECEIPTS:
                    raise CommandError("生活辅助请求回执已达上限，请导出并整理数据库")
            response = self._dispatch(payload)
            if request_id:
                self.store._connection.execute("INSERT INTO assistive_receipts VALUES(?,?,?,?)",
                                              (request_id, fingerprint, encode(response), _iso(self._now())))
            return response

    def _args(self, body, required=(), optional=()):
        missing = set(required) - body.keys()
        extra = body.keys() - {"op", *required, *optional}
        if missing or extra:
            raise CommandError(f"操作字段不匹配，缺少 {sorted(missing)}，不支持 {sorted(extra)}")

    def _response(self, message, record=None, **extra):
        result = {"ok": True, "message": message, "assistive": {"type": record["kind"] if record else "profile", **extra}}
        if record:
            result["assistive"]["record"] = deepcopy(record)
        return result

    @staticmethod
    def _validate_reminder_due(due, now, end):
        if due is None:
            raise CommandError("结束日期前没有可安排的日历提醒")
        if due < now - timedelta(days=1) or due > now + timedelta(days=366):
            raise CommandError("提醒时间须在过去一天至未来一年之间")
        if end is not None and due > end:
            raise CommandError("结束时间不能早于提醒时间")

    @staticmethod
    def _assert_reminder_revision(body, record):
        if "expected_revision" not in body:
            return
        expected = body["expected_revision"]
        if type(expected) is not int or expected < 1:
            raise CommandError("expected_revision 必须为正整数")
        if expected != record.get("revision", 1):
            raise CommandError("提醒已变化，请刷新后重新核对操作")

    def _dispatch(self, body):
        op = _text(body.get("op"), "op", 60)
        if op.startswith(("incident.", "equipment.", "handover.", "wellbeing.")):
            from .living_operations import dispatch
            return dispatch(self, body)
        if op == "reminder.create":
            self._args(body, ("title",), ("category", "due_at", "delay_seconds", "repeat_seconds", "calendar", "end_at", "grace_seconds", "note"))
            title = _text(body["title"], "title", 160)
            category = _choice(body.get("category", "general"), REMINDER_CATEGORIES, "category")
            now = self._now()
            if sum(key in body for key in ("due_at", "delay_seconds", "calendar")) != 1:
                raise CommandError("due_at、delay_seconds 和 calendar 必须且只能提供一个")
            calendar = validate_calendar(body["calendar"]) if "calendar" in body else None
            repeat = body.get("repeat_seconds")
            if repeat is not None:
                repeat = _number(repeat, "repeat_seconds", 60, 366 * 86400)
            if calendar and repeat is not None:
                raise CommandError("calendar 与 repeat_seconds 不能同时设置")
            end = _date(body["end_at"], "end_at") if body.get("end_at") is not None else None
            due = (next_calendar_occurrence(calendar, now, end_at=end) if calendar else
                   _date(body["due_at"]) if "due_at" in body else
                   now + timedelta(seconds=_number(body["delay_seconds"], "delay_seconds", 0, 366 * 86400)))
            self._validate_reminder_due(due, now, end)
            grace = _number(body.get("grace_seconds", 900), "grace_seconds", 30, 86400)
            record = self._new("reminder", "pending", title=title, category=category, due_at=_iso(due), repeat_seconds=repeat,
                               calendar=calendar, end_at=_iso(end) if end is not None else None, scheduled_due_at=_iso(due),
                               grace_seconds=grace, note=_text(body.get("note", ""), "note", empty=True),
                               occurrence=1, acknowledged_count=0, notification=None, medical_decision=False)
            message = "提醒已保存；到时在本地生活辅助面板显示，需本人确认。"
        elif op == "reminder.update":
            self._args(body, ("id",), ("title", "due_at", "delay_seconds", "repeat_seconds", "calendar", "end_at", "expected_revision"))
            changes = set(body) - {"op", "id", "expected_revision"}
            if not changes:
                raise CommandError("至少提供一项提醒修改")
            record = self._load(body["id"], "reminder")
            previous_reminder = deepcopy(record)
            if record["state"] in {"acknowledged", "cancelled", "completed"}:
                raise CommandError("提醒已结束，请新建提醒")
            self._assert_reminder_revision(body, record)
            if "title" in body:
                record["title"] = _text(body["title"], "title", 160)
            if "due_at" in body and "delay_seconds" in body:
                raise CommandError("due_at 与 delay_seconds 不能同时提供")
            calendar = record.get("calendar")
            repeat = record.get("repeat_seconds")
            if "calendar" in body:
                calendar = validate_calendar(body["calendar"]) if body["calendar"] is not None else None
            if "repeat_seconds" in body:
                repeat = (_number(body["repeat_seconds"], "repeat_seconds", 60, 366 * 86400)
                          if body["repeat_seconds"] is not None else None)
            if body.get("calendar") is not None and body.get("repeat_seconds") is not None:
                raise CommandError("calendar 与 repeat_seconds 不能同时设置")
            if body.get("calendar") is not None:
                repeat = None
            elif body.get("repeat_seconds") is not None:
                calendar = None
            time_changed = "due_at" in body or "delay_seconds" in body
            if calendar and time_changed:
                raise CommandError("日历提醒请修改 calendar；改为单次时间须明确 calendar:null")
            end_value = body.get("end_at", record.get("end_at"))
            end = _date(end_value, "end_at") if end_value is not None else None
            now = self._now()
            due = _date(record["due_at"])
            # Full-form clients may submit an unchanged calendar together with
            # a new title/cutoff. Do not consume the outstanding occurrence.
            calendar_changed = body.get("calendar") is not None and calendar != record.get("calendar")
            interval_changed = body.get("repeat_seconds") is not None and (
                record.get("calendar") is not None or repeat != record.get("repeat_seconds"))
            rescheduled = time_changed or calendar_changed
            if calendar_changed:
                due = next_calendar_occurrence(calendar, now, end_at=end)
            elif time_changed:
                due = (_date(body["due_at"]) if "due_at" in body else
                       now + timedelta(seconds=_number(body["delay_seconds"], "delay_seconds", 0, 366 * 86400)))
            if rescheduled:
                self._validate_reminder_due(due, now, end)
                record.update(state="pending", notification=None, due_at=_iso(due), scheduled_due_at=_iso(due))
                record.pop("snoozed_at", None)
            elif end is not None and due > end:
                raise CommandError("结束时间不能早于当前待确认的提醒时间；如不再需要请取消")
            if interval_changed and not rescheduled:
                # A changed interval starts at the retained current occurrence,
                # including a previously snoozed occurrence, not an old rule's
                # hidden baseline. Unchanged interval edits preserve that base.
                record["scheduled_due_at"] = record["due_at"]
                record.pop("snoozed_at", None)
            record.update(calendar=calendar, repeat_seconds=repeat, end_at=_iso(end) if end is not None else None)
            message = "提醒已修改；此前记录仍保留在审计中。"
        elif op in {"reminder.ack", "reminder.snooze", "reminder.cancel"}:
            self._args(body, ("id",), ("seconds", "expected_revision") if op == "reminder.snooze" else ("expected_revision",))
            record = self._load(body["id"], "reminder")
            self._assert_reminder_revision(body, record)
            if record["state"] in {"acknowledged", "cancelled", "completed"}:
                raise CommandError("提醒已结束")
            if op == "reminder.cancel":
                record.update(state="cancelled", cancelled_at=_iso(self._now()))
                message = "提醒已取消。"
            elif op == "reminder.snooze":
                delay = _number(body.get("seconds", 600), "seconds", 30, 86400)
                snoozed_due = self._now() + timedelta(seconds=delay)
                if record.get("end_at") and snoozed_due > _date(record["end_at"], "end_at"):
                    raise CommandError("稍后提醒时间超出结束日期，请修改结束日期或取消提醒")
                record.setdefault("scheduled_due_at", record["due_at"])
                record.update(state="pending", due_at=_iso(snoozed_due), notification=None,
                              snoozed_at=_iso(self._now()))
                message = "已稍后提醒。"
            else:
                if record["state"] == "pending" and _date(record["due_at"]) > self._now():
                    raise CommandError("提醒尚未到时；如不再需要，可取消")
                record.update(state="acknowledged", acknowledged_at=_iso(self._now()),
                              acknowledged_count=record["acknowledged_count"] + 1,
                              acknowledgement_source="local_user", completion_claim="本人确认已知晓；不证明服药或实际活动已完成")
                if record["notification"]:
                    record["notification"]["user_acknowledged"] = True
                next_due, skipped = None, 1
                now = self._now()
                end = _date(record["end_at"], "end_at") if record.get("end_at") else None
                anchor = _date(record.get("scheduled_due_at", record["due_at"]))
                if end is not None and max(now, anchor) >= end:
                    pass  # No recurrence calculation or timezone data needed.
                elif record.get("calendar"):
                    next_due = next_calendar_occurrence(record["calendar"], max(now, anchor), end_at=end)
                elif record.get("repeat_seconds"):
                    due, interval = anchor, record["repeat_seconds"]
                    skipped = max(1, int((now - due).total_seconds() // interval) + 1)
                    candidate = due + timedelta(seconds=skipped * interval)
                    if end is None or candidate <= end:
                        next_due = candidate
                if next_due is not None:
                    record.update(state="pending", due_at=_iso(next_due), scheduled_due_at=_iso(next_due),
                                  occurrence=record["occurrence"] + skipped, notification=None)
                    record.pop("snoozed_at", None)
                message = "已记录本人知晓。" + ("下一次提醒已安排。" if next_due is not None else
                                                   "已达到重复提醒结束范围。" if record.get("calendar") or record.get("repeat_seconds") else "")
        elif op == "assistance.create":
            self._args(body, ("category",), ("detail", "urgency", "contact_id", "consent", "escalate_seconds"))
            category = _choice(body["category"], HELP_CATEGORIES, "category")
            urgency = _choice(body.get("urgency", "urgent" if category in {"emergency", "home_hazard"} else "normal"), {"normal", "urgent"}, "urgency")
            consent = _boolean(body.get("consent", False), "consent")
            contact = body.get("contact_id")
            if contact is not None:
                contact_record = self._load(contact, "contact")
                if contact_record["state"] == "removed":
                    raise CommandError("联系人已移除")
                if not consent:
                    raise CommandError("选择联系人需要本人明确同意")
            escalate = _number(body.get("escalate_seconds", 60 if urgency == "urgent" else 900), "escalate_seconds", 10, 86400)
            record = self._new("assistance", "created", category=category, title=HELP_CATEGORIES[category],
                               detail=_text(body.get("detail", ""), "detail", empty=True), urgency=urgency,
                               contact_id=contact, consent=consent, delivery_status="not_sent", external_delivery_confirmed=False,
                               escalate_at=_iso(self._now() + timedelta(seconds=escalate)), reports=[],
                               physical_action_executed=False)
            message = "已记录人工协助请求，尚未发送给任何人。请使用已配置的现实求助方式联系能够提供帮助的人。"
        elif op in {"assistance.report", "assistance.cancel"}:
            self._args(body, ("id",), ("status", "note") if op == "assistance.report" else ())
            record = self._load(body["id"], "assistance")
            if record["state"] in {"resolved", "cancelled"}:
                raise CommandError("协助请求已结束")
            if op == "assistance.cancel":
                record.update(state="cancelled", cancelled_at=_iso(self._now()))
                message = "本地协助请求已取消。"
            else:
                status = _choice(body.get("status"), {"acknowledged", "resolved", "escalated"}, "status")
                note = _text(body.get("note", ""), "note", empty=True)
                record["reports"] = [*record["reports"], {"status": status, "note": note, "time": _iso(self._now()),
                                                           "source": "local_user_report", "independently_verified": False}][-100:]
                record.update(state=status)
                message = "已保存本人的处理情况记录；系统没有独立核实人员响应或服务完成。"
        elif op == "profile.update":
            self._args(body, ("changes",))
            changes = body["changes"]
            if not isinstance(changes, dict) or not changes or changes.keys() - DEFAULT_PROFILE.keys():
                raise CommandError("个人偏好字段无效")
            profile = json.loads(self.store._connection.execute("SELECT value FROM assistive_meta WHERE key='profile'").fetchone()[0])
            for key, value in changes.items():
                if key == "display_name": value = _text(value, key, 80)
                elif key == "language": value = _choice(value, {"zh-CN", "en"}, key)
                elif key == "timezone": value = _choice(value, {"Asia/Hong_Kong", "UTC"}, key)
                elif key == "text_scale": value = _number(value, key, 1, 2)
                elif key == "speech_rate": value = _number(value, key, 0.5, 1.5)
                elif key == "switch_scan_seconds": value = _number(value, key, 1, 10)
                else: value = _boolean(value, key)
                if key == "confirmation_required" and value is not True:
                    raise CommandError("保留本人确认权，不能关闭任务确认要求")
                profile[key] = value
            self.store._connection.execute("UPDATE assistive_meta SET value=? WHERE key='profile'", (encode(profile),))
            self._audit(op, fields=sorted(changes))
            return self._response("个人偏好已保存。", profile=profile)
        elif op == "contact.save":
            self._args(body, ("name",), ("id", "relationship", "contact_hint"))
            record = self._load(body["id"], "contact") if "id" in body else self._new("contact", "active")
            record.update(state="active", name=_text(body["name"], "name", 80),
                          relationship=_text(body.get("relationship", ""), "relationship", 80, empty=True),
                          contact_hint=_text(body.get("contact_hint", ""), "contact_hint", 160, empty=True), channel="manual", verified=False)
            message = "联系人已保存在本机；尚未连接消息或电话服务。"
        elif op == "contact.remove":
            self._args(body, ("id",))
            record = self._load(body["id"], "contact")
            record.update(state="removed")
            message = "联系人已从可选列表移除，历史请求仍保留原引用。"
        elif op == "checklist.start":
            self._args(body, (), ("routine", "title", "items"))
            if "routine" in body:
                if "items" in body or "title" in body:
                    raise CommandError("routine 不能同时提供自定义 title/items")
                routine = _choice(body["routine"], ROUTINES, "routine")
                template = ROUTINES[routine]
                title, items = template["label"], template["items"]
            else:
                routine = "custom"
                title, items = _text(body.get("title"), "title", 120), body.get("items")
            if not isinstance(items, list) or not 1 <= len(items) <= 30:
                raise CommandError("清单应有 1 到 30 个项目")
            items = [_text(item, "item", 160) for item in items]
            if len(set(items)) != len(items):
                raise CommandError("清单项目不得重复")
            profile = json.loads(self.store._connection.execute("SELECT value FROM assistive_meta WHERE key='profile'").fetchone()[0])
            day = self._now().astimezone(timezone(timedelta(hours=8 if profile["timezone"] == "Asia/Hong_Kong" else 0))).date().isoformat()
            if routine != "custom":
                rows = self.store._connection.execute("SELECT snapshot FROM assistive_records WHERE kind='checklist' AND state='active'").fetchall()
                for row in rows:
                    old = json.loads(row[0])
                    if old["routine"] == routine and old["day"] == day:
                        return self._response("今天的该清单已存在。", old, already_exists=True)
            record = self._new("checklist", "active", title=title, routine=routine, day=day,
                               items=[{"id": str(index), "title": item, "checked": False} for index, item in enumerate(items)])
            message = "清单已开始，请逐项核对。"
        elif op in {"checklist.check", "checklist.reset"}:
            self._args(body, ("id", "item_id", "checked") if op == "checklist.check" else ("id",))
            record = self._load(body["id"], "checklist")
            if op == "checklist.reset":
                for item in record["items"]:
                    item.update(checked=False)
                    item.pop("checked_at", None)
            else:
                item_id = _text(body["item_id"], "item_id", 20)
                item = next((item for item in record["items"] if item["id"] == item_id), None)
                if item is None:
                    raise CommandError("清单项目不存在")
                item.update(checked=_boolean(body["checked"], "checked"))
                if item["checked"]: item["checked_at"] = _iso(self._now())
                else: item.pop("checked_at", None)
            record["state"] = "completed" if all(item["checked"] for item in record["items"]) else "active"
            message = "清单核对记录已保存。"
        elif op == "need.add":
            self._args(body, ("title",), ("quantity", "category", "note"))
            record = self._new("need", "open", title=_text(body["title"], "title", 160),
                               quantity=_text(body.get("quantity", "1"), "quantity", 40),
                               category=_choice(body.get("category", "shopping"), {"shopping", "care", "repair", "other"}, "category"),
                               note=_text(body.get("note", ""), "note", empty=True), purchase_made=False)
            message = "需求已加入清单；未进行购买或付款。"
        elif op in {"need.check", "need.remove"}:
            self._args(body, ("id", "checked") if op == "need.check" else ("id",))
            record = self._load(body["id"], "need")
            if record["state"] == "removed":
                raise CommandError("需求已移除")
            record["state"] = "removed" if op == "need.remove" else ("completed" if _boolean(body["checked"], "checked") else "open")
            message = "需求清单已更新。"
        elif op == "checkin.create":
            self._args(body, ("feeling",), ("note", "needs_help"))
            feeling = _choice(body["feeling"], {"good", "okay", "uncomfortable", "need_help"}, "feeling")
            needs_help = _boolean(body.get("needs_help", feeling in {"uncomfortable", "need_help"}), "needs_help")
            note = _text(body.get("note", ""), "note", empty=True)
            record = self._new("checkin", "recorded", feeling=feeling, note=note, needs_help=needs_help,
                               source="local_user_self_report", diagnosis=None, assistance_id=None)
            if needs_help:
                request = self._new("assistance", "created", category="pain_report", title=HELP_CATEGORIES["pain_report"],
                                    detail=note or "本人状态记录提出需要帮助", urgency="normal", contact_id=None, consent=False,
                                    delivery_status="not_sent", external_delivery_confirmed=False,
                                    escalate_at=_iso(self._now() + timedelta(seconds=900)), reports=[], physical_action_executed=False)
                self._save(request)
                self._audit("assistance.from_checkin", request)
                record["assistance_id"] = request["id"]
            message = "已保存本人状态记录。" + ("同时创建了尚未发送的本地协助请求。" if needs_help else "")
        else:
            raise CommandError("不支持的生活辅助操作")
        self._save(record)
        if op == "reminder.update":
            audited = {"title", "due_at", "repeat_seconds", "calendar", "end_at", "state", "revision"}
            self._audit(op, record, previous={key: previous_reminder.get(key) for key in audited},
                        current={key: record.get(key) for key in audited})
        else:
            self._audit(op, record, state=record["state"])
        return self._response(message, record)

    def preview(self, text):
        action = self._interpret(text)
        if action is None:
            return None
        if action["op"] == "assistive.status":
            return {"ok": True, "kind": "assistive", "action": action, "message": "查看本地生活辅助记录。", "requires_confirmation": False}
        return {"ok": True, "kind": "assistive", "action": action, "message": self._describe(action), "requires_confirmation": False}

    def command(self, text, request_id=None):
        action = self._interpret(text)
        if action is None:
            return None
        if action["op"] == "assistive.status":
            return {"ok": True, "message": "这是本地生活辅助记录。", "assistive": {"type": "status"}, "snapshot": self.snapshot()}
        if request_id is not None:
            action["request_id"] = request_id
        return self.action(action)

    def _describe(self, action):
        op = action["op"]
        if op == "reminder.create": return f"保存提醒：{action['title']}。到时需要本人确认已知晓。"
        if op == "assistance.create": return f"记录{HELP_CATEGORIES[action['category']]}请求。请求仅保存在本机，尚未发送。"
        if op == "need.add": return f"加入需求清单：{action['title']}，不自动购买。"
        if op == "checklist.start": return f"开始{ROUTINES[action['routine']]['label']}，由本人逐项核对。"
        if op == "checkin.create": return "保存本人自述状态；需要帮助时同时记录本地协助请求。"
        if op == "incident.create": return "记录本人报告的异常并建立本地人工协助请求；没有自动检测或对外发送。"
        if op == "equipment.add": return "登记辅助设备台账；没有连接或检测真实设备。"
        if op == "handover.build": return "生成未结束待办和未来一天提醒的本机摘要；不会发送。"
        if op == "wellbeing.start": return "开始本人自选的定时确认等待；未确认仅建立本地协助请求，不推断身体状况。"
        return "保存个人无障碍交互偏好。"

    def _interpret(self, text):
        if not isinstance(text, str) or len(text) > 2000:
            raise CommandError("指令须是 1 到 2000 字符的文本")
        text = text.strip().rstrip("。！! ")
        if not text:
            raise CommandError("请输入指令")
        # Never execute the positive half of a negated, conditional, or compound request.
        if re.search(r"不要|别|取消|如果|假如|等到|除非|然后|再帮|并且|同时", text):
            return None
        from .assistive_catalog import SCENARIO_ACTIONS
        if text in SCENARIO_ACTIONS:
            return deepcopy(SCENARIO_ACTIONS[text])
        if text in {"生活辅助状态", "查看生活辅助", "查看提醒", "查看购物清单", "查看求助记录"}:
            return {"op": "assistive.status"}
        if text.startswith(("每周", "每星期")):
            with self.store._lock:
                profile = json.loads(self.store._connection.execute("SELECT value FROM assistive_meta WHERE key='profile'").fetchone()[0])
            return parse_weekly_reminder(text, profile["timezone"])
        wellbeing = re.fullmatch(r"开始([0-9]+(?:\.[0-9]+)?|[一二两三四五六七八九十半]+)(秒|分钟|小时)平安确认", text)
        if wellbeing:
            seconds = self._chinese_number(wellbeing[1]) * {"秒": 1, "分钟": 60, "小时": 3600}[wellbeing[2]]
            return {"op": "wellbeing.start", "seconds": _number(seconds, "确认等待时长", 30, 86400)}
        reminder = re.fullmatch(r"(?:请)?(?:在)?([0-9]+(?:\.[0-9]+)?|[一二两三四五六七八九十半]+)(秒|分钟|小时|天)后提醒我(.{1,160})", text)
        if reminder:
            value = self._chinese_number(reminder[1])
            seconds = value * {"秒": 1, "分钟": 60, "小时": 3600, "天": 86400}[reminder[2]]
            _number(seconds, "提醒间隔", 0, 366 * 86400)
            title = reminder[3].strip()
            category = next((key for key, words in {
                "hydration": ["喝水", "饮水"], "medication": ["吃药", "服药"], "meal_reminder": ["吃饭", "用餐"],
                "position_reminder": ["翻身", "姿势"], "exercise_reminder": ["锻炼", "活动", "运动"], "appointment": ["预约", "看诊", "开会"],
            }.items() if any(word in title for word in words)), "general")
            return {"op": "reminder.create", "title": title, "category": category, "delay_seconds": seconds}
        daily = re.fullmatch(r"每天([0-9]{1,2})(?:点|:)([0-9]{1,2})?(?:分)?提醒我(.{1,160})", text)
        if daily:
            hour, minute = int(daily[1]), int(daily[2] or 0)
            if hour > 23 or minute > 59: raise CommandError("每天提醒时间无效")
            with self.store._lock:
                profile = json.loads(self.store._connection.execute("SELECT value FROM assistive_meta WHERE key='profile'").fetchone()[0])
            offset = 8 if profile["timezone"] == "Asia/Hong_Kong" else 0
            local = self._now().astimezone(timezone(timedelta(hours=offset)))
            due = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if due <= local: due += timedelta(days=1)
            return {"op": "reminder.create", "title": daily[3].strip(), "due_at": _iso(due), "repeat_seconds": 86400}
        shopping = re.fullmatch(r"(?:请)?把(.{1,160}?)(?:加入|添加到)(?:购物|采购|需求)清单", text)
        if shopping: return {"op": "need.add", "title": shopping[1]}
        routines = {"晨间": "morning", "早晨": "morning", "睡前": "night", "出门": "outdoor", "回家": "return_home", "用餐": "meal", "居家检查": "home_safety"}
        match = re.fullmatch(r"(?:开始|打开)(.+)清单", text)
        if match and match[1] in routines: return {"op": "checklist.start", "routine": routines[match[1]]}
        if text in {"打开大字模式", "开启大字模式"}: return {"op": "profile.update", "changes": {"text_scale": 1.5}}
        if text in {"打开高对比模式", "开启高对比模式"}: return {"op": "profile.update", "changes": {"high_contrast": True}}
        if text in {"记录我现在感觉良好", "记录我状态良好", "我今天感觉很好"}: return {"op": "checkin.create", "feeling": "good"}
        if text in {"记录我现在感觉一般", "记录我状态一般"}: return {"op": "checkin.create", "feeling": "okay"}
        if text in {"记录我不舒服", "我身体不舒服需要帮助"}: return {"op": "checkin.create", "feeling": "uncomfortable", "needs_help": True, "note": text}
        help_words = {
            "toileting": ["如厕", "上厕所"], "bathing": ["洗澡", "洗浴"], "dressing": ["穿衣", "脱衣"],
            "transfer": ["移乘", "从床到轮椅", "从轮椅到床"], "feeding": ["喂饭", "进食协助"],
            "positioning": ["翻身"], "oral_care": ["刷牙", "口腔清洁"], "grooming": ["梳头", "洗脸"],
            "cooking": ["做饭", "热饭"], "dishwashing": ["洗碗"], "laundry": ["洗衣", "晾衣"],
            "cleaning": ["打扫", "清洁房间"], "waste": ["倒垃圾"], "package": ["取快递", "拿快递"],
            "escort": ["陪同出门", "陪我出门"], "stairs": ["上下楼", "上楼", "下楼"],
            "mobility_aid": ["调整轮椅", "调整助行器"], "visitor": ["应门", "核实访客"],
            "pet_care": ["照顾宠物", "喂猫", "喂狗"], "plant_care": ["浇花"], "paperwork": ["填写表格", "处理文件"],
            "communication": ["联系家人", "联系朋友"], "companionship": ["有人陪伴", "陪我聊天"],
            "charging": ["充电"], "home_hazard": ["漏水", "烟雾", "着火", "煤气味"],
            "pain_report": ["不舒服", "疼痛"], "emergency": ["紧急求助", "救命", "摔倒", "跌倒"],
        }
        # A request must be direct; prose mentioning somebody else's needs is not a command.
        direct = bool(re.match(r"^(?:请)?(?:我需要|我想要|帮我|我身体|家里)", text)) or text in {"紧急求助", "救命", "我摔倒了", "我跌倒了"}
        if direct:
            if re.search(r"怎么|如何|是否|吗|？|\?|还是|但是|或者|以及|并|接着|以后|之前", text):
                return None
            for category, words in help_words.items():
                if any(re.fullmatch(r"(?:请)?(?:我需要|我想要|帮我|我身体|家里|我)?" + re.escape(word) + r"(?:了)?(?:需要)?(?:帮助|协助)?", text) for word in words):
                    return {"op": "assistance.create", "category": category, "detail": text,
                            "urgency": "urgent" if category in {"home_hazard", "emergency"} else "normal"}
            if re.fullmatch(r"我需要(?:生活)?帮助", text): return {"op": "assistance.create", "category": "general", "detail": text}
        return None

    @staticmethod
    def _chinese_number(value):
        if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value): return float(value)
        if value == "半": return 0.5
        digits = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        if value in digits: return digits[value]
        if re.fullmatch(r"[一二三四五六七八九]?十[一二三四五六七八九]?", value):
            left, right = value.split("十")
            return digits.get(left, 1) * 10 + digits.get(right, 0)
        raise CommandError("提醒数字格式不支持，请使用数字，例如 10分钟")
