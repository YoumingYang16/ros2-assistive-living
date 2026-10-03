import threading
import time
import unittest
from datetime import datetime, timezone
from robot_voice_patrol.config import load_config
from robot_voice_patrol.contracts import CommandError, Plan, Step
from robot_voice_patrol.engine import MissionEngine
from robot_voice_patrol.mock_adapter import MockAdapter
from robot_voice_patrol.plan_validation import validate_plan, plan_from_dict


class ConditionsTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.config["mock"].update(travel_seconds=.01, inspection_seconds=.01)

    def run_plan(self, outcome):
        class Observation(MockAdapter):
            calls = []
            def execute(inner, step, cancel, feedback):
                inner.calls.append(step.step_id)
                if step.kind == "inspect":
                    return {"kind": "inspect", "target": step.target, "status": "succeeded", "outcome": outcome,
                            "evidence": {"fixture": True}, "simulated": True,
                            "observed_at": datetime.now(timezone.utc).isoformat()}
                return super().execute(step, cancel, feedback)
        adapter = Observation(self.config)
        engine = MissionEngine(self.config, adapter)
        plan = Plan("conditional", [Step("navigate", "meeting_room", step_id="go1"),
            Step("inspect", "meeting_room", object_name="水杯", step_id="look1"),
            Step("navigate", "storage", step_id="go2", condition={"step_id": "look1", "outcome": "not_found"}),
            Step("navigate", "home", step_id="return")], "conditional")
        try:
            engine.submit_plan(plan)
            deadline = time.monotonic() + 3
            while engine.snapshot()["state"] == "running" and time.monotonic() < deadline:
                time.sleep(.01)
            result = engine.snapshot()
            self.assertEqual(result["state"], "succeeded")
            return result, adapter.calls
        finally:
            engine.close()

    def test_not_found_runs_fallback(self):
        state, calls = self.run_plan("not_found")
        self.assertIn("go2", calls)
        self.assertEqual(state["mission"]["report"]["skipped_steps"], 0)

    def test_found_skips_fallback(self):
        state, calls = self.run_plan("found")
        self.assertNotIn("go2", calls)
        self.assertEqual(state["mission"]["report"]["skipped_steps"], 1)

    def test_inconclusive_never_becomes_not_found(self):
        state, calls = self.run_plan("inconclusive")
        self.assertNotIn("go2", calls)
        self.assertEqual(state["mission"]["report"]["inconclusive_observations"], 1)
        self.assertIn("无法得出明确结论", state["mission"]["report"]["message"])

    def test_cycles_and_forward_references_rejected(self):
        for source in ("a", "future"):
            with self.assertRaises(CommandError):
                validate_plan(Plan("bad", [Step("navigate", "home", step_id="a", condition={"step_id": source, "outcome": "found"})], "bad"), self.config)

    def test_condition_cannot_treat_navigation_as_object_detection(self):
        with self.assertRaises(CommandError):
            validate_plan(Plan("bad", [Step("navigate", "home", step_id="a"),
                Step("navigate", "storage", step_id="b", condition={"step_id": "a", "outcome": "not_found"})], "bad"), self.config)

    def test_unknown_fields_and_nan_model_output_rejected(self):
        for step in ({"kind": "navigate", "target": "home", "shell": "anything"},
                     {"kind": "wait", "seconds": float("nan")}, {"kind": "wait", "seconds": True}):
            with self.assertRaises(CommandError):
                plan_from_dict({"summary": "bad", "steps": [step]}, self.config)


if __name__ == "__main__":
    unittest.main()
