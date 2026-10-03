"""Build an explicit remaining-work draft from durable, verified checkpoints."""
from __future__ import annotations
import copy
import hashlib
from dataclasses import replace
from .contracts import CommandError, Plan, Step
from .plan_validation import plan_from_dict, validate_plan, condition_value
from .scheduler import config_fingerprint


def _simplify(condition, completed, negate=False):
    if condition is None:
        return not negate
    if "step_id" in condition:
        if condition["step_id"] not in completed:
            return {"not": copy.deepcopy(condition)} if negate else copy.deepcopy(condition)
        value = condition_value(condition, list(completed.values()))
        # Push NOT to leaves before reducing unknown. Only True dispatches;
        # unknown cannot become a positive condition through negation.
        return False if value is None else (not value if negate else value)
    if "not" in condition:
        return _simplify(condition["not"], completed, not negate)
    operator = "all" if "all" in condition else "any"
    children = [_simplify(child, completed, negate) for child in condition[operator]]
    if negate:
        operator = "any" if operator == "all" else "all"
    if operator == "all":
        if False in children:
            return False
        pending = [child for child in children if child is not True]
        return {"all": pending} if pending else True
    if True in children:
        return True
    pending = [child for child in children if child is not False]
    return {"any": pending} if pending else False


def recovery_preview(engine, mission_id):
    mission = engine.store.mission(mission_id)
    if not mission or mission["state"] not in {"interrupted", "failed", "cancelled"}:
        raise CommandError("只能为中断、失败或取消的历史任务生成恢复计划")
    if mission.get("resumed_as"):
        raise CommandError("该任务已创建恢复任务，请查看关联任务：" + mission["resumed_as"])
    if engine._hardware_locked():
        return {"ok": True, "plan": None, "blocked": True, "requires_confirmation": True,
                "assessment": {"source_mission_id": mission_id, "automatic_replay": False,
                               "external": {"verified": False, "remote_state": "unknown"}},
                "message": "硬件未知状态互锁仍在；确认历史记录或 ROS 终态不能代替可信本机核实"}
    if not engine.can_dispatch():
        raise CommandError("当前任务或外部操作尚未结束，不能恢复")
    reconcile = getattr(engine.adapter, "reconcile_mission", None)
    if reconcile:
        external = reconcile(mission)
    else:
        external = {"verified": engine.adapter.mode == "mock", "remote_state": "none" if engine.adapter.mode == "mock" else "unknown",
                    "reason": "软件模拟没有外部残留动作" if engine.adapter.mode == "mock" else "接口未提供历史动作核对能力"}
    assessment = {"external": external, "original_state": mission["state"], "source_mission_id": mission_id,
                  "automatic_replay": False, "completed_steps_preserved": [], "omitted_steps": []}
    if not external.get("verified") or external.get("remote_state") in {"active", "unknown"}:
        return {"ok": True, "plan": None, "assessment": assessment, "blocked": True,
                "requires_confirmation": True, "message": "外部动作状态尚未核对，不能生成可执行恢复计划"}
    original = plan_from_dict({k: mission[k] for k in ("command", "steps", "summary", "metadata", "version") if k in mission}, engine.config)
    from .engine import NON_REPLAYABLE_SKILLS
    if any(step.kind in NON_REPLAYABLE_SKILLS for step in original.steps):
        return {"ok": True, "plan": None, "assessment": assessment, "blocked": True,
                "requires_confirmation": True,
                "message": "外部机械动作的载荷、交接、姿态或设备状态需要重新核实；不从历史检查点重放，请核实后创建新的明确任务"}
    completed = {r["step_id"]: r for r in mission.get("results", [])
                 if r.get("status") in {"succeeded", "skipped"} or r.get("handled_failure")}
    remaining = []
    for step in original.steps:
        if step.step_id in completed:
            assessment["completed_steps_preserved"].append(step.step_id)
            continue
        condition = _simplify(step.condition, completed)
        if condition is False:
            completed[step.step_id] = {"step_id": step.step_id, "status": "skipped"}
            assessment["omitted_steps"].append(step.step_id)
            continue
        remaining.append(replace(step, condition=None if condition is True else condition))
    if not remaining:
        return {"ok": True, "plan": None, "assessment": assessment, "requires_confirmation": False,
                "message": "没有需要继续执行的步骤，可核对历史记录"}
    # Historical arrival does not establish the pose after process restart.
    # Any inspect without a preceding remaining navigation gets an explicit new navigation.
    rebuilt = []
    last_target = None
    used_ids = {step.step_id for step in original.steps}
    for step in remaining:
        if step.kind == "inspect" and last_target != step.target:
            navigation_id = "recover_" + step.step_id
            if len(navigation_id) > 64 or navigation_id in used_ids:
                suffix = hashlib.sha256(step.step_id.encode()).hexdigest()[:16]
                navigation_id = "recover_nav_" + suffix
                index = 1
                while navigation_id in used_ids:
                    navigation_id = "recover_nav_" + suffix + "_" + str(index)
                    index += 1
            used_ids.add(navigation_id)
            rebuilt.append(Step("navigate", step.target, timeout=engine.config["navigation_timeout"],
                                step_id=navigation_id, condition=copy.deepcopy(step.condition)))
        rebuilt.append(step)
        if step.kind == "navigate":
            last_target = step.target if step.condition is None else None
        elif step.kind == "inspect":
            last_target = step.target if step.condition is None else None
    metadata = {**original.metadata, "parent_mission_id": mission_id, "recovery": assessment,
                "requires_confirmation": True, "config_fingerprint": config_fingerprint(engine.config)}
    goal = metadata.get("goal")
    if goal and not any(s.kind == "inspect" and s.object_name == goal["object_name"] for s in rebuilt):
        metadata["prior_goal"] = metadata.pop("goal")
    plan = validate_plan(Plan("恢复任务 " + mission_id, rebuilt, "继续剩余步骤：" + original.summary, metadata), engine.config)
    return {"ok": True, "plan": plan.to_dict(), "assessment": assessment, "requires_confirmation": True,
            "message": "已核对记录并生成剩余步骤；确认后创建关联的新任务"}
