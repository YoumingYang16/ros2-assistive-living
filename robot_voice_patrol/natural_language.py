"""Stateless dialogue planning with explicit context and bounded conditionals.

The engine persists returned context; this module never executes a robot action.
Model proposals use the same semantic validator as deterministic plans.
"""
from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
import re

from .contracts import CommandError, Plan, PlanningResult, Step
from .model_provider import provider_from_env, validate_model_output
from .planner import (_CONTROLS, _OBJECT_SYNONYMS, _inspection, _location_names,
                      _navigation, _normalize, _pattern, _number, _NUMBER, parse_command)


_CONTROL_ALIASES = {
    "停止执行": "stop", "立即停止": "stop", "停一下": "stop", "取消当前任务": "stop",
    "暂停一下": "pause", "先暂停": "pause", "暂时停一下": "pause",
    "接着执行": "resume", "继续执行": "resume", "恢复执行": "resume",
    "现在什么状态": "status", "进度怎么样": "status", "执行到哪了": "status", "报告状态": "status",
}
_REPORT = r"(?:告诉我|通知我|向我报告|汇报结果|告诉我结果)"
_CONNECTOR = r"(?:,|;|然后|并且|并|接着|最后)*"


def detect_control(text: str) -> str | None:
    """Recognize only a complete control utterance, without models or locks."""
    try:
        normalized = _normalize(text)
    except CommandError:
        return None
    return _CONTROLS.get(normalized) or _CONTROL_ALIASES.get(normalized)


def parse_schedule_intent(text: str, *, now=None, timezone="Asia/Hong_Kong") -> dict | None:
    """Extract explicit scheduling only; caller validates and queues inner text.

    Returns text/run_at/repeat/after_current/kind. No scheduling or execution is
    performed here. Naive supplied datetimes are rejected to avoid timezone drift.
    """
    from datetime import datetime, timedelta, timezone as fixed_timezone
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    normalized = _normalize(text)
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        if timezone == "Asia/Hong_Kong":
            zone = fixed_timezone(timedelta(hours=8), "Asia/Hong_Kong")
        elif timezone in {"UTC", "Etc/UTC"}:
            zone = fixed_timezone.utc
        else:
            raise CommandError("时区不可用，请使用有效 IANA 时区") from None
    current = now if now is not None else datetime.now(zone)
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise CommandError("调度参考时间必须包含时区")
    current = current.astimezone(zone)
    def result(inner, kind, run_at=None, repeat=None, after_current=False):
        inner = inner.lstrip(",:;")
        if not inner or detect_control(inner) is not None:
            raise CommandError("调度必须包含完整任务，不能安排停止/暂停等即时控制")
        if re.match(r"(?:每天|每隔|当前任务|完成当前任务|等当前任务)", inner):
            raise CommandError("不支持嵌套调度")
        return {"text": inner, "kind": kind, "run_at": run_at.isoformat() if run_at else None,
                "repeat": repeat, "after_current": after_current}
    after = re.fullmatch(r"(?:当前任务(?:完成|结束)后|完成当前任务后|等当前任务结束后|做完当前任务后)(.+)", normalized)
    if after:
        return result(after.group(1), "after_current", after_current=True)
    relative = re.fullmatch(f"(?:在)?({_NUMBER})(秒|分钟|小时)(?:以)?后(.+)", normalized)
    if relative:
        seconds = _number(relative.group(1)) * {"秒": 1, "分钟": 60, "小时": 3600}[relative.group(2)]
        if not 1 <= seconds <= 7*86400:
            raise CommandError("延后时间必须为1秒到7天")
        return result(relative.group(3), "once", current + timedelta(seconds=seconds))
    periodic = re.fullmatch(f"每隔({_NUMBER})(秒|分钟|小时)(.+)", normalized)
    if periodic:
        seconds = _number(periodic.group(1)) * {"秒": 1, "分钟": 60, "小时": 3600}[periodic.group(2)]
        if not 10 <= seconds <= 30*86400:
            raise CommandError("重复间隔必须为10秒到30天")
        return result(periodic.group(3), "interval", current + timedelta(seconds=seconds), {"interval_seconds": seconds})
    timed = re.fullmatch(f"(每天|今天|明天)(上午|下午|晚上|早上)?({_NUMBER})(?:点|:)(?:({_NUMBER})(?:分)?)?(.+)", normalized)
    if timed:
        day, period, hour_text, minute_text, inner = timed.groups()
        hour_value, minute_value = _number(hour_text), _number(minute_text) if minute_text else 0
        if not hour_value.is_integer() or not float(minute_value).is_integer():
            raise CommandError("每天/日期调度必须使用整数时分")
        hour, minute = int(hour_value), int(minute_value)
        if period:
            if not 1 <= hour <= 12:
                raise CommandError("带上午/下午的小时必须为1..12")
            hour = hour % 12 + (12 if period in {"下午", "晚上"} else 0)
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise CommandError("调度时间超出有效范围")
        scheduled = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if day == "明天" or day == "每天" and scheduled <= current:
            scheduled += timedelta(days=1)
        if day == "今天" and scheduled <= current:
            raise CommandError("今天指定的时间已经过去")
        recurrence = {"daily_at": f"{hour:02d}:{minute:02d}", "timezone": timezone} if day == "每天" else None
        return result(inner, "daily" if recurrence else "once", scheduled, recurrence)
    # Clear schedule intent must not fall through into a fuzzy task planner.
    if re.match(r"^(每天|今天|明天|每隔|当前任务|完成当前任务|等当前任务)", normalized):
        raise CommandError("无法识别调度时间，请用“十分钟后去会议室”或“每天9:00巡逻”")
    return None


