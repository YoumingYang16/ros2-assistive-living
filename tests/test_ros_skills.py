from concurrent.futures import Future
import json
import threading
import time
from types import SimpleNamespace as NS
import unittest

from robot_voice_patrol.contracts import ExecutionCancelled, Step
from robot_voice_patrol.ros_adapter import RosExecutionError
from tests.test_ros_adapter import adapter_fixture, FakeClient, FakeHandle, run_step, wait_until


def skill_fixture():
    adapter = adapter_fixture()
    adapter._skill_client = FakeClient()
    adapter._skill_type = NS(Goal=lambda: NS())
    adapter._capabilities_type = NS(Request=lambda: NS())
    adapter._capability_future = None
    adapter._capability_checked = adapter._capability_requested = 0
    adapter._advertised_skills = set()
    adapter._capabilities_simulated = False
    adapter._capabilities_provider = None
    adapter._status_cache = {}
    adapter.capability_calls = []
    def request(_):
        future = Future()
        adapter.capability_calls.append(future)
        return future
    adapter._capabilities_client = NS(service_is_ready=lambda: True, call_async=request)
    return adapter


def announce(adapter, skills=("turn", "capture", "dock", "follow")):
    adapter.capabilities()
    adapter.capability_calls[-1].set_result(NS(protocol_version="3", skills=list(skills), simulated=True, provider="fixture"))


def skill_result(goal, **updates):
    sec, nanosec = divmod(time.time_ns(), 1_000_000_000)
    return NS(**{**dict(request_id=goal.request_id, skill=goal.skill, success=True, error_code=0, error_message="",
                       observed_at=NS(sec=sec, nanosec=nanosec), evidence_json='{"fixture":true}',
                       media_uri="mock://frames/1.png", simulated=True), **updates})


