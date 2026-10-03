import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from robot_voice_patrol.config import load_config
from robot_voice_patrol.contracts import CommandError, ExecutionError, Plan, Step
from robot_voice_patrol.engine import MissionEngine
from robot_voice_patrol.mock_adapter import MockAdapter


def wait_for(engine, states, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = engine.snapshot()
        if state["state"] in states:
            return state
        time.sleep(.005)
    raise AssertionError(f"Timed out waiting for {states}: {engine.snapshot()}")


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.config["mock"].update(travel_seconds=.01, inspection_seconds=.01)
        self.engine = MissionEngine(self.config, MockAdapter(self.config))

    def tearDown(self):
        self.engine.close()

    def test_full_inspect_and_return_has_evidence_and_real_outcome(self):
        self.engine.submit("去会议室看看有没有水杯然后返回起点")
        result = wait_for(self.engine, {"succeeded", "failed"})
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["robot"]["location"], "home")
        observation = result["mission"]["report"]["observations"][0]
        self.assertTrue(observation["found"])
        self.assertTrue(observation["simulated"])
        self.assertIn("预设数据", observation["evidence"])
        self.assertIn("observed_at", observation)
        self.assertEqual(len(result["memory"]), 1)

    def test_not_observed_is_reported_as_not_found(self):
        self.engine.submit("去仓库检查有没有水杯")
        state = wait_for(self.engine, {"succeeded"})
        self.assertFalse(state["mission"]["report"]["observations"][0]["found"])

    def test_unknown_tail_does_not_start_partial_mission(self):
        with self.assertRaises(CommandError):
            self.engine.submit("去会议室然后去不存在的地方")
        self.assertEqual(self.engine.snapshot()["state"], "idle")
        self.assertEqual(self.engine.snapshot()["robot"]["location"], "home")

    def test_concurrent_submission_rejected(self):
        self.engine.submit("等待两秒")
        with self.assertRaises(CommandError):
            self.engine.submit("去会议室")
        self.engine.control("stop")
        wait_for(self.engine, {"cancelled"})

    def test_pause_resume_reexecutes_unfinished_step(self):
        self.config["mock"]["travel_seconds"] = .15
        self.engine.submit("去会议室然后返回起点")
        time.sleep(.03)
        self.engine.control("pause")
        state = wait_for(self.engine, {"paused"})
        self.assertEqual(len(state["mission"]["results"]), 0)
        pose = state["robot"]
        time.sleep(.04)
        self.assertEqual(self.engine.snapshot()["robot"], pose)
        self.engine.control("resume")
        state = wait_for(self.engine, {"succeeded"})
        self.assertEqual(state["robot"]["location"], "home")
        self.assertEqual(len(state["mission"]["results"]), 2)

    def test_stop_while_paused_does_not_run_remaining_steps(self):
        self.engine.submit("等待两秒然后去会议室")
        self.engine.control("pause")
        wait_for(self.engine, {"paused"})
        self.engine.control("stop")
        state = wait_for(self.engine, {"cancelled"})
        self.assertEqual(state["robot"]["location"], "home")
        self.assertEqual(len(state["mission"]["results"]), 0)

    def test_timeout_is_failure_without_false_completion(self):
        self.config["mock"]["travel_seconds"] = .2
        self.engine.submit_plan(Plan("timeout", [Step("navigate", "meeting_room", timeout=.03, max_retries=0)], "timeout"))
        state = wait_for(self.engine, {"failed"})
        self.assertIn("超时", state["mission"]["error"])
        self.assertNotEqual(state["robot"]["location"], "meeting_room")
        self.assertEqual(state["mission"]["results"][0]["status"], "failed")
        self.assertEqual(state["mission"]["report"]["completed_steps"], 0)

    def test_retry_is_bounded_and_failures_not_swallowed(self):
        class FailingAdapter(MockAdapter):
            attempts = 0

            def execute(self, *args):
                self.attempts += 1
                raise ExecutionError("navigation rejected")

        adapter = FailingAdapter(self.config)
        engine = MissionEngine(self.config, adapter)
        try:
            engine.submit_plan(Plan("retry", [Step("navigate", "home", max_retries=2)], "retry"))
            state = wait_for(engine, {"failed"})
            self.assertEqual(adapter.attempts, 3)
            self.assertEqual(state["mission"]["report"]["completed_steps"], 0)
        finally:
            engine.close()

    def test_unknown_cancellation_result_is_failure_not_cancelled(self):
        started = threading.Event()

        class UncertainAdapter(MockAdapter):
            def execute(self, step, cancel, feedback):
                started.set()
                cancel.wait(2)
                raise ExecutionError("Nav2 terminal result missing")

        engine = MissionEngine(self.config, UncertainAdapter(self.config))
        try:
            engine.submit("去会议室")
            self.assertTrue(started.wait(1))
            engine.control("stop")
            state = wait_for(engine, {"failed", "cancelled"})
            self.assertEqual(state["state"], "failed")
            self.assertIn("未获执行接口确认", state["mission"]["error"])
        finally:
            engine.close()

    def test_completed_mission_written_to_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missions.jsonl"
            engine = MissionEngine(self.config, MockAdapter(self.config), path)
            try:
                engine.submit("去会议室")
                state = wait_for(engine, {"succeeded"})
                saved = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(saved["id"], state["mission"]["id"])
                self.assertEqual(saved["report"]["status"], "succeeded")
            finally:
                engine.close()

    def test_plan_cannot_inject_arbitrary_action_or_coordinate(self):
        for step in (Step("shell", "rm"), Step("navigate", "unknown"), Step("wait", seconds=float("nan"))):
            with self.assertRaises(CommandError):
                self.engine.submit_plan(Plan("invalid", [step], "invalid"))

    def test_preview_never_moves_robot(self):
        preview = self.engine.preview("去会议室然后返回起点")
        self.assertEqual(preview["plan"]["steps"][0]["target"], "meeting_room")
        self.assertEqual(self.engine.snapshot()["state"], "idle")

    def test_maximum_wait_plan_can_be_previewed_and_cancelled(self):
        self.assertEqual(self.engine.preview("等待60分钟")["plan"]["steps"][0]["seconds"], 3600)
        self.engine.submit("等待60分钟")
        self.engine.control("stop")
        wait_for(self.engine, {"cancelled"})

    def test_stop_exception_does_not_strand_failed_mission(self):
        class BrokenStop(MockAdapter):
            def execute(self, *args):
                raise ExecutionError("adapter failure")

            def stop(self):
                raise RuntimeError("stop transport unavailable")

            def close(self):
                pass

        engine = MissionEngine(self.config, BrokenStop(self.config))
        try:
            engine.submit("去会议室")
            state = wait_for(engine, {"failed"})
            self.assertIn("stop transport unavailable", state["mission"]["error"])
        finally:
            engine.close()


if __name__ == "__main__":
    unittest.main()
