from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from robot_voice_patrol.config import load_config
from robot_voice_patrol.hardware_gateway import HardwareGateway, GatewayError, load_driver, parse_goal
from robot_voice_patrol.hardware_node import wire_goal
from robot_voice_patrol.hardware_node import hardware_node_class


class ReceiptDriver:
    """A test fixture only: deliberately does not implement real actuation."""
    def __init__(self):
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.stopped = threading.Event()
        self.change = lambda receipt: receipt
        self.error = None
        self.closed = False

    def capabilities(self):
        return {name: {"available": True, "simulated": True} for name in
                ("home_control", "pick_object", "place_object", "handover_object", "capture", "dock", "follow", "turn")}

    def execute(self, command, cancel, feedback):
        self.calls.append(command)
        self.started.set()
        self.release.wait(2)
        if self.error:
            raise self.error
        step = command.step
        evidence = {"target": step.target, **step.params, "readback_confirmed": True,
                    "reported_state": step.params.get("state"), "payload_confirmed": True,
                    "surface_confirmed": True, "released": True, "recipient_acknowledged": True,
                    "receipt_id": "fixture-receipt", "media_created": True, "docked": True,
                    "charging_confirmed": True, "tracking_confirmed": True, "motion_complete": True}
        receipt = {"request_id": command.request_id, "kind": step.kind, "target": step.target,
                   "status": "succeeded", "terminal_confirmed": True, "simulated": True,
                   "observed_at": datetime.now(timezone.utc).isoformat(), "evidence": evidence}
        if step.kind == "capture":
            receipt["media_uri"] = "fixture://image/one.png"
        return self.change(receipt)

    def stop(self):
        self.stopped.set()

    def snapshot(self):
        return {"fixture": True}

    def close(self):
        self.closed = True


class HardwareGatewayTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(Path(__file__).parents[1] / "config/home.json")
        self.driver = ReceiptDriver()
        self.gateway = HardwareGateway(self.config, self.driver)
        self.addCleanup(self.gateway.close)

    def goal(self, request_id="one", **updates):
        result = {"request_id": request_id, "skill": 5, "target": "bedroom", "timeout_seconds": 1,
                  "parameters_json": '{"device":"light","state":"on"}', "camera": "", "image_format": "",
                  "subject": "", "duration_seconds": 0.0, "distance_meters": 0.0, "angle_degrees": 0.0}
        result.update(updates)
        return result

    def assert_code(self, code, callback):
        with self.assertRaises(GatewayError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)

    def run_background(self, cancel=None, goal=None):
        result = {}
        thread = threading.Thread(target=lambda: result.update(self.gateway.execute(goal or self.goal(), cancel)), daemon=True)
        thread.start()
        self.assertTrue(self.driver.started.wait(1))
        self.addCleanup(self.driver.release.set)
        self.addCleanup(lambda: thread.join(2))
        return thread, result

    def test_wire_goal_to_provider_and_correlated_receipt(self):
        wire = wire_goal(SimpleNamespace(**self.goal()))
        result = self.gateway.execute(wire)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.driver.calls[0].step.params, {"device": "light", "state": "on"})
        self.assertEqual(result["request_id"], wire["request_id"])
        self.assertTrue(result["simulated"])

    def test_unconfigured_provider_advertises_nothing_and_never_fakes_success(self):
        other = HardwareGateway(self.config)
        self.addCleanup(other.close)
        self.assertEqual(other.capabilities(), {})
        self.assert_code("CAPABILITY_UNAVAILABLE", lambda: other.execute(self.goal()))

    def test_duplicate_terminal_request_is_not_reexecuted(self):
        first = self.gateway.execute(self.goal())
        replay = self.gateway.execute(self.goal())
        self.assertTrue(replay.pop("replayed"))
        self.assertEqual(first, replay)
        self.assertEqual(len(self.driver.calls), 1)

    def test_same_request_with_changed_parameters_is_rejected(self):
        self.gateway.execute(self.goal())
        self.assert_code("REQUEST_CONFLICT", lambda: self.gateway.execute(self.goal(parameters_json='{"device":"light","state":"off"}')))

    def test_overlapping_request_never_enters_driver(self):
        self.driver.release.clear()
        thread, result = self.run_background()
        self.assert_code("BUSY", lambda: self.gateway.execute(self.goal("two")))
        self.driver.release.set()
        thread.join(1)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(self.driver.calls), 1)

    def test_cancel_keeps_lease_until_actual_driver_return(self):
        cancel = threading.Event()
        self.driver.release.clear()
        thread, result = self.run_background(cancel)
        cancel.set()
        self.assertTrue(self.driver.stopped.wait(1))
        self.assertTrue(thread.is_alive())
        self.assert_code("BUSY", lambda: self.gateway.execute(self.goal("two")))
        self.driver.release.set()
        thread.join(1)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["driver_status"], "succeeded")
        self.assertTrue(result["evidence"]["readback_confirmed"])

    def test_timeout_keeps_lease_and_preserves_late_evidence(self):
        self.driver.release.clear()
        thread, result = self.run_background(goal=self.goal(timeout_seconds=.03))
        self.assertTrue(self.driver.stopped.wait(1))
        self.assertEqual(self.gateway.snapshot()["active_request_id"], "one")
        self.assert_code("BUSY", lambda: self.gateway.execute(self.goal("two")))
        self.driver.release.set()
        thread.join(1)
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(result["driver_status"], "succeeded")

    def test_cancel_before_dispatch_does_not_call_provider(self):
        cancel = threading.Event()
        cancel.set()
        self.assert_code("CANCELLED_BEFORE_DISPATCH", lambda: self.gateway.execute(self.goal(), cancel))
        self.assertEqual(self.driver.calls, [])

    def test_provider_exception_is_uncertain_not_confirmed_stop(self):
        self.driver.error = RuntimeError("Serial response lost")
        result = self.gateway.execute(self.goal())
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["terminal_confirmed"])
        self.assertEqual(self.gateway.capabilities(), {})
        self.assert_code("UNCERTAIN_HARDWARE", lambda: self.gateway.execute(self.goal("two")))

    def test_wrong_receipt_correlation_blocks_further_operations(self):
        self.driver.change = lambda receipt: {**receipt, "request_id": "someone-else"}
        self.assertEqual(self.gateway.execute(self.goal())["status"], "unknown")
        self.assert_code("UNCERTAIN_HARDWARE", lambda: self.gateway.execute(self.goal()))

    def test_missing_home_readback_cannot_succeed(self):
        self.driver.change = lambda receipt: {**receipt, "evidence": {"device": "light", "target": "bedroom", "reported_state": "on"}}
        self.assertEqual(self.gateway.execute(self.goal())["status"], "unknown")

    def test_old_or_future_receipt_cannot_succeed(self):
        for seconds in (-30, 30):
            with self.subTest(seconds=seconds):
                driver = ReceiptDriver()
                driver.change = lambda receipt: {**receipt, "observed_at": (datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat()}
                gateway = HardwareGateway(self.config, driver)
                self.addCleanup(gateway.close)
                self.assertEqual(gateway.execute(self.goal())["status"], "unknown")

    def test_simulation_flag_must_match_declared_capability(self):
        self.driver.change = lambda receipt: {**receipt, "simulated": False}
        self.assertEqual(self.gateway.execute(self.goal())["status"], "unknown")

    def test_missing_terminal_ack_cannot_release_to_next_action(self):
        self.driver.change = lambda receipt: {**receipt, "terminal_confirmed": False}
        self.assertEqual(self.gateway.execute(self.goal())["status"], "unknown")
        self.assertEqual(self.gateway.snapshot()["uncertain_requests"], ["one"])

    def test_driver_failure_with_terminal_ack_is_terminal(self):
        self.driver.change = lambda receipt: {**receipt, "status": "failed", "evidence": {"motor_stopped": True}}
        self.assertEqual(self.gateway.execute(self.goal())["status"], "failed")
        self.assertTrue(self.gateway.capabilities())
        self.assertEqual(self.gateway.execute(self.goal("two"))["status"], "failed")

    def test_shutdown_cannot_claim_active_provider_is_closed(self):
        self.driver.release.clear()
        thread, result = self.run_background()
        self.assertFalse(self.gateway.close())
        self.assertFalse(self.driver.closed)
        self.assertEqual(self.gateway.capabilities(), {})
        self.assert_code("CLOSED", lambda: self.gateway.execute(self.goal("two")))
        self.driver.release.set()
        thread.join(1)
        self.assertTrue(self.gateway.close())
        self.assertTrue(self.driver.closed)

    def test_strict_wire_validation(self):
        cases = [{"skill": True}, {"skill": 99}, {"request_id": "bad id"}, {"target": "missing"},
                 {"timeout_seconds": float("nan")}, {"timeout_seconds": 0}, {"timeout_seconds": 4000},
                 {"parameters_json": '{"device":"light","device":"fan","state":"on"}'},
                 {"parameters_json": '{"device":"oven","state":"on"}'},
                 {"parameters_json": '{"device":"light","state":"on","temperature":50}'},
                 {"camera": "unexpected"}, {"parameters_json": '{"device":"light","state":NaN}'}]
        for update in cases:
            with self.subTest(update=update):
                self.assert_code("INVALID_GOAL", lambda: self.gateway.execute(self.goal(**update)))
        self.assertEqual(self.driver.calls, [])

    def test_unknown_fields_are_rejected(self):
        self.assert_code("INVALID_GOAL", lambda: self.gateway.execute({**self.goal(), "shell": "unsafe"}))

    def test_all_eight_wire_codes_have_typed_provider_path(self):
        variants = [dict(skill=1, parameters_json="", camera="front", image_format="png"),
                    dict(skill=2, parameters_json="", target="home"),
                    dict(skill=3, parameters_json="", target="", subject="本人", duration_seconds=.1, distance_meters=1.5),
                    dict(skill=4, parameters_json="", target="", angle_degrees=90), {},
                    dict(skill=6, parameters_json='{"item":"手机"}'),
                    dict(skill=7, parameters_json='{"item":"手机","surface":"桌面"}'),
                    dict(skill=8, parameters_json='{"item":"手机","recipient":"本人"}')]
        for index, variant in enumerate(variants):
            result = self.gateway.execute(self.goal(f"code-{index}", **variant))
            self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(len(self.driver.calls), 8)

    def test_reconciliation_requires_fresh_matching_physical_receipt(self):
        self.driver.error = RuntimeError("Disconnected")
        self.gateway.execute(self.goal())
        command = self.driver.calls[0]
        self.driver.error = None
        checked = self.driver.execute(command, threading.Event(), lambda _: None)
        self.assert_code("RECONCILIATION_NOTE", lambda: self.gateway.reconcile("one", checked, "ok"))
        result = self.gateway.reconcile("one", checked, "Operator checked device and read back final state")
        self.assertTrue(result["reconciled"])
        self.assertTrue(self.gateway.execute(self.goal())["replayed"])

    def test_persistent_terminal_receipt_survives_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipts.db"
            first = HardwareGateway(self.config, self.driver, journal_path=path)
            first.execute(self.goal())
            first.close()
            second_driver = ReceiptDriver()
            second = HardwareGateway(self.config, second_driver, journal_path=path)
            try:
                self.assertTrue(second.execute(self.goal())["replayed"])
                self.assertEqual(second_driver.calls, [])
            finally:
                second.close()

    def test_crash_marker_blocks_every_new_action_after_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipts.db"
            first = HardwareGateway(self.config, self.driver, journal_path=path)
            first.execute(self.goal())
            first.close()
            with sqlite3.connect(path) as connection:
                connection.execute("UPDATE hardware_receipts SET status='running', receipt=NULL")
            connection.close()
            second = HardwareGateway(self.config, ReceiptDriver(), journal_path=path)
            try:
                self.assertEqual(second.snapshot()["uncertain_requests"], ["one"])
                self.assertEqual(second.capabilities(), {})
                self.assert_code("UNCERTAIN_HARDWARE", lambda: second.execute(self.goal("new")))
            finally:
                second.close()

    def test_journal_is_exclusive_between_process_owners(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipts.db"
            first = HardwareGateway(self.config, self.driver, journal_path=path)
            try:
                with self.assertRaises(RuntimeError):
                    HardwareGateway(self.config, ReceiptDriver(), journal_path=path)
            finally:
                first.close()

    def test_provider_loader_is_explicit_and_default_unavailable(self):
        self.assertEqual(load_driver("", self.config).capabilities(), {})
        self.assert_code("PROVIDER_INVALID", lambda: load_driver("https://server/driver", self.config))
        self.assert_code("PROVIDER_INVALID", lambda: load_driver("os:system;bad", self.config))

    def test_mission_database_cannot_be_reused_as_hardware_journal(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "mission.db"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE missions (id TEXT)")
            connection.commit()
            connection.close()
            self.assert_code("WRONG_JOURNAL", lambda: HardwareGateway(self.config, self.driver, journal_path=path))


class RosWrapperContractTests(unittest.TestCase):
    """Exercise wrapper callbacks with injected ROS types; NOT native ROS/DDS."""
    def setUp(self):
        class FakeNode:
            def __init__(self, name):
                self.params = {}
                self.logs = []

            def declare_parameter(self, name, default):
                self.params[name] = "" if name == "journal_path" else default

            def get_parameter(self, name):
                return SimpleNamespace(value=self.params[name])

            def create_service(self, interface, endpoint, callback, **kwargs):
                return SimpleNamespace(interface=interface, endpoint=endpoint, callback=callback)

            def get_logger(self):
                return SimpleNamespace(info=self.logs.append, warning=self.logs.append, error=self.logs.append)

        class FakeAction:
            @staticmethod
            def Result():
                return SimpleNamespace(observed_at=SimpleNamespace(sec=0, nanosec=0))

            Feedback = SimpleNamespace

        self.driver = ReceiptDriver()
        modules = {
            "rclpy.node": SimpleNamespace(Node=FakeNode),
            "rclpy.action": SimpleNamespace(ActionServer=lambda *args, **kwargs: SimpleNamespace(args=args, **kwargs),
                                            GoalResponse=SimpleNamespace(ACCEPT="accepted", REJECT="rejected"),
                                            CancelResponse=SimpleNamespace(ACCEPT="cancel_accepted")),
            "rclpy.callback_groups": SimpleNamespace(ReentrantCallbackGroup=object),
            "voice_patrol_interfaces.action": SimpleNamespace(ExecuteSkill=FakeAction),
            "voice_patrol_interfaces.srv": SimpleNamespace(GetCapabilities=object),
        }
        with patch.dict(sys.modules, modules), patch("robot_voice_patrol.hardware_node.load_driver", return_value=self.driver):
            self.node = hardware_node_class()()
        self.addCleanup(self.node.close_gateway)

    def handle(self):
        goal = SimpleNamespace(request_id="wire-one", skill=5, target="bedroom", timeout_seconds=1.0,
            parameters_json='{"device":"light","state":"on"}', camera="", image_format="", subject="",
            duration_seconds=0.0, distance_meters=0.0, angle_degrees=0.0)
        handle = SimpleNamespace(request=goal, is_cancel_requested=False, terminal=None, feedback=[])
        handle.succeed = lambda: setattr(handle, "terminal", "succeeded")
        handle.abort = lambda: setattr(handle, "terminal", "aborted")
        handle.canceled = lambda: setattr(handle, "terminal", "cancelled")
        handle.publish_feedback = handle.feedback.append
        return handle

    def test_node_exposes_protocol5_typed_actions_and_capabilities(self):
        response = self.node.get_capabilities(None, SimpleNamespace())
        self.assertEqual(response.protocol_version, "5")
        self.assertEqual(len(response.skills), 8)
        self.assertTrue(response.simulated)
        self.assertEqual(self.node.capability_service.endpoint, "/voice_patrol/get_capabilities")
        self.assertEqual(self.node.action.args[2], "/voice_patrol/execute_skill")

    def test_typed_goal_to_actual_gateway_provider_and_typed_result(self):
        handle = self.handle()
        self.assertEqual(self.node.accept_goal(handle.request), "accepted")
        result = self.node.execute_skill(handle)
        self.assertEqual(handle.terminal, "succeeded")
        self.assertTrue(result.success)
        self.assertEqual(result.request_id, handle.request.request_id)
        self.assertEqual(result.skill, 5)
        self.assertEqual(result.error_code, 0)
        self.assertTrue(json.loads(result.evidence_json)["gateway_terminal_confirmed"])
        self.assertGreater(result.observed_at.sec, 0)

    def test_action_rejects_malformed_goal_without_provider_call(self):
        handle = self.handle()
        handle.request.parameters_json = '{"device":"oven","state":"on"}'
        self.assertEqual(self.node.accept_goal(handle.request), "rejected")
        self.assertEqual(self.driver.calls, [])

    def test_invalid_evidence_aborts_action_and_withdraws_capabilities(self):
        handle = self.handle()
        self.driver.change = lambda receipt: {**receipt, "terminal_confirmed": False}
        result = self.node.execute_skill(handle)
        self.assertEqual(handle.terminal, "aborted")
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, 4)
        self.assertEqual(self.node.get_capabilities(None, SimpleNamespace()).skills, [])

    def test_cancel_is_not_ros_terminal_until_driver_ack(self):
        handle = self.handle()
        self.driver.release.clear()
        values = {}
        thread = threading.Thread(target=lambda: values.update(result=self.node.execute_skill(handle)), daemon=True)
        thread.start()
        self.assertTrue(self.driver.started.wait(1))
        handle.is_cancel_requested = True
        self.assertTrue(self.driver.stopped.wait(1))
        self.assertIsNone(handle.terminal)
        self.driver.release.set()
        thread.join(1)
        self.assertEqual(handle.terminal, "cancelled")
        self.assertFalse(values["result"].success)
        self.assertEqual(values["result"].error_code, 2)
        self.assertEqual(json.loads(values["result"].evidence_json)["gateway_driver_status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
