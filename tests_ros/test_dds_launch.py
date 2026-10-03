"""Real ROS/DDS process tests. Requires a built/sourced Jazzy workspace.

These tests use software action servers, not Nav2's planner/controller and not
robot hardware. They deliberately fail to import if ROS is unavailable.
"""
import copy
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest

import launch
from launch.actions import ExecuteProcess
import launch_testing
import launch_testing.actions
import launch_testing.asserts
import launch_testing.markers
import pytest
import rclpy
from rclpy.parameter import Parameter
from rclpy.parameter_client import AsyncParameterClient
from std_msgs.msg import String
from lifecycle_msgs.srv import ChangeState, GetState
from lifecycle_msgs.msg import Transition

from robot_voice_patrol.config import load_config
from robot_voice_patrol.contracts import ExecutionError, Step
from robot_voice_patrol.ros_adapter import Ros2Adapter

ROOT = Path(__file__).resolve().parents[1]
_temporary = tempfile.TemporaryDirectory(prefix="voice_patrol_dds_")
CONFIG = load_config(ROOT / "config" / "default.json")
CONFIG["navigation_timeout"] = 4.0
CONFIG["inspection_timeout"] = 3.0
CONFIG["max_retries"] = 0
CONFIG["mock"]["travel_seconds"] = 0.2
CONFIG["mock"]["inspection_seconds"] = 0.2
CONFIG["ros"].update(perception_backend="action", cancel_timeout=1.0, service_discovery_timeout=2.0)
CONFIG_PATH = Path(_temporary.name) / "config.json"
CONFIG_PATH.write_text(json.dumps(CONFIG, ensure_ascii=False), encoding="utf-8")


@pytest.mark.launch_test
@launch_testing.markers.keep_alive
def generate_test_description():
    # Keep tests isolated from robots/other test jobs on the developer network.
    os.environ["ROS_DOMAIN_ID"] = str(20 + os.getpid() % 180)
    os.environ["ROS_AUTOMATIC_DISCOVERY_RANGE"] = "LOCALHOST"
    env = {"PYTHONPATH": str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
           "PYTHONUNBUFFERED": "1", "ROS_DOMAIN_ID": os.environ["ROS_DOMAIN_ID"],
           "ROS_AUTOMATIC_DISCOVERY_RANGE": "LOCALHOST"}
    fixture = ExecuteProcess(cmd=[sys.executable, str(ROOT / "examples" / "ros_mock_endpoints.py"),
        "--ros-args", "-p", f"config:={CONFIG_PATH}"], additional_env=env, output="screen")
    assistant = ExecuteProcess(cmd=[sys.executable, "-m", "robot_voice_patrol.ros_node", "--ros-args",
        "-p", f"config:={CONFIG_PATH}", "-p", "dashboard:=false", "-p",
        f"db_path:={Path(_temporary.name) / 'missions.sqlite3'}"], additional_env=env, output="screen")
    return launch.LaunchDescription([fixture, assistant, launch_testing.actions.ReadyToTest()]), {
        "fixture": fixture, "assistant": assistant}


class TestDDSWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rclpy.init()

    @classmethod
    def tearDownClass(cls):
        rclpy.shutdown()

    def setUp(self):
        self.node = rclpy.create_node("voice_patrol_dds_test")
        self.state = None
        self.subscription = self.node.create_subscription(String, "/voice_patrol/state", self.on_state, 10)
        self.publisher = self.node.create_publisher(String, "/voice_patrol/command", 10)
        self.params = AsyncParameterClient(self.node, "voice_patrol_software_fixture")
        self.wait_for(lambda: self.state is not None and self.publisher.get_subscription_count() > 0, 20)

    def tearDown(self):
        self.node.destroy_node()

    def on_state(self, msg):
        self.state = json.loads(msg.data)

    def wait_for(self, predicate, timeout=10):
        deadline = time.monotonic()+timeout
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail(f"DDS condition timed out. Last state: {self.state}")
            rclpy.spin_once(self.node, timeout_sec=0.05)

    def configure(self, **values):
        self.assertTrue(self.params.wait_for_services(timeout_sec=5))
        future = self.params.set_parameters([Parameter(name, value=value) for name, value in values.items()])
        self.wait_for(future.done, 5)
        self.assertTrue(all(result.successful for result in future.result().results))

    def command(self, text):
        old_id = (self.state.get("mission") or {}).get("id")
        self.publisher.publish(String(data=text))
        self.wait_for(lambda: (self.state.get("mission") or {}).get("id") not in (None, old_id))

    def terminal(self, timeout=10):
        self.wait_for(lambda: self.state["state"] in ("succeeded", "failed", "cancelled"), timeout)
        return self.state

    def test_10_navigation_and_typed_observation(self):
        self.configure(inspection_outcome="", sensor_available=True)
        self.command("去会议室检查有没有水杯，然后返回起点")
        state = self.terminal()
        self.assertEqual(state["state"], "succeeded")
        observation = next(r for r in state["mission"]["results"] if r["kind"] == "inspect")
        self.assertEqual(observation["outcome"], "found")
        self.assertEqual(observation["source"], "ros2_inspection_action")
        self.assertTrue(observation["simulated"])
        self.assertEqual(state["robot"]["location"], "home")

    def test_20_inconclusive_is_preserved(self):
        self.configure(inspection_outcome="inconclusive")
        self.command("去会议室检查有没有水杯")
        state = self.terminal()
        self.assertEqual(state["state"], "succeeded")
        observation = next(r for r in state["mission"]["results"] if r["kind"] == "inspect")
        self.assertEqual(observation["outcome"], "inconclusive")
        self.assertIsNone(observation["found"])
        self.configure(inspection_outcome="")

    def test_30_inspection_cancel_is_confirmed(self):
        self.configure(inspection_delay_seconds=2.5)
        self.command("去会议室检查有没有水杯")
        self.wait_for(lambda: (self.state["robot"].get("action") or {}).get("kind") == "inspect")
        self.publisher.publish(String(data="停止"))
        state = self.terminal()
        self.assertEqual(state["state"], "cancelled")
        self.assertFalse(state["robot"]["action_pending"])
        self.configure(inspection_delay_seconds=-1.0)

    def test_40_late_goal_ack_is_cancelled_over_dds(self):
        self.configure(acknowledgement_delay_seconds=0.4, navigation_delay_seconds=2.0)
        adapter = Ros2Adapter(copy.deepcopy(CONFIG))
        try:
            deadline = time.monotonic()+10
            while not adapter.snapshot()["health"]["navigation_ready"] and time.monotonic() < deadline:
                time.sleep(0.05)
            with self.assertRaises(ExecutionError):
                adapter.execute(Step("navigate", "home", timeout=0.1), threading.Event(), lambda _: None)
            self.assertTrue(adapter.snapshot()["action_pending"])
            deadline = time.monotonic()+5
            while adapter.snapshot()["action_pending"] and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(adapter.snapshot()["action_pending"], "late accepted goal did not terminate")
        finally:
            adapter.close()
            self.configure(acknowledgement_delay_seconds=0.0, navigation_delay_seconds=-1.0)

    def test_50_sensor_error_fails_task(self):
        self.configure(sensor_available=False)
        self.command("去会议室检查有没有水杯")
        state = self.terminal()
        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["robot"]["health"]["last_error"]["code"], "ACTION_ABORTED")
        self.configure(sensor_available=True)

    def test_60_typed_external_skills_advertise_and_return_fixture_evidence(self):
        adapter = Ros2Adapter(copy.deepcopy(CONFIG))
        try:
            deadline = time.monotonic() + 10
            while not adapter.capabilities()["capture"]["available"] and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(adapter.capabilities()["capture"]["available"])
            capture = adapter.execute(Step("capture", params={"camera": "front", "format": "png"}, timeout=3),
                                      threading.Event(), lambda _: None)
            self.assertTrue(capture["simulated"])
            self.assertTrue(capture["evidence"]["media_uri"].startswith("mock://"))
            turn = adapter.execute(Step("turn", params={"angle_degrees": 45}, timeout=3),
                                   threading.Event(), lambda _: None)
            self.assertEqual(turn["status"], "succeeded")
            self.assertFalse(adapter.snapshot()["action_pending"])
        finally:
            adapter.close()

    def test_70_managed_lifecycle_blocks_commands_when_inactive(self):
        change = self.node.create_client(ChangeState, "/voice_patrol/change_state")
        get_state = self.node.create_client(GetState, "/voice_patrol/get_state")
        try:
            self.assertTrue(change.wait_for_service(timeout_sec=5))
            request = ChangeState.Request()
            request.transition.id = Transition.TRANSITION_DEACTIVATE
            future = change.call_async(request)
            self.wait_for(future.done)
            self.assertTrue(future.result().success)
            self.wait_for(lambda: self.state.get("lifecycle", {}).get("state") == "inactive")
            old_id = (self.state.get("mission") or {}).get("id")
            self.publisher.publish(String(data="去前台"))
            deadline = time.monotonic() + .5
            while time.monotonic() < deadline:
                rclpy.spin_once(self.node, timeout_sec=.05)
            self.assertEqual((self.state.get("mission") or {}).get("id"), old_id)
            state_future = get_state.call_async(GetState.Request())
            self.wait_for(state_future.done)
            self.assertEqual(state_future.result().current_state.label, "inactive")
            request.transition.id = Transition.TRANSITION_ACTIVATE
            future = change.call_async(request)
            self.wait_for(future.done)
            self.assertTrue(future.result().success)
            self.wait_for(lambda: self.state.get("lifecycle", {}).get("state") == "active")
        finally:
            self.node.destroy_client(change)
            self.node.destroy_client(get_state)

    @unittest.skipUnless(hasattr(signal, "SIGKILL"), "Linux fault injection")
    def test_90_server_loss_never_claims_success(self, proc_info, fixture):
        self.configure(navigation_delay_seconds=20.0)
        self.command("去会议室")
        self.wait_for(lambda: (self.state["robot"].get("action") or {}).get("phase") == "executing")
        os.kill(proc_info[fixture].pid, signal.SIGKILL)
        state = self.terminal(timeout=12)
        self.assertEqual(state["state"], "failed")
        self.assertTrue(state["robot"]["action_pending"])
        self.assertTrue(state["robot"]["health"]["blocked"])


@launch_testing.post_shutdown_test()
class TestCleanShutdown(unittest.TestCase):
    def test_exit_codes(self, proc_info, fixture, assistant):
        launch_testing.asserts.assertExitCodes(proc_info, process=assistant, allowable_exit_codes=[0])
        launch_testing.asserts.assertExitCodes(proc_info, process=fixture, allowable_exit_codes=[0, -9])
