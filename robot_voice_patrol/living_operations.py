"""Support interruptions, equipment upkeep and read-only care handovers.

This module records reports and prepares assistance; it does not infer a
medical condition, detect hazards, contact anyone or repair a device.
All writes use the caller's existing SQLite transaction and receipt.
"""
from __future__ import annotations
from copy import deepcopy
from datetime import timedelta

INCIDENTS = {
    "power_failure": ("停电", "确认可用照明、通信及辅助设备的备用安排"),
    "network_failure": ("网络中断", "使用本人事先安排的离线或人工联系渠道"),
    "device_failure": ("辅助设备故障", "停止依赖故障设备，联系人工检查"),
    "blocked_route": ("通道或出口受阻", "请求人员核实通道，不让机器人自行强行通过"),
    "caregiver_absent": ("照护人员未到", "核实原有安排并联系本人同意的备用人员"),
    "extreme_weather": ("恶劣天气影响", "核实本人出行和居家安排，等待人工协助"),
    "lost_communication": ("呼叫设备不可用", "请使用本人已有的其他现实求助方式"),
}


def dispatch(service, body):
    from .assistive_service import _text, _date, _choice, _iso, _number
    from .contracts import CommandError
    op=body["op"]
    if op == "handover.build":
        service._args(body, (), ("note",))
        note=_text(body.get("note", ""), "note", empty=True)
        now=service._now()
        # Query all active records; dashboard's 250-row display limit must not hide work.
        rows=service.store._connection.execute("SELECT snapshot FROM assistive_records WHERE state NOT IN ('removed','cancelled','resolved','completed') AND NOT (kind='reminder' AND state='acknowledged') ORDER BY updated_at").fetchall()
        import json
        active=[json.loads(row[0]) for row in rows]
        report={"generated_at":_iso(now),"note":note,"source":"local_records_not_independently_verified",
                "external_messages_sent":0,"reminders":[],"assistance":[],"incidents":[],"equipment":[],"needs":[],"checklists":[],"wellbeing":[]}
        names={"reminder":"reminders","assistance":"assistance","incident":"incidents","equipment":"equipment","need":"needs","checklist":"checklists","wellbeing":"wellbeing"}
        for record in active:
            if record["kind"] not in names:continue
            if record["kind"]=="reminder" and _date(record["due_at"])>now+timedelta(days=1):continue
            # Do not include contact details or full private history in a handover by default.
            allowed={"id","kind","state","title","category","due_at","urgency","delivery_status","acknowledgement_source","assistance_id","service_due_at","items","updated_at","source","escalation_error"}
            report[names[record["kind"]]].append({k:deepcopy(v) for k,v in record.items() if k in allowed})
        report["counts"]={key:len(report[key]) for key in names.values()}
        return {"ok":True,"message":"已生成本机待办交接摘要，尚未分享给任何人。", "assistive":{"type":"handover","report":report}}
    if op == "wellbeing.start":
        service._args(body,(),("seconds","title"))
        seconds=_number(body.get("seconds",1800),"seconds",30,86400)
        record=service._new("wellbeing","waiting",title=_text(body.get("title","本人平安确认"),"title",100),
                            due_at=_iso(service._now()+timedelta(seconds=seconds)),assistance_id=None,
                            source="user_opted_in",health_inference=None)
        message="已开始本人自选的确认等待；到时未确认会创建本机协助记录，不推断发生危险，也不会自动对外呼叫。"
    elif op in {"wellbeing.confirm","wellbeing.cancel"}:
        service._args(body,("id",))
        record=service._load(body["id"],"wellbeing")
        if record["state"] not in {"waiting","overdue"}:raise CommandError("这次确认等待已经结束")
        record.update(state="completed" if op.endswith("confirm") else "cancelled",
                      confirmed_by_user=op.endswith("confirm"),finished_at=_iso(service._now()))
        message="已记录本人确认。" if op.endswith("confirm") else "已取消本次等待。"
        if record["assistance_id"]:message+="此前已生成的人工协助记录需另行核实处理。"
    elif op == "incident.create":
        service._args(body,("category",),("detail",))
        category=_choice(body["category"],INCIDENTS,"category")
        label, response=INCIDENTS[category]
        detail=_text(body.get("detail", ""),"detail",empty=True)
        # Repeated equivalent active interruption reports attach to the existing event.
        rows=service.store._connection.execute("SELECT snapshot FROM assistive_records WHERE kind='incident' AND state IN ('open','acknowledged')").fetchall()
        import json
        existing=next((json.loads(r[0]) for r in rows if json.loads(r[0])["category"]==category),None)
        if existing:
            return service._response("此类异常已有未结束记录，请在原记录中核实处理。",existing,already_exists=True)
        request=service._dispatch({"op":"assistance.create","category":"support_interruption","detail":label+"；"+detail,
                                   "urgency":"urgent" if category in {"blocked_route","lost_communication"} else "normal"})["assistive"]["record"]
        record=service._new("incident","open",category=category,title=label,detail=detail,
                            source="local_user_report",sensor_confirmed=False,physical_recovery_confirmed=False,
                            assistance_id=request["id"],suggested_coordination=response)
        message="异常已按本人报告记录，同时建立尚未发送的人工协助请求。"
    elif op in {"incident.ack", "incident.resolve"}:
        service._args(body,("id",),("note",))
        record=service._load(body["id"],"incident")
        if record["state"]=="resolved":raise CommandError("异常已结束")
        note=_text(body.get("note", ""),"note",empty=True)
        record.update(state="acknowledged" if op.endswith("ack") else "resolved",
                      report_note=note,report_source="local_user_report",physical_recovery_confirmed=False)
        message="已记录本人核实结果；未独立确认供电、网络、设备或人员状态。关联协助请求需另行核对。"
    elif op == "equipment.add":
        service._args(body,("title","category"),("service_due_at","note"))
        title=_text(body["title"],"title",100)
        category=_choice(body["category"],{"mobility_aid","communication","home_device","other"},"category")
        due=_iso(_date(body["service_due_at"],"service_due_at")) if "service_due_at" in body else None
        reminder=None
        if due:
            reminder=service._dispatch({"op":"reminder.create","title":"检查维护："+title,"due_at":due,"category":"general"})["assistive"]["record"]["id"]
        record=service._new("equipment","active",title=title,category=category,service_due_at=due,
                            note=_text(body.get("note", ""),"note",empty=True),reminder_id=reminder,
                            source="local_user_record",hardware_verified=False,service_reports=[])
        message="设备台账已登记；维护日期由本人填写，不代表已连接或检查过设备。"
    elif op == "equipment.service":
        service._args(body,("id",),("next_due_at","note"))
        record=service._load(body["id"],"equipment")
        if record["state"]!="active":raise CommandError("设备已停用")
        note=_text(body.get("note", ""),"note",empty=True)
        due=_iso(_date(body["next_due_at"],"next_due_at")) if "next_due_at" in body else None
        if record.get("reminder_id"):
            reminder=service._load(record["reminder_id"],"reminder")
            if reminder["state"] not in {"acknowledged","cancelled"}:
                service._dispatch({"op":"reminder.cancel","id":reminder["id"]})
        reminder_id=None
        if due:
            reminder_id=service._dispatch({"op":"reminder.create","title":"检查维护："+record["title"],"due_at":due})["assistive"]["record"]["id"]
        record.update(service_due_at=due,reminder_id=reminder_id)
        record["service_reports"]=[*record["service_reports"],{"time":_iso(service._now()),"note":note,"source":"local_user_report","independently_verified":False}][-100:]
        message="本人维护记录已保存，后续提醒已更新；未独立核实设备安全或维修结果。"
    elif op == "equipment.retire":
        service._args(body,("id",))
        record=service._load(body["id"],"equipment")
        if record["state"]!="active":raise CommandError("设备已停用")
        if record.get("reminder_id"):
            reminder=service._load(record["reminder_id"],"reminder")
            if reminder["state"] not in {"acknowledged","cancelled"}:
                service._dispatch({"op":"reminder.cancel","id":reminder["id"]})
        record.update(state="completed",retired_at=_iso(service._now()))
        message="设备已从在用台账停用，维护提醒已取消。"
    else:raise CommandError("未知生活支持操作")
    service._save(record);service._audit(op,record)
    return service._response(message,record)


