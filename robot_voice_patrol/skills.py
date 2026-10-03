"""Trusted skill plugins with explicit schemas, capabilities and cancellation.

Registration is an in-process developer API. HTTP never imports plugin code.
Every executor returns evidence of its operation; unavailable external skills
are rejected before dispatch instead of producing a simulated success.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import math
import re
import threading
import time
from typing import Callable

from .contracts import CommandError, ExecutionCancelled, ExecutionError, Step


class SkillExecutionError(ExecutionError):
    def __init__(self, code, message, *, retryable=False):
        super().__init__(message)
        self.code, self.retryable = code, retryable


def _schema(properties=None, required=()):
    return {"type": "object", "properties": properties or {}, "required": list(required),
            "additionalProperties": False}


def _string(maximum=200, **extra):
    return {"type": "string", "minLength": 1, "maxLength": maximum, **extra}


def _number(low, high, **extra):
    return {"type": "number", "minimum": low, "maximum": high, **extra}


def validate_parameters(value, schema, path="params"):
    """Validate the documented, deliberately bounded JSON-schema subset."""
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            try:
                return validate_parameters(value, option, path)
            except CommandError:
                pass
        raise CommandError(f"{path} 格式无效")
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            raise CommandError(f"{path} 必须为对象")
        properties = schema.get("properties", {})
        unknown = set(value) - set(properties)
        if unknown and schema.get("additionalProperties", False) is False:
            raise CommandError(f"{path} 包含未知参数: {', '.join(sorted(map(str, unknown)))}")
        if set(schema.get("required", [])) - set(value):
            raise CommandError(f"{path} 缺少必要参数")
        result = copy.deepcopy(value)
        for name, spec in properties.items():
            if name not in result and "default" in spec:
                result[name] = copy.deepcopy(spec["default"])
            if name in result:
                result[name] = validate_parameters(result[name], spec, f"{path}.{name}")
        return result
    if kind == "string":
        if not isinstance(value, str) or not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 4096):
            raise CommandError(f"{path} 字符串长度无效")
        if any(ord(c) < 32 and c not in "\n\t" for c in value):
            raise CommandError(f"{path} 不接受控制字符")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise CommandError(f"{path} 格式无效")
    elif kind in {"number", "integer"}:
        valid = type(value) is int if kind == "integer" else type(value) in {int, float}
        try:
            valid = valid and math.isfinite(value)
        except OverflowError:
            valid = False
        if not valid or value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            raise CommandError(f"{path} 数值超出允许范围")
    elif kind == "boolean":
        if type(value) is not bool:
            raise CommandError(f"{path} 必须是布尔值")
    elif kind == "null":
        if value is not None:
            raise CommandError(f"{path} 必须为空")
    elif kind == "array":
        if not isinstance(value, list) or not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 100):
            raise CommandError(f"{path} 列表长度无效")
        value = [validate_parameters(item, schema["items"], f"{path}[{i}]") for i, item in enumerate(value)]
    elif kind is not None:
        raise CommandError(f"技能 schema 包含不支持的类型 {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise CommandError(f"{path} 不在允许值列表中")
    return copy.deepcopy(value)


@dataclass(frozen=True)
class SkillSpec:
    name: str
    label: str
    description: str
    schema: dict
    executor: Callable
    target: str = "none"  # none, required, optional
    external: bool = False
    validator: Callable | None = None


class SkillRegistry:
    def __init__(self):
        self._skills = {}
        self._lock = threading.RLock()

    def register(self, spec: SkillSpec):
        if not isinstance(spec, SkillSpec) or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", spec.name):
            raise ValueError("技能必须有合法名称和 SkillSpec")
        if not callable(spec.executor) or spec.target not in {"none", "required", "optional"}:
            raise ValueError("技能执行器或地点策略无效")
        json.dumps(spec.schema, allow_nan=False)
        with self._lock:
            if spec.name in self._skills:
                raise ValueError(f"技能已注册: {spec.name}")
            self._skills[spec.name] = replace(spec, schema=copy.deepcopy(spec.schema))
        return spec.name

    def names(self):
        with self._lock:
            return frozenset(self._skills)

    def _get(self, name):
        with self._lock:
            spec = self._skills.get(name) if isinstance(name, str) else None
        if spec is None:
            raise CommandError(f"未注册的技能: {name}")
        return spec

    @staticmethod
    def _capabilities(adapter):
        if adapter is None:
            return {}
        try:
            value = adapter.capabilities() if hasattr(adapter, "capabilities") else {}
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def catalog(self, adapter=None):
        capabilities = self._capabilities(adapter)
        with self._lock:
            specs = list(self._skills.values())
        result = []
        for spec in specs:
            capability = capabilities.get(spec.name, False)
            available = capability.get("available", False) is True if isinstance(capability, dict) else capability is True
            uses_adapter = spec.external or spec.name in {"navigate", "inspect", "wait"} and bool(capabilities)
            available = available if uses_adapter else True
            result.append({"name": spec.name, "label": spec.label, "description": spec.description,
                           "params_schema": copy.deepcopy(spec.schema), "target": spec.target,
                           "external": spec.external, "available": available,
                           "reason": "" if available else "外部技能接口未连接或未声明此能力",
                           "simulated": bool(isinstance(capability, dict) and capability.get("simulated"))})
        return result

    def validate_step(self, step, config):
        spec = self._get(step.kind)
        params = validate_parameters(getattr(step, "params", {}), spec.schema)
        target = step.target
        if not isinstance(step.object_name, str) or len(step.object_name) > 100:
            raise CommandError("观察目标格式无效")
        if type(step.seconds) not in {int, float}:
            raise CommandError("等待秒数必须为数值")
        try:
            valid_seconds = math.isfinite(step.seconds) and 0 <= step.seconds <= 3600
        except OverflowError:
            valid_seconds = False
        if not valid_seconds:
            raise CommandError("等待秒数超出范围")
        if spec.name == "dock" and target is None:
            target = "home"
        if spec.target == "none" and target is not None:
            raise CommandError(f"{spec.name} 技能不接受地点")
        if spec.target == "required" and not target:
            raise CommandError(f"{spec.name} 技能需要地点")
        if target is not None and (not isinstance(target, str) or target not in config["locations"]):
            raise CommandError("技能引用了未配置地点")
        if step.object_name and spec.name != "inspect":
            raise CommandError("只有 inspect 技能可以设置 object_name")
        if spec.name == "inspect" and step.object_name and step.object_name not in config["object_names"]:
            raise CommandError("观察目标未配置")
        if spec.name != "wait" and step.seconds != 0:
            raise CommandError("只有 wait 技能可以设置 seconds")
        if spec.name == "wait" and (step.seconds <= 0 or step.timeout <= step.seconds):
            raise CommandError("等待时间应大于零且小于步骤超时")
        if spec.name == "follow" and params["duration_seconds"] >= step.timeout:
            raise CommandError("跟随时长必须小于步骤超时")
        result = replace(step, target=target, params=params)
        return spec.validator(result, config) if spec.validator else result

    def execute(self, step, adapter, cancel, feedback, context=None):
        context = context or {}
        config = context.get("config") or getattr(adapter, "config", None)
        if not isinstance(config, dict):
            raise CommandError("技能执行缺少配置上下文")
        step = self.validate_step(step, config)
        spec = self._get(step.kind)
        if cancel.is_set():
            raise ExecutionCancelled("技能执行前已取消")
        if spec.external:
            entry = next(item for item in self.catalog(adapter) if item["name"] == step.kind)
            if not entry["available"]:
                raise SkillExecutionError("CAPABILITY_UNAVAILABLE", entry["reason"])
        result = spec.executor(step, adapter, cancel, feedback, context)
        if not isinstance(result, dict) or result.get("status") != "succeeded":
            raise SkillExecutionError("INVALID_SKILL_RESULT", "技能没有返回成功终态")
        if result.get("kind", step.kind) != step.kind:
            raise SkillExecutionError("INVALID_SKILL_RESULT", "技能返回类型不匹配")
        try:
            json.dumps(result, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise SkillExecutionError("INVALID_SKILL_RESULT", "技能结果不是有效 JSON") from exc
        return result


def _adapter_execute(step, adapter, cancel, feedback, context):
    return adapter.execute(step, cancel, feedback)


def _local_result(step, message, **data):
    return {"kind": step.kind, "target": step.target, "status": "succeeded", "outcome": "succeeded",
            "source": "software_skill", "simulated": False, "message": message,
            "completed_at": datetime.now(timezone.utc).isoformat(), **data}


def _speak(step, adapter, cancel, feedback, context):
    emit = context.get("emit_speech")
    if not callable(emit):
        raise SkillExecutionError("SPEECH_UNAVAILABLE", "没有接入播报输出接口")
    emit(step.params["text"])
    feedback({"kind": "speak", "text": step.params["text"], "delivered_to": "speech_output"})
    return _local_result(step, "播报文本已提交", text=step.params["text"], delivered_to="speech_output",
                         audible_confirmed=False)


def _report(step, adapter, cancel, feedback, context):
    results = copy.deepcopy(context.get("results", []))
    observations = [r for r in results if r.get("kind") == "inspect" and r.get("status") == "succeeded"]
    report = {"title": step.params["title"], "mission_id": context.get("mission_id"),
              "completed_steps": sum(r.get("status") == "succeeded" for r in results),
              "failed_steps": sum(r.get("status") in {"failed", "timed_out"} for r in results),
              "observations": observations if step.params["include_observations"] else [],
              "simulated": any(r.get("simulated") is True for r in results)}
    feedback({"kind": "report", "report": report})
    return _local_result(step, "任务阶段报告已生成", report=report)


def _wait_state(step, adapter, cancel, feedback, context):
    params, started = step.params, time.monotonic()
    missing = object()
    while True:
        if cancel.is_set():
            raise ExecutionCancelled("等待状态已取消")
        if time.monotonic() - started >= step.timeout:
            raise SkillExecutionError("STATE_TIMEOUT", "等待状态超时")
        observed = adapter.snapshot()
        for part in params["field"].split("."):
            observed = observed.get(part, missing) if isinstance(observed, dict) else missing
        expected, operator = params["value"], params["operator"]
        matched = False
        if observed is not missing:
            if operator in {"eq", "ne"}:
                same_type = type(observed) is type(expected) or type(observed) in {int, float} and type(expected) in {int, float}
                equal = same_type and observed == expected
                matched = equal if operator == "eq" else not equal
            elif type(observed) in {int, float} and type(expected) in {int, float}:
                matched = {"gt": lambda: observed > expected, "gte": lambda: observed >= expected,
                           "lt": lambda: observed < expected, "lte": lambda: observed <= expected}[operator]()
        feedback({"kind": "wait_state", "field": params["field"],
                  "observed": None if observed is missing else observed, "matched": matched})
        if matched:
            return _local_result(step, "接口状态已满足条件", field=params["field"], observed=observed,
                                 evidence={"field": params["field"], "operator": operator, "expected": expected},
                                 simulated=adapter.mode == "mock")
        cancel.wait(min(params["poll_seconds"], max(0, step.timeout - (time.monotonic() - started))))


def _wait_state_validator(step, config):
    field = step.params["field"]
    if any(part.startswith("_") for part in field.split(".")):
        raise CommandError("只能读取公开状态字段")
    if step.params["operator"] not in {"eq", "ne"} and type(step.params["value"]) not in {int, float}:
        raise CommandError("大小比较需要数值")
    return step


def _builtins():
    registry = SkillRegistry()
    for name, label, target in [("navigate", "导航", "required"), ("inspect", "观察", "required"), ("wait", "等待", "none")]:
        registry.register(SkillSpec(name, label, "通过机器人适配器执行并保留取消语义", _schema(), _adapter_execute, target))
    registry.register(SkillSpec("speak", "播报", "提交文本到播报通道，不假定扬声器已经播放", _schema({"text": _string(1000)}, ["text"]), _speak))
    registry.register(SkillSpec("report", "阶段报告", "汇总当前任务已执行步骤与观察证据", _schema({
        "title": _string(120, default="任务阶段报告"), "include_observations": {"type": "boolean", "default": True}}), _report))
    scalar = {"anyOf": [{"type": "string", "maxLength": 200}, {"type": "number"}, {"type": "boolean"}, {"type": "null"}]}
    registry.register(SkillSpec("wait_state", "等待接口状态", "轮询公开状态字段，支持取消和有界超时", _schema({
        "field": _string(120, pattern=r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){0,4}"),
        "operator": {"type": "string", "enum": ["eq", "ne", "gt", "gte", "lt", "lte"], "default": "eq"},
        "value": scalar, "poll_seconds": _number(.02, 2, default=.1)}, ["field", "value"]), _wait_state,
        validator=_wait_state_validator))
    external = [
        ("capture", "拍照接口", "optional", _schema({"camera": _string(80, default="front"), "format": {"type": "string", "enum": ["jpeg", "png"], "default": "jpeg"}})),
        ("dock", "回充接口", "optional", _schema()),
        ("follow", "跟随接口", "none", _schema({"subject": _string(100), "duration_seconds": _number(.1, 600), "distance_meters": _number(.2, 10, default=1.5)}, ["subject", "duration_seconds"])),
        ("turn", "转向接口", "none", _schema({"angle_degrees": _number(-360, 360)}, ["angle_degrees"]))]
    for name, label, target, schema in external:
        registry.register(SkillSpec(name, label, "需要外部端点明确声明能力并返回真实操作结果", schema, _adapter_execute, target, True))
    from .home_skills import register_home_skills
    register_home_skills(registry)
    return registry


_REGISTRY = _builtins()


def get_registry():
    return _REGISTRY
