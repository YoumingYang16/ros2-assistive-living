"""ROS 2 server for an explicitly installed, trusted HardwareDriver provider."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path

from .config import load_config
from .hardware_gateway import GatewayError, HardwareGateway, load_driver


def wire_goal(goal):
    return {name: getattr(goal, name) for name in (
        "request_id", "skill", "target", "timeout_seconds", "parameters_json", "camera", "image_format",
        "subject", "duration_seconds", "distance_meters", "angle_degrees")}


def hardware_node_class():
    # Import only after the ROS entrypoint is used, preserving ordinary Python
    # tests and mock-mode operation on hosts without ROS native libraries.
    from rclpy.node import Node
    from rclpy.action import ActionServer, GoalResponse, CancelResponse
    from rclpy.callback_groups import ReentrantCallbackGroup
    from voice_patrol_interfaces.action import ExecuteSkill
    from voice_patrol_interfaces.srv import GetCapabilities

    class HardwareGatewayNode(Node):
        def __init__(self):
            super().__init__("voice_patrol_hardware")
            for name, default in (("config", ""), ("provider", ""),
                                  ("journal_path", ".runtime/hardware-receipts.sqlite3")):
                self.declare_parameter(name, default)
            path = self.get_parameter("config").value
            config = load_config(path or Path(__file__).parent / "config" / "home.json")
            provider = str(self.get_parameter("provider").value)
            driver = load_driver(provider, config)
            self.gateway = HardwareGateway(config, driver, journal_path=self.get_parameter("journal_path").value,
                                           provider=provider or "unconfigured")
            group = ReentrantCallbackGroup()
            settings = config.get("ros", {})
            self.action = ActionServer(self, ExecuteSkill, settings.get("skill_action", "/voice_patrol/execute_skill"),
                                       execute_callback=self.execute_skill, goal_callback=self.accept_goal,
                                       cancel_callback=lambda _: CancelResponse.ACCEPT, callback_group=group)
            self.capability_service = self.create_service(GetCapabilities,
                settings.get("capabilities_service", "/voice_patrol/get_capabilities"), self.get_capabilities,
                callback_group=group)
            self.get_logger().info("Hardware gateway started; only provider-declared capabilities are enabled")
            if not provider:
                self.get_logger().warning("No hardware provider configured: all physical skills unavailable")

        def get_capabilities(self, request, response):
            capabilities = self.gateway.capabilities()
            response.protocol_version = "5"
            response.skills = sorted(capabilities)
            # Protocol has one global simulation bit. Conservative mixed-provider
            # reporting marks the entire endpoint simulated if any skill is.
            response.simulated = any(item["simulated"] for item in capabilities.values())
            response.provider = self.gateway.provider
            return response

        def accept_goal(self, goal):
            try:
                self.gateway.check_goal(wire_goal(goal))
                return GoalResponse.ACCEPT
            except (GatewayError, ValueError, TypeError, AttributeError) as exc:
                self.get_logger().warning(f"Rejected hardware goal: {exc}")
                return GoalResponse.REJECT

        def execute_skill(self, handle):
            goal = handle.request
            result = ExecuteSkill.Result()
            result.request_id, result.skill = goal.request_id, goal.skill

            class CancelSignal:
                def is_set(self):
                    return bool(handle.is_cancel_requested)

            def feedback(payload):
                item = ExecuteSkill.Feedback()
                progress = payload.get("progress", 0.0)
                item.progress = float(max(0, min(1, progress))) if type(progress) in (int, float) and math.isfinite(progress) else 0.0
                item.phase = str(payload.get("phase", "executing"))[:100]
                item.message = str(payload.get("message", ""))[:1000]
                handle.publish_feedback(item)

            try:
                receipt = self.gateway.execute(wire_goal(goal), CancelSignal(), feedback)
            except Exception as exc:
                receipt = {"status": "failed", "terminal_confirmed": getattr(exc, "code", "") == "CANCELLED_BEFORE_DISPATCH", "simulated": False,
                           "evidence": {}, "error_code": getattr(exc, "code", "GATEWAY_FAILURE"),
                           "message": str(exc)[:1000], "observed_at": datetime.now(timezone.utc).isoformat()}
            result.simulated = bool(receipt.get("simulated"))
            evidence = dict(receipt.get("evidence", {}))
            if "driver_status" in receipt:
                evidence["gateway_driver_status"] = receipt["driver_status"]
            evidence["gateway_terminal_confirmed"] = receipt.get("terminal_confirmed") is True
            evidence["gateway_replayed"] = bool(receipt.get("replayed"))
            result.evidence_json = json.dumps(evidence, ensure_ascii=False, allow_nan=False)
            result.media_uri = receipt.get("media_uri", "")
            stamp = datetime.fromisoformat(receipt["observed_at"].replace("Z", "+00:00")).timestamp()
            result.observed_at.sec = int(stamp)
            result.observed_at.nanosec = min(999999999, max(0, int((stamp-int(stamp))*1_000_000_000)))
            result.success = receipt["status"] == "succeeded" and not handle.is_cancel_requested
            result.error_code = 0 if result.success else {"cancelled": 2, "timed_out": 3, "unknown": 4}.get(receipt["status"], 1)
            result.error_message = "" if result.success else str(receipt.get("error_code", receipt["status"])) + ": " + str(receipt.get("message", "Hardware operation did not complete"))[:1000]
            if result.success:
                handle.succeed()
            elif handle.is_cancel_requested and receipt.get("terminal_confirmed") is True:
                handle.canceled()
            else:
                handle.abort()
            return result

        def close_gateway(self):
            confirmed = self.gateway.close()
            if not confirmed:
                self.get_logger().error("Driver still active; journal remains unresolved. Stop request is not physical stop confirmation.")
            return confirmed

    return HardwareGatewayNode


def main(args=None):
    import rclpy
    from rclpy.executors import MultiThreadedExecutor
    rclpy.init(args=args)
    node = None
    executor = MultiThreadedExecutor(num_threads=4)
    try:
        node = hardware_node_class()()
        executor.add_node(node)
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        if node:
            node.close_gateway()
        # Wait briefly only: a stuck driver retains a durable unresolved row.
        executor.shutdown(timeout_sec=1.0, wait_for_threads=False)
        if node:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
