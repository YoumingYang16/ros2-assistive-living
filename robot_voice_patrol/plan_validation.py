"""V3 trust boundary and three-valued, bounded condition evaluation."""
from __future__ import annotations
from dataclasses import replace
import copy
import json
import math
import re
from .contracts import CommandError, Plan, Step

OUTCOMES = {"found", "not_found", "inconclusive", "succeeded", "failed", "timed_out", "skipped"}
MAX_STEPS, MAX_CONDITION_DEPTH, MAX_CONDITION_NODES = 100, 8, 64


def _number(value, name, lower, upper, *, inclusive=True):
    try:
        finite = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or value < lower or not inclusive and value == lower or value > upper:
        raise CommandError(f"{name} 必须是范围 {lower}..{upper} 内的有限数值")
    return float(value)


def _json_copy(value, name, maximum=16384):
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(encoded) > maximum:
            raise CommandError(f"{name} 过大")
        return json.loads(encoded)
    except (TypeError, ValueError, RecursionError):
        raise CommandError(f"{name} 必须是大小受限的有效 JSON") from None


def validate_condition(condition, prior):
    count = 0
    def visit(node, depth):
        nonlocal count
        count += 1
        if depth > MAX_CONDITION_DEPTH or count > MAX_CONDITION_NODES or type(node) is not dict:
            raise CommandError("条件超过深度/节点限制，或结构不是对象")
        keys = set(node)
        if keys in ({"all"}, {"any"}):
            key = next(iter(keys))
            if not isinstance(node[key], list) or not 1 <= len(node[key]) <= 16:
                raise CommandError("all/any 必须包含 1..16 个条件")
            return {key: [visit(child, depth+1) for child in node[key]]}
        if keys == {"not"}:
            return {"not": visit(node["not"], depth+1)}
        if keys != {"step_id", "outcome"}:
            raise CommandError("条件仅支持 all / any / not 或 step_id+outcome")
        identifier, outcome = node["step_id"], node["outcome"]
        if not isinstance(identifier, str) or identifier not in prior:
            raise CommandError("条件只能引用先前步骤，不能循环或引用未来步骤")
        if not isinstance(outcome, str) or outcome not in OUTCOMES:
            raise CommandError("条件结果类型无效")
        source = prior[identifier]
        if outcome in {"found", "not_found", "inconclusive"} and (source.kind != "inspect" or not source.object_name):
            raise CommandError("物体结果条件必须引用指定物体的观察步骤")
        return {"step_id": identifier, "outcome": outcome}
    return visit(condition, 1)


def validate_plan(plan, config):
    if not isinstance(plan, Plan) or not isinstance(plan.steps, list) or not 1 <= len(plan.steps) <= MAX_STEPS:
        raise CommandError("任务必须包含 1 到 100 个步骤")
    if not isinstance(plan.command, str) or len(plan.command) > 2000:
        raise CommandError("任务原文过长或格式无效")
    if not isinstance(plan.summary, str) or not plan.summary.strip() or len(plan.summary) > 12000:
        raise CommandError("任务摘要为空或过长")
    if type(plan.metadata) is not dict:
        raise CommandError("任务元数据格式无效")
    from .skills import get_registry
    registry = get_registry()
    steps, prior = [], {}
    for index, step in enumerate(plan.steps):
        if not isinstance(step, Step) or not isinstance(step.kind, str):
            raise CommandError("任务包含无效技能")
        if not isinstance(step.step_id, str):
            raise CommandError("步骤 ID 必须为字符串")
        identifier = step.step_id or f"s{index+1:03d}"
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", identifier) or identifier in prior:
            raise CommandError("步骤 ID 无效或重复")
        _number(step.seconds, "等待时间", 0, 3600)
        _number(step.timeout, "步骤超时", 0, 3605, inclusive=False)
        if type(step.max_retries) is not int or not 0 <= step.max_retries <= 3:
            raise CommandError("重试次数必须在 0 到 3 之间")
        if not isinstance(step.on_failure, str) or step.on_failure not in {"abort", "continue"}:
            raise CommandError("on_failure 只能为 abort 或 continue")
        if type(step.params) is not dict:
            raise CommandError("技能 params 必须为对象")
        params = _json_copy(step.params, "技能参数", 8192)
        condition = validate_condition(step.condition, prior) if step.condition is not None else None
        normalized = registry.validate_step(replace(step, step_id=identifier, condition=condition, params=params), config)
        if not isinstance(normalized, Step):
            raise CommandError("技能验证器返回无效步骤")
        steps.append(normalized)
        prior[identifier] = normalized
    metadata = _json_copy(plan.metadata, "任务元数据")
    goal = metadata.get("goal")
    if goal is not None:
        if type(goal) is not dict or set(goal) != {"kind", "object_name"} or goal.get("kind") not in {"find_object", "deliver_object"}:
            raise CommandError("goal 必须是 kind=find_object 与 object_name")
        if goal.get("object_name") not in config["object_names"] or not any(step.kind == "inspect" and step.object_name == goal["object_name"] for step in steps):
            raise CommandError("查找目标必须是配置物体，且计划包含相应观察步骤")
        if goal["kind"] == "deliver_object" and not any(step.kind == "handover_object" and step.params["item"] == goal["object_name"] for step in steps):
            raise CommandError("送物目标必须包含同一物品的确认交接步骤")
    return Plan(plan.command, steps, plan.summary, metadata)


