"""Source-attributed historical observation queries; history is not current truth."""
from __future__ import annotations
import copy
from datetime import datetime, timedelta, timezone
import re
from .contracts import Plan, Step, PlanningResult, CommandError
from .scheduling import parse_instant, timezone_for


class ObservationMemory:
    def __init__(self, store, config, *, timezone_name="Asia/Hong_Kong", freshness_seconds=3600):
        self.store, self.config = store, config
        self.timezone_name, self.freshness_seconds = timezone_name, freshness_seconds

    def query(self, **filters):
        filters = {k: v for k, v in filters.items() if v is not None and v != ""}
        rows = self.store.search_observations(**filters)
        instant = datetime.now(timezone.utc)
        for row in rows:
            try:
                age = max(0, (instant - parse_instant(row["observed_at"])).total_seconds())
            except (KeyError, CommandError):
                age = None
            row.update(age_seconds=round(age, 1) if age is not None else None,
                       stale=age is None or age > self.freshness_seconds, historical=True,
                       source={"mission_id": row["mission_id"], "step_id": row["step_id"],
                               "observation_id": row["observation_id"]})
            row["location_label"] = self.config["locations"].get(row.get("target"), {}).get("label", row.get("target", "未知地点"))
        return {"ok": True, "observations": rows, "summary": f"找到 {len(rows)} 条历史观察；不代表目标当前仍在原处",
                "freshness_seconds": self.freshness_seconds, "timezone": self.timezone_name}

    def interpret(self, text, context=None):
        """Only exact supported query families; unrecognized text returns None."""
        normalized = re.sub(r"[\s，。？！?!]", "", text)
        context = copy.deepcopy(context or {})
        object_pattern = "(?:" + "|".join(re.escape(obj) for obj in sorted(self.config["object_names"], key=len, reverse=True)) + "|它)"
        navigation_pattern = rf"去(?:最近一次|上次|最后一次)发现{object_pattern}的(?:地方|地点|位置)"
        query_patterns = [rf"(?:上次|最近|最后一次)(?:在哪里|在哪儿|在哪)(?:发现|看到){object_pattern}",
                          rf"{object_pattern}(?:上次|最近|最后一次)(?:在哪里|在哪儿|在哪)"]
        wants_navigation = bool(re.fullmatch(navigation_pattern, normalized))
        known_query = any(re.fullmatch(pattern, normalized) for pattern in query_patterns)
        objects = [obj for obj in self.config["object_names"] if obj in normalized]
        history_words = any(word in normalized for word in ("上次", "最近", "最后一次", "历史"))
        location_query = any(word in normalized for word in ("哪里", "哪儿", "什么地方", "在哪"))
        if not objects and "它" in normalized and context.get("last_object"):
            objects = [context["last_object"]]
        if len(objects) == 1 and history_words and (known_query or wants_navigation):
            obj = objects[0]
            result = self.query(object_name=obj, outcome="found", limit=1)
            if not result["observations"]:
                return PlanningResult("answer", message=f"没有找到关于{obj}的历史发现记录。", context=context)
            observation = result["observations"][0]
            target = observation["target"]
            location = observation["location_label"]
            stamp = observation["observed_at"]
            context.update(last_object=obj, memory_source=observation["source"])
            message = f"历史记录显示：{stamp} 在{location}发现{obj}，来源任务 {observation['mission_id']} 的步骤 {observation['step_id']}。这不是当前位置证明。"
            if observation["stale"]:
                message += "该记录已超过新鲜度窗口。"
            if wants_navigation:
                if target not in self.config["locations"]:
                    raise CommandError("历史地点已不在当前配置中，无法生成导航计划")
                from .scheduler import config_fingerprint
                plan = Plan(text, [Step("navigate", target=target)], f"前往历史发现地点：{location}",
                            {"requires_confirmation": True, "memory_source": observation["source"],
                             "historical_observed_at": stamp, "config_fingerprint": config_fingerprint(self.config)})
                context["pending_plan"] = plan.to_dict()
                return PlanningResult("task", plan, message + "请核对历史时间后确认是否前往。", context, ["确认执行", "取消计划"])
            return PlanningResult("answer", message=message, context=context)
        if normalized in {"今天哪些地方检查过", "今天检查过哪些地方", "今天巡检过哪些地方", "今天的检查记录"}:
            zone = timezone_for(self.timezone_name)
            local = datetime.now(zone)
            since = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
            rows = self.query(since=since.isoformat(), until=(since + timedelta(days=1)).isoformat(), limit=500)["observations"]
            places = list(dict.fromkeys(row["location_label"] for row in rows))
            message = f"按 {self.timezone_name} 日期，今天有 {len(rows)} 条观察，涉及：{'、'.join(places)}。" if places else "今天没有保存的观察记录。"
            context["memory_sources"] = [row["source"] for row in rows[:20]]
            return PlanningResult("answer", message=message, context=context)
        if normalized in {"上次实际到达哪里", "上次实际去了哪里", "最近到达的地点"}:
            for mission in self.store.history(200):
                for result in reversed(mission.get("results", [])):
                    if result.get("kind") == "navigate" and result.get("status") == "succeeded":
                        label = self.config["locations"].get(result.get("target"), {}).get("label", result.get("target"))
                        return PlanningResult("answer", message=f"任务 {mission['id']} 中最近一次记录的导航成功地点是{label}；实时位置请查看当前状态。", context=context)
            return PlanningResult("answer", message="没有已完成导航的历史记录。", context=context)
        return None
