"""A bounded, deterministic Chinese command grammar, independent of ROS.

The whole command is parsed before a Plan is returned. Unknown destinations,
objects, trailing instructions and ambiguous negation fail atomically. This is
deliberately not a general natural-language or LLM reasoning implementation.
"""
from __future__ import annotations

import re
import unicodedata

from .contracts import CommandError, ParsedCommand, Plan, Step


MAX_STEPS = 100
MAX_PATROL_ROUNDS = 10
MAX_WAIT_SECONDS = 3600

_CONTROLS = {
    "停止执行": "stop", "立即停止": "stop", "停一下": "stop", "取消当前任务": "stop",
    "暂停一下": "pause", "先暂停": "pause", "暂时停一下": "pause",
    "接着执行": "resume", "继续执行": "resume", "恢复执行": "resume",
    "现在什么状态": "status", "进度怎么样": "status", "执行到哪了": "status", "报告状态": "status",
    "停止": "stop", "停下": "stop", "停止任务": "stop", "取消": "stop", "取消任务": "stop",
    "急停": "stop", "不要动": "stop", "别动": "stop",
    "暂停": "pause", "暂停任务": "pause",
    "继续": "resume", "继续任务": "resume", "恢复": "resume", "恢复任务": "resume",
    "状态": "status", "查询状态": "status", "任务状态": "status", "当前状态": "status", "现在到哪了": "status",
}
_OBJECT_SYNONYMS = {"杯子": "水杯", "人员": "人", "行人": "人"}
_NUMBER = r"(?:[0-9]+(?:\.[0-9]+)?|[零〇一二两三四五六七八九十百]+)"


def _number(text: str) -> float:
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", text):
        return float(text)
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    # Explicit grammar avoids accepting malformed numbers such as 十十 or 两三.
    if text in digits:
        return float(digits[text])
    if re.fullmatch(r"[一二两三四五六七八九]?十[一二三四五六七八九]?", text):
        left, right = text.split("十")
        return float(digits[left] * 10 if left else 10) + (digits[right] if right else 0)
    if re.fullmatch(r"[一二两三四五六七八九]百(?:零[一二三四五六七八九]|[一二三四五六七八九]十[一二三四五六七八九]?)?", text):
        left, right = text.split("百")
        return float(digits[left] * 100) + (_number(right.removeprefix("零")) if right else 0)
    raise CommandError(f"无法识别数字“{text}”")


def _location_names(config: dict) -> dict[str, str]:
    return {name: identifier for identifier, location in config["locations"].items()
            for name in [identifier, location["label"], *location.get("aliases", [])]}


def _pattern(names: dict[str, str]) -> str:
    return "(?:" + "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True)) + ")"


