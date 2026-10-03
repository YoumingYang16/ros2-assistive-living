import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from robot_voice_patrol.config import load_config
from robot_voice_patrol.contracts import CommandError, Plan, Step, PlanningResult
from robot_voice_patrol.engine import MissionEngine
from robot_voice_patrol.mock_adapter import MockAdapter
from robot_voice_patrol.store import MissionStore


def finish(engine):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        value = engine.snapshot()
        if value["state"] in {"succeeded", "failed", "cancelled"}:
            return value
        time.sleep(.005)
    raise AssertionError(engine.snapshot())


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "tasks.sqlite3"
        self.config = load_config()
        self.config["mock"].update(travel_seconds=.01, inspection_seconds=.01)
        self.engines = []

    def make(self, **kwargs):
        e = MissionEngine(self.config, MockAdapter(self.config), db_path=self.path, **kwargs)
        self.engines.append(e)
        return e

    def tearDown(self):
        for engine in reversed(self.engines):
            engine.close()
        self.temp.cleanup()

    def test_duplicate_request_after_completion_and_restart_does_not_reexecute(self):
        first = self.make()
        response = first.submit("去会议室", request_id="same-request")
        mission_id = response["mission_id"]
        finish(first)
        self.assertTrue(first.submit("去会议室", request_id="same-request")["duplicate"])
        self.assertEqual(first.metrics()["missions_total"], 1)
        first.close()
        second = self.make()
        repeated = second.submit("去会议室", request_id="same-request")
        self.assertTrue(repeated["duplicate"])
        self.assertEqual(repeated["mission_id"], mission_id)
        self.assertEqual(second.snapshot()["state"], "idle")
        self.assertEqual(second.metrics()["missions_total"], 1)

    def test_same_request_id_cannot_change_payload(self):
        engine = self.make()
        engine.submit("查询状态", request_id="r")
        with self.assertRaises(CommandError):
            engine.submit("去会议室", request_id="r")

    def test_request_link_survives_missing_final_receipt(self):
        engine = self.make()
        with patch.object(engine.store, "finish_request", side_effect=RuntimeError("receipt interrupted")):
            with self.assertRaisesRegex(RuntimeError, "receipt interrupted"):
                engine.submit("等待0.01秒", request_id="missing-receipt")
        finish(engine)
        mission_id = engine.snapshot()["mission"]["id"]
        engine.close()
        replacement = self.make()
        receipt = replacement.submit("等待0.01秒", request_id="missing-receipt")
        self.assertTrue(receipt["duplicate"])
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["mission_id"], mission_id)
        self.assertEqual(replacement.metrics()["missions_total"], 1)

    def test_failed_planner_initialization_releases_database_lease(self):
        with patch("robot_voice_patrol.natural_language.DialoguePlanner", side_effect=ValueError("invalid model setup")):
            with self.assertRaises(ValueError):
                self.make()
        engine = self.make()
        self.assertTrue(engine.health()["live"])

    def test_process_lease_prevents_concurrent_writers(self):
        engine = self.make()
        with self.assertRaisesRegex(RuntimeError, "占用"):
            MissionStore(self.path)
        engine.close()
        replacement = MissionStore(self.path)
        replacement.close()

    def test_actual_process_crash_recovers_checkpoint_without_dispatch(self):
        program = """
import os,sys,time
from robot_voice_patrol.config import load_config
from robot_voice_patrol.engine import MissionEngine
from robot_voice_patrol.mock_adapter import MockAdapter
c=load_config()
e=MissionEngine(c,MockAdapter(c),db_path=sys.argv[1])
e.submit('等待60秒然后去会议室',request_id='crash-request')
time.sleep(.15)
os._exit(17)
"""
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, "-c", program, str(self.path)], timeout=10, env=environment,
                                cwd=Path(__file__).resolve().parents[1], capture_output=True)
        self.assertEqual(result.returncode, 17, result.stderr)
        engine = self.make()
        state = engine.snapshot()
        self.assertEqual(state["state"], "idle")
        self.assertEqual(state["robot"]["location"], "home")
        self.assertEqual(len(state["recoveries"]), 1)
        interrupted = state["recoveries"][0]
        self.assertEqual(interrupted["state"], "interrupted")
        self.assertEqual(interrupted["step_states"][0]["status"], "interrupted")
        engine.dismiss_recovery(interrupted["id"])
        self.assertEqual(engine.snapshot()["recoveries"], [])
        self.assertEqual(engine.snapshot()["state"], "idle")

    def test_clarification_context_survives_restart(self):
        engine = self.make()
        response = engine.submit("去那里", session_id="dialogue")
        self.assertTrue(response["needs_clarification"])
        engine.close()
        replacement = self.make()
        replacement.submit("会议室", session_id="dialogue")
        state = finish(replacement)
        self.assertEqual(state["robot"]["location"], "meeting_room")

    def test_configuration_persists_and_resets_mock_start_position(self):
        engine = self.make()
        config = copy.deepcopy(self.config)
        config["locations"]["home"]["x"] = 8
        engine.update_config(config)
        engine.close()
        replacement = self.make()
        self.assertEqual(replacement.config["locations"]["home"]["x"], 8)
        self.assertEqual(replacement.snapshot()["robot"]["x"], 8)

    def test_stop_during_model_planning_prevents_late_dispatch(self):
        entered, release = threading.Event(), threading.Event()
        config = self.config
        class SlowPlanner:
            def interpret(self, text, context=None):
                entered.set()
                release.wait(3)
                return PlanningResult("task", Plan(text, [Step("navigate", "home")], "late"), context=context or {})
        engine = self.make(planner=SlowPlanner())
        errors = []
        def submit():
            try:
                engine.submit("自定义慢模型指令")
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=submit)
        thread.start()
        self.assertTrue(entered.wait(1))
        engine.submit("停止")
        release.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(errors[0], CommandError)
        self.assertEqual(engine.snapshot()["state"], "idle")
        self.assertEqual(engine.metrics()["missions_total"], 0)

    def test_observation_evidence_is_persistent(self):
        engine = self.make()
        engine.submit("去会议室看看有没有水杯")
        finish(engine)
        engine.close()
        replacement = self.make()
        observation = replacement.snapshot()["memory"][0]
        self.assertEqual(observation["outcome"], "found")
        self.assertIn("observed_at", observation)
        self.assertTrue(observation["simulated"])


if __name__ == "__main__":
    unittest.main()
