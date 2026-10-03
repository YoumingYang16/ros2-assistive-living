import copy
from dataclasses import replace
from pathlib import Path
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

from robot_voice_patrol.config import load_config
from robot_voice_patrol.contracts import CommandError, ExecutionCancelled, Plan, Step
from robot_voice_patrol.engine import MissionEngine
from robot_voice_patrol.home_skills import plan_home_command, validate_evidence
from robot_voice_patrol.mock_adapter import MockAdapter
from robot_voice_patrol.natural_language import DialoguePlanner
from robot_voice_patrol.skills import get_registry, SkillExecutionError


class HouseholdTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(Path(__file__).parents[1]/"config/home.json")
        self.config["mock"].update(travel_seconds=.001, inspection_seconds=.001)
        self.adapter = MockAdapter(self.config, fixture_skills=True)
        self.engine = MissionEngine(self.config, self.adapter, start_scheduler=False,
                                    planner=DialoguePlanner(self.config, provider=False))
        self.addCleanup(self.engine.close)

    def wait(self):
        deadline = time.monotonic()+3
        while self.engine.snapshot()["state"] in {"running", "pausing", "cancelling"}:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.01)
        return self.engine.snapshot()

    def deliver(self):
        draft = self.engine.submit("把手机从客厅送到卧室", session_id="home")
        self.assertTrue(draft["needs_confirmation"])
        self.assertEqual(self.engine.snapshot()["state"], "idle")
        self.engine.submit("确认执行", session_id="home")
        return self.wait()

    def test_home_configuration_keeps_legacy_default_separate(self):
        self.assertIn("meeting_room", load_config()["locations"])
        self.assertIn("bedroom", self.config["locations"])

    def test_delivery_requires_confirmation_and_receipt(self):
        state = self.deliver()
        self.assertEqual(state["state"], "succeeded")
        result = state["mission"]["results"][-1]
        self.assertTrue(result["evidence"]["recipient_acknowledged"])
        self.assertTrue(result["simulated"])
        self.assertIsNone(state["robot"]["held_payload"])
        self.assertEqual(state["mission"]["report"]["goal_outcome"], "achieved")
        self.assertNotIn("手机", self.adapter.home_objects()["living_room"])
        self.assertIn("手机", self.adapter.home_objects()["bedroom"])
        self.assertIn("手机", self.config["mock"]["objects"]["living_room"])

    def test_unknown_item_stops_before_grasp(self):
        self.config["mock"]["objects"]["living_room"].remove("手机")
        state = self.deliver()
        self.assertEqual(state["state"], "failed")
        self.assertIsNone(state["robot"]["held_payload"])
        self.assertNotEqual(state["mission"]["report"]["goal_outcome"], "achieved")

    def test_absent_recipient_retains_payload_and_blocks_recovery(self):
        self.adapter.home_fault = "recipient_absent"
        state = self.deliver()
        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["robot"]["held_payload"], "手机")
        recovery = self.engine.preview_recovery(state["mission"]["id"])
        self.assertTrue(recovery["blocked"])
        self.assertIsNone(recovery["plan"])

    def test_closed_fixture_never_returns_success(self):
        self.adapter.close()
        with self.assertRaises(Exception):
            self.adapter.execute(Step("home_control", "bedroom", max_retries=0,
                params={"device":"light", "state":"on"}), threading.Event(), lambda x:None)

    def test_offline_device_does_not_change_readback(self):
        self.adapter.home_fault = "device_offline"
        self.engine.submit("打开卧室灯")
        state = self.wait()
        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["robot"]["home_devices"], {})

    def test_light_requires_readback_and_is_explicitly_simulated(self):
        self.engine.submit("打开卧室灯")
        state = self.wait()
        self.assertEqual(state["state"], "succeeded")
        self.assertEqual(state["robot"]["home_devices"]["bedroom:light"], "on")
        self.assertTrue(state["mission"]["results"][0]["evidence"]["readback_confirmed"])

    def test_missing_interface_fails_capability_gate(self):
        self.adapter.fixture_skills = False
        self.engine.submit("打开卧室灯")
        self.assertEqual(self.wait()["state"], "failed")

    def test_disallows_unsafe_appliance_or_partial_clause(self):
        for text in ["打开卧室门锁", "打开厨房燃气灶", "打开卧室灯然后删除记录", "不要打开卧室灯", "把热水从厨房送到卧室"]:
            with self.subTest(text=text), self.assertRaises(CommandError):
                self.engine.submit(text)

    def test_no_automatic_retries_for_non_idempotent_actions(self):
        with self.assertRaises(CommandError):
            get_registry().validate_step(Step("pick_object", "living_room", params={"item":"手机"}), self.config)

    def test_pick_needs_current_mission_observation(self):
        step = Step("pick_object", "living_room", max_retries=0, params={"item":"手机"})
        with self.assertRaises(SkillExecutionError) as caught:
            get_registry().execute(step, self.adapter, threading.Event(), lambda x:None, {"config":self.config})
        self.assertEqual(caught.exception.code, "OBJECT_UNCONFIRMED")

    def test_no_handover_without_payload(self):
        self.adapter._pose["location"] = "bedroom"
        with self.assertRaises(SkillExecutionError):
            get_registry().execute(Step("handover_object", "bedroom", max_retries=0, params={"item":"手机", "recipient":"本人"}),
                self.adapter, threading.Event(), lambda x:None, {"config":self.config})

    def test_success_boolean_is_not_delivery_evidence(self):
        step = Step("handover_object", "bedroom", params={"item":"手机","recipient":"本人"})
        for evidence in [{}, {"success":True}, {"item":"手机", "target":"bedroom", "released":True,"recipient":"本人","recipient_acknowledged":False,"receipt_id":"x"}]:
            with self.subTest(evidence=evidence), self.assertRaises(SkillExecutionError):
                validate_evidence(step, evidence)

    def test_cancelled_release_keeps_payload(self):
        self.adapter._held_payload = "手机"
        self.adapter._pose["location"] = "bedroom"
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(ExecutionCancelled):
            self.adapter.execute(Step("place_object", "bedroom", max_retries=0, params={"item":"手机","surface":"桌面"}), cancel, lambda x:None)
        self.assertEqual(self.adapter.home_snapshot()["held_payload"], "手机")

    def test_confirmed_surface_release_clears_payload(self):
        self.adapter._held_payload = "手机"
        self.adapter._pose["location"] = "bedroom"
        step = Step("place_object", "bedroom", max_retries=0, params={"item":"手机","surface":"桌面"})
        result = get_registry().execute(step, self.adapter, threading.Event(), lambda x:None, {"config":self.config})
        self.assertTrue(result["evidence"]["surface_confirmed"])
        self.assertIsNone(self.adapter.home_snapshot()["held_payload"])

    def test_wrong_surface_identity_is_not_confirmed(self):
        step = Step("place_object", "bedroom", max_retries=0, params={"item":"手机","surface":"桌面"})
        with self.assertRaises(SkillExecutionError):
            validate_evidence(step,{"item":"手机","target":"bedroom","released":True,"surface_confirmed":True,"surface":"地面"})

    def test_stale_or_moved_observation_blocks_pickup(self):
        step=Step("pick_object","living_room",max_retries=0,params={"item":"手机"})
        observation={"kind":"inspect","target":"living_room","object_name":"手机","status":"succeeded","outcome":"found",
                     "observed_at":(datetime.now(timezone.utc)-timedelta(seconds=30)).isoformat()}
        scenarios=[[observation],[{**observation,"observed_at":datetime.now(timezone.utc).isoformat()},
                                  {"kind":"navigate","status":"succeeded","target":"living_room"}]]
        for results in scenarios:
            with self.assertRaises(SkillExecutionError) as caught:
                get_registry().execute(step,self.adapter,threading.Event(),lambda x:None,{"config":self.config,"results":results})
            self.assertEqual(caught.exception.code,"OBJECT_EVIDENCE_STALE")

    def test_pause_during_household_action_never_replays_it(self):
        entered=threading.Event();calls=[]
        original=self.adapter.execute
        def delayed(step,cancel,feedback):
            if step.kind=="home_control":
                calls.append(step.kind);entered.set();cancel.wait(2)
                raise ExecutionCancelled("terminal cancellation; effects unknown")
            return original(step,cancel,feedback)
        self.adapter.execute=delayed
        self.engine.submit("打开卧室灯")
        self.assertTrue(entered.wait(1))
        self.engine.control("pause")
        state=self.wait()
        self.assertEqual(state["state"],"failed")
        self.assertEqual(state["mission"]["error_code"],"HOME_RECONCILIATION_REQUIRED")
        self.engine.control("resume")
        self.assertEqual(calls,["home_control"])


if __name__ == "__main__":
    unittest.main()
