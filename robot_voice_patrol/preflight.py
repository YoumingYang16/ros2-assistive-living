"""Read-only plan inspection. Readiness is a snapshot, never a dispatch guarantee."""
from __future__ import annotations

import copy
from collections import Counter
from .contracts import CommandError
from .plan_validation import plan_from_dict, validate_plan
from .workflow import compile_workflow
from .skills import get_registry


def structured_plan(payload, config):
    if not isinstance(payload, dict):
        raise CommandError("请提供工作流或计划对象")
    selected = [key for key in ("workflow", "plan") if payload.get(key) is not None]
    if len(selected) != 1:
        raise CommandError("请且只提供 workflow 或 plan；预检不会修改自然语言会话")
    if selected[0] == "workflow":
        return compile_workflow(payload["workflow"], config, parameters=payload.get("parameters"))
    if payload.get("parameters") is not None:
        raise CommandError("parameters 只能与 workflow 一起使用")
    return plan_from_dict(payload["plan"], config)


def inspect_plan(plan, config, adapter, lifecycle=None, *, busy=False):
    plan = validate_plan(copy.deepcopy(plan), config)
    catalog = {item["name"]: item for item in get_registry().catalog(adapter)}
    robot = adapter.snapshot()
    findings = []

    def note(severity, code, message, step_id=None):
        findings.append({"severity": severity, "code": code, "message": message, "step_id": step_id})

    if lifecycle is not None and not lifecycle.get("active"):
        note("blocker", "LIFECYCLE_INACTIVE", "执行生命周期尚未激活")
    if busy:
        note("blocker", "ENGINE_BUSY", "已有任务占用执行器，可加入队列或等待当前任务结束")
    if plan.metadata.get("config_fingerprint"):
        from .scheduler import config_fingerprint
        if plan.metadata["config_fingerprint"] != config_fingerprint(config):
            note("blocker", "CONFIG_CHANGED", "计划生成后配置已变化，请重新生成计划")
    if any(robot.get(key) for key in ("action_pending", "navigation_pending", "cancellation_pending")):
        note("blocker", "EXTERNAL_ACTION_PENDING", "外部操作仍未确认结束")
    if plan.metadata.get("requires_confirmation"):
        note("blocker", "CONFIRMATION_REQUIRED", "该草稿需要明确确认后才能提交")
    possible_locations = {robot.get("location") if robot.get("pose_valid") else None}
    conditional, attempts, budget = 0, 0, 0.0
    for step in plan.steps:
        capability = catalog[step.kind]
        if not capability["available"]:
            note("blocker", "CAPABILITY_UNAVAILABLE", f"{capability['label']}尚未连接或能力声明不可用", step.step_id)
        if step.condition:
            conditional += 1
        attempts += 1 + step.max_retries
        budget += step.timeout * (1 + step.max_retries)
        if step.kind in {"inspect", "capture"} and step.target and possible_locations != {step.target}:
            note("warning", "LOCATION_NOT_GUARANTEED", "此前步骤不能保证已经到达观察地点；请核对导航和失败分支", step.step_id)
        if step.kind in {"navigate", "dock"}:
            if step.condition is None and step.on_failure == "abort":
                possible_locations = {step.target}
            else:
                possible_locations |= {step.target, None}
        elif step.kind == "follow":
            possible_locations = {None}
        if step.on_failure == "continue":
            note("info", "EXPLICIT_FAILURE_CONTINUATION", "该步骤明确允许失败后继续；未决外部动作仍会阻塞", step.step_id)
    if conditional:
        note("info", "CONDITIONAL_EXECUTION", "条件结果由实际证据决定；未执行的分支不会计为目标达成")
    if adapter.mode == "mock":
        note("info", "SIMULATED_ADAPTER", "当前执行适配器使用预设数据，不能验证真实运动或感知能力")
    counts = Counter(step.kind for step in plan.steps)
    return {"ok": True, "ready": not any(item["severity"] == "blocker" for item in findings),
            "read_only": True, "dispatch_guarantee": False, "mode": adapter.mode,
            "summary": plan.summary, "steps": len(plan.steps), "conditional_steps": conditional,
            "skills": dict(counts), "maximum_attempts": attempts,
            "configured_timeout_budget_seconds": round(budget, 3),
            "budget_note": "全部步骤与重试的配置超时之和，非实际耗时预测；不含远端取消确认等待。",
            "findings": findings, "plan": plan.to_dict()}
