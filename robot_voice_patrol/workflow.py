"""Compile a finite serial workflow DSL to the same validated execution plan.

Nodes: step, repeat, if, search. Loops expand before execution; no eval, imports,
code expressions or unbounded jumps. Exact '${name}' values are typed parameters.
"""
from __future__ import annotations
import copy
import math
import re
from .contracts import CommandError, Plan, Step
from .plan_validation import MAX_STEPS, OUTCOMES, validate_plan, _json_copy

MAX_WORKFLOW_DEPTH, MAX_SOURCE_NODES = 5, 100


def _keys(data, allowed, required, label):
    if type(data) is not dict or set(data) - set(allowed) or not set(required) <= set(data):
        raise CommandError(f"{label} 含未知字段、缺少必填字段或类型无效")


def combine_conditions(*conditions):
    items = [copy.deepcopy(condition) for condition in conditions if condition is not None]
    if not items:
        return None
    if len(items) == 1:
        return items[0]
    return {"all": items}


def _parameters(data, config):
    definitions, supplied = data.get("parameters", {}), data.get("values", {})
    if type(definitions) is not dict or type(supplied) is not dict or len(definitions) > 20 or set(supplied) - set(definitions):
        raise CommandError("参数定义或取值无效，最多20个参数")
    values = {}
    for name, definition in definitions.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", name):
            raise CommandError("模板参数名称无效")
        _keys(definition, {"type", "default", "min", "max", "label"}, {"type"}, "参数")
        if name not in supplied and "default" not in definition:
            raise CommandError(f"缺少模板参数: {name}")
        value, kind = supplied.get(name, definition.get("default")), definition["type"]
        if not isinstance(kind, str):
            raise CommandError("参数类型必须为字符串")
        if kind == "location":
            valid = isinstance(value, str) and value in config["locations"]
        elif kind == "object":
            valid = isinstance(value, str) and value in config["object_names"]
        elif kind == "text":
            valid = isinstance(value, str) and 1 <= len(value) <= 2000
        elif kind in {"integer", "number"}:
            try:
                valid = type(value) in ((int,) if kind == "integer" else (int, float)) and math.isfinite(value)
            except OverflowError:
                valid = False
            if valid:
                low, high = definition.get("min", -3600), definition.get("max", 3600)
                if type(low) not in (int, float) or type(high) not in (int, float) or not math.isfinite(low) or not math.isfinite(high) or low > high:
                    raise CommandError("模板数值参数边界无效")
                valid = low <= value <= high
        else:
            valid = False
        if not valid:
            raise CommandError(f"模板参数类型或取值无效: {name}")
        values[name] = value
    def substitute(value, depth=0):
        if depth > 24:
            raise CommandError("工作流数据嵌套过深")
        if isinstance(value, str):
            match = re.fullmatch(r"\$\{([A-Za-z][A-Za-z0-9_]*)\}", value)
            if match:
                if match.group(1) not in values:
                    raise CommandError("引用了未声明的模板参数")
                return copy.deepcopy(values[match.group(1)])
            if "${" in value:
                raise CommandError("模板参数只能作为完整值使用，不支持表达式或字符串插值")
        if isinstance(value, list):
            return [substitute(item, depth+1) for item in value]
        if isinstance(value, dict):
            return {key: substitute(item, depth+1) for key, item in value.items()}
        return value
    return substitute({key: value for key, value in data.items() if key not in {"parameters", "values"}})