class RosSkillTests(unittest.TestCase):
    def test_gateway_abort_without_physical_ack_blocks_even_navigation(self):
        adapter = skill_fixture()
        announce(adapter)
        thread, outcome = run_step(adapter, Step("turn", params={"angle_degrees": 90}, timeout=2))
        wait_until(lambda: adapter._skill_client.sent)
        goal, future = adapter._skill_client.sent[0]
        handle = FakeHandle()
        future.set_result(handle)
        handle.finish(status=6, result=skill_result(goal, success=False, error_code=4,
            evidence_json='{"gateway_terminal_confirmed":false}'))
        thread.join(1)
        self.assertEqual(outcome["error"].code, "UNKNOWN_HARDWARE_STATE")
        self.assertIsNotNone(adapter._active)
        self.assertFalse(adapter._active.done.is_set())
        self.assertTrue(adapter.snapshot()["hardware_uncertain"])
        self.assertFalse(adapter.capabilities()["navigate"]["available"])
        with self.assertRaises(RosExecutionError) as raised:
            adapter.execute(Step("navigate", "home"), threading.Event(), lambda _: None)
        self.assertEqual(raised.exception.code, "UNKNOWN_ACTION_STATE")
        self.assertEqual(adapter._navigation.sent, [])
        self.assertFalse(adapter.reconcile_mission({"mode":"ros2"})["verified"])

    def test_gateway_confirmed_failed_terminal_releases_physical_lease(self):
        adapter = skill_fixture()
        announce(adapter)
        thread, outcome = run_step(adapter, Step("turn", params={"angle_degrees": 90}, timeout=2))
        wait_until(lambda: adapter._skill_client.sent)
        goal, future = adapter._skill_client.sent[0]
        handle = FakeHandle()
        future.set_result(handle)
        handle.finish(status=6, result=skill_result(goal, success=False, error_code=1,
            evidence_json='{"gateway_terminal_confirmed":true,"motor_stopped":true}'))
        thread.join(1)
        self.assertEqual(outcome["error"].code, "ACTION_ABORTED")
        self.assertIsNone(adapter._active)

    def home_fixture(self):
        adapter = skill_fixture()
        adapter._skill_type = NS(Goal=lambda: NS(parameters_json=""))
        adapter.capabilities()
        adapter.capability_calls[-1].set_result(NS(protocol_version="5",skills=["home_control","pick_object","place_object","handover_object"],simulated=True,provider="fixture"))
        return adapter

    def test_home_skills_require_v5_capability_protocol(self):
        old=skill_fixture();announce(old,["home_control"])
        self.assertFalse(old.capabilities()["home_control"]["available"])
        self.assertTrue(self.home_fixture().capabilities()["home_control"]["available"])

    def test_home_location_gate_prevents_dispatch(self):
        adapter=self.home_fixture();adapter._location="home"
        thread,outcome=run_step(adapter,Step("pick_object","bedroom",max_retries=0,params={"item":"手机"},timeout=1))
        thread.join(1)
        self.assertEqual(outcome["error"].code,"LOCATION_UNCONFIRMED")
        self.assertEqual(adapter._skill_client.sent,[])

    def test_home_control_serializes_params_and_requires_readback(self):
        for valid in [True,False]:
            adapter=self.home_fixture()
            thread,outcome=run_step(adapter,Step("home_control","home",max_retries=0,params={"device":"light","state":"on"},timeout=1))
            wait_until(lambda:adapter._skill_client.sent)
            goal,future=adapter._skill_client.sent[0]
            self.assertEqual(goal.skill,5)
            self.assertEqual(json.loads(goal.parameters_json),{"device":"light","state":"on"})
            handle=FakeHandle();future.set_result(handle)
            evidence={"device":"light","target":"home","reported_state":"on","readback_confirmed":valid}
            handle.finish(result=skill_result(goal,evidence_json=json.dumps(evidence)))
            thread.join(1)
            if valid:self.assertEqual(outcome["result"]["status"],"succeeded")
            else:self.assertEqual(outcome["error"].code,"HOME_EVIDENCE_UNCONFIRMED")

    def test_capability_requires_explicit_fresh_advertisement(self):
        adapter = skill_fixture()
        self.assertFalse(adapter.capabilities()["turn"]["available"])
        announce(adapter, ["turn"])
        self.assertTrue(adapter.capabilities()["turn"]["available"])
        self.assertFalse(adapter.capabilities()["capture"]["available"])
        adapter._capability_checked -= 6
        self.assertFalse(adapter.capabilities()["turn"]["available"])

    def test_unsupported_protocol_never_enables_skill(self):
        adapter = skill_fixture()
        adapter.capabilities()
        adapter.capability_calls[-1].set_result(NS(protocol_version="99", skills=["turn"], simulated=False, provider="unknown"))
        self.assertFalse(adapter.capabilities()["turn"]["available"])

    def test_external_goal_uses_typed_parameters_and_correlated_result(self):
        adapter = skill_fixture()
        announce(adapter)
        step = Step("turn", params={"angle_degrees": 90}, timeout=2)
        thread, outcome = run_step(adapter, step)
        wait_until(lambda: adapter._skill_client.sent)
        goal, future = adapter._skill_client.sent[0]
        self.assertEqual((goal.skill, goal.angle_degrees), (4, 90))
        self.assertIs(type(goal.angle_degrees), float)
        handle = FakeHandle()
        future.set_result(handle)
        handle.finish(result=skill_result(goal))
        thread.join(1)
        self.assertEqual(outcome["result"]["source"], "ros_fixture")
        self.assertTrue(outcome["result"]["simulated"])

    def test_corrupt_or_stale_external_results_never_succeed(self):
        for invalid in [{"request_id": "wrong"}, {"evidence_json": "{}"},
                        {"observed_at": NS(sec=1, nanosec=0)}, {"media_uri": ""},
                        {"evidence_json": '{"invalid":NaN}'}]:
            with self.subTest(invalid=invalid):
                adapter = skill_fixture()
                announce(adapter)
                thread, outcome = run_step(adapter, Step("capture", params={"camera": "front", "format": "png"}, timeout=2))
                wait_until(lambda: adapter._skill_client.sent)
                goal, future = adapter._skill_client.sent[0]
                handle = FakeHandle()
                future.set_result(handle)
                handle.finish(result=skill_result(goal, **invalid))
                thread.join(1)
                self.assertEqual(outcome["error"].code, "INVALID_SKILL_RESULT")

    def test_unadvertised_skill_has_no_action_side_effect(self):
        adapter = skill_fixture()
        announce(adapter, ["capture"])
        with self.assertRaises(RosExecutionError) as caught:
            adapter.execute(Step("turn", params={"angle_degrees": 90}), threading.Event(), lambda _: None)
        self.assertEqual(caught.exception.code, "CAPABILITY_UNAVAILABLE")
        self.assertEqual(adapter._skill_client.sent, [])

    def test_external_cancel_retains_ownership_until_terminal(self):
        adapter = skill_fixture()
        announce(adapter)
        thread, outcome = run_step(adapter, Step("follow", params={"subject": "person", "duration_seconds": 1}, timeout=2))
        wait_until(lambda: adapter._skill_client.sent)
        handle = FakeHandle()
        adapter._skill_client.sent[0][1].set_result(handle)
        adapter.stop()
        self.assertTrue(adapter.snapshot()["action_pending"])
        handle.finish(status=5)
        thread.join(1)
        self.assertIsInstance(outcome["error"], ExecutionCancelled)
        self.assertFalse(adapter.snapshot()["action_pending"])

    def test_recovery_requires_received_terminal_goal_status(self):
        adapter = skill_fixture()
        identifier = bytes(range(16))
        mission = {"mode": "ros2", "execution_refs": [{"goal_id": identifier.hex(), "action_name": "/navigate_to_pose"}]}
        self.assertFalse(adapter.reconcile_mission(mission)["verified"])
        entry = NS(goal_info=NS(goal_id=NS(uuid=identifier)), status=2)
        adapter._on_action_status("/navigate_to_pose", NS(status_list=[entry]))
        self.assertEqual(adapter.reconcile_mission(mission)["remote_state"], "active")
        entry.status = 5
        adapter._on_action_status("/navigate_to_pose", NS(status_list=[entry]))
        self.assertTrue(adapter.reconcile_mission(mission)["verified"])

    def test_execution_uuid_is_in_feedback_for_durable_reconciliation(self):
        adapter = skill_fixture()
        feedback, outcome = [], {}
        def execute():
            outcome["result"] = adapter.execute(Step("navigate", "home", step_id="s1", timeout=2), threading.Event(), feedback.append)
        worker = threading.Thread(target=execute)
        worker.start()
        wait_until(lambda: adapter._navigation.sent)
        handle = FakeHandle()
        handle.goal_id = NS(uuid=bytes(range(16)))
        adapter._navigation.sent[0][1].set_result(handle)
        handle.finish()
        worker.join(1)
        references = [item["execution_ref"] for item in feedback if item.get("execution_ref")]
        self.assertEqual(references[-1]["goal_id"], bytes(range(16)).hex())
        self.assertEqual(references[-1]["step_id"], "s1")


if __name__ == "__main__":
    unittest.main()
