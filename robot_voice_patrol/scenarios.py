"""Isolated, bounded scenario execution using the production mission engine."""
from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timezone
import threading
import time
import uuid
from .contracts import CommandError, ExecutionCancelled, Plan
from .mock_adapter import MockAdapter, MockExecutionError
from .preflight import structured_plan, inspect_plan


SCENARIOS = (
    {"id": "configured", "label": "正常预设", "description": "沿用地点中的预设物体。"},
    {"id": "empty", "label": "所有地点未找到", "description": "观察返回明确未发现，验证多地点搜索和目标未达成。"},
    {"id": "inconclusive", "label": "观察无法判断", "description": "目标观察均返回未知，验证未知不会被误当作未找到。"},
    {"id": "sensor_failure", "label": "感知接口失败", "description": "观察返回不可重试的传感器错误。"},
    {"id": "navigation_timeout", "label": "导航超时", "description": "导航直接返回确定的超时终态，验证备用分支。"},
    {"id": "transient_navigation", "label": "首次导航失败后恢复", "description": "每个导航步骤首次失败，之后成功，验证有限重试。"},
)
BUILTIN_SKILLS = frozenset({"navigate", "inspect", "wait", "speak", "report", "wait_state", "capture", "dock", "follow", "turn"})


class ScenarioBusy(CommandError):
    """A second validation job must not consume another execution worker."""


class ScenarioAdapter(MockAdapter):
    def __init__(self, config, identifier):
        super().__init__(config, fixture_skills=True)
        self.scenario_id, self.attempts = identifier, {}

    def execute(self, step, cancel, feedback):
        if cancel.is_set():
            raise ExecutionCancelled("场景运行已取消")
        self.attempts[step.step_id] = self.attempts.get(step.step_id, 0) + 1
        if step.kind == "navigate":
            if self.scenario_id == "navigation_timeout":
                raise MockExecutionError("EXECUTION_TIMEOUT", "[场景注入] 导航超时，已确认终态")
            if self.scenario_id == "transient_navigation" and self.attempts[step.step_id] == 1:
                raise MockExecutionError("TRANSIENT_NAVIGATION", "[场景注入] 首次导航失败", retryable=True)
        if step.kind == "inspect" and self.scenario_id == "sensor_failure":
            raise MockExecutionError("SENSOR_UNAVAILABLE", "[场景注入] 感知接口不可用")
        if step.kind == "wait":
            return {"kind": "wait", "status": "succeeded", "simulated": True,
                    "message": "[加速场景] 已跨过配置等待", "source": "scenario",
                    "evidence": {"requested_seconds": step.seconds, "timing_accelerated": True}}
        # Preserve logical arguments and retry policy while accelerating adapter time.
        params = dict(step.params)
        if step.kind == "follow":
            params["duration_seconds"] = .001
        result = super().execute(replace(step, timeout=max(step.timeout, .2), params=params), cancel, feedback)
        if step.kind == "inspect" and self.scenario_id == "inconclusive" and step.object_name:
            result.update(found=None, outcome="inconclusive", objects=[], message="[场景注入] 观察无法判断",
                          evidence={"scenario": self.scenario_id, "synthetic": True})
        result.update(source="scenario", simulated=True, scenario_id=self.scenario_id)
        return result


