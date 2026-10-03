import copy
import threading
import time
from types import SimpleNamespace
import unittest

from robot_voice_patrol.config import load_config
from robot_voice_patrol.contracts import CommandError, ExecutionCancelled, Step
from robot_voice_patrol.mock_adapter import MockAdapter
from robot_voice_patrol.engine import MissionEngine
from robot_voice_patrol.contracts import Plan
from robot_voice_patrol.skills import SkillExecutionError, SkillRegistry, SkillSpec, get_registry


class SkillTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.adapter = MockAdapter(self.config)
        self.registry = get_registry()
        self.feedback, self.speech = [], []

    def execute(self, step, **context):
        return self.registry.execute(step, self.adapter, threading.Event(), self.feedback.append,
                                     {"config": self.config, "emit_speech": self.speech.append, **context})

    def test_speech_is_delivered_but_never_claims_speaker_playback(self):
        result = self.execute(Step("speak", params={"text": "巡检开始"}))
        self.assertEqual(self.speech, ["巡检开始"])
        self.assertFalse(result["audible_confirmed"])
        self.assertFalse(result["simulated"])

    def test_missing_speech_sink_does_not_claim_success(self):
        with self.assertRaises(SkillExecutionError) as caught:
            self.execute(Step("speak", params={"text": "你好"}), emit_speech=None)
        self.assertEqual(caught.exception.code, "SPEECH_UNAVAILABLE")

    def test_report_preserves_observation_provenance_and_can_omit_details(self):
        rows = [{"kind": "inspect", "status": "succeeded", "source": "camera-a", "evidence": {"frame": 42}, "simulated": True},
                {"kind": "navigate", "status": "failed"}]
        result = self.execute(Step("report"), results=rows, mission_id="m1")
        self.assertEqual(result["report"]["observations"][0]["evidence"], {"frame": 42})
        self.assertEqual(result["report"]["failed_steps"], 1)
        self.assertTrue(result["report"]["simulated"])
        hidden = self.execute(Step("report", params={"include_observations": False}), results=rows)
        self.assertEqual(hidden["report"]["observations"], [])

    def test_state_wait_observes_actual_adapter_state_changes(self):
        def move():
            with self.adapter._lock:
                self.adapter._pose["battery"] = .9
        timer = threading.Timer(.04, move)
        timer.start()
        try:
            result = self.execute(Step("wait_state", timeout=.5, params={"field": "battery", "operator": "gte", "value": .8, "poll_seconds": .02}))
            self.assertEqual(result["observed"], .9)
            self.assertTrue(result["simulated"])
        finally:
            timer.join()

    def test_missing_state_field_cannot_satisfy_not_equal(self):
        with self.assertRaises(SkillExecutionError) as caught:
            self.execute(Step("wait_state", timeout=.03, params={"field": "unknown", "operator": "ne", "value": 0}))
        self.assertEqual(caught.exception.code, "STATE_TIMEOUT")

    def test_wait_state_cancellation_is_prompt(self):
        cancel = threading.Event()
        timer = threading.Timer(.02, cancel.set)
        timer.start()
        try:
            with self.assertRaises(ExecutionCancelled):
                self.registry.execute(Step("wait_state", params={"field": "location", "value": "storage"}),
                                      self.adapter, cancel, lambda _: None, {"config": self.config})
        finally:
            timer.join()

    def test_external_skills_require_explicit_connected_capability(self):
        with self.assertRaises(SkillExecutionError) as caught:
            self.execute(Step("turn", params={"angle_degrees": 90}))
        self.assertEqual(caught.exception.code, "CAPABILITY_UNAVAILABLE")
        self.assertEqual(self.adapter.snapshot()["yaw"], 0)

    def test_explicit_fixture_turn_and_capture_have_simulated_evidence(self):
        self.adapter = MockAdapter(self.config, fixture_skills=True)
        result = self.execute(Step("turn", params={"angle_degrees": 90}))
        self.assertAlmostEqual(self.adapter.snapshot()["yaw"], 1.5707963267948966)
        self.assertTrue(result["simulated"])
        result = self.execute(Step("capture"))
        self.assertTrue(result["evidence"]["media_uri"].startswith("mock://"))
        self.assertFalse(result["evidence"]["image_generated"])

    def test_invalid_parameters_are_rejected_before_side_effects(self):
        for step in [Step("speak", params={"text": "你好", "shell": "do something"}),
                     Step("turn", params={"angle_degrees": float("nan")}),
                     Step("follow", timeout=2, params={"subject": "person", "duration_seconds": 2}),
                     Step("wait_state", params={"field": "__dict__", "value": 1}),
                     Step("wait_state", params={"field": "x", "operator": "gt", "value": "a"})]:
            with self.subTest(step=step), self.assertRaises(CommandError):
                self.execute(step)
        self.assertEqual(self.speech, [])

    def test_catalog_cannot_mutate_registered_schema(self):
        entry = next(x for x in self.registry.catalog() if x["name"] == "speak")
        entry["params_schema"]["properties"].clear()
        self.assertIn("text", next(x for x in self.registry.catalog() if x["name"] == "speak")["params_schema"]["properties"])

    def test_trusted_plugin_registration_and_return_validation(self):
        registry = SkillRegistry()
        spec = SkillSpec("measure", "测量", "例子", {"type": "object", "properties": {}, "additionalProperties": False},
                         lambda *args: {"kind": "wrong", "status": "succeeded"})
        registry.register(spec)
        with self.assertRaises(ValueError):
            registry.register(spec)
        with self.assertRaises(SkillExecutionError):
            registry.execute(Step("measure"), self.adapter, threading.Event(), lambda _: None, {"config": self.config})

    def test_cancelled_step_never_emits_speech(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(ExecutionCancelled):
            self.registry.execute(Step("speak", params={"text": "不要播报"}), self.adapter, event, lambda _: None,
                                  {"config": self.config, "emit_speech": self.speech.append})
        self.assertEqual(self.speech, [])

    def test_actual_mock_timeout_dispatches_timeout_fallback(self):
        engine = MissionEngine(self.config, self.adapter)
        try:
            plan = Plan("超时后生成报告", [
                Step("navigate", "meeting_room", timeout=.02, max_retries=0, step_id="move", on_failure="continue"),
                Step("report", step_id="fallback", condition={"step_id": "move", "outcome": "timed_out"})], "超时补救")
            engine.submit_plan(plan)
            engine._worker.join(2)
            results = engine.snapshot()["mission"]["results"]
            self.assertEqual(results[0]["outcome"], "timed_out")
            self.assertEqual(results[0]["error_code"], "EXECUTION_TIMEOUT")
            self.assertEqual(results[1]["status"], "succeeded")
            self.assertEqual(results[1]["kind"], "report")
        finally:
            engine.close()


if __name__ == "__main__":
    unittest.main()
