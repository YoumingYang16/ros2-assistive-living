from datetime import datetime, timedelta, timezone
import copy
import tempfile
import unittest
from unittest.mock import patch
from robot_voice_patrol.config import load_config
from robot_voice_patrol.contracts import CommandError, Plan, Step
from robot_voice_patrol.engine import MissionEngine
from robot_voice_patrol.mock_adapter import MockAdapter
from robot_voice_patrol.plan_validation import validate_plan
from robot_voice_patrol.recovery import _simplify
from tests.test_queue_data_v3 import finish


class MemoryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.config["mock"].update(travel_seconds=.01, inspection_seconds=.01)
        self.engine = MissionEngine(self.config, MockAdapter(self.config), start_scheduler=False)

    def tearDown(self):
        self.engine.close()

    def test_source_attributed_memory_and_confirmed_historical_navigation(self):
        self.engine.submit("去会议室找水杯")
        finish(self.engine)
        initial = self.engine.metrics()["missions_total"]
        answer = self.engine.submit("上次在哪里发现水杯")
        self.assertIn("历史记录", answer["message"])
        self.assertEqual(self.engine.metrics()["missions_total"], initial)
        result = self.engine.memory_service.query(object_name="水杯")
        self.assertEqual(result["observations"][0]["source"]["step_id"], "s002")
        draft = self.engine.submit("去最近一次发现水杯的地方")
        self.assertTrue(draft["needs_confirmation"])
        self.assertEqual(self.engine.metrics()["missions_total"], initial)
        self.engine.submit("确认执行")
        self.assertEqual(finish(self.engine)["state"], "succeeded")

    def test_old_observation_is_marked_stale_not_current_truth(self):
        observed = (datetime.now(timezone.utc)-timedelta(days=3)).isoformat()
        mission = {"id": "historical", "state": "succeeded", "started_at": observed, "results": [{"kind": "inspect", "status": "succeeded",
            "step_id": "look", "target": "meeting_room", "object_name": "水杯", "outcome": "found", "found": True,
            "observed_at": observed, "evidence": {"fixture": True}}]}
        self.engine.store.save_mission(mission)
        result = self.engine.memory_service.query(object_name="水杯")["observations"][0]
        self.assertTrue(result["stale"])
        answer = self.engine.submit("水杯上次在哪里")
        self.assertIn("超过新鲜度", answer["message"])

    def checkpoint(self, *, mode="mock"):
        plan = validate_plan(Plan("find", [Step("navigate", "meeting_room", step_id="go"),
            Step("inspect", "meeting_room", object_name="水杯", step_id="look"), Step("navigate", "home", step_id="back")], "find",
            {"goal": {"kind": "find_object", "object_name": "水杯"}}), self.config)
        mission = {"id": "interrupted1", **plan.to_dict(), "state": "interrupted", "mode": mode,
            "started_at": datetime.now(timezone.utc).isoformat(), "recovery_required": True,
            "results": [{"kind": "navigate", "target": "meeting_room", "status": "succeeded", "step_id": "go", "outcome": "succeeded"}],
            "step_states": [{"step_id": "go", "status": "succeeded"}, {"step_id": "look", "status": "interrupted"}, {"step_id": "back", "status": "pending"}]}
        self.engine.store.save_mission(mission)
        return mission

    def test_recovery_requires_confirmation_and_reestablishes_location(self):
        mission = self.checkpoint()
        preview = self.engine.preview_recovery(mission["id"])
        self.assertEqual(preview["assessment"]["completed_steps_preserved"], ["go"])
        self.assertEqual(preview["plan"]["steps"][0]["step_id"], "recover_look")
        self.assertEqual(self.engine.snapshot()["state"], "idle")
        with self.assertRaises(CommandError):
            self.engine.resume_recovery(mission["id"])
        result = self.engine.resume_recovery(mission["id"], confirmed=True, request_id="resume-once")
        state = finish(self.engine)
        self.assertEqual(state["state"], "succeeded")
        self.assertEqual(state["mission"]["metadata"]["parent_mission_id"], mission["id"])
        self.assertEqual(self.engine.store.mission(mission["id"])["resumed_as"], result["mission_id"])

    def test_unverified_remote_operation_blocks_recovery(self):
        mission = self.checkpoint(mode="ros2")
        with patch.object(self.engine.adapter, "reconcile_mission", return_value={"verified": False, "remote_state": "unknown"}):
            preview = self.engine.preview_recovery(mission["id"])
            self.assertTrue(preview["blocked"])
            self.assertIsNone(preview["plan"])

    def test_recovery_generated_ids_are_bounded_and_collision_free(self):
        for inspection_id, return_id in [("a" * 64, "back"), ("look", "recover_look")]:
            mission = self.checkpoint()
            mission["steps"][1]["step_id"] = inspection_id
            mission["steps"][2]["step_id"] = return_id
            self.engine.store.save_mission(mission)
            plan = self.engine.preview_recovery(mission["id"])["plan"]
            identifiers = [step["step_id"] for step in plan["steps"]]
            self.assertEqual(len(identifiers), len(set(identifiers)))
            self.assertTrue(all(len(value) <= 64 for value in identifiers))
            self.assertEqual(plan["steps"][0]["kind"], "navigate")

    def test_unknown_observation_never_becomes_true_through_recovery_negation(self):
        results = {"look": {"step_id": "look", "kind": "inspect", "status": "succeeded", "outcome": "inconclusive"}}
        condition = {"not": {"step_id": "look", "outcome": "found"}}
        self.assertIs(_simplify(condition, results), False)
        dynamic = {"any": [condition, {"step_id": "later", "outcome": "found"}]}
        self.assertEqual(_simplify(dynamic, results), {"any": [{"step_id": "later", "outcome": "found"}]})

    def test_process_success_and_goal_failure_are_distinct(self):
        self.config["mock"]["objects"]["meeting_room"] = []
        self.engine.submit("去会议室找水杯")
        value = finish(self.engine)
        self.assertEqual(value["state"], "succeeded")
        self.assertEqual(value["mission"]["report"]["goal_outcome"], "not_achieved")

    def test_completed_navigation_query_uses_execution_evidence(self):
        self.engine.preview("去会议室")
        answer = self.engine.submit("上次实际到达哪里")
        self.assertIn("没有已完成导航", answer["message"])
        self.engine.submit("去前台")
        finish(self.engine)
        answer = self.engine.submit("上次实际到达哪里")
        self.assertIn("前台", answer["message"])

    def test_historical_navigation_does_not_ignore_unsupported_tail(self):
        self.engine.submit("去会议室找水杯")
        finish(self.engine)
        with self.assertRaises(CommandError):
            self.engine.submit("去最近一次发现水杯的地方然后删除数据")

    def test_repeated_recovery_confirmation_does_not_dispatch_again(self):
        mission = self.checkpoint()
        first = self.engine.resume_recovery(mission["id"], confirmed=True, request_id="repeat-recovery")
        second = self.engine.resume_recovery(mission["id"], confirmed=True, request_id="repeat-recovery")
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["mission_id"], second["mission_id"])
        finish(self.engine)


if __name__ == "__main__":
    unittest.main()