class ScenarioService:
    MAX_STEPS = 100
    RUN_TIMEOUT = 5.0

    def __init__(self):
        self._slot = threading.BoundedSemaphore(1)

    def catalog(self):
        return {"ok": True, "scenarios": copy.deepcopy(list(SCENARIOS)), "max_steps": self.MAX_STEPS,
                "max_scenarios": len(SCENARIOS), "run_timeout_seconds": self.RUN_TIMEOUT,
                "isolated": True, "simulated": True, "timing_accelerated": True,
                "scope": "验证流程、证据和失败处理，不是物理仿真或真实耗时测试。"}

    def run(self, payload, config):
        allowed = {"plan", "workflow", "parameters", "scenario_ids"}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise CommandError("场景请求只接受 plan/workflow、parameters、scenario_ids")
        identifiers = payload.get("scenario_ids", ["configured"])
        known = {scenario["id"] for scenario in SCENARIOS}
        if (not isinstance(identifiers, list) or not 1 <= len(identifiers) <= len(SCENARIOS)
                or any(not isinstance(value, str) or value not in known for value in identifiers)
                or len(set(identifiers)) != len(identifiers)):
            raise CommandError("请选择 1 到 6 个不同的已知场景")
        frozen_config = copy.deepcopy(config)
        plan = structured_plan(payload, frozen_config)
        if len(plan.steps) > self.MAX_STEPS or any(step.kind not in BUILTIN_SKILLS for step in plan.steps):
            raise CommandError("场景只运行最多 100 个内置技能；可信扩展插件不在隔离验证范围")
        if not self._slot.acquire(blocking=False):
            raise ScenarioBusy("已有场景正在验证，请等待结束后再试")
        try:
            runs = [self._run_one(plan, frozen_config, identifier) for identifier in identifiers]
            return {"ok": True, "id": uuid.uuid4().hex, "created_at": datetime.now(timezone.utc).isoformat(),
                    "simulated": True, "isolated": True, "timing_accelerated": True,
                    "live_history_modified": False, "summary": plan.summary, "runs": runs,
                    "comparison": [{"scenario_id": run["scenario_id"], "state": run["state"],
                                    "goal_outcome": run["report"]["goal_outcome"],
                                    "failed_steps": run["report"]["failed_steps"],
                                    "skipped_steps": run["report"]["skipped_steps"],
                                    "retries": run["retries"], "bounded_stop": run["bounded_stop"]} for run in runs]}
        finally:
            self._slot.release()

    def _run_one(self, original, config, identifier):
        from .engine import MissionEngine
        cfg = copy.deepcopy(config)
        cfg["mock"].update(travel_seconds=.001, inspection_seconds=.001)
        if identifier == "empty":
            cfg["mock"]["objects"] = {target: [] for target in cfg["locations"]}
        adapter = ScenarioAdapter(cfg, identifier)
        # State polling is local skill code, so bound its real wall time explicitly.
        steps = [replace(step, timeout=min(step.timeout, .03)) if step.kind == "wait_state" else step for step in original.steps]
        metadata = {**copy.deepcopy(original.metadata), "scenario_id": identifier, "simulation_only": True}
        metadata.pop("requires_confirmation", None)  # Only this isolated copy; live drafts remain unconfirmed.
        plan = Plan(original.command, steps, original.summary, metadata)
        engine = MissionEngine(cfg, adapter, start_scheduler=False)
        started = time.monotonic()
        bounded_stop = False
        try:
            preflight = inspect_plan(original, config, adapter, engine.lifecycle.snapshot())
            engine.submit_structured(plan)
            while True:
                with engine._condition:
                    running = engine._worker is not None and engine._worker.is_alive()
                if not running:
                    break
                if time.monotonic() - started >= self.RUN_TIMEOUT:
                    bounded_stop = True
                    engine.control("stop")
                    engine._worker.join(1)
                    break
                time.sleep(.005)
            mission = engine.snapshot()["mission"]
            # Expose compact result evidence and timeline, not an operational receipt.
            return {"scenario_id": identifier, "state": mission["state"], "simulated": True,
                    "elapsed_seconds": round(time.monotonic() - started, 4), "bounded_stop": bounded_stop,
                    "retries": sum(max(0, s.get("attempt", 1) - 1) for s in mission["step_states"]),
                    "preflight": preflight, "report": mission["report"], "results": mission["results"],
                    "step_states": mission["step_states"], "robot": adapter.snapshot(),
                    "timing_note": "等待和接口运动已加速；wait_state 最多运行 30ms，导航超时为注入终态。"}
        finally:
            engine.close()
