"""Session-scoped, durable daily-living dialogue over structured service actions.

Preview never saves context. Choice commands carry a record fingerprint, so a
button from a preview cannot accidentally act on a later reminder occurrence.
"""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re

from .contracts import CommandError

TTL_SECONDS = 300
MAX_OPTIONS = 5
CONTEXT_KEY = "assistive_dialogue"
NUMBER = r"[0-9]+(?:\.[0-9]+)?|[一二两三四五六七八九十半]+"


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True).encode()).hexdigest()


def _title(value):
    value = value.strip()
    for left, right in (("「", "」"), ("『", "』"), ('"', '"'), ("“", "”")):
        if value.startswith(left) and value.endswith(right):
            return value[1:-1].strip()
    return value


def _epoch(record):
    keys = ("id", "kind", "title", "created_at", "due_at", "occurrence", "calendar", "end_at", "routine")
    return _hash({key: record.get(key) for key in keys})


class AssistiveDialogue:
    def __init__(self, service):
        self.service, self.store = service, service.store

    def _now(self):
        return self.service._now()

    def _zone(self):
        profile = json.loads(self.store._connection.execute("SELECT value FROM assistive_meta WHERE key='profile'").fetchone()[0])
        return profile["timezone"]

    def _number(self, value):
        return self.service._chinese_number(value)

    def _duration(self, amount, unit):
        seconds = self._number(amount) * {"秒": 1, "分钟": 60, "小时": 3600, "天": 86400}[unit]
        if not 30 <= seconds <= 86400:
            raise CommandError("稍后提醒时长须为30秒至24小时")
        return seconds

    def _date(self, text):
        from .reminder_schedule import calendar_timezone, parse_clock_text
        zone = calendar_timezone(self._zone())
        local = self._now().astimezone(zone)
        match = re.fullmatch(r"(今天|明天|后天)(.+)", text)
        if match:
            day = (local + timedelta(days={"今天": 0, "明天": 1, "后天": 2}[match[1]])).date()
            clock = parse_clock_text(match[2])
            if clock:
                return datetime.fromisoformat(str(day)+"T"+clock).replace(tzinfo=zone).isoformat()
        try:
            value = datetime.fromisoformat(text)
            if value.tzinfo is None:
                value = value.replace(tzinfo=zone)
            return value.isoformat()
        except ValueError as exc:
            raise CommandError("请给出完整日期时间，例如明天上午八点或2026-10-10T08:00+08:00") from exc

    def _descriptor(self, text):
        """Return only complete, explicitly supported clauses; never split prose."""
        text = text.strip().rstrip("。！! ")
        tag = re.search(r"〔([0-9a-f]{8}):([0-9a-f]{10})〕$", text)
        token = tag.groups() if tag else None
        if tag:
            text = text[:tag.start()].strip()
        if text.startswith("请"):
            text = text[1:]
        base = {"text": text, "token": token, "target": ""}
        if text in {"我在", "我没事", "确认平安", "确认平安等待"}:
            return {**base, "kind": "wellbeing", "op": "wellbeing.confirm"}
        match = re.fullmatch(r"取消(刚才的|这次|这个|这条)(定时确认|平安确认|平安等待)", text)
        if match:
            return {**base, "kind": "wellbeing", "op": "wellbeing.cancel", "target": "刚才的"}
        match = re.fullmatch(r"(确认平安等待|取消平安确认|取消平安等待)(.*)", text)
        if match:
            return {**base, "kind": "wellbeing", "op": "wellbeing.cancel" if match[1].startswith("取消") else "wellbeing.confirm", "target": _title(match[2])}
        # Robot confirmation remains exclusively with the mission planner.
        if text in {"确认", "确认执行", "执行计划", "就这样", "取消计划"}:
            return None
        match = re.fullmatch(r"(这条|这个|刚才的)提醒我(?:已经)?知道了", text)
        if match:
            return {**base, "kind": "reminder", "op": "reminder.ack", "target": match[1]}
        for pattern in (r"确认提醒(.*)", r"确认(.+?)提醒", r"我知道(.*?)提醒了"):
            match = re.fullmatch(pattern, text)
            if match:
                return {**base, "kind": "reminder", "op": "reminder.ack", "target": _title(match[1])}
        if text in {"知道了", "提醒知道了", "已知晓提醒"}:
            return {**base, "kind": "reminder", "op": "reminder.ack"}
        for pattern in (r"取消提醒(.*)", r"取消(.+?)提醒"):
            match = re.fullmatch(pattern, text)
            if match:
                return {**base, "kind": "reminder", "op": "reminder.cancel", "target": _title(match[1])}
        match = re.fullmatch(r"(?:提醒(.*?))?稍后("+NUMBER+r")(秒|分钟|小时)(?:再提醒(?:我)?|提醒(?:我)?)?", text)
        if not match:
            match = re.fullmatch(r"(.*?)提醒稍后("+NUMBER+r")(秒|分钟|小时)", text)
        if match:
            return {**base, "kind": "reminder", "op": "reminder.snooze", "target": _title(match[1] or ""),
                    "seconds": self._duration(match[2], match[3])}
        match = re.fullmatch(r"(?:把)?(.*?)提醒(改名为|改标题为|改到|改成|改为)(.{1,160})", text)
        if match:
            target, verb, value = _title(match[1]), match[2], match[3]
            if verb in {"改名为", "改标题为"}:
                changes = {"title": _title(value)}
            elif value == "单次":
                changes = {"calendar": None, "repeat_seconds": None}
            elif value.startswith(("每周", "每星期")):
                from .reminder_schedule import parse_weekly_reminder
                action = parse_weekly_reminder(value+"提醒我待修改", timezone_name=self._zone())
                if action is None:
                    raise CommandError("周历规则未识别，例如每周一和周三上午八点")
                changes = {"calendar": action["calendar"]}
            else:
                relative = re.fullmatch(r"("+NUMBER+r")(秒|分钟|小时|天)后", value)
                if relative:
                    seconds = self._number(relative[1]) * {"秒": 1, "分钟": 60, "小时": 3600, "天": 86400}[relative[2]]
                    changes = {"delay_seconds": seconds, "calendar": None}
                else:
                    changes = {"due_at": self._date(value), "calendar": None}
            return {**base, "kind": "reminder", "op": "reminder.update", "target": target,
                    "changes": changes, "verb": verb, "value": value}
        assistance = {"有人回应了": "acknowledged", "有人回应": "acknowledged", "请求有人回应了": "acknowledged",
                      "协助有人回应了": "acknowledged", "协助已解决": "resolved", "协助已经解决": "resolved", "请求已解决": "resolved"}
        if text in assistance:
            return {**base, "kind": "assistance", "op": "assistance.report", "status": assistance[text]}
        match = re.fullmatch(r"记录协助(.*?)(有人回应|已解决)", text)
        if match:
            return {**base, "kind": "assistance", "op": "assistance.report", "target": _title(match[1]),
                    "status": "acknowledged" if match[2] == "有人回应" else "resolved"}
        match = re.fullmatch(r"取消协助请求(.*)", text)
        if match:
            return {**base, "kind": "assistance", "op": "assistance.cancel", "target": _title(match[1])}
        match = re.fullmatch(r"(完成|取消勾选)(.*?)第("+NUMBER+r")个?(?:项目|项)", text)
        if match:
            number = self._number(match[3])
            if int(number) != number or not 1 <= number <= 30:
                raise CommandError("清单项目序号须为1至30")
            return {**base, "kind": "checklist", "op": "checklist.check", "target": _title(match[2]),
                    "item_number": int(number), "checked": match[1] == "完成"}
        match = re.fullmatch(r"(.{1,160}?)(已备好|已经备好|已买好|已经买好)", text)
        if match:
            return {**base, "kind": "need", "op": "need.check", "target": _title(match[1]), "checked": True}
        if text == "用品已备好":
            return {**base, "kind": "need", "op": "need.check", "checked": True}
        return None

    def _eligible(self, descriptor, record):
        if record["kind"] != descriptor["kind"]:
            return False
        state, op = record["state"], descriptor["op"]
        if record["kind"] == "reminder":
            if state not in {"pending", "due", "missed"}:
                return False
            return op != "reminder.ack" or datetime.fromisoformat(record["due_at"].replace("Z", "+00:00")) <= self._now()
        if record["kind"] == "wellbeing":
            return state in {"waiting", "overdue"}
        if record["kind"] == "assistance":
            return state not in {"resolved", "cancelled"}
        if record["kind"] == "checklist":
            return state in {"active", "completed"}
        return state != "removed" and (state != "completed" or descriptor.get("checked") is False)

    def _records(self, kind):
        rows = self.store._connection.execute("SELECT snapshot FROM assistive_records WHERE kind=? ORDER BY COALESCE(due_at,updated_at),id", (kind,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def _matches_title(self, desired, actual):
        if not desired or desired in {"这个", "这条", "刚才的", "用品", "清单"}:
            return True
        return desired == actual or desired.removesuffix("清单") == actual.removesuffix("清单")

    def _action(self, descriptor, record):
        op = descriptor["op"]
        action = {"op": op, "id": record["id"]}
        if op.startswith("reminder."):
            action["expected_revision"] = record.get("revision", 1)
        if op == "reminder.snooze":
            action["seconds"] = descriptor["seconds"]
        elif op == "reminder.update":
            action.update(deepcopy(descriptor["changes"]))
        elif op == "assistance.report":
            action.update(status=descriptor["status"], note="本人通过文字或语音报告；没有独立核实人员响应")
        elif op == "checklist.check":
            index = descriptor["item_number"]-1
            if index >= len(record["items"]):
                raise CommandError("这份清单没有该序号的项目")
            action.update(item_id=record["items"][index]["id"], checked=descriptor["checked"])
        elif op == "need.check":
            action["checked"] = descriptor["checked"]
        return action

    def _canonical(self, descriptor, record):
        op, title = descriptor["op"], "「"+record["title"]+"」"
        if op == "reminder.ack": text = "确认提醒"+title
        elif op == "reminder.cancel": text = "取消提醒"+title
        elif op == "reminder.snooze": text = "提醒"+title+"稍后"+str(descriptor["seconds"])+"秒"
        elif op == "reminder.update": text = "把"+title+"提醒"+descriptor["verb"]+descriptor["value"]
        elif op == "wellbeing.confirm": text = "确认平安等待"+title
        elif op == "wellbeing.cancel": text = "取消平安确认"+title
        elif op == "assistance.cancel": text = "取消协助请求"+title
        elif op == "assistance.report": text = "记录协助"+title+("有人回应" if descriptor["status"] == "acknowledged" else "已解决")
        elif op == "checklist.check": text = ("完成" if descriptor["checked"] else "取消勾选")+title+"第"+str(descriptor["item_number"])+"个项目"
        else: text = title+"已备好"
        return text+"〔"+record["id"][:8]+":"+_hash(record)[:10]+"〕"

    def _response(self, message, *, options=None, option_labels=None, clarify=False, action=None):
        result = {"ok": True, "kind": "clarify" if clarify else "assistive", "message": message,
                  "options": options or [], "option_labels": option_labels or [],
                  "needs_clarification": clarify, "needs_confirmation": False,
                  "requires_confirmation": False, "assistive": {"type": "dialogue"}}
        if action is not None:
            result["action"] = deepcopy(action)
        return result

    def _selection(self, text):
        match = re.fullmatch(r"(?:选择)?第?("+NUMBER+r")(?:个|条|项)?", text.strip())
        if match:
            value = self._number(match[1])
            return int(value)-1 if int(value) == value else -1
        return None

    def _clarify(self, descriptor, records):
        if len(records) > MAX_OPTIONS:
            return {"response": self._response("符合的记录超过五条，请补充准确名称后再操作。", clarify=True), "pending": None}
        options = [self._canonical(descriptor, record) for record in records]
        spoken = []
        for index, record in enumerate(records):
            when = record.get("due_at")
            if when:
                from .reminder_schedule import calendar_timezone
                when = datetime.fromisoformat(when.replace("Z", "+00:00")).astimezone(calendar_timezone(self._zone())).strftime("%m月%d日%H点%M分")
            spoken.append(f"第{index+1}个，{record['title']}"+(f"，{when}" if when else ""))
        message = "需要选择哪一条？"+"；".join(spoken)+"。请说第一个、第二个等序号，或选择对应按钮。"
        return {"response": self._response(message, options=options, option_labels=spoken, clarify=True),
                "pending": {"descriptor": descriptor, "candidates": [{"id": r["id"], "fingerprint": _hash(r)} for r in records],
                            "expires_at": (self._now()+timedelta(seconds=TTL_SECONDS)).isoformat()}}

    def _resolve(self, text, session_id):
        session = self.store.session(session_id)
        context = deepcopy(session.get(CONTEXT_KEY, {}))
        pending = context.get("pending")
        selection = self._selection(text)
        if text.strip() == "取消生活选择":
            return {"response": self._response("已取消本次生活记录选择，未修改任何生活记录。"), "pending": None, "awaiting_detail": False}
        short_assent = text.strip().rstrip("。！! ") in {"确认", "就这样"}
        if context.get("awaiting_detail") and not pending and (short_assent or selection is not None):
            # Too many/no matching records and expired choices have no valid
            # numbered list. Retain topic ownership without inventing one.
            return {"response": self._response("生活操作仍需补充准确名称或重新发出完整指令，目前没有可按序号执行的候选；如要执行机器人草稿，请明确说确认执行。", clarify=True), "pending": None}
        if pending and short_assent:
            # A short assent belongs to the visible living question, not an
            # older robot plan retained in this session. Explicit robot
            # confirmation still goes through the mission planner.
            if self._now() > datetime.fromisoformat(pending["expires_at"]):
                return {"response": self._response("生活选择已超过五分钟，请重新说明生活操作；如要执行机器人草稿，请明确说确认执行。"), "pending": None}
            descriptor = pending["descriptor"]
            records = [r for r in self._records(descriptor["kind"]) if self._eligible(descriptor, r)
                       and self._matches_title(descriptor["target"], r["title"])]
            result = self._clarify(descriptor, records) if records else {
                "response": self._response("候选生活记录已变化，请重新说明操作。"), "pending": None}
            result["response"]["message"] = "生活记录还需要选择；没有执行机器人草稿。如要执行机器人草稿，请明确说确认执行。"+result["response"]["message"]
            return result
        if selection is not None:
            if not pending:
                return None
            if self._now() > datetime.fromisoformat(pending["expires_at"]):
                return {"response": self._response("上次选择已超过五分钟，请重新说明要处理哪条生活记录。"), "pending": None}
            descriptor = pending["descriptor"]
            records = [r for r in self._records(descriptor["kind"]) if self._eligible(descriptor, r)
                       and self._matches_title(descriptor["target"], r["title"])]
            current = [{"id": r["id"], "fingerprint": _hash(r)} for r in records]
            if current != pending["candidates"]:
                result = self._clarify(descriptor, records) if records else {"response": self._response("候选记录已结束或发生变化，请重新说明操作。"), "pending": None}
                result["response"]["message"] = "原候选已变化，旧序号未执行。"+result["response"]["message"]
                return result
            if not 0 <= selection < len(records):
                return {"response": self._response("序号不在刚才的候选范围内，请重新选择。", clarify=True)}
            return {"action": self._action(descriptor, records[selection]), "record": records[selection], "descriptor": descriptor, "pending": None}
        descriptor = self._descriptor(text)
        if descriptor is None:
            existing = self.service.preview(text)
            if existing is None:
                return None
            return {"action": existing["action"], "response": {**existing, "options": [], "needs_clarification": False, "needs_confirmation": False}, "pending": None}
        records = self._records(descriptor["kind"])
        token = descriptor["token"]
        if token:
            matches = [r for r in records if r["id"].startswith(token[0]) and _hash(r).startswith(token[1])
                       and self._eligible(descriptor, r) and self._matches_title(descriptor["target"], r["title"])]
            if len(matches) != 1:
                return {"response": self._response("这条选择对应的记录已变化或结束，未执行旧选择；请重新发出完整指令。"), "pending": None}
            selected = matches[0]
        else:
            focus = context.get("focus", {}).get(descriptor["kind"])
            generic = not descriptor["target"] or descriptor["target"] in {"这个", "这条", "刚才的", "用品", "清单"}
            matches = [r for r in records if self._eligible(descriptor, r) and self._matches_title(descriptor["target"], r["title"])]
            if len(matches) > 1 and descriptor["target"] not in {"这个", "这条", "刚才的"}:
                return self._clarify(descriptor, matches)
            if generic and focus:
                anchored = next((r for r in records if r["id"] == focus["id"]), None)
                if anchored is None or _epoch(anchored) != focus["epoch"] or not self._eligible(descriptor, anchored):
                    return {"response": self._response("刚才那条记录已经处理或已进入新的时间周期；请说明准确名称重新选择，不会自动处理下一条。")}
            if generic and focus and self._now() <= datetime.fromisoformat(focus["expires_at"]):
                selected = next((r for r in records if r["id"] == focus["id"]), None)
                if selected is None or _epoch(selected) != focus["epoch"] or not self._eligible(descriptor, selected):
                    return {"response": self._response("刚才那条记录已经处理或已进入新的时间周期；请说明准确名称重新选择，不会自动处理下一条。")}
                action = self._action(descriptor, selected)
                comparison = {k:v for k,v in action.items() if k != "expected_revision"}
                effect_present = True
                if action["op"] == "checklist.check":
                    # A structured reset/uncheck may have undone a previous
                    # spoken check without changing this checklist's identity.
                    effect_present = any(item["id"] == action["item_id"] and item["checked"] is action["checked"]
                                         for item in selected["items"])
                if focus.get("last_action") == comparison and effect_present:
                    return {"response": self._response("刚才这项操作已经记录，没有重复处理，也没有改动其他记录。"), "awaiting_detail": False}
            else:
                matches = [r for r in records if self._eligible(descriptor, r) and self._matches_title(descriptor["target"], r["title"])]
                if not matches:
                    return {"response": self._response("没有匹配且可处理的生活记录。请查看名称、到时状态，或先创建记录。"), "pending": None}
                if len(matches) > 1:
                    return self._clarify(descriptor, matches)
                selected = matches[0]
        return {"action": self._action(descriptor, selected), "record": selected, "descriptor": descriptor, "pending": None}

    def preview(self, text, session_id="default"):
        with self.store._lock:
            resolved = self._resolve(text, session_id)
            if resolved is None:
                return None
            if "action" not in resolved:
                return resolved["response"]
            if "response" in resolved:
                return resolved["response"]
            response = self._response("将更新生活记录："+resolved["record"]["title"]+"。", action=resolved["action"])
            response["execution_command"] = self._canonical(resolved["descriptor"], resolved["record"])
            return response

    def command(self, text, session_id="default", request_id=None):
        with self.store._lock:
            resolved = self._resolve(text, session_id)
            if resolved is None:
                return None
            session = self.store.session(session_id)
            context = deepcopy(session.get(CONTEXT_KEY, {}))
            if "pending" in resolved:
                context["pending"] = resolved["pending"]
            # Candidate lists expire, but an unresolved living question must
            # never hand a short assent to an older robot plan. This flag has
            # no action or candidate data and cannot authorize an operation.
            context["awaiting_detail"] = resolved.get("awaiting_detail", "action" not in resolved)
            if "action" in resolved:
                action = deepcopy(resolved["action"])
                if action["op"] == "assistive.status":
                    response = {**self._response("这是本地生活辅助记录。"), "assistive": {"type": "status"}, "snapshot": self.service.snapshot()}
                else:
                    response = self.service.action({**action, **({"request_id": request_id} if request_id else {})})
                    response.update(kind="assistive", options=[], needs_clarification=False, needs_confirmation=False)
                    record = response.get("assistive", {}).get("record")
                    if record and record.get("kind") in {"reminder", "assistance", "wellbeing", "checklist", "need"}:
                        anchor = resolved.get("record", record) if action["op"] == "reminder.ack" else record
                        context.setdefault("focus", {})[record["kind"]] = {"id": record["id"], "epoch": _epoch(anchor),
                            "expires_at": (self._now()+timedelta(seconds=TTL_SECONDS)).isoformat(),
                            "last_action": {k:v for k,v in action.items() if k != "expected_revision"}}
            else:
                response = resolved["response"]
            session[CONTEXT_KEY] = context
            self.store.save_session(session_id, session)
            return response
