import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import unittest

from robot_voice_patrol.config import load_config
from robot_voice_patrol.contracts import CommandError, Plan, Step
from robot_voice_patrol.model_provider import PLANNING_SCHEMA, validate_model_output
from robot_voice_patrol.natural_language import DialoguePlanner, parse_schedule_intent
from robot_voice_patrol.plan_validation import condition_value, evaluate_goal, plan_from_dict, validate_plan
from robot_voice_patrol.planner import parse_command
from robot_voice_patrol.workflow import compile_workflow, workflow_schema


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def search(self, **changes):
        node = {"type": "search", "id": "find", "locations": ["meeting_room", "storage", "reception"],
                "object_name": "水杯", "return_home": "found", **changes}
        return compile_workflow({"version": 1, "name": "三地搜索", "steps": [node]}, self.config)

    def test_search_checks_all_prior_observations_and_inconclusive_is_unknown(self):
        plan = self.search()
        third = plan.steps[4]
        results = [{"step_id": "find_look_1", "status": "succeeded", "outcome": "not_found"},
                   {"step_id": "find_look_2", "status": "succeeded", "outcome": "not_found"}]
        self.assertTrue(condition_value(third.condition, results))
        results[0]["outcome"] = "inconclusive"
        self.assertIsNone(condition_value(third.condition, results))
        permissive = self.search(continue_on=["not_found", "inconclusive"])
        self.assertTrue(condition_value(permissive.steps[4].condition, results))
        results[0]["outcome"] = "found"
        self.assertFalse(condition_value(permissive.steps[4].condition, results))
        self.assertTrue(condition_value(plan.steps[-1].condition, results))

    def test_goal_reports_found_negative_and_unknown_separately(self):
        plan = self.search()
        inspections = [step for step in plan.steps if step.kind == "inspect"]
        results = [{"step_id": step.step_id, "status": "succeeded", "outcome": "not_found"} for step in inspections]
        self.assertEqual("not_achieved", evaluate_goal(plan, results)["outcome"])
        self.assertEqual("unknown", evaluate_goal(plan, results[:-1])["outcome"])
        results[0]["outcome"] = "inconclusive"
        self.assertEqual("unknown", evaluate_goal(plan, results)["outcome"])
        results[1]["outcome"] = "found"
        goal = evaluate_goal(plan, results)
        self.assertEqual("achieved", goal["outcome"])
        self.assertEqual([inspections[1].step_id], goal["supporting_step_ids"])

    def test_not_does_not_convert_unknown_to_true(self):
        leaf = {"step_id": "look", "outcome": "found"}
        for results in ([], [{"step_id": "look", "status": "skipped"}],
                        [{"step_id": "look", "status": "succeeded", "outcome": "inconclusive"}]):
            self.assertIsNone(condition_value({"not": leaf}, results))

    def test_failure_and_timeout_conditions_remain_distinct(self):
        failed = {"step_id": "go", "outcome": "failed"}
        timed = {"step_id": "go", "outcome": "timed_out"}
        for result in ({"step_id": "go", "status": "timed_out"},
                       {"step_id": "go", "status": "failed", "error_code": "NAV_TIMEOUT"}):
            self.assertTrue(condition_value(timed, [result]))
            self.assertFalse(condition_value(failed, [result]))
        result = {"step_id": "go", "status": "failed", "error_code": "NAV_ABORTED"}
        self.assertTrue(condition_value(failed, [result]))
        self.assertFalse(condition_value(timed, [result]))

    def test_repeat_resolves_each_iterations_local_references(self):
        workflow = {"version": 1, "name": "重复", "steps": [{"type": "repeat", "id": "round", "count": 2,
            "body": [{"type": "step", "id": "go", "kind": "navigate", "target": "storage", "on_failure": "continue"},
                     {"type": "if", "id": "fallback", "condition": {"step_id": "go", "outcome": "failed"},
                      "then": [{"type": "step", "id": "say", "kind": "speak", "params": {"text": "导航失败"}}]}]}]}
        plan = compile_workflow(workflow, self.config)
        self.assertEqual(["round_1__go", "round_2__go"], [plan.steps[i].condition["step_id"] for i in (1, 3)])
        self.assertEqual(4, len({step.step_id for step in plan.steps}))

    def test_if_else_preserves_three_valued_gate(self):
        workflow = {"version": 1, "name": "分支", "steps": [
            {"type": "step", "id": "look", "kind": "inspect", "target": "storage", "object_name": "水杯"},
            {"type": "if", "condition": {"step_id": "look", "outcome": "found"},
             "then": [{"type": "step", "kind": "speak", "params": {"text": "找到"}}],
             "else": [{"type": "step", "kind": "speak", "params": {"text": "未找到"}}]}]}
        plan = compile_workflow(workflow, self.config)
        results = [{"step_id": "look", "status": "succeeded", "outcome": "inconclusive"}]
        self.assertIsNone(condition_value(plan.steps[1].condition, results))
        self.assertIsNone(condition_value(plan.steps[2].condition, results))

    def test_parameter_overrides_preserve_types_and_do_not_mutate_template(self):
        data = {"version": 1, "name": "参数模板", "parameters": {
            "place": {"type": "location", "default": "meeting_room"},
            "times": {"type": "integer", "default": 1, "min": 1, "max": 3}},
            "steps": [{"type": "repeat", "count": "${times}", "body": [
                {"type": "step", "kind": "navigate", "target": "${place}"}]}]}
        original = copy.deepcopy(data)
        plan = compile_workflow(data, self.config, parameters={"place": "storage", "times": 3})
        self.assertEqual(["storage"] * 3, [step.target for step in plan.steps])
        self.assertEqual(original, data)
        for parameters in ({"place": "moon"}, {"times": True}, {"times": 4}, {"unknown": 1}):
            with self.subTest(parameters=parameters), self.assertRaises(CommandError):
                compile_workflow(data, self.config, parameters=parameters)
        data["values"] = []
        with self.assertRaises(CommandError):
            compile_workflow(data, self.config, parameters={})

    def test_rejects_unbounded_repetition_expressions_duplicates_and_future_refs(self):
        for nodes in (
            [{"type": "repeat", "count": 11, "body": [{"type": "step", "kind": "wait", "seconds": 1}]}],
            [{"type": "step", "kind": "navigate", "target": "${place + 1}"}],
            [{"type": "step", "kind": "navigate", "target": "home", "id": "same"}] * 2,
            [{"type": "step", "kind": "navigate", "target": "home", "condition": {"step_id": "future", "outcome": "succeeded"}}],
            [{"type": "repeat", "count": 10, "body": [{"type": "repeat", "count": 10, "body": [
                {"type": "step", "kind": "wait", "seconds": 1}, {"type": "step", "kind": "wait", "seconds": 1}]}]}],
        ):
            with self.subTest(nodes=nodes), self.assertRaises(CommandError):
                compile_workflow({"version": 1, "name": "bad", "steps": nodes}, self.config)

    def test_plan_versions_one_two_three_import_as_v3(self):
        for version in (1, 2, 3):
            imported = plan_from_dict({"version": version, "summary": "旧任务", "steps": [{"kind": "navigate", "target": "home"}]}, self.config)
            self.assertEqual(3, imported.to_dict()["version"])
            self.assertEqual("abort", imported.steps[0].on_failure)
            self.assertEqual({}, imported.steps[0].params)
        for version in (0, 4, True, "3"):
            with self.assertRaises(CommandError):
                plan_from_dict({"version": version, "summary": "bad", "steps": [{"kind": "navigate", "target": "home"}]}, self.config)

    def test_condition_depth_and_node_limits(self):
        condition = {"step_id": "go", "outcome": "succeeded"}
        for _ in range(8):
            condition = {"not": condition}
        with self.assertRaises(CommandError):
            validate_plan(Plan("deep", [Step("navigate", "home", step_id="go"), Step("navigate", "storage", condition=condition)], "deep"), self.config)
        condition = {"all": [{"any": [{"step_id": "go", "outcome": "succeeded"}] * 16}] * 4}
        with self.assertRaises(CommandError):
            validate_plan(Plan("wide", [Step("navigate", "home", step_id="go"), Step("navigate", "storage", condition=condition)], "wide"), self.config)