def compile_workflow(data: dict, config: dict, parameters: dict | None = None) -> Plan:
    _keys(data, {"version", "name", "description", "command", "steps", "goal", "parameters", "values"}, {"version", "name", "steps"}, "工作流")
    data = _json_copy(data, "工作流", 131072)
    if parameters is not None:
        if type(parameters) is not dict or type(data.get("values", {})) is not dict:
            raise CommandError("模板参数取值必须为对象")
        data["values"] = _json_copy({**data.get("values", {}), **parameters}, "模板参数取值")
    if type(data["version"]) is not int or data["version"] != 1:
        raise CommandError("仅支持工作流 DSL version=1")
    if not isinstance(data["name"], str) or not 1 <= len(data["name"].strip()) <= 100:
        raise CommandError("工作流名称应为1..100个字符")
    if "description" in data and (not isinstance(data["description"], str) or len(data["description"]) > 2000):
        raise CommandError("工作流描述过长或格式无效")
    data = _parameters(data, config)
    compiled, source_count, auto_count, search_objects = [], 0, 0, set()

    def resolve(condition, symbols, depth=1):
        if condition is None:
            return None
        if type(condition) is not dict or depth > 8:
            raise CommandError("条件必须为对象")
        if set(condition) in ({"all"}, {"any"}):
            key = next(iter(condition))
            if not isinstance(condition[key], list) or not 1 <= len(condition[key]) <= 16:
                raise CommandError("复合条件必须为列表")
            return {key: [resolve(item, symbols, depth+1) for item in condition[key]]}
        if set(condition) == {"not"}:
            return {"not": resolve(condition["not"], symbols, depth+1)}
        if set(condition) != {"step_id", "outcome"} or not isinstance(condition["step_id"], str):
            raise CommandError("条件叶节点格式无效")
        return {"step_id": symbols.get(condition["step_id"], condition["step_id"]), "outcome": condition["outcome"]}

    def emit(step):
        compiled.append(step)
        if len(compiled) > MAX_STEPS:
            raise CommandError("展开后的工作流超过100步")

    def nodes(items, prefix="", inherited=None, symbols=None, depth=1):
        nonlocal source_count, auto_count
        if depth > MAX_WORKFLOW_DEPTH or not isinstance(items, list) or not 1 <= len(items) <= 100:
            raise CommandError("工作流节点应为非空列表，嵌套不能超过5层")
        symbols = dict(symbols or {})
        local_ids = set()
        for node in items:
            source_count += 1
            auto_count += 1
            if source_count > MAX_SOURCE_NODES * 10 or type(node) is not dict:
                raise CommandError("工作流节点数量或类型无效")
            identifier = node.get("id", f"n{auto_count:03d}")
            if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,23}", identifier) or identifier in local_ids:
                raise CommandError("同一作用域内节点ID必须唯一，并使用1..24位英文标识符")
            local_ids.add(identifier)
            full_id = prefix + identifier
            kind = node.get("type")
            if kind == "step":
                _keys(node, {"type", "id", "kind", "target", "seconds", "object_name", "timeout", "max_retries", "condition", "params", "on_failure"}, {"type", "kind"}, "技能节点")
                timeout = config["navigation_timeout"] if node["kind"] == "navigate" else config["inspection_timeout"] if node["kind"] == "inspect" else 60.0
                if node["kind"] == "wait" and type(node.get("seconds")) in (int, float):
                    timeout = node["seconds"] + 5
                step = Step(kind=node["kind"], target=node.get("target"), seconds=node.get("seconds", 0),
                            object_name=node.get("object_name", ""), timeout=node.get("timeout", timeout),
                            max_retries=node.get("max_retries", 0 if node["kind"] in {"wait", "speak", "report"} else config["max_retries"]),
                            step_id=full_id, condition=combine_conditions(inherited, resolve(node.get("condition"), symbols)),
                            params=node.get("params", {}), on_failure=node.get("on_failure", "abort"))
                emit(step)
                symbols[identifier] = full_id
            elif kind == "repeat":
                _keys(node, {"type", "id", "count", "body", "condition"}, {"type", "count", "body"}, "重复节点")
                count = node["count"]
                if type(count) is not int or not 1 <= count <= 10:
                    raise CommandError("重复次数必须为1..10的整数")
                condition = combine_conditions(inherited, resolve(node.get("condition"), symbols))
                for iteration in range(1, count+1):
                    nodes(node["body"], full_id + f"_{iteration}__", condition, symbols, depth+1)
            elif kind == "if":
                _keys(node, {"type", "id", "condition", "then", "else"}, {"type", "condition", "then"}, "分支节点")
                condition = resolve(node["condition"], symbols)
                nodes(node["then"], full_id + "_yes__", combine_conditions(inherited, condition), symbols, depth+1)
                if node.get("else"):
                    nodes(node["else"], full_id + "_no__", combine_conditions(inherited, {"not": condition}), symbols, depth+1)
                elif "else" in node and node["else"] != []:
                    raise CommandError("else 必须为节点列表")
            elif kind == "search":
                _keys(node, {"type", "id", "locations", "object_name", "return_home", "continue_on", "condition"}, {"type", "locations", "object_name"}, "搜索节点")
                locations, obj = node["locations"], node["object_name"]
                if not isinstance(locations, list) or not 1 <= len(locations) <= 10 or any(not isinstance(target, str) or target not in config["locations"] for target in locations):
                    raise CommandError("搜索必须列出1..10个已配置地点")
                if len(locations) != len(set(locations)) or not isinstance(obj, str) or obj not in config["object_names"]:
                    raise CommandError("搜索地点不能重复，物体必须已配置")
                returning, outcomes = node.get("return_home", "always"), node.get("continue_on", ["not_found"])
                if not isinstance(returning, str) or returning not in {"always", "found", "never"}:
                    raise CommandError("return_home 必须为 always/found/never")
                if not isinstance(outcomes, list) or not outcomes or any(value not in ("not_found", "inconclusive") for value in outcomes) or len(set(outcomes)) != len(outcomes):
                    raise CommandError("continue_on 只能显式包含 not_found/inconclusive")
                search_objects.add(obj)
                seen = []
                parent = combine_conditions(inherited, resolve(node.get("condition"), symbols))
                for index, target in enumerate(locations, 1):
                    checks = [({"step_id": prior, "outcome": outcomes[0]} if len(outcomes) == 1 else {"any": [{"step_id": prior, "outcome": outcome} for outcome in outcomes]}) for prior in seen]
                    condition = combine_conditions(parent, {"all": checks} if len(checks)>1 else checks[0] if checks else None)
                    nav_id, inspect_id = full_id+f"_go_{index}", full_id+f"_look_{index}"
                    emit(Step("navigate", target, timeout=config["navigation_timeout"], max_retries=config["max_retries"], step_id=nav_id, condition=condition))
                    emit(Step("inspect", target, object_name=obj, timeout=config["inspection_timeout"], max_retries=config["max_retries"], step_id=inspect_id, condition=condition))
                    seen.append(inspect_id)
                if returning != "never":
                    found = {"any": [{"step_id": item, "outcome": "found"} for item in seen]} if returning == "found" else None
                    emit(Step("navigate", "home", timeout=config["navigation_timeout"], max_retries=config["max_retries"], step_id=full_id+"_home", condition=combine_conditions(parent, found)))
            else:
                raise CommandError("未知工作流节点类型")
        return symbols
    nodes(data["steps"])
    goal = data.get("goal")
    if goal is None and len(search_objects) == 1:
        goal = {"kind": "find_object", "object_name": next(iter(search_objects))}
    metadata = {"planner": "workflow_compiler", "workflow_name": data["name"], "workflow_version": 1}
    if goal is not None:
        metadata["goal"] = goal
    return validate_plan(Plan(data.get("command", "工作流：" + data["name"]), compiled,
                              data["name"] + f"（展开后{len(compiled)}步）", metadata), config)


