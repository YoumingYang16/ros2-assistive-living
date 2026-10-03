"""Software-only checks for atomic parsing and configuration invariants."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from robot_voice_patrol.config import ConfigError, load_config, validate_config
from robot_voice_patrol.contracts import CommandError
from robot_voice_patrol.planner import parse_command


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def plan(self, command):
        result = parse_command(command, self.config)
        self.assertEqual("task", result.kind)
        self.assertIsNotNone(result.plan)
        return result.plan

    def test_multi_step_navigation_preserves_order_and_destinations(self):
        plan = self.plan("去会议室然后去前台，最后返回起点")
        self.assertEqual(["meeting_room", "reception", "home"], [step.target for step in plan.steps])
        self.assertTrue(all(step.kind == "navigate" for step in plan.steps))

    def test_inspection_return_forms_complete_closed_loop_plan(self):
        plan = self.plan("去会议室看看有没有水杯然后返回起点")
        self.assertEqual(["navigate", "inspect", "navigate"], [step.kind for step in plan.steps])
        self.assertEqual("水杯", plan.steps[1].object_name)
        self.assertEqual("meeting_room", plan.steps[1].target)
        self.assertEqual("home", plan.steps[2].target)

    def test_location_and_object_aliases_resolve_to_canonical_values(self):
        plan = self.plan("请帮我去会议间看看有没有杯子吧。")
        self.assertEqual("meeting_room", plan.steps[0].target)
        self.assertEqual("水杯", plan.steps[1].object_name)

    def test_status_and_execution_controls_do_not_create_plans(self):
        for command, expected in [("暂停", "pause"), ("继续任务", "resume"), ("停止", "stop"),
                                  ("急停", "stop"), ("不要动", "stop"), ("查询状态", "status")]:
            with self.subTest(command=command):
                result = parse_command(command, self.config)
                self.assertEqual(expected, result.kind)
                self.assertIsNone(result.plan)

    def test_default_patrol_expands_navigation_inspection_and_dwell(self):
        plan = self.plan("巡逻两圈")
        routes = self.config["patrol_routes"]["default"]["waypoints"]
        self.assertEqual(routes * 2, [step.target for step in plan.steps if step.kind == "navigate"])
        self.assertEqual(routes * 2, [step.target for step in plan.steps if step.kind == "inspect"])
        self.assertEqual(18, len(plan.steps))

    def test_all_ui_examples_are_supported(self):
        cases = {
            "去会议室然后返回起点": ["navigate", "navigate"],
            "开始巡逻两圈": ["navigate", "inspect", "wait"] * 6,
            "去仓库检查有没有水杯，然后返回起点": ["navigate", "inspect", "navigate"],
        }
        for text, kinds in cases.items():
            with self.subTest(text=text):
                plan = self.plan(text)
                self.assertEqual(kinds, [step.kind for step in plan.steps])
        inspect = self.plan("去仓库检查有没有水杯，然后返回起点").steps[1]
        self.assertEqual("storage", inspect.target)
        self.assertEqual("水杯", inspect.object_name)

    def test_selected_patrol_locations_are_ordered(self):
        for command in ("在会议室和走廊巡逻两圈", "巡检会议室、走廊两圈", "巡护会议室及走廊2圈"):
            with self.subTest(command=command):
                plan = self.plan(command)
                self.assertEqual(["meeting_room", "corridor"] * 2,
                                 [step.target for step in plan.steps if step.kind == "navigate"])

    def test_named_patrol_route_and_return(self):
        plan = self.plan("沿默认巡逻路线巡逻一圈然后返回基地")
        self.assertEqual("home", plan.steps[-1].target)
        self.assertEqual(10, len(plan.steps))

    def test_wait_then_inspect_uses_preceding_explicit_location(self):
        plan = self.plan("去仓库，等待五秒，检查有没有箱子")
        self.assertEqual(["navigate", "wait", "inspect"], [step.kind for step in plan.steps])
        self.assertEqual(5, plan.steps[1].seconds)
        self.assertEqual("storage", plan.steps[2].target)

    def test_wait_units_and_numbers(self):
        for command, seconds in [("等待1.5秒", 1.5), ("等两分钟", 120), ("等待十一秒", 11),
                                 ("等待一百零五秒", 105), ("等待六十分钟", 3600)]:
            with self.subTest(command=command):
                step = self.plan(command).steps[0]
                self.assertEqual(seconds, step.seconds)
                self.assertGreater(step.timeout, step.seconds)

    def test_standalone_location_inspection(self):
        for command in ("在会议室检查有没有水杯", "检查会议室有没有水杯"):
            with self.subTest(command=command):
                plan = self.plan(command)
                self.assertEqual(["navigate", "inspect"], [step.kind for step in plan.steps])
                self.assertEqual("水杯", plan.steps[-1].object_name)

    def test_scene_inspection_needs_no_object(self):
        plan = self.plan("检查会议室")
        self.assertEqual("", plan.steps[-1].object_name)

    def test_unknown_destination_and_unsupported_tail_fail_entire_command(self):
        bad_commands = [
            "去会议室然后去月球", "去前台并打开门", "去会议室然后删除文件",
            "去会议室看看有没有水杯并开门", "去会议室看看有没有水杯开门",
            "去会议室看看有没有水杯和箱子", "去会议室找恐龙", "去会议室旁边",
            "去会议室然后", "去会议室，停止", "去会议室如果有人就返回",
        ]
        for command in bad_commands:
            with self.subTest(command=command), self.assertRaises(CommandError):
                parse_command(command, self.config)

    def test_negation_and_photography_are_explicitly_rejected(self):
        for command in ("不要去会议室", "别去前台", "去会议室但不要回来", "去会议室拍照", "去会议室拍张照片"):
            with self.subTest(command=command), self.assertRaises(CommandError):
                parse_command(command, self.config)

    def test_implicit_current_location_is_not_invented(self):
        with self.assertRaises(CommandError):
            parse_command("检查有没有水杯", self.config)

    def test_patrol_round_bounds_and_malformed_numbers(self):
        for command in ("巡逻零圈", "巡逻11圈", "巡逻1.5圈", "巡逻十十圈", "巡逻两三圈", "巡逻无限圈"):
            with self.subTest(command=command), self.assertRaises(CommandError):
                parse_command(command, self.config)
        self.assertEqual(90, len(self.plan("巡逻十圈").steps))

    def test_plan_limit_includes_all_compound_commands(self):
        with self.assertRaises(CommandError):
            parse_command("巡逻十圈然后巡逻两圈", self.config)

    def test_wait_bounds(self):
        for command in ("等待零秒", "等待-1秒", "等待3601秒", "等待61分钟", "等待十十秒"):
            with self.subTest(command=command), self.assertRaises(CommandError):
                parse_command(command, self.config)

    def test_empty_non_string_and_oversized_inputs(self):
        for value in ("", "  ", None, 42, "请", "去" * 501):
            with self.subTest(value=value), self.assertRaises(CommandError):
                parse_command(value, self.config)

    def test_planning_does_not_modify_configuration(self):
        before = copy.deepcopy(self.config)
        self.plan("巡逻两圈然后返回起点")
        self.assertEqual(before, self.config)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def invalid(self, config):
        with self.assertRaises(ConfigError):
            validate_config(config)

    def test_default_is_valid_and_has_required_home(self):
        self.assertIn("home", self.config["locations"])
        self.assertEqual(1, self.config["version"])

    def test_nonfinite_and_boolean_coordinates_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf"), True, "1.0"):
            with self.subTest(value=value):
                config = copy.deepcopy(self.config)
                config["locations"]["home"]["x"] = value
                self.invalid(config)

    def test_ambiguous_alias_and_id_collision_rejected(self):
        for alias in ("前台", "reception", "接待处"):
            with self.subTest(alias=alias):
                config = copy.deepcopy(self.config)
                config["locations"]["home"]["aliases"].append(alias)
                self.invalid(config)

    def test_duplicate_local_alias_rejected(self):
        self.config["locations"]["home"]["aliases"].append("基地")
        self.invalid(self.config)

    def test_reserved_home_alias_and_normalization_ambiguity_rejected(self):
        config = copy.deepcopy(self.config)
        config["locations"]["home"]["label"] = "家"
        config["locations"]["reception"]["aliases"].append("起点")
        self.invalid(config)
        config = copy.deepcopy(self.config)
        config["locations"]["home"]["aliases"].append("ｈｏｍｅ")
        self.invalid(config)

    def test_invalid_route_reference_rejected(self):
        self.config["patrol_routes"]["default"]["waypoints"].append("unknown")
        self.invalid(self.config)

    def test_invalid_retry_or_timeout_rejected(self):
        for field, value in [("max_retries", True), ("max_retries", 1.5), ("max_retries", 4),
                             ("navigation_timeout", 0), ("inspection_timeout", float("nan"))]:
            with self.subTest(field=field, value=value):
                config = copy.deepcopy(self.config)
                config[field] = value
                self.invalid(config)

    def test_mock_timeout_incompatibility_rejected(self):
        self.config["mock"]["travel_seconds"] = self.config["navigation_timeout"]
        self.invalid(self.config)

    def test_missing_home_and_unknown_fields_rejected(self):
        config = copy.deepcopy(self.config)
        del config["locations"]["home"]
        self.invalid(config)
        config = copy.deepcopy(self.config)
        config["max_retrys"] = 2
        self.invalid(config)

    def test_mock_observation_requires_declared_object_and_location(self):
        config = copy.deepcopy(self.config)
        config["mock"]["objects"]["meeting_room"].append("恐龙")
        self.invalid(config)
        config = copy.deepcopy(self.config)
        config["mock"]["objects"]["unknown"] = []
        self.invalid(config)

    def test_loaded_config_is_independent(self):
        result = validate_config(self.config)
        result["locations"]["home"]["x"] = 20
        self.assertEqual(0, self.config["locations"]["home"]["x"])

    def test_optional_ros_fields_and_partial_override(self):
        config = copy.deepcopy(self.config)
        del config["ros"]
        validate_config(config)
        config["ros"] = {"map_frame": "robot_1/map", "cancel_timeout": 0.1}
        self.assertEqual("robot_1/map", validate_config(config)["ros"]["map_frame"])

    def test_typed_perception_and_freshness_settings(self):
        for overrides in ({"perception_backend": "action", "inspection_action": "/robot/inspect", "service_discovery_timeout": .1, "pose_stale_seconds": 60},
                          {"perception_backend": "json"}):
            config = copy.deepcopy(self.config)
            config["ros"] = overrides
            self.assertEqual(overrides, validate_config(config)["ros"])
        for overrides in ({"perception_backend": "magic"}, {"perception_backend": []},
                          {"pose_stale_seconds": 0}, {"service_discovery_timeout": float("nan")}):
            config = copy.deepcopy(self.config)
            config["ros"] = overrides
            self.invalid(config)

    def test_invalid_ros_names_cancellation_bounds_and_unknown_fields(self):
        for overrides in ({"pose_topic": ""}, {"navigation_action": "/123"}, {"pose_topic": "/a//b"},
                          {"map_frame": "map frame"}, {"cancel_timeout": 0}, {"cancel_timeout": 31},
                          {"cancel_timeout": True}, {"cancel_timeout": float("nan")}, {"typo": "value"},
                          {"inspection_request_topic": "/shared", "inspection_result_topic": "shared"}):
            with self.subTest(overrides=overrides):
                config = copy.deepcopy(self.config)
                config["ros"] = overrides
                self.invalid(config)

    def test_explicit_utf8_bom_path_and_duplicate_json_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(self.config, ensure_ascii=False), encoding="utf-8-sig")
            self.assertEqual(self.config, load_config(path))
            path.write_text('{"version": 1, "version": 2}', encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_pip_target_layout_finds_installed_default(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            installed = target / "share" / "robot_voice_patrol" / "config" / "default.json"
            installed.parent.mkdir(parents=True)
            installed.write_text(json.dumps(self.config, ensure_ascii=False), encoding="utf-8")
            fake_module = target / "robot_voice_patrol" / "config.py"
            with patch("robot_voice_patrol.config.__file__", str(fake_module)):
                self.assertEqual(self.config, load_config())


if __name__ == "__main__":
    unittest.main()