def _normalize(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise CommandError("指令不能为空")
    if len(text) > 500:
        raise CommandError("单条指令不得超过 500 个字符")
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", "", text).rstrip("。.!！?？")
    while True:
        reduced = re.sub(r"^(?:请问|请你|请|麻烦你|麻烦|帮我|机器人)", "", text, count=1)
        if reduced == text:
            break
        text = reduced
    if text.endswith("吧"):
        text = text[:-1]
    if not text:
        raise CommandError("指令不能为空")
    return text


def _inspect_object(text: str, config: dict) -> str:
    """Parse an entire inspection tail; an empty result means scene inspection."""
    if text in {"检查", "巡检", "巡查", "观察", "看看", "查看", "检测"}:
        return ""
    object_text = re.sub(r"^(?:检查|观察|看看|查看|检测)(?:一下)?", "", text, count=1)
    object_text = re.sub(r"^(?:有没有|是否有|有无|寻找|查找|找)", "", object_text, count=1)
    if object_text == text or not object_text:
        raise CommandError(f"不支持的观察指令“{text}”；例如：检查有没有水杯")
    canonical = object_text if object_text in config.get("object_names", []) else _OBJECT_SYNONYMS.get(object_text, object_text)
    if canonical not in config.get("object_names", []):
        raise CommandError(f"未配置或无法识别的观察目标“{object_text}”")
    return canonical


def _navigation(target: str, config: dict) -> Step:
    return Step("navigate", target=target, timeout=config["navigation_timeout"], max_retries=config["max_retries"])


def _inspection(target: str, config: dict, object_name: str = "") -> Step:
    return Step("inspect", target=target, object_name=object_name,
                timeout=config["inspection_timeout"], max_retries=config["max_retries"])


def _wait(seconds: float) -> Step:
    if not 0 < seconds <= MAX_WAIT_SECONDS:
        raise CommandError(f"等待时间必须大于 0 且不超过 {MAX_WAIT_SECONDS} 秒")
    return Step("wait", seconds=seconds, timeout=seconds + 5, max_retries=0)


def _patrol(clause: str, names: dict[str, str], config: dict) -> list[Step] | None:
    match = re.fullmatch(r"(?:开始)?(?:巡逻|巡检|巡护)(.*)", clause)
    if match:
        body = match.group(1)
    else:
        match = re.fullmatch(r"(?:在|沿)(.+)(?:巡逻|巡检|巡护)(.*)", clause)
        if not match:
            return None
        body = match.group(1) + match.group(2)
    rounds = 1
    round_match = re.search(f"({_NUMBER})圈$", body)
    if round_match:
        parsed_number = _number(round_match.group(1))
        if not parsed_number.is_integer() or not 1 <= parsed_number <= MAX_PATROL_ROUNDS:
            raise CommandError(f"巡逻圈数必须为 1..{MAX_PATROL_ROUNDS} 的整数")
        rounds = int(parsed_number)
        body = body[:round_match.start()]
    if "圈" in body:
        raise CommandError("无法识别巡逻圈数")
    route_names = {name: identifier for identifier, route in config["patrol_routes"].items()
                   for name in (identifier, route["label"])}
    route_names.update({"": "default", "默认路线": "default", "默认巡逻路线": "default"})
    if body in route_names:
        route = config["patrol_routes"][route_names[body]]
        waypoints, dwell = route["waypoints"], route["dwell_seconds"]
    else:
        requested = [body] if body in names else re.split(r"和|及|、", body)
        if not requested or any(location not in names for location in requested):
            raise CommandError(f"未配置或无法识别的巡逻地点/路线“{body}”")
        waypoints = [names[location] for location in requested]
        dwell = config["patrol_routes"]["default"]["dwell_seconds"]
    count = len(waypoints) * rounds * (3 if dwell else 2)
    if count > MAX_STEPS:
        raise CommandError(f"巡逻计划超过 {MAX_STEPS} 步，请减少圈数或地点")
    steps: list[Step] = []
    for _ in range(rounds):
        for target in waypoints:
            steps.extend((_navigation(target, config), _inspection(target, config)))
            if dwell:
                steps.append(_wait(dwell))
    return steps


def _clauses(text: str) -> list[str]:
    # Enumeration commas are reserved for patrol location lists. Other punctuation
    # and conjunctions are command boundaries; compound boundaries are allowed.
    separators = re.compile(r"(?:(?:[,;])|(?:然后|接着|之后|最后|并且|并))+")
    if re.search(r"(?:然后|接着|之后|最后|并且|并|[,;])$", text):
        raise CommandError("指令连接词后缺少动作")
    return [part for part in separators.split(text) if part]


def parse_command(text: str, config: dict) -> ParsedCommand:
    """Compile one entire command using a configuration from load_config.

    Examples: 去会议室然后去前台，最后返回起点；巡逻两圈；
    在会议室和走廊巡逻一圈；去会议室看看有没有水杯然后返回起点；
    去仓库，等待五秒，检查有没有箱子；暂停；继续；停止；查询状态。
    """
    raw = text
    normalized = _normalize(text)
    if normalized in _CONTROLS:
        return ParsedCommand(_CONTROLS[normalized])
    if re.search(r"不|别|禁止|无需|不用", normalized):
        raise CommandError("暂不支持带否定条件的任务，请给出明确的正向指令；停止请直接说“停止”")
    if any(word in normalized for word in ("拍照", "拍摄", "录像", "拍张", "拍一张", "照片")):
        raise CommandError("此版本未实现图像拍摄接口，不接受拍照或录像指令")
    names = _location_names(config)
    targets = _pattern(names)
    steps: list[Step] = []
    current_location: str | None = None
    for clause in _clauses(normalized):
        clause = re.sub(r"^(?:先|再)", "", clause, count=1)
        if clause in _CONTROLS:
            raise CommandError("停止、暂停、继续和状态查询必须作为单独指令发送")
        wait_match = re.fullmatch(f"(?:等待|等)({_NUMBER})(秒钟|秒|分钟|分)", clause)
        if wait_match:
            seconds = _number(wait_match.group(1)) * (60 if wait_match.group(2) in {"分钟", "分"} else 1)
            steps.append(_wait(seconds))
        elif clause in {"返回", "回去", "回起点", "返回起点", "回基地", "返回基地", "回充电点", "返回充电点"}:
            steps.append(_navigation("home", config))
            current_location = "home"
        else:
            patrol = _patrol(clause, names, config)
            if patrol is not None:
                steps.extend(patrol)
                current_location = next(step.target for step in reversed(patrol) if step.target is not None)
            else:
                motion = re.fullmatch(f"(?:导航到|移动到|前往|返回|去|到)({targets})(.*)", clause)
                at_location = re.fullmatch(f"在({targets})(.+)", clause)
                inspection_first = re.fullmatch(f"(?:检查|观察|查看)({targets})(.*)", clause)
                if motion:
                    target, tail = names[motion.group(1)], motion.group(2)
                    object_name = _inspect_object(tail, config) if tail else None
                    steps.append(_navigation(target, config))
                    if object_name is not None:
                        steps.append(_inspection(target, config, object_name))
                    current_location = target
                elif at_location or inspection_first:
                    matched = at_location or inspection_first
                    assert matched is not None
                    target, tail = names[matched.group(1)], matched.group(2)
                    object_name = _inspect_object(tail if at_location else "检查" + tail, config)
                    steps.extend((_navigation(target, config), _inspection(target, config, object_name)))
                    current_location = target
                elif re.match(r"^(?:检查|观察|看看|查看|检测|寻找|查找|找)", clause):
                    if current_location is None:
                        raise CommandError("观察任务需要明确地点，例如：去会议室看看有没有水杯")
                    steps.append(_inspection(current_location, config, _inspect_object(clause, config)))
                else:
                    raise CommandError(f"未配置的地点或不支持的完整指令“{clause}”；请使用明确的导航、巡逻、观察或等待指令")
        if len(steps) > MAX_STEPS:
            raise CommandError(f"任务计划不得超过 {MAX_STEPS} 步")
    if not steps:
        raise CommandError("未识别到可执行任务")
    descriptions = []
    for step in steps:
        if step.kind == "wait":
            descriptions.append(f"等待 {step.seconds:g} 秒")
        else:
            label = config["locations"][step.target]["label"]
            if step.kind == "navigate":
                descriptions.append(f"前往{label}")
            else:
                descriptions.append(f"检查{label}" + (f"是否有{step.object_name}" if step.object_name else "现场"))
    objects = {step.object_name for step in steps if step.kind == "inspect" and step.object_name}
    metadata = {"goal": {"kind": "find_object", "object_name": next(iter(objects))}} if len(objects) == 1 else {}
    return ParsedCommand("task", Plan(raw, steps, " → ".join(descriptions), metadata))
