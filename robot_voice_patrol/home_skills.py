"""Bounded household actuation and evidence-based delivery contracts.

These are driver interfaces, not implementations of grasping or human care.
No arbitrary appliance identifiers, network URLs or physical-care commands.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from .contracts import CommandError, Plan, Step

HOME_SKILLS = {"home_control", "pick_object", "place_object", "handover_object"}
SKILL_CODES = {"capture": 1, "dock": 2, "follow": 3, "turn": 4,
               "home_control": 5, "pick_object": 6, "place_object": 7, "handover_object": 8}
DEVICES = {"light": "灯", "curtain": "窗帘", "fan": "风扇", "television": "电视"}
ITEMS = ("空水杯", "密封饮用水", "手机", "遥控器", "纸巾", "毛巾", "眼镜", "书", "钥匙")


def validate_home_step(step, config):
    if step.kind != "home_control" and step.params["item"] not in config["object_names"]:
        raise CommandError("物品未配置，请先添加物品及感知接口")
    if step.max_retries != 0 or step.on_failure != "abort":
        raise CommandError("生活辅助动作必须禁用自动重试并在失败时停止；先核实物品和设备状态")
    return step


def validate_evidence(step, evidence):
    """Called for BOTH software fixtures and ROS terminal responses."""
    from .skills import SkillExecutionError
    valid = isinstance(evidence, dict)
    if valid and step.kind == "home_control":
        valid = (evidence.get("device") == step.params["device"] and
                 evidence.get("target") == step.target and
                 evidence.get("reported_state") == step.params["state"] and
                 evidence.get("readback_confirmed") is True)
    elif valid:
        valid = evidence.get("item") == step.params["item"] and evidence.get("target") == step.target
        if step.kind == "pick_object":
            valid = valid and evidence.get("payload_confirmed") is True
        elif step.kind == "place_object":
            valid = (valid and evidence.get("released") is True and evidence.get("surface_confirmed") is True
                     and evidence.get("surface") == step.params["surface"])
        elif step.kind == "handover_object":
            valid = (valid and evidence.get("released") is True and
                     evidence.get("recipient") == step.params["recipient"] and
                     evidence.get("recipient_acknowledged") is True and
                     isinstance(evidence.get("receipt_id"), str) and 1 <= len(evidence["receipt_id"]) <= 128)
    if not valid:
        raise SkillExecutionError("HOME_EVIDENCE_UNCONFIRMED", "设备回读、载荷或交接证据未确认，不能宣告完成")


def execute_home(step, adapter, cancel, feedback, context):
    from .skills import SkillExecutionError
    if step.kind == "pick_object":
        results = context.get("results", [])
        observations = [(i, r) for i, r in enumerate(results) if r.get("kind") == "inspect"
                        and r.get("target") == step.target and r.get("object_name") == step.params["item"]]
        if not observations or observations[-1][1].get("outcome") != "found" or observations[-1][1].get("status") != "succeeded":
            raise SkillExecutionError("OBJECT_UNCONFIRMED", "当前任务没有明确发现此物品，停止抓取")
        index, observation = observations[-1]
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(observation["observed_at"].replace("Z", "+00:00"))).total_seconds()
            fresh = -1 <= age <= 10
        except (KeyError, ValueError, TypeError, AttributeError):
            fresh = False
        moved = any(r.get("kind") in {"navigate", "turn", "follow", "dock", "pick_object", "place_object", "handover_object"}
                    and r.get("status") != "skipped" for r in results[index+1:])
        if not fresh or moved:
            raise SkillExecutionError("OBJECT_EVIDENCE_STALE", "抓取前需在当前位置重新观察；物品证据过期或其后发生过运动/操作")
    result = adapter.execute(step, cancel, feedback)
    validate_evidence(step, result.get("evidence"))
    return result


def register_home_skills(registry):
    from .skills import SkillSpec, _schema, _string
    schemas = {
        "home_control": _schema({"device": _string(enum=list(DEVICES)),
                                 "state": _string(enum=["on", "off"])}, ["device", "state"]),
        "pick_object": _schema({"item": _string(enum=list(ITEMS))}, ["item"]),
        "place_object": _schema({"item": _string(enum=list(ITEMS)),
                                 "surface": _string(80)}, ["item", "surface"]),
        "handover_object": _schema({"item": _string(enum=list(ITEMS)),
                                    "recipient": _string(80)}, ["item", "recipient"]),
    }
    labels = {"home_control": "家居控制与回读", "pick_object": "取物接口", "place_object": "放置接口", "handover_object": "确认交接接口"}
    for kind, schema in schemas.items():
        registry.register(SkillSpec(kind, labels[kind], "需要硬件能力及本次执行证据；软件测试会明确标记", schema,
                                    execute_home, "required", True, validate_home_step))


def plan_home_command(text, config):
    """Exact supported clauses only; never silently discard a second request."""
    normalized = re.sub(r"\s+", "", text).strip("。！!？?")
    names = {name: key for key, place in config["locations"].items()
             for name in [key, place["label"], *place.get("aliases", [])]}
    loc = "(?:" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + ")"
    device = re.fullmatch(r"(?:请|帮我)?(打开|关闭)(" + loc + r")(?:的)?(灯|窗帘|风扇|电视)", normalized)
    if device:
        operation, location, label = device.groups()
        kind = next(k for k, v in DEVICES.items() if v == label)
        return Plan(text, [Step("home_control", names[location], step_id="device", max_retries=0,
                               params={"device": kind, "state": "on" if operation == "打开" else "off"})],
                    f"{operation}{location}{label}，等待设备状态回读", {"planner": "household_rules"})
    delivery = re.fullmatch(r"(?:请|帮我)?把(" + "|".join(ITEMS) + r")从(" + loc + r")(?:送到|拿到)(" + loc + r")", normalized)
    if delivery:
        item, source, destination = delivery.groups()
        start, end = names[source], names[destination]
        steps = [Step("navigate", start, step_id="arrive_source"),
                 Step("inspect", start, object_name=item, step_id="locate"),
                 Step("pick_object", start, step_id="pickup", max_retries=0, params={"item": item}),
                 Step("navigate", end, step_id="carry", max_retries=0),
                 Step("handover_object", end, step_id="handover", max_retries=0, params={"item": item, "recipient": "本人"})]
        return Plan(text, steps, f"在{source}确认{item}，取物后送到{destination}，等待本人接收确认",
                    {"planner": "household_rules", "requires_confirmation": True,
                     "goal": {"kind": "deliver_object", "object_name": item}})
    if any(token in normalized for token in ("打开", "关闭", "拿来", "抓取", "送到", "拿到", "喂我", "抱我", "扶我")):
        # Reject physical requests before optional models can invent a capability.
        raise CommandError("请使用已支持的完整命令，如“打开卧室灯”或“把手机从客厅送到卧室”；身体照护请创建人工协助请求")
    return None