class DialoguePlanner:
    def __init__(self, config: dict, provider=None):
        self.config = config
        self.provider = provider_from_env() if provider is None else (None if provider is False else provider)

    def summary(self) -> dict:
        return {"name": "dialogue_v3", "deterministic": True, "conditionals": True,
                "model": self.provider.summary() if self.provider is not None else {"enabled": False}}

    def _validate(self, plan: Plan) -> Plan:
        from .plan_validation import validate_plan
        return validate_plan(plan, self.config)

    def _config_fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.config, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()

    def _assert_fresh_draft(self, plan: Plan) -> None:
        previous = plan.metadata.get("config_fingerprint")
        if previous and previous != self._config_fingerprint():
            raise CommandError("配置已经变化，请重新预览完整任务后再确认或修改草稿")

    def _task(self, plan: Plan, context: dict, message="") -> PlanningResult:
        metadata = {**plan.metadata, "config_fingerprint": self._config_fingerprint()}
        observed_objects = {step.object_name for step in plan.steps if step.kind == "inspect" and step.object_name}
        if len(observed_objects) == 1 and "goal" not in metadata:
            metadata["goal"] = {"kind": "find_object", "object_name": next(iter(observed_objects))}
        plan = self._validate(replace(plan, metadata=metadata))
        result = copy.deepcopy(context)
        result.pop("clarification", None)
        result.pop("model_clarification", None)
        destinations = [step.target for step in plan.steps if step.kind == "navigate" and step.target != "home"]
        destinations = destinations or [step.target for step in plan.steps if step.kind == "navigate"]
        if destinations:
            previous = result.get("last_target")
            if previous and previous != destinations[-1]:
                result["previous_target"] = previous
            result["last_target"] = destinations[-1]
        objects = [step.object_name for step in plan.steps if step.kind == "inspect" and step.object_name]
        if objects:
            result["last_object"] = objects[-1]
        result["pending_plan"] = plan.to_dict()
        return PlanningResult("task", plan, message or plan.summary, result)

    def _clarify(self, context: dict, template: str, message: str, options=None, slot="destination") -> PlanningResult:
        identifiers = list(self.config["locations"]) if options is None else options
        if slot == "object":
            labels = list(identifiers)
        else:
            labels = [self.config["locations"][identifier]["label"] for identifier in identifiers]
        result = copy.deepcopy(context)
        result["clarification"] = {"template": template, "slot": slot, "options": list(identifiers)}
        result.pop("pending_plan", None)
        return PlanningResult("clarify", message=message, context=result, options=labels[:10])

    def _paraphrase(self, text: str) -> str:
        names = _pattern(_location_names(self.config))
        # Each rewrite has a complete lexical boundary. No unknown text is dropped.
        text = re.sub(r"^(?:麻烦你|请你|帮忙|帮我|请|你(?=去|前往|到|帮我))", "", text)
        text = re.sub(r"^(?:带我去|帮我前往|导航至|移动至|走到|过去到)", "去", text)
        text = re.sub(f"^到({names})去$", r"去\1", text)
        text = re.sub(f"^去({names})(?:一趟|一下)$", r"去\1", text)
        text = re.sub(r"^(?:回到|回到家|回家)$", "返回起点", text)
        text = re.sub(r"^回到", "返回", text)
        text = re.sub(r"^(?:开始)?(?:巡视|巡查)", "巡逻", text)
        text = re.sub(r"^(?:停留|等候)", "等待", text)
        boundary = f"(^|{names}|[,;]|然后|接着)"
        text = re.sub(boundary + r"(?:搜索|找一找|寻找一下)", lambda m: m.group(1) + "寻找", text)
        text = re.sub(boundary + r"(?:看一看|查看一下|检查一下)", lambda m: m.group(1) + "检查", text)
        text = re.sub(r"(检查|查看|看看|检测)是否存在", r"\1是否有", text)
        text = re.sub(r"(检查|查看|看看|检测)有没有发现", r"\1有没有", text)
        # Natural VOS forms, anchored to a configured target and exact object.
        objects = _pattern({name: name for name in [*self.config["object_names"], *_OBJECT_SYNONYMS]})
        match = re.fullmatch(f"(?:寻找|查找|找)({objects})(?:到|去|在)({names})", text)
        if match:
            text = "去" + match.group(2) + "找" + match.group(1)
        return text

    def _conditional(self, text: str, raw: str) -> Plan | None:
        """Two-site search; unknown observation never takes a not_found branch."""
        if not any(token in text for token in ("没找到", "未找到", "如果", "找到后", "没有找到")):
            return None
        names = _location_names(self.config)
        targets = _pattern(names)
        objects = _pattern({name: name for name in [*self.config["object_names"], *_OBJECT_SYNONYMS]})
        pattern = (f"(?:先)?(?:去|到|前往)({targets})(?:找|寻找|查找|检查有没有|看看有没有)({objects})"
                   + _CONNECTOR + r"(?:如果|若)?(?:没找到|未找到|没有找到)(?:就|则|再)?(?:去|到|前往)"
                   + f"({targets})(?:(?:找|寻找|查找)(?:同一个|这个)?({objects})?)?(.*)")
        match = re.fullmatch(pattern, text)
        if not match:
            raise CommandError("条件任务请明确两个地点和同一个观察目标，例如：先去会议室找水杯，没找到就去仓库，找到后返回起点")
        first, second = names[match.group(1)], names[match.group(3)]
        canonical = lambda value: value if value in self.config["object_names"] else _OBJECT_SYNONYMS.get(value, value)
        object_name = canonical(match.group(2))
        if match.group(4) and canonical(match.group(4)) != object_name:
            raise CommandError("同一个条件搜索任务不能悄悄更换观察目标")
        tail = match.group(5)
        tail = re.sub("^" + _CONNECTOR, "", tail)
        found_only = False
        report = False
        returning = False
        if tail:
            if tail.startswith("找到后"):
                found_only = True
                tail = tail[len("找到后"):]
            report_match = re.match(_REPORT, tail)
            if report_match:
                report = True
                tail = tail[report_match.end():]
                tail = re.sub("^" + _CONNECTOR, "", tail)
            if tail in {"返回起点", "返回基地", "回起点", "回基地", "返回充电点"}:
                returning = True
            elif tail:
                raise CommandError("条件搜索末尾包含不支持的动作")
            if found_only and not report and not returning:
                raise CommandError("找到后缺少后续动作")
        steps = [replace(_navigation(first, self.config), step_id="s001"),
                 replace(_inspection(first, self.config, object_name), step_id="s002")]
        missing = {"step_id": "s002", "outcome": "not_found"}
        steps.extend((replace(_navigation(second, self.config), step_id="s003", condition=missing),
                      replace(_inspection(second, self.config, object_name), step_id="s004", condition=missing)))
        if returning and found_only:
            steps.extend((replace(_navigation("home", self.config), step_id="s005", condition={"step_id": "s002", "outcome": "found"}),
                          replace(_navigation("home", self.config), step_id="s006", condition={"step_id": "s004", "outcome": "found"})))
        elif returning:
            steps.append(replace(_navigation("home", self.config), step_id="s005"))
        summary = f"在{self.config['locations'][first]['label']}查找{object_name}；明确未发现时再去{self.config['locations'][second]['label']}查找"
        if returning:
            summary += "；找到后返回起点" if found_only else "；最后返回起点"
        metadata = {"planner": "deterministic_dialogue", "conditional_search": True,
                    "report_on_found": report, "inconclusive_policy": "do_not_treat_as_not_found"}
        return Plan(raw, steps, summary, metadata)

    def _correction(self, text: str, context: dict) -> PlanningResult | None:
        explicit = re.match(r"^(?:改去|改成|改为|把.+改成)", text)
        if not explicit:
            return None
        if context.get("active_mission") and "pending_schedule" not in context:
            raise CommandError("任务正在运行，请先停止；修改只适用于尚未执行的草稿")
        draft = context.get("pending_plan")
        if not isinstance(draft, dict):
            raise CommandError("没有待修改的计划，请发送完整的新任务")
        from .plan_validation import plan_from_dict
        plan = plan_from_dict(draft, self.config)
        self._assert_fresh_draft(plan)
        names = _location_names(self.config)
        target_pattern = _pattern(names)
        replace_match = re.fullmatch(f"把({target_pattern})改成({target_pattern})", text)
        simple_match = re.fullmatch(f"改(?:去|为|成去|成)({target_pattern})", text)
        if replace_match:
            old, new = names[replace_match.group(1)], names[replace_match.group(2)]
        elif simple_match:
            previous = {step.target for step in plan.steps if step.kind == "navigate" and step.target != "home"}
            if len(previous) != 1:
                raise CommandError("草稿包含多个目的地，请明确说“把某地点改成某地点”")
            old, new = previous.pop(), names[simple_match.group(1)]
        else:
            raise CommandError("请明确说“改去仓库”或“把会议室改成仓库”")
        if not any(step.target == old for step in plan.steps):
            raise CommandError("草稿中不存在要修改的地点")
        steps = [replace(step, target=new) if step.target == old else step for step in plan.steps]
        revised = Plan(text, steps, f"修改草稿：{self.config['locations'][old]['label']} → {self.config['locations'][new]['label']}",
                       {**plan.metadata, "revision": int(plan.metadata.get("revision", 0)) + 1, "requires_confirmation": True})
        return self._task(revised, context, "草稿已修改，尚未执行；请预览后确认")

    def _v3_plan(self, text: str, raw: str, context: dict) -> Plan | None:
        """Whole-utterance grammar for bounded V3 workflow and skill requests."""
        from .workflow import compile_workflow
        names = _location_names(self.config)
        targets = _pattern(names)
        objects = _pattern({name: name for name in [*self.config["object_names"], *_OBJECT_SYNONYMS]})
        canonical = lambda value: value if value in self.config["object_names"] else _OBJECT_SYNONYMS.get(value, value)
        speech = re.fullmatch(r"(?:播报|朗读|说一句)[:：]?(?:[“\"])?(.+?)(?:[”\"])?", text)
        if speech:
            return Plan(raw, [Step("speak", params={"text": speech.group(1)}, max_retries=0)], "播报指定文本")
        if text in {"汇报结果", "生成报告", "汇报任务结果"}:
            return Plan(raw, [Step("report", params={"include_observations": True}, max_retries=0)], "汇报当前任务结果")
        if text in {"拍照", "拍一张照片", "前置相机拍照"}:
            return Plan(raw, [Step("capture", params={"camera": "front", "format": "jpeg"}, max_retries=0)], "请求前置相机拍照（需要已接入能力）")
        if text in {"对接充电点", "对接充电桩", "开始充电对接"}:
            return Plan(raw, [Step("dock", target="home", max_retries=0)], "请求对接起点充电设备（需要已接入能力）")
        if text == "等待定位有效":
            return Plan(raw, [Step("wait_state", params={"field": "pose_valid", "operator": "eq", "value": True}, max_retries=0)], "等待定位有效")
        turning = re.fullmatch(f"(?:原地)?(左转|右转)({_NUMBER})度", text)
        if turning:
            angle = _number(turning.group(2)) * (1 if turning.group(1) == "左转" else -1)
            return Plan(raw, [Step("turn", params={"angle_degrees": angle}, max_retries=0)], f"请求原地转向{angle:g}度（需要已接入能力）")
        follow = re.fullmatch(f"跟随(.{{1,40}}?)持续({_NUMBER})秒", text)
        if follow:
            seconds = _number(follow.group(2))
            return Plan(raw, [Step("follow", params={"subject": follow.group(1), "duration_seconds": seconds, "distance_meters": 1.0}, timeout=seconds+5, max_retries=0)], "请求跟随指定目标（需要已接入能力）")
        repeat = re.fullmatch(f"(?:重复|循环)({_NUMBER})次[:：](.+)", text)
        if repeat:
            count = _number(repeat.group(1))
            if not count.is_integer() or not 1 <= count <= 10:
                raise CommandError("重复次数必须为1..10的整数")
            # A nested rule workflow cannot activate model fallback or rewrite session state.
            result = DialoguePlanner(self.config, provider=False).interpret(repeat.group(2), context)
            if result.kind != "task" or result.plan.metadata.get("requires_confirmation"):
                raise CommandError("重复内容必须是完整、明确的可执行任务")
            body = []
            for step in result.plan.steps:
                record = step.to_dict()
                identifier = record.pop("step_id")
                body.append({"type": "step", "id": identifier, **record})
            workflow = {"version": 1, "name": f"重复{int(count)}次任务", "command": raw,
                        "steps": [{"type": "repeat", "id": "repeat", "count": int(count), "body": body}]}
            if result.plan.metadata.get("goal"):
                workflow["goal"] = result.plan.metadata["goal"]
            return compile_workflow(workflow, self.config)
        # Explicit handled failures retain evidence and only select declared fallback.
        failure = re.fullmatch(f"(?:先)?去({targets})(?:,|然后)(?:如果)?(?:导航)?(失败或超时|超时|失败)(?:就|则)?去({targets})(.*)", text)
        if failure:
            tail = failure.group(4)
            if tail not in {"", ",最后返回起点", "然后返回起点", ",然后返回起点"}:
                raise CommandError("失败备用任务包含不支持的尾句")
            outcomes = ["failed", "timed_out"] if failure.group(2) == "失败或超时" else ["timed_out" if failure.group(2) == "超时" else "failed"]
            leaves = [{"step_id": "primary", "outcome": outcome} for outcome in outcomes]
            condition = leaves[0] if len(leaves) == 1 else {"any": leaves}
            steps = [replace(_navigation(names[failure.group(1)], self.config), step_id="primary", on_failure="continue"),
                     replace(_navigation(names[failure.group(3)], self.config), step_id="fallback", condition=condition)]
            if tail:
                steps.append(replace(_navigation("home", self.config), step_id="home"))
            return Plan(raw, steps, "导航未完成时执行明确指定的备用任务", {"planner": "workflow_rules"})
        # Two independent observations followed by explicit ALL/ANY condition.
        compound = re.fullmatch(f"去({targets})找({objects})(?:然后|,)去({targets})找({objects})(?:,|然后)(?:如果)?(两处都找到|任意一处找到|至少一处找到|两处都没找到|至少一处没找到)(?:就|则)?返回起点", text)
        if compound:
            one, obj1, two, obj2, operator = compound.groups()
            expected = "not_found" if "没找到" in operator else "found"
            boolean = "all" if operator.startswith("两处都") else "any"
            steps = [replace(_navigation(names[one], self.config), step_id="go1"),
                     replace(_inspection(names[one], self.config, canonical(obj1)), step_id="look1"),
                     replace(_navigation(names[two], self.config), step_id="go2"),
                     replace(_inspection(names[two], self.config, canonical(obj2)), step_id="look2"),
                     replace(_navigation("home", self.config), step_id="home", condition={boolean: [{"step_id": item, "outcome": expected} for item in ("look1", "look2")]})]
            return Plan(raw, steps, operator + "时返回起点", {"planner": "workflow_rules"})
        listed = re.fullmatch(f"依次(?:去|到)?(.+?)(?:寻找|查找|找)({objects})(.*)", text)
        locations, obj, tail = None, None, ""
        if listed:
            pieces = re.split(r"、|和|及|,", listed.group(1))
            if not pieces or any(piece not in names for piece in pieces):
                raise CommandError("依次搜索的地点必须全部已配置")
            locations, obj, tail = [names[piece] for piece in pieces], canonical(listed.group(2)), listed.group(3)
        else:
            chain = re.fullmatch(f"(?:先)?去({targets})(?:找|寻找)({objects})(.+)", text)
            if chain:
                remainder = chain.group(3)
                candidates = [names[chain.group(1)]]
                while True:
                    fallback = re.match(f"(?:,|然后)(?:再)?(?:如果|若)?(?:没找到|未找到|没有找到)(?:就|则|再)?去({targets})", remainder)
                    if not fallback:
                        break
                    candidates.append(names[fallback.group(1)])
                    remainder = remainder[fallback.end():]
                if len(candidates) >= 3:
                    locations, obj, tail = candidates, canonical(chain.group(2)), remainder
        if locations is not None:
            returning, continue_on, report = "never", ["not_found"], False
            tail = re.sub("^" + _CONNECTOR, "", tail)
            marker = "即使结果不确定也继续"
            if tail.startswith(marker):
                continue_on.append("inconclusive")
                tail = re.sub("^" + _CONNECTOR, "", tail[len(marker):])
            if tail.startswith("找到后"):
                returning = "found"
                tail = tail[len("找到后"):]
            reporting = re.match(_REPORT, tail)
            if reporting:
                report = True
                tail = re.sub("^" + _CONNECTOR, "", tail[reporting.end():])
            if tail in {"返回起点", "返回基地", "回起点", "回基地"}:
                returning = "always" if returning == "never" else returning
            elif tail:
                raise CommandError("多地点搜索包含不支持的尾句")
            elif returning == "found":
                returning = "never"
            data = {"version": 1, "name": "多地点搜索" + obj, "command": raw,
                    "steps": [{"type": "search", "id": "find", "locations": locations, "object_name": obj,
                               "return_home": returning, "continue_on": continue_on}]}
            plan = compile_workflow(data, self.config)
            return replace(plan, metadata={**plan.metadata, "report_on_found": report})
        return None

    def interpret(self, text: str, context: dict | None = None) -> PlanningResult:
        ctx = copy.deepcopy(context or {})
        if not isinstance(ctx, dict):
            raise CommandError("对话上下文格式无效")
        raw = text
        normalized = _normalize(text)
        control = detect_control(text)
        if control:
            return PlanningResult(control, message="控制指令：" + normalized, context=ctx)
        if normalized in {"取消计划", "放弃草稿", "取消草稿"}:
            ctx.pop("pending_plan", None)
            ctx.pop("pending_schedule", None)
            ctx.pop("clarification", None)
            ctx.pop("model_clarification", None)
            return PlanningResult("answer", message="已清除当前会话的待确认计划和澄清问题。", context=ctx)
        if normalized in {"帮助", "你能做什么", "有哪些功能", "支持哪些指令", "怎么使用"}:
            return PlanningResult("answer", message="可以导航、找物、巡逻、等待、播报、汇报、暂停和停止；支持最多十个地点的顺序搜索、有界重复、复合条件和失败备用任务。可以通过队列安排定时任务。地点必须已配置，拍照、回充、跟随和转向需要已接入能力；尚未执行的计划可修改。", context=ctx)
        if normalized in {"上次去了哪里", "刚才说的是哪里", "上次的地点"}:
            target = ctx.get("last_target")
            label = self.config["locations"].get(target, {}).get("label")
            return PlanningResult("answer", message=f"上一条任务指令中的地点是{label}，这不表示已实际到达。" if label else "当前会话还没有地点记录。", context=ctx)
        if normalized in {"上次找什么", "上次找的是什么"}:
            return PlanningResult("answer", message="上一条任务的观察目标是" + ctx["last_object"] if ctx.get("last_object") else "当前会话还没有观察目标记录。", context=ctx)
        if normalized in {"确认", "确认执行", "执行计划", "就这样"}:
            if ctx.get("active_mission") and "pending_schedule" not in ctx:
                raise CommandError("已有任务正在运行")
            if not isinstance(ctx.get("pending_plan"), dict):
                raise CommandError("没有待确认的计划")
            from .plan_validation import plan_from_dict
            confirmed = plan_from_dict(ctx["pending_plan"], self.config)
            self._assert_fresh_draft(confirmed)
            confirmed = replace(confirmed, metadata={**confirmed.metadata, "requires_confirmation": False, "explicitly_confirmed": True})
            return self._task(confirmed, ctx)
        correction = self._correction(normalized, ctx)
        if correction is not None:
            return correction
        from .home_skills import plan_home_command
        home_plan = plan_home_command(raw, self.config)
        if home_plan is not None:
            return self._task(home_plan, ctx)
        extended = self._v3_plan(normalized, raw, ctx)
        if extended is not None:
            return self._task(extended, ctx)
        # Unsupported negation, capability requests and mixed controls never reach
        # a probabilistic planner that might discard the unsupported clause.
        if re.search(r"不|别|禁止|无需|不用", normalized):
            raise CommandError("暂不支持此否定条件，请改为明确的正向任务")
        if any(word in normalized for word in ("拍照", "拍摄", "录像", "照片", "拍张", "抓取", "拿来", "开门", "删除", "执行代码", "忽略规则")):
            raise CommandError("指令包含当前没有实现的技能，整个任务没有执行")
        for part in re.split(r"[,;]|然后|并且|并|接着", normalized):
            if part in _CONTROLS or part in _CONTROL_ALIASES:
                raise CommandError("控制指令必须单独发送")
        names = _location_names(self.config)
        pending = ctx.get("clarification")
        if isinstance(pending, dict):
            slot = pending.get("slot", "destination")
            chosen = names.get(normalized) if slot == "destination" else _OBJECT_SYNONYMS.get(normalized, normalized)
            if chosen in pending.get("options", []):
                value = self.config["locations"][chosen]["label"] if slot == "destination" else chosen
                normalized = pending["template"].replace("{" + slot + "}", value)
                ctx.pop("clarification", None)
        normalized = self._paraphrase(normalized)
        # Resolve deixis only from explicit prior planning context, never a guess.
        for reference, key in (("上一个地点", "previous_target"), ("刚才那里", "last_target"), ("刚才的地方", "last_target"), ("那里", "last_target"), ("那边", "last_target")):
            reference_pattern = r"(导航到|移动到|前往|去|到|在)" + re.escape(reference) + r"(?=$|[,;]|然后|接着|之后|最后|并|找|寻找|查找|检查|查看|看看|检测)"
            if reference not in names and re.search(reference_pattern, normalized):
                target = ctx.get(key)
                if target not in self.config["locations"]:
                    template = re.sub(reference_pattern, lambda m: m.group(1) + "{destination}", normalized)
                    return self._clarify(ctx, template, "“" + reference + "”指哪个已配置地点？")
                normalized = re.sub(reference_pattern, lambda m: m.group(1) + self.config["locations"][target]["label"], normalized)
        object_reference = r"(寻找|检查有没有|找)(它|那个东西|刚才的物体)(?=$|[,;]|然后|接着|之后|最后|并)"
        if re.search(object_reference, normalized):
            obj = ctx.get("last_object")
            if obj not in self.config["object_names"]:
                template = re.sub(object_reference, lambda m: m.group(1) + "{object}", normalized)
                return self._clarify(ctx, template, "要寻找哪一种物体？", self.config["object_names"], "object")
            normalized = re.sub(object_reference, lambda m: m.group(1) + obj, normalized)
        conditional = self._conditional(normalized, raw)
        if conditional is not None:
            return self._task(conditional, ctx)
        # Missing destinations and explicit alternatives require a reply.
        if normalized in {"去", "前往", "导航", "去一下", "去看看"}:
            return self._clarify(ctx, "去{destination}", "要前往哪个地点？")
        alternatives = re.fullmatch(r"(?:去|到|前往)(.+?)(?:还是|或者|或)(.+)", normalized)
        if alternatives and all(value in names for value in alternatives.groups()):
            return self._clarify(ctx, "去{destination}", "请选择一个目的地。", [names[value] for value in alternatives.groups()])
        bare_inspect = re.fullmatch(r"(?:找|寻找|检查有没有|看看有没有)(.+)", normalized)
        if bare_inspect:
            obj = _OBJECT_SYNONYMS.get(bare_inspect.group(1), bare_inspect.group(1))
            if obj in self.config["object_names"]:
                return self._clarify(ctx, "去{destination}找" + obj, "到哪个地点寻找" + obj + "？")
        missing_object = re.fullmatch(f"((?:去|在)({_pattern(names)}))(?:找|寻找|找一下)", normalized)
        if missing_object:
            return self._clarify(ctx, missing_object.group(1) + "找{object}", "要寻找哪一种物体？", self.config["object_names"], "object")
        partial = re.fullmatch(r"(?:去|到|前往)(.{1,20})", normalized)
        if partial and partial.group(1) not in names:
            choices = sorted({identifier for alias, identifier in names.items() if partial.group(1) in alias})
            if choices:
                return self._clarify(ctx, "去{destination}", "地点名称不完整，请确认目的地。", choices)
        report_requested = bool(re.search(_CONNECTOR + _REPORT + "$", normalized))
        if report_requested:
            normalized = re.sub(_CONNECTOR + _REPORT + "$", "", normalized)
        try:
            parsed = parse_command(normalized, self.config)
            if parsed.plan is None:
                return PlanningResult(parsed.kind, context=ctx)
            plan = replace(parsed.plan, command=raw, metadata={"planner": "deterministic_dialogue", "report_requested": report_requested})
            return self._task(plan, ctx)
        except CommandError:
            if self.provider is None:
                raise
        proposal = validate_model_output(self.provider.generate(raw, self.config, ctx), self.config, raw)
        if proposal["kind"] == "task":
            from .plan_validation import plan_from_dict
            plan = plan_from_dict(proposal["plan"], self.config, command=raw)
            plan = replace(plan, metadata={**plan.metadata, "planner": "model_proposal", "requires_confirmation": True})
            return self._task(plan, ctx, "模型已生成待确认计划，请先预览并确认；尚未执行。")
        if proposal["kind"] == "clarify":
            ctx["model_clarification"] = {"instruction": raw, "question": proposal["message"], "options": proposal["options"]}
        return PlanningResult(proposal["kind"], message=proposal["message"], context=ctx, options=proposal["options"])
