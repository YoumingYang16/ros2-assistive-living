"""Durable orchestration interlocks using explicit software fixtures, no DDS."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from robot_voice_patrol.config import load_config
from robot_voice_patrol.contracts import CommandError, ExecutionCancelled, ExecutionError, Plan, Step
from robot_voice_patrol.data_management import restore_offline
from robot_voice_patrol.engine import MissionEngine, NON_REPLAYABLE_SKILLS
from robot_voice_patrol.mock_adapter import MockAdapter
from robot_voice_patrol.ros_adapter import RosExecutionError


class SoftwareRosFixture(MockAdapter):
    # Exercise ROS-mode persistence using an explicitly simulated provider.
    mode = "ros2"

    def __init__(self, config, behavior="succeed"):
        super().__init__(config, fixture_skills=True)
        self.behavior = behavior
        self.calls = []
        self.entered = threading.Event()
        self.uncertain = False
        self.dispatch_observer = None

    def execute(self, step, cancel, feedback):
        self.calls.append(step.kind)
        if self.dispatch_observer:
            self.dispatch_observer(step)
        if step.kind == "navigate":
            count = self.calls.count("navigate")
            if self.behavior == "nav_pause" and count == 1:
                self.entered.set()
                cancel.wait(2)
                raise ExecutionCancelled("Navigation terminal cancellation acknowledged")
            if (self.behavior in {"nav_retry", "nav_result_retry"} and count == 1) or self.behavior == "nav_failed":
                code = "NAVIGATION_FAILED" if self.behavior == "nav_result_retry" else "ACTION_ABORTED"
                raise RosExecutionError(code, "Nav2 reported a confirmed terminal failure", retryable=True)
            if self.behavior == "nav_unknown":
                self.uncertain = True
                raise RosExecutionError("UNKNOWN_ACTION_STATE", "Nav2 goal acknowledgement lost", retryable=True)
        if step.kind in NON_REPLAYABLE_SKILLS:
            self.entered.set()
            if self.behavior == "unknown":
                self.uncertain = True
                raise RosExecutionError("UNKNOWN_HARDWARE_STATE", "Gateway did not confirm physical terminal state")
            if self.behavior == "failed":
                raise RosExecutionError("ACTION_ABORTED", "Confirmed terminal failure", retryable=True)
            if self.behavior == "disconnected":
                raise ExecutionError("Device connection lost")
            if self.behavior == "pause":
                cancel.wait(2)
                raise ExecutionCancelled("Driver acknowledged cancellation, previous effects possible")
        return super().execute(step, cancel, feedback)

    def snapshot(self):
        return {**super().snapshot(), "hardware_uncertain": self.uncertain, "action_pending": self.uncertain}

    def reconcile_mission(self, mission):
        return {"verified": not self.uncertain, "remote_state": "unknown" if self.uncertain else "terminal"}


class HardwareInterlockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = load_config(Path(__file__).parents[1] / "config/home.json")
        self.config["mock"].update(travel_seconds=.001, inspection_seconds=.001)
        self.db = Path(self.temp.name) / "missions.db"
        self.engines = []

    def tearDown(self):
        for engine in self.engines:
            engine.close()
        self.temp.cleanup()

    def make(self, behavior="succeed", path=None):
        adapter = SoftwareRosFixture(self.config, behavior)
        engine = MissionEngine(self.config, adapter, db_path=path or self.db, start_scheduler=False)
        self.engines.append(engine)
        return engine

    def finish(self, engine):
        if engine._worker:
            engine._worker.join(2)
            self.assertFalse(engine._worker.is_alive())
        return engine.snapshot()

    def plan(self, kind="turn"):
        params = {"turn": {"angle_degrees": 90}, "capture": {"camera": "front", "format": "png"},
                  "dock": {}, "follow": {"subject": "本人", "duration_seconds": .1},
                  "home_control": {"device": "light", "state": "on"}, "pick_object": {"item": "手机"},
                  "place_object": {"item": "手机", "surface": "桌面"},
                  "handover_object": {"item": "手机", "recipient": "本人"}}[kind]
        target = "living_room" if kind in {"pick_object", "place_object", "handover_object"} else (
                 "bedroom" if kind == "home_control" else "home" if kind == "dock" else None)
        steps = []
        if kind == "pick_object":
            steps = [Step("navigate", target), Step("inspect", target, object_name="手机")]
        steps.append(Step(kind, target, timeout=1, max_retries=0 if kind in {"home_control", "pick_object", "place_object", "handover_object"} else 3, params=params))
        return Plan("fixture " + kind, steps, "fixture " + kind)

    def navigation(self):
        return Plan("去卧室", [Step("navigate", "bedroom")], "去卧室")

    def verifier(self, record):
        return {"interlock_id": record["id"], "all_resources_terminal": True, "terminal_confirmed": True,
                "observed_at": datetime.now(timezone.utc).isoformat(), "verified_by": "test fixture operator",
                "evidence": {"fixture": True, "all_drives_stopped": True, "payload_checked": True}}

    def marker(self, identifier="uncertain-one", status="unknown"):
        return {"id": identifier, "status": status, "mission_id": "historical", "related_mission_ids": ["historical"],
                "step_id": "step-one", "kind": "turn", "created_at": datetime.now(timezone.utc).isoformat()}

    def test_unknown_hardware_failure_blocks_new_navigation_after_restart(self):
        engine = self.make("unknown")
        engine.submit_plan(self.plan())
        state = self.finish(engine)
        self.assertEqual(state["mission"]["error_code"], "UNKNOWN_HARDWARE_STATE")
        self.assertEqual(state["hardware_interlock"]["status"], "unknown")
        identifier = state["mission"]["id"]
        engine.close()
        restarted = self.make()
        self.assertFalse(restarted.can_dispatch())
        self.assertFalse(restarted.health()["ready"])
        with self.assertRaises(CommandError):
            restarted.submit_plan(self.navigation())
        self.assertEqual(restarted.adapter.calls, [])
        self.assertTrue(restarted.preview_recovery(identifier)["blocked"])

    def test_navigation_process_crash_blocks_new_motion_after_restart(self):
        program = """