def advance_wellbeing(service, now):
    """Called in the service tick transaction; absence of response is not diagnosis."""
    from .assistive_service import _date
    from .contracts import CommandError
    import json
    changed=0
    rows=service.store._connection.execute("SELECT snapshot FROM assistive_records WHERE kind='wellbeing' AND state IN ('waiting','overdue')").fetchall()
    for row in rows:
        record=json.loads(row[0])
        if _date(record["due_at"])>now or record.get("assistance_id"):continue
        connection=service.store._connection
        connection.execute("SAVEPOINT wellbeing_help")
        try:
            request=service._dispatch({"op":"assistance.create","category":"support_interruption",
                "detail":"未在本人设定的时间收到确认，需要人工核实；这不证明发生危险。","urgency":"urgent"})["assistive"]["record"]
        except CommandError as exc:
            # A full assistance ledger must not roll back unrelated due reminders.
            connection.execute("ROLLBACK TO wellbeing_help")
            connection.execute("RELEASE wellbeing_help")
            error=str(exc)
            if record["state"]!="overdue" or record.get("escalation_error")!=error:
                record.update(state="overdue",escalation_error=error)
                service._save(record);service._audit("wellbeing.help_pending",record);changed+=1
        else:
            connection.execute("RELEASE wellbeing_help")
            record.update(state="overdue",assistance_id=request["id"],escalation_error=None)
            service._save(record);service._audit("wellbeing.overdue",record);changed+=1
    return changed