def plan_from_dict(data, config, command=""):
    if not isinstance(data, dict) or set(data) - {"command", "steps", "summary", "metadata", "version"}:
        raise CommandError("计划包含未知字段或不是对象")
    if type(data.get("version", 3)) is not int or data.get("version", 3) not in (1, 2, 3):
        raise CommandError("不支持的计划版本")
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= MAX_STEPS:
        raise CommandError("计划步骤数量无效")
    allowed = {"kind", "target", "seconds", "object_name", "timeout", "max_retries", "step_id", "condition", "params", "on_failure"}
    steps = []
    for raw in raw_steps:
        if not isinstance(raw, dict) or set(raw) - allowed or "kind" not in raw:
            raise CommandError("步骤结构包含未知字段")
        try:
            steps.append(Step(**raw))
        except TypeError:
            raise CommandError("步骤结构无效") from None
    return validate_plan(Plan(command or data.get("command", ""), steps, data.get("summary", ""), data.get("metadata", {})), config)


def condition_value(condition, results):
    """Kleene three-valued logic: unknown stays unknown under NOT."""
    if "all" in condition:
        values = [condition_value(child, results) for child in condition["all"]]
        return False if False in values else None if None in values else True
    if "any" in condition:
        values = [condition_value(child, results) for child in condition["any"]]
        return True if True in values else None if None in values else False
    if "not" in condition:
        value = condition_value(condition["not"], results)
        return None if value is None else not value
    source = next((r for r in reversed(results) if r.get("step_id") == condition["step_id"]), None)
    if source is None:
        return None
    expected, status = condition["outcome"], source.get("status")
    if status not in {"succeeded", "failed", "timed_out", "skipped", "cancelled"}:
        return None
    if expected in {"found", "not_found", "inconclusive"}:
        if status != "succeeded":
            return None
        actual = source.get("outcome")
        if actual not in {"found", "not_found", "inconclusive"}:
            return None
        if expected in {"found", "not_found"} and actual == "inconclusive":
            return None
        return actual == expected
    if expected == "succeeded":
        return status == "succeeded"
    if expected == "skipped":
        return status == "skipped"
    timed_out = status == "timed_out" or source.get("outcome") == "timed_out" or "TIMEOUT" in str(source.get("error_code", "")).upper() or "TIMED_OUT" in str(source.get("error_code", "")).upper()
    if expected == "timed_out":
        return timed_out if status in {"failed", "timed_out"} else False
    return status == "failed" and not timed_out


def condition_matches(step, results):
    if step.condition is None:
        return True, ""
    value = condition_value(step.condition, results)
    return value is True, "" if value is True else "条件未知：缺少明确结果" if value is None else "条件不满足"


def evaluate_goal(plan, results):
    """Evaluate task purpose separately from whether process steps completed."""
    goal = plan.metadata.get("goal")
    if not goal:
        return {"outcome": "not_applicable", "reason": "计划没有声明可判定目标", "supporting_step_ids": []}
    if goal.get("kind") == "deliver_object":
        deliveries = [r for r in results if r.get("kind") == "handover_object" and r.get("status") == "succeeded"
                      and r.get("evidence", {}).get("item") == goal["object_name"]
                      and r.get("evidence", {}).get("recipient_acknowledged") is True]
        return {"outcome": "achieved" if deliveries else "not_achieved", "goal": copy.deepcopy(goal),
                "reason": "已收到交接证据（模拟结果另行标注）" if deliveries else "尚无确认交接证据",
                "supporting_step_ids": [r["step_id"] for r in deliveries]}
    relevant = [step for step in plan.steps if step.kind == "inspect" and step.object_name == goal["object_name"]]
    by_id = {result.get("step_id"): result for result in results}
    found = [step.step_id for step in relevant if by_id.get(step.step_id, {}).get("status") == "succeeded" and by_id[step.step_id].get("outcome") == "found"]
    if found:
        return {"outcome": "achieved", "reason": "观察证据确认找到目标", "supporting_step_ids": found, "goal": copy.deepcopy(goal)}
    negative = [step.step_id for step in relevant if by_id.get(step.step_id, {}).get("status") == "succeeded" and by_id[step.step_id].get("outcome") == "not_found"]
    if relevant and len(negative) == len(relevant):
        return {"outcome": "not_achieved", "reason": "所有计划地点均明确未发现目标", "supporting_step_ids": negative, "goal": copy.deepcopy(goal)}
    return {"outcome": "unknown", "reason": "仍有未执行、失败或不确定的观察，不能确定目标结果", "supporting_step_ids": negative, "goal": copy.deepcopy(goal)}