def workflow_schema() -> dict:
    """Machine-readable editor schema; semantic prior-ref checks remain in Python."""
    condition = {"oneOf": [
        {"type": "object", "properties": {"step_id": {"type": "string"}, "outcome": {"enum": sorted(OUTCOMES)}}, "required": ["step_id", "outcome"], "additionalProperties": False},
        *[{"type": "object", "properties": {name: {"type": "array", "minItems": 1, "maxItems": 16, "items": {"$ref": "#/$defs/condition"}}}, "required": [name], "additionalProperties": False} for name in ("all", "any")],
        {"type": "object", "properties": {"not": {"$ref": "#/$defs/condition"}}, "required": ["not"], "additionalProperties": False}]}
    node_list = {"type": "array", "minItems": 1, "maxItems": 100, "items": {"$ref": "#/$defs/node"}}
    identifier = {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_-]{0,23}$"}
    def shape(properties, required):
        return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}
    step = shape({"type": {"const": "step"}, "id": identifier, "kind": {"type": "string"}, "target": {"type": ["string", "null"]},
        "seconds": {"type": ["number", "string"]}, "object_name": {"type": "string"}, "timeout": {"type": ["number", "string"]},
        "max_retries": {"type": ["integer", "string"]}, "condition": {"$ref": "#/$defs/condition"}, "params": {"type": "object"},
        "on_failure": {"enum": ["abort", "continue"]}}, ["type", "kind"])
    repeat = shape({"type": {"const": "repeat"}, "id": identifier, "count": {"type": ["integer", "string"], "minimum": 1, "maximum": 10}, "body": node_list, "condition": {"$ref": "#/$defs/condition"}}, ["type", "count", "body"])
    branch = shape({"type": {"const": "if"}, "id": identifier, "condition": {"$ref": "#/$defs/condition"}, "then": node_list, "else": {**node_list, "minItems": 0}}, ["type", "condition", "then"])
    search = shape({"type": {"const": "search"}, "id": identifier, "locations": {"type": "array", "minItems": 1, "maxItems": 10, "uniqueItems": True, "items": {"type": "string"}}, "object_name": {"type": "string"},
        "return_home": {"enum": ["always", "found", "never"]}, "continue_on": {"type": "array", "minItems": 1, "maxItems": 2, "uniqueItems": True, "items": {"enum": ["not_found", "inconclusive"]}}, "condition": {"$ref": "#/$defs/condition"}}, ["type", "locations", "object_name"])
    schema = shape({"version": {"const": 1}, "name": {"type": "string", "minLength": 1, "maxLength": 100}, "description": {"type": "string", "maxLength": 2000}, "command": {"type": "string", "maxLength": 2000},
        "steps": node_list, "goal": shape({"kind": {"const": "find_object"}, "object_name": {"type": "string"}}, ["kind", "object_name"]), "parameters": {"type": "object", "maxProperties": 20}, "values": {"type": "object"}}, ["version", "name", "steps"])
    schema.update({"$schema": "https://json-schema.org/draft/2020-12/schema", "$defs": {"condition": condition, "node": {"oneOf": [step, repeat, branch, search]}},
                   "x-limits": {"compiled_steps": 100, "nesting": 5, "repeat_count": 10, "condition_depth": 8, "condition_nodes": 64}})
    return schema