class LanguageV3Tests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.planner = DialoguePlanner(self.config, False)

    def test_all_single_object_search_entrypoints_have_goals(self):
        for text in ("去会议室找水杯", "先去会议室找水杯，没找到就去仓库", "依次去前台、仓库、会议室找水杯"):
            with self.subTest(text=text):
                self.assertEqual({"kind": "find_object", "object_name": "水杯"}, self.planner.interpret(text).plan.metadata["goal"])
        self.assertEqual("水杯", parse_command("去会议室找水杯", self.config).plan.metadata["goal"]["object_name"])
        self.assertNotIn("goal", self.planner.interpret("去前台找人然后去会议室找水杯").plan.metadata)

    def test_cancel_draft_removes_pending_schedule_without_mutating_caller(self):
        context = {"pending_plan": {}, "pending_schedule": {"kind": "daily"}, "clarification": {}, "model_clarification": {}, "last_target": "storage"}
        result = self.planner.interpret("取消计划", context)
        self.assertEqual({"last_target": "storage"}, result.context)
        self.assertIn("pending_schedule", context)

    def test_scheduled_draft_can_confirm_or_correct_while_another_task_is_active(self):
        draft = self.planner.interpret("去会议室").context
        draft.update(active_mission="currently-running", pending_schedule={})
        revised = self.planner.interpret("改去仓库", draft)
        self.assertEqual("storage", revised.plan.steps[0].target)
        self.assertTrue(self.planner.interpret("确认执行", revised.context).plan.metadata["explicitly_confirmed"])
        draft.pop("pending_schedule")
        for text in ("改去仓库", "确认执行"):
            with self.assertRaises(CommandError):
                self.planner.interpret(text, draft)

    def test_repeat_compound_failure_and_external_skills_compile(self):
        repeated = self.planner.interpret("重复三次：去仓库然后返回起点").plan
        self.assertEqual(6, len(repeated.steps))
        compound = self.planner.interpret("去会议室找水杯然后去仓库找水杯，如果两处都没找到就返回起点").plan
        self.assertIn("all", compound.steps[-1].condition)
        failure = self.planner.interpret("去会议室，如果失败或超时就去前台").plan
        self.assertEqual("continue", failure.steps[0].on_failure)
        self.assertIn("any", failure.steps[1].condition)
        for text, kind in (("播报：请注意安全", "speak"), ("生成报告", "report"), ("等待定位有效", "wait_state"),
                           ("拍照", "capture"), ("对接充电点", "dock"), ("左转90度", "turn"), ("跟随小王持续五秒", "follow")):
            with self.subTest(text=text):
                self.assertEqual(kind, self.planner.interpret(text).plan.steps[0].kind)

    def test_explicit_schedule_parsing_relative_daily_interval_and_after_current(self):
        now = datetime(2026, 10, 3, 2, 0, tzinfo=timezone.utc)  # 10:00 Hong Kong
        relative = parse_schedule_intent("十分钟后去会议室", now=now)
        self.assertEqual("2026-10-03T10:10:00+08:00", relative["run_at"])
        self.assertEqual("去会议室", relative["text"])
        daily = parse_schedule_intent("每天上午9点巡逻", now=now)
        self.assertEqual("2026-10-04T09:00:00+08:00", daily["run_at"])
        self.assertEqual({"daily_at": "09:00", "timezone": "Asia/Hong_Kong"}, daily["repeat"])
        interval = parse_schedule_intent("每隔十分钟去前台", now=now)
        self.assertEqual({"interval_seconds": 600}, interval["repeat"])
        self.assertTrue(parse_schedule_intent("当前任务完成后去仓库", now=now)["after_current"])
        self.assertIsNone(parse_schedule_intent("去会议室", now=now))

    def test_schedule_rejects_past_invalid_nested_and_control_requests(self):
        now = datetime(2026, 10, 3, 2, 0, tzinfo=timezone.utc)
        for text in ("今天9点去前台", "每天25点去前台", "每隔一秒去前台", "十分钟后停止", "每天9点每天8点去前台", "今天某时去前台"):
            with self.subTest(text=text), self.assertRaises(CommandError):
                parse_schedule_intent(text, now=now)
        with self.assertRaises(CommandError):
            parse_schedule_intent("十分钟后去前台", now=now.replace(tzinfo=None))


