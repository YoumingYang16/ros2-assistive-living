from datetime import datetime, timezone, timedelta
import threading
import time
import unittest
from unittest.mock import patch
from robot_voice_patrol.config import load_config
from robot_voice_patrol.contracts import CommandError, ExecutionError, Plan, Step, PlanningResult
from robot_voice_patrol.engine import MissionEngine
from robot_voice_patrol.mock_adapter import MockAdapter
from robot_voice_patrol.plan_validation import validate_plan


class BoundariesTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.step = Step("inspect", "meeting_room", object_name="水杯")
        self.result = {"status": "succeeded", "kind": "inspect", "target": "meeting_room", "object_name": "水杯",
                       "found": True, "outcome": "found", "evidence": {"fixture": True},
                       "observed_at": datetime.now(timezone.utc).isoformat()}

    def test_mismatched_or_contradictory_observations_rejected(self):
        for patch in ({"object_name": "箱子"}, {"found": False}, {"outcome": "not_found"},
                      {"outcome": "inconclusive"}, {"found": 1}, {"evidence": {}},
                      {"observed_at": (datetime.now(timezone.utc)-timedelta(days=1)).isoformat()}):
            with self.subTest(patch=patch), self.assertRaises(ExecutionError):
                MissionEngine._normalize_result(self.step, {**self.result, **patch})

    def test_ill_typed_plan_never_leaks_raw_type_error(self):
        cases = [Step(["navigate"], "home"), Step("navigate", ["home"]), Step("navigate", "home", step_id=0),
                 Step("wait", seconds=10**999, timeout=60), Step("wait", seconds=False)]
        for step in cases:
            with self.subTest(step=step), self.assertRaises(CommandError):
                validate_plan(Plan("bad", [step], "bad"), self.config)

    def test_stop_clears_previous_pending_draft(self):
        engine = MissionEngine(self.config, MockAdapter(self.config))
        try:
            engine.preview("去会议室")
            self.assertIn("pending_plan", engine.store.session("default"))
            engine.submit("停止")
            self.assertNotIn("pending_plan", engine.store.session("default"))
            with self.assertRaises(CommandError):
                engine.submit("确认执行")
        finally:
            engine.close()

    def test_pending_inspection_blocks_configuration_and_new_mission(self):
        engine = MissionEngine(self.config, MockAdapter(self.config))
        try:
            with patch.object(engine.adapter, "snapshot", return_value={"action_pending": True}):
                with self.assertRaises(CommandError):
                    engine.submit_plan(Plan("go", [Step("navigate", "home")], "go"))
                with self.assertRaises(CommandError):
                    engine.update_config(self.config)
                self.assertFalse(engine.health()["ready"])
            self.assertEqual(engine.metrics()["missions_total"], 0)
        finally:
            engine.close()

    def test_late_model_draft_after_stop_cannot_be_confirmed(self):
        started, release = threading.Event(), threading.Event()
        class DraftPlanner:
            def interpret(self, text, context=None):
                started.set()
                release.wait(2)
                return PlanningResult("task", Plan(text, [Step("navigate", "home")], "draft",
                    {"requires_confirmation": True}), context={"pending_plan": {"stale": True}})
        engine = MissionEngine(self.config, MockAdapter(self.config), planner=DraftPlanner())
        errors = []
        def submit():
            try:
                engine.submit("模型草稿")
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=submit)
        try:
            thread.start()
            self.assertTrue(started.wait(1))
            engine.submit("立即停止")
            release.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertIsInstance(errors[0], CommandError)
            self.assertNotIn("pending_plan", engine.store.session("default"))
        finally:
            release.set()
            thread.join(3)
            engine.close()


if __name__ == "__main__":
    unittest.main()
