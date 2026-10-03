from types import SimpleNamespace
import unittest

from robot_voice_patrol.contracts import CommandError
from robot_voice_patrol.lifecycle import LifecycleController
from robot_voice_patrol.ros_node import RosLifecycleBridge


class LifecycleTests(unittest.TestCase):
    def test_full_lifecycle_rejects_commands_outside_active(self):
        life = LifecycleController(initial_state="unconfigured")
        for action, expected in [("configure", "inactive"), ("activate", "active"),
                                 ("deactivate", "inactive"), ("cleanup", "unconfigured")]:
            self.assertEqual(life.transition(action)["state"], expected)
            if expected == "active":
                life.ensure_active()
            else:
                with self.assertRaises(CommandError):
                    life.ensure_active()
        self.assertEqual(len(life.snapshot()["transitions"]), 4)

    def test_active_job_or_unconfirmed_external_action_blocks_deactivation(self):
        for busy, pending in [(True, False), (False, True)]:
            life = LifecycleController(SimpleNamespace(snapshot=lambda: {"action_pending": pending}), busy=lambda: busy)
            with self.assertRaises(CommandError):
                life.transition("deactivate")
            self.assertTrue(life.snapshot()["active"])

    def test_error_hook_failure_requires_reset_before_configuration(self):
        def fail():
            raise RuntimeError("resource unavailable")
        life = LifecycleController(initial_state="unconfigured", hooks={"configure": fail})
        with self.assertRaises(CommandError):
            life.transition("configure")
        self.assertEqual(life.snapshot()["state"], "error")
        self.assertIn("resource unavailable", life.snapshot()["last_error"])
        self.assertEqual(life.transition("reset_error")["state"], "unconfigured")

    def test_illegal_transitions_do_not_change_state(self):
        life = LifecycleController()
        for action in ["activate", "configure", "cleanup", "reset_error", "shell"]:
            with self.subTest(action=action), self.assertRaises(CommandError):
                life.transition(action)
        self.assertEqual(life.snapshot()["state"], "active")

    def test_ros_bridge_uses_native_trigger_and_checks_callback_outcome(self):
        control = LifecycleController(initial_state="inactive")
        calls = []
        def trigger():
            calls.append("activate")
            control.transition("activate")
        node = SimpleNamespace(control=control, trigger_activate=trigger, trigger_deactivate=lambda: None)
        bridge = RosLifecycleBridge(node)
        self.assertTrue(bridge.transition("activate")["managed_ros_node"])
        self.assertEqual(calls, ["activate"])
        with self.assertRaises(CommandError):
            bridge.transition("deactivate")


if __name__ == "__main__":
    unittest.main()