class ModelV3Tests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def response(self, steps):
        return {"kind": "task", "message": "待确认", "options": [], "plan": {"summary": "待确认任务", "steps": [step.to_dict() for step in steps]}}

    def test_v3_compound_failure_response_and_local_skill(self):
        steps = [Step("navigate", "storage", step_id="go", on_failure="continue"),
                 Step("speak", step_id="say", params={"text": "请协助"}, condition={"any": [
                     {"step_id": "go", "outcome": "failed"}, {"step_id": "go", "outcome": "timed_out"}]})]
        result = validate_model_output(self.response(steps), self.config)
        self.assertEqual("speak", result["plan"]["steps"][1]["kind"])

    def test_wrong_skill_parameters_and_unprotected_observation_rejected(self):
        with self.assertRaises(CommandError):
            validate_model_output(self.response([Step("navigate", "home", params={"text": "bad"})]), self.config)
        with self.assertRaises(CommandError):
            validate_model_output(self.response([Step("navigate", "home", step_id="go", on_failure="continue"),
                Step("inspect", "home", object_name="水杯", step_id="look")]), self.config)

    def test_generated_schemas_match_python_contract(self):
        directory = Path(__file__).resolve().parents[1] / "schemas"
        model = json.loads((directory / "model-output.schema.json").read_text(encoding="utf-8"))
        model.pop("$schema", None)
        self.assertEqual(PLANNING_SCHEMA, model)
        self.assertEqual(workflow_schema(), json.loads((directory / "workflow-v1.schema.json").read_text(encoding="utf-8")))
        # Every object used for strict generation is closed and all properties required.
        def visit(value):
            if isinstance(value, dict):
                if value.get("type") == "object":
                    self.assertFalse(value["additionalProperties"])
                    self.assertEqual(set(value["properties"]), set(value["required"]))
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(PLANNING_SCHEMA)


if __name__ == "__main__":
    unittest.main()
