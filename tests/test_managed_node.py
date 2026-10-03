"""Lifecycle resource/command contract with Python stand-ins, not native DDS."""
from enum import Enum
import sys
import threading
import time
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import patch

from robot_voice_patrol.contracts import CommandError, Plan, Step
from robot_voice_patrol.mock_adapter import MockAdapter
from robot_voice_patrol.ros_node import managed_node_class


class Return(Enum):
    SUCCESS = 1
    FAILURE = 2
    ERROR = 3


class NativeNodeStandIn:
    def __init__(self, name):
        self.parameters, self.destroyed = {}, []
        self.messages = []

    def declare_parameter(self, name, default):
        self.parameters[name] = NS(value=default)

    def get_parameter(self, name):
        return self.parameters[name]

    def get_logger(self):
        return NS(info=self.messages.append, warning=self.messages.append, error=self.messages.append)

    def create_publisher(self, *args, **kwargs):
        return NS(publish=lambda msg: self.messages.append(msg.data))

    create_lifecycle_publisher = create_publisher

    def create_subscription(self, *args, **kwargs):
        return NS(kind="subscription")

    def create_timer(self, *args, **kwargs):
        return NS(kind="timer")

    def destroy_entity(self, entity):
        self.destroyed.append(entity)

    destroy_publisher = destroy_lifecycle_publisher = destroy_subscription = destroy_timer = destroy_entity

    def transition_result(self, state):
        return Return.SUCCESS

    on_configure = on_activate = on_deactivate = on_cleanup = on_shutdown = transition_result


def modules():
    result = {}
    for name in ("rclpy", "rclpy.lifecycle", "rclpy.callback_groups", "rclpy.clock", "std_msgs", "std_msgs.msg"):
        result[name] = ModuleType(name)
    result["rclpy.lifecycle"].LifecycleNode = NativeNodeStandIn
    result["rclpy.lifecycle"].TransitionCallbackReturn = Return
    result["rclpy.callback_groups"].ReentrantCallbackGroup = lambda: object()
    result["rclpy.clock"].Clock = lambda **kwargs: object()
    result["rclpy.clock"].ClockType = NS(STEADY_TIME=1)
    result["std_msgs.msg"].String = lambda **kwargs: NS(**kwargs)
    return result


class ManagedNodeTests(unittest.TestCase):
    def setUp(self):
        self.fake_modules = patch.dict(sys.modules, modules())
        self.fake_modules.start()
        self.adapter_patch = patch("robot_voice_patrol.ros_node.Ros2Adapter", side_effect=lambda config, **kwargs: MockAdapter(config))
        self.adapter_patch.start()
        self.node = managed_node_class()()
        self.node.parameters["dashboard"].value = False
        self.node.parameters["db_path"].value = ":memory:"

    def tearDown(self):
        self.node._release_resources()
        self.adapter_patch.stop()
        self.fake_modules.stop()

    def test_configure_activate_cleanup_releases_resources_and_can_configure_again(self):
        self.assertEqual(self.node.on_configure(None), Return.SUCCESS)
        first = self.node.engine
        with self.assertRaises(CommandError):
            first.submit_plan(Plan("wait", [Step("wait", seconds=.01)], "等待"))
        self.assertEqual(self.node.on_activate(None), Return.SUCCESS)
        first.submit_plan(Plan("wait", [Step("wait", seconds=.01)], "等待"))
        first._worker.join(1)
        self.assertEqual(first.snapshot()["state"], "succeeded")
        self.assertEqual(self.node.on_deactivate(None), Return.SUCCESS)
        self.assertEqual(self.node.on_cleanup(None), Return.SUCCESS)
        self.assertIsNone(self.node.engine)
        self.assertTrue(first.adapter._closed)
        self.assertTrue(first.store._closed)
        self.assertEqual(self.node.on_configure(None), Return.SUCCESS)
        self.assertIsNot(self.node.engine, first)

    def test_deactivation_rejects_live_mission_and_preserves_active_state(self):
        self.node.on_configure(None)
        self.node.on_activate(None)
        engine = self.node.engine
        engine.submit_plan(Plan("wait", [Step("wait", seconds=1)], "等待"))
        self.assertEqual(self.node.on_deactivate(None), Return.FAILURE)
        self.assertEqual(self.node.control.snapshot()["state"], "active")
        engine.control("stop")
        engine._worker.join(1)
        self.assertEqual(self.node.on_deactivate(None), Return.SUCCESS)

    def test_inactive_ros_command_cannot_start_mission(self):
        self.node.on_configure(None)
        self.node.command_received(NS(data="去会议室"))
        self.assertIsNone(self.node.engine.snapshot()["mission"])

    def test_configure_failure_runs_error_resource_cleanup(self):
        with patch("robot_voice_patrol.ros_node.MissionEngine", side_effect=RuntimeError("database unavailable")):
            self.assertEqual(self.node.on_configure(None), Return.ERROR)
        adapter = self.node.adapter
        self.assertEqual(self.node.on_error(None), Return.SUCCESS)
        self.assertTrue(adapter._closed)
        self.assertEqual(self.node.control.snapshot()["state"], "unconfigured")


if __name__ == "__main__":
    unittest.main()