import os, sys
from robot_voice_patrol.config import load_config
from robot_voice_patrol.contracts import Plan, Step
from robot_voice_patrol.engine import MissionEngine
from robot_voice_patrol.mock_adapter import MockAdapter
class CrashAdapter(MockAdapter):
    mode = 'ros2'
    def execute(self, step, cancel, feedback):
        if step.kind == 'navigate':
            os._exit(29)
        return super().execute(step, cancel, feedback)
config = load_config(sys.argv[2])
engine = MissionEngine(config, CrashAdapter(config), db_path=sys.argv[1], start_scheduler=False)
engine.submit_plan(Plan('crash during navigation', [Step('navigate', 'bedroom')], 'crash during navigation'))
engine._worker.join(3)
sys.exit(1)
"""
        root = Path(__file__).parents[1]
        result = subprocess.run([sys.executable, "-c", program, str(self.db), str(root / "config/home.json")],
            cwd=root, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "VOICE_PATROL_MODEL_PROVIDER": "none"},
            capture_output=True, timeout=8)
        self.assertEqual(result.returncode, 29, result.stderr)
        restarted = self.make()
        self.assertFalse(restarted.can_dispatch())
        self.assertEqual(restarted.snapshot()["hardware_interlock"]["kind"], "navigate")
        with self.assertRaises(CommandError):
            restarted.submit_plan(self.plan())
        self.assertEqual(restarted.adapter.calls, [])

    def test_navigation_confirmed_pause_settles_before_resuming_absolute_goal(self):
        engine = self.make("nav_pause")
        observed = []
        engine.adapter.dispatch_observer = lambda step: observed.append(engine.store.get_setting("hardware_interlock"))
        engine.submit_plan(self.navigation())
        self.assertTrue(engine.adapter.entered.wait(1))
        engine.control("pause")
        deadline = time.monotonic()+1
        while engine.snapshot()["state"] != "paused" and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertEqual(engine.snapshot()["state"], "paused")
        self.assertEqual(engine.snapshot()["hardware_interlock"]["status"], "resolved")
        engine.control("resume")
        self.assertEqual(self.finish(engine)["state"], "succeeded")
        self.assertEqual(engine.adapter.calls, ["navigate", "navigate"])
        self.assertEqual([item["status"] for item in observed], ["pending", "pending"])
        self.assertNotEqual(observed[0]["id"], observed[1]["id"])
        self.assertTrue(engine.can_dispatch())

    def test_navigation_confirmed_failure_settles_before_retry(self):
        for behavior in ("nav_retry", "nav_result_retry"):
            with self.subTest(behavior=behavior):
                engine = self.make(behavior, Path(self.temp.name)/(behavior+".db"))
                observed = []
                engine.adapter.dispatch_observer = lambda step: observed.append(engine.store.get_setting("hardware_interlock"))
                engine.submit_plan(self.navigation())
                self.assertEqual(self.finish(engine)["state"], "succeeded")
                self.assertEqual(engine.adapter.calls, ["navigate", "navigate"])
                self.assertNotEqual(observed[0]["id"], observed[1]["id"])
                self.assertTrue(engine.can_dispatch())

    def test_navigation_exhausted_confirmed_failure_does_not_lock_future_tasks(self):
        engine = self.make("nav_failed")
        engine.submit_plan(Plan("nav fail", [Step("navigate", "bedroom", max_retries=0)], "nav fail"))
        state = self.finish(engine)
        self.assertEqual(state["mission"]["error_code"], "ACTION_ABORTED")
        self.assertEqual(state["hardware_interlock"]["status"], "resolved")
        self.assertTrue(engine.can_dispatch())

    def test_navigation_uncertain_failure_cannot_retry_or_restart_unlocked(self):
        engine = self.make("nav_unknown")
        engine.submit_plan(self.navigation())
        self.finish(engine)
        self.assertEqual(engine.adapter.calls, ["navigate"])
        self.assertFalse(engine.can_dispatch())
        engine.close()
        self.assertFalse(self.make().can_dispatch())

    def test_old_navigation_crash_without_intent_is_quarantined(self):
        engine = self.make()
        from robot_voice_patrol.plan_validation import validate_plan
        plan = validate_plan(self.navigation(), self.config).to_dict()
        step_id = plan["steps"][0]["step_id"]
        engine.store.save_mission({**plan, "id": "oldnav", "state": "running", "mode": "ros2",
            "started_at": datetime.now(timezone.utc).isoformat(), "step_index": 0, "results": [],
            "step_states": [{"step_id": step_id, "status": "running"}]})
        engine.close()
        restarted = self.make()
        self.assertFalse(restarted.can_dispatch())
        self.assertEqual(restarted.snapshot()["hardware_interlock"]["kind"], "navigate")

    def test_confirmed_cancel_marker_prevents_false_quarantine_of_paused_navigation(self):
        engine = self.make()
        from robot_voice_patrol.plan_validation import validate_plan
        plan = validate_plan(self.navigation(), self.config).to_dict()
        step_id = plan["steps"][0]["step_id"]
        created = datetime.now(timezone.utc).isoformat()
        engine.store.save_mission({**plan, "id": "pausednav", "state": "paused", "mode": "ros2",
            "started_at": created, "step_index": 0, "results": [],
            "step_states": [{"step_id": step_id, "status": "running"}]})
        engine.store.set_setting("hardware_interlock", {"id": "resolved-nav", "status": "resolved", "mission_id": "pausednav",
            "kind": "navigate", "step_id": step_id, "created_at": created, "error_code": "NAVIGATION_CANCEL_CONFIRMED"})
        engine.close()
        restarted = self.make()
        self.assertTrue(restarted.can_dispatch())
        self.assertEqual(restarted.snapshot()["hardware_interlock"]["status"], "resolved")

    def test_dispatch_intent_is_durable_before_driver_executes(self):
        engine = self.make("pause")
        engine.submit_plan(self.plan())
        self.assertTrue(engine.adapter.entered.wait(1))
        marker = engine.store.get_setting("hardware_interlock")
        self.assertEqual(marker["status"], "pending")
        self.assertEqual(marker["kind"], "turn")
        engine.control("stop")
        self.finish(engine)

    def test_pending_crash_marker_becomes_unknown_and_cannot_be_dismissed(self):
        engine = self.make()
        marker = self.marker(status="pending")
        engine.store.set_setting("hardware_interlock", marker)
        plan = self.plan().to_dict()
        mission = {**plan, "id": "historical", "state": "interrupted", "mode": "ros2", "started_at": marker["created_at"],
                   "recovery_required": True, "step_index": 0, "results": [],
                   "step_states": [{"step_id": "step-one", "status": "interrupted"}]}
        engine.store.save_mission(mission)
        engine.close()
        restarted = self.make()
        self.assertEqual(restarted.snapshot()["hardware_interlock"]["status"], "unknown")
        restarted.dismiss_recovery("historical")
        self.assertEqual(restarted.store.recoveries(), [])
        for transition in ("deactivate", "cleanup", "configure", "activate"):
            restarted.lifecycle_transition(transition)
        self.assertFalse(restarted.can_dispatch())
        with self.assertRaises(CommandError):
            restarted.submit_plan(self.navigation())

    def test_old_interrupted_ros_external_step_without_marker_is_quarantined(self):
        engine = self.make()
        from robot_voice_patrol.plan_validation import validate_plan
        plan = validate_plan(self.plan(), self.config).to_dict()
        identifier = plan["steps"][0]["step_id"]
        mission = {**plan, "id": "oldmission", "state": "running", "mode": "ros2", "started_at": datetime.now(timezone.utc).isoformat(),
                   "step_index": 0, "results": [], "step_states": [{"step_id": identifier, "status": "running"}]}
        engine.store.save_mission(mission)
        engine.close()
        restarted = self.make()
        self.assertFalse(restarted.can_dispatch())
        self.assertEqual(restarted.snapshot()["hardware_interlock"]["mission_id"], "oldmission")

    def test_all_eight_external_skills_pause_without_automatic_replay(self):
        for kind in sorted(NON_REPLAYABLE_SKILLS):
            with self.subTest(kind=kind):
                engine = self.make("pause", Path(self.temp.name) / (kind+".db"))
                engine.submit_plan(self.plan(kind))
                self.assertTrue(engine.adapter.entered.wait(1))
                engine.control("pause")
                engine.control("resume")  # Race resume against cancellation acknowledgement.
                state = self.finish(engine)
                expected = "HOME_RECONCILIATION_REQUIRED" if kind in {"home_control", "pick_object", "place_object", "handover_object"} else "EXTERNAL_RECONCILIATION_REQUIRED"
                self.assertEqual(state["mission"]["error_code"], expected)
                self.assertEqual(engine.adapter.calls.count(kind), 1)
                self.assertEqual(state["hardware_interlock"]["status"], "resolved")
                self.assertTrue(engine.preview_recovery(state["mission"]["id"])["blocked"])
                engine.close()

    def test_all_eight_external_skills_never_retry_confirmed_failure(self):
        for kind in sorted(NON_REPLAYABLE_SKILLS):
            with self.subTest(kind=kind):
                engine = self.make("failed", Path(self.temp.name) / (kind+".db"))
                engine.submit_plan(self.plan(kind))
                state = self.finish(engine)
                self.assertEqual(state["mission"]["error_code"], "ACTION_ABORTED")
                self.assertEqual(engine.adapter.calls.count(kind), 1)
                self.assertTrue(engine.can_dispatch())
                engine.close()

    def test_uncorrelated_driver_error_stays_locked_without_explicit_pending_flag(self):
        engine = self.make("disconnected")
        engine.submit_plan(self.plan())
        self.finish(engine)
        self.assertFalse(engine.adapter.snapshot()["action_pending"])
        self.assertFalse(engine.can_dispatch())
        engine.close()
        self.assertFalse(self.make().can_dispatch())

    def test_success_checkpoint_clears_intent_and_restart_can_navigate(self):
        engine = self.make()
        engine.submit_plan(self.plan())
        state = self.finish(engine)
        self.assertEqual(state["state"], "succeeded")
        self.assertEqual(state["hardware_interlock"]["status"], "resolved")
        engine.close()
        restarted = self.make()
        restarted.submit_plan(self.navigation())
        self.assertEqual(self.finish(restarted)["state"], "succeeded")

    def test_checkpoint_write_failure_keeps_interlock(self):
        engine = self.make()
        save = engine.store.save_mission
        fault = []
        def fail_once(mission, **kwargs):
            if mission.get("results") and not fault:
                fault.append(True)
                raise OSError("test checkpoint write failure")
            return save(mission, **kwargs)
        with patch.object(engine.store, "save_mission", side_effect=fail_once):
            engine.submit_plan(self.plan())
            self.finish(engine)
        self.assertFalse(engine.can_dispatch())
        self.assertEqual(engine.adapter.calls.count("turn"), 1)
        engine.close()
        self.assertFalse(self.make().can_dispatch())

    def test_interlock_release_write_failure_does_not_unlock_in_memory(self):
        engine = self.make()
        save = engine.store.set_setting
        fault = []
        def fail_once(key, value):
            if key == "hardware_interlock" and value.get("status") == "resolved" and not fault:
                fault.append(True)
                raise OSError("test interlock release write failure")
            return save(key, value)
        with patch.object(engine.store, "set_setting", side_effect=fail_once):
            engine.submit_plan(self.plan())
            self.finish(engine)
        self.assertFalse(engine.can_dispatch())
        self.assertEqual(engine.snapshot()["hardware_interlock"]["status"], "unknown")
        engine.close()
        self.assertFalse(self.make().can_dispatch())

    def test_explicit_trusted_verifier_unlocks_after_reconnect_without_replaying(self):
        engine = self.make("unknown")
        engine.submit_plan(self.plan())
        state = self.finish(engine)
        with self.assertRaises(CommandError):
            engine.reconcile_hardware(self.verifier, note="Operator verified every relevant actuator")
        engine.close()
        restarted = self.make()
        result = restarted.reconcile_hardware(self.verifier, note="Operator verified every relevant actuator")
        self.assertEqual(result["hardware_interlock"]["status"], "resolved")
        self.assertEqual(restarted.adapter.calls, [])
        self.assertTrue(restarted.preview_recovery(state["mission"]["id"])["blocked"])
        restarted.submit_plan(self.navigation())
        self.assertEqual(self.finish(restarted)["state"], "succeeded")

    def test_untrusted_flag_stale_or_mismatched_verification_cannot_clear_lock(self):
        engine = self.make()
        engine.store.set_setting("hardware_interlock", self.marker())
        engine.close()
        restarted = self.make()
        invalid = [lambda r: {"confirmed": True}, lambda r: {**self.verifier(r), "interlock_id": "wrong"},
            lambda r: {**self.verifier(r), "observed_at": (datetime.now(timezone.utc)-timedelta(seconds=40)).isoformat()},
            lambda r: {**self.verifier(r), "all_resources_terminal": False}]
        for verifier in invalid:
            with self.assertRaises(CommandError):
                restarted.reconcile_hardware(verifier, note="Operator verification attempt")
            self.assertFalse(restarted.can_dispatch())
        with self.assertRaises(CommandError):
            restarted.reconcile_hardware(True, note="Not an actual trusted verifier")

    def test_restore_old_backup_preserves_current_unknown_interlock(self):
        engine = self.make()
        backup = engine.data.backup()["backup"]
        engine.store.set_setting("hardware_interlock", self.marker())
        engine.close()
        restore_offline(self.db, backup["id"])
        restarted = self.make()
        self.assertFalse(restarted.can_dispatch())
        self.assertEqual(restarted.snapshot()["hardware_interlock"]["id"], "uncertain-one")

    def test_restore_source_pending_intent_is_not_lost_to_current_clear_state(self):
        engine = self.make()
        engine.store.set_setting("hardware_interlock", self.marker(status="pending"))
        backup = engine.data.backup()["backup"]
        engine.store.set_setting("hardware_interlock", {**self.marker(), "status": "resolved"})
        engine.close()
        restore_offline(self.db, backup["id"])
        restarted = self.make()
        self.assertEqual(restarted.snapshot()["hardware_interlock"]["status"], "unknown")
        self.assertFalse(restarted.can_dispatch())

    def test_restore_merges_two_different_unresolved_physical_operations(self):
        engine = self.make()
        engine.store.set_setting("hardware_interlock", self.marker("older", "pending"))
        backup = engine.data.backup()["backup"]
        engine.store.set_setting("hardware_interlock", self.marker("newer"))
        engine.close()
        restore_offline(self.db, backup["id"])
        restarted = self.make()
        marker = restarted.snapshot()["hardware_interlock"]
        self.assertEqual({r["id"] for r in marker["sources"]}, {"older", "newer"})
        self.assertFalse(restarted.can_dispatch())

    def test_failed_recurring_external_job_does_not_requeue(self):
        engine = self.make("failed")
        job = engine.scheduler.add(self.plan(), repeat={"interval_seconds": 60})["job"]
        engine.scheduler.tick()
        self.finish(engine)
        engine.scheduler.tick()
        stored = engine.store.job(job["id"])
        self.assertEqual(stored["status"], "failed")
        self.assertIsNone(stored["repeat"])
        self.assertEqual(stored["occurrence"], 0)

    def test_restart_before_scheduler_final_tick_cannot_repeat_failed_external_job(self):
        engine = self.make("failed")
        job = engine.scheduler.add(self.plan(), repeat={"interval_seconds": 60})["job"]
        engine.scheduler.tick()
        self.finish(engine)
        engine.close()
        restarted = self.make()
        stored = restarted.store.job(job["id"])
        self.assertEqual(stored["status"], "failed")
        self.assertIsNone(stored["repeat"])
        self.assertEqual(stored["occurrence"], 0)
        restarted.scheduler.control("resume")
        restarted.scheduler.tick()
        self.assertEqual(restarted.adapter.calls, [])


if __name__ == "__main__":
    unittest.main()
