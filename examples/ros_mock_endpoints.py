#!/usr/bin/env python3
"""Explicit software fixtures for ROS interface demonstrations, WITHOUT hardware.

This is not a navigation algorithm, physics simulation, or camera recognizer.
It interpolates configured coordinates and returns configured object fixtures.
Never run this node alongside a real server on the same navigation action.

In an installed/sourced ROS 2 workspace:
  python3 examples/ros_mock_endpoints.py
  ros2 launch robot_voice_patrol assistant.launch.py
"""
from datetime import datetime, timezone
import json
import math
import signal
import threading
import time

import rclpy
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.clock import Clock, ClockType
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.signals import SignalHandlerOptions
from geometry_msgs.msg import PoseWithCovarianceStamped
from nav2_msgs.action import NavigateToPose
from std_msgs.msg import String

from robot_voice_patrol.config import load_config


class SoftwareFixture(Node):
    def __init__(self):
        super().__init__("voice_patrol_software_fixture")
        self.declare_parameter("config", "")
        self.declare_parameter("navigation_delay_seconds", -1.0)
        self.declare_parameter("acknowledgement_delay_seconds", 0.0)
        self.declare_parameter("inspection_delay_seconds", -1.0)
        self.declare_parameter("inspection_outcome", "")
        self.declare_parameter("sensor_available", True)
        self.declare_parameter("accept_cancellation", True)
        self.declare_parameter("skill_delay_seconds", 0.2)
        self.declare_parameter("available_skills", ["capture", "dock", "follow", "turn"])
        self.config = load_config(self.get_parameter("config").value or None)
        settings = self.config["ros"]
        self._frame = settings["map_frame"]
        home = self.config["locations"]["home"]
        self._pose = [home["x"], home["y"], home["yaw"]]
        self._lock = threading.Lock()
        self._closing = threading.Event()
        group = ReentrantCallbackGroup()
        self._pose_pub = self.create_publisher(PoseWithCovarianceStamped, settings["pose_topic"], 10)
        self._result_pub = self.create_publisher(String, settings["inspection_result_topic"], 10)
        self._inspection_sub = self.create_subscription(
            String, settings["inspection_request_topic"], self.inspect, 10, callback_group=group)
        self._action = ActionServer(
            self, NavigateToPose, settings["navigation_action"], execute_callback=self.navigate,
            goal_callback=self.accept_goal, cancel_callback=self.accept_cancel,
            callback_group=group)
        self._inspect_type = self._observation_type = self._inspect_action = None
        if settings.get("perception_backend", "action") == "action":
            from voice_patrol_interfaces.action import Inspect
            from voice_patrol_interfaces.msg import Observation
            self._inspect_type, self._observation_type = Inspect, Observation
            self._inspect_action = ActionServer(self, Inspect, settings.get("inspection_action", "/voice_patrol/inspect"),
                execute_callback=self.inspect_action, goal_callback=self.accept_inspection,
                cancel_callback=self.accept_cancel, callback_group=group)
        from voice_patrol_interfaces.action import ExecuteSkill
        from voice_patrol_interfaces.srv import GetCapabilities
        self._skill_type = ExecuteSkill
        self._skill_action = ActionServer(self, ExecuteSkill, settings.get("skill_action", "/voice_patrol/execute_skill"),
            execute_callback=self.execute_skill, goal_callback=self.accept_skill,
            cancel_callback=self.accept_cancel, callback_group=group)
        self._capabilities_service = self.create_service(GetCapabilities,
            settings.get("capabilities_service", "/voice_patrol/get_capabilities"), self.get_capabilities, callback_group=group)
        self._timer = self.create_timer(0.2, self.publish_pose, clock=Clock(clock_type=ClockType.STEADY_TIME))
        self.get_logger().warning(
            "SOFTWARE FIXTURE ONLY: generated poses and preset observations; no physical robot, SLAM or visual recognition.")

    def accept_goal(self, goal):
        self._closing.wait(max(0, self.get_parameter("acknowledgement_delay_seconds").value))
        p = goal.pose.pose
        valid = goal.pose.header.frame_id == self._frame and all(math.isfinite(v) for v in (
            p.position.x, p.position.y, p.orientation.x, p.orientation.y, p.orientation.z, p.orientation.w))
        return GoalResponse.ACCEPT if valid and not self._closing.is_set() else GoalResponse.REJECT

    def accept_cancel(self, _):
        return CancelResponse.ACCEPT if self.get_parameter("accept_cancellation").value else CancelResponse.REJECT

    def accept_inspection(self, goal):
        valid = (goal.target in self.config["locations"] and bool(goal.request_id) and
                 math.isfinite(goal.timeout_seconds) and goal.timeout_seconds > 0)
        return GoalResponse.ACCEPT if valid and not self._closing.is_set() else GoalResponse.REJECT

    def get_capabilities(self, request, response):
        response.protocol_version = "3"
        response.skills = list(self.get_parameter("available_skills").value)
        response.simulated, response.provider = True, "voice_patrol_software_fixture"
        return response

    def accept_skill(self, goal):
        name = {1: "capture", 2: "dock", 3: "follow", 4: "turn"}.get(goal.skill)
        valid = (name in self.get_parameter("available_skills").value and bool(goal.request_id)
                 and math.isfinite(goal.timeout_seconds) and 0 < goal.timeout_seconds <= 3605)
        if name == "dock":
            valid = valid and goal.target in self.config["locations"]
        if name == "capture":
            valid = valid and bool(goal.camera) and goal.image_format in {"jpeg", "png"}
        if name == "follow":
            valid = valid and bool(goal.subject) and 0 < goal.duration_seconds < goal.timeout_seconds and .2 <= goal.distance_meters <= 10
        if name == "turn":
            valid = valid and math.isfinite(goal.angle_degrees) and abs(goal.angle_degrees) <= 360
        return GoalResponse.ACCEPT if valid and not self._closing.is_set() else GoalResponse.REJECT

    def execute_skill(self, handle):
        goal = handle.request
        result = self._skill_type.Result()
        result.request_id, result.skill, result.simulated = goal.request_id, goal.skill, True
        duration = goal.duration_seconds if goal.skill == 3 else max(.02, self.get_parameter("skill_delay_seconds").value)
        started = time.monotonic()
        while True:
            if handle.is_cancel_requested:
                handle.canceled()
                return result
            elapsed = time.monotonic() - started
            if self._closing.is_set() or elapsed >= goal.timeout_seconds:
                result.error_code, result.error_message = 3, "software fixture skill interrupted/timeout"
                handle.abort()
                return result
            feedback = self._skill_type.Feedback()
            feedback.progress = min(1.0, elapsed / duration)
            feedback.phase, feedback.message = "executing", "[软件测试数据] 外部技能端点正在执行"
            handle.publish_feedback(feedback)
            if elapsed >= duration:
                break
            self._closing.wait(.02)
        with self._lock:
            if goal.skill == 2:
                location = self.config["locations"][goal.target]
                self._pose = [location["x"], location["y"], location["yaw"]]
            elif goal.skill == 4:
                yaw = self._pose[2] + math.radians(goal.angle_degrees)
                self._pose[2] = math.atan2(math.sin(yaw), math.cos(yaw))
        evidence = {"fixture": "typed_skill_software_endpoint", "skill": goal.skill,
                    "target": goal.target, "duration_seconds": elapsed, "physical_action": False}
        if goal.skill == 1:
            result.media_uri = f"mock://frames/{goal.request_id}.{goal.image_format}"
            evidence["image_generated"] = False
        result.evidence_json = json.dumps(evidence)
        result.observed_at.sec, result.observed_at.nanosec = divmod(time.time_ns(), 1_000_000_000)
        result.success = True
        handle.succeed()
        return result

    def publish_pose(self):
        msg = PoseWithCovarianceStamped()
        msg.header.frame_id = self._frame
        msg.header.stamp = self.get_clock().now().to_msg()
        with self._lock:
            x, y, yaw = self._pose
        msg.pose.pose.position.x = float(x)
        msg.pose.pose.position.y = float(y)
        msg.pose.pose.orientation.z = math.sin(yaw / 2)
        msg.pose.pose.orientation.w = math.cos(yaw / 2)
        self._pose_pub.publish(msg)

    def navigate(self, handle):
        result = NavigateToPose.Result()
        result.error_msg = "VOICE_PATROL_SOFTWARE_FIXTURE"
        p = handle.request.pose.pose
        q = p.orientation
        target = [p.position.x, p.position.y, math.atan2(2*(q.w*q.z + q.x*q.y), 1-2*(q.y*q.y+q.z*q.z))]
        with self._lock:
            start_pose = list(self._pose)
        override = self.get_parameter("navigation_delay_seconds").value
        duration = max(0.05, override if override >= 0 else self.config["mock"]["travel_seconds"])
        started = time.monotonic()
        while True:
            if handle.is_cancel_requested:
                handle.canceled()
                return result
            if self._closing.is_set():
                handle.abort()
                result.error_code = 1
                return result
            progress = min(1.0, (time.monotonic() - started) / duration)
            with self._lock:
                self._pose = [a + (b-a)*progress for a, b in zip(start_pose, target)]
            feedback = NavigateToPose.Feedback()
            feedback.distance_remaining = float(math.hypot(target[0]-start_pose[0], target[1]-start_pose[1])*(1-progress))
            handle.publish_feedback(feedback)
            if progress >= 1:
                handle.succeed()
                return result
            self._closing.wait(0.05)

    def _observation(self, request):
        target, object_name = request["target"], request.get("object_name", "")
        objects = self.config["mock"]["objects"].get(target, [])
        outcome = self.get_parameter("inspection_outcome").value or (
            ("found" if object_name in objects else "not_found") if object_name else "observed")
        if outcome not in {"found", "not_found", "inconclusive", "observed"}:
            outcome = "inconclusive"
        return {"request_id": request["request_id"], "target": target, "object_name": object_name,
                "success": True, "simulated": True, "outcome": outcome,
                "found": True if outcome == "found" else False if outcome == "not_found" else None,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "evidence": {"type": "software_fixture", "simulated": True, "preset_objects": objects},
                "summary": "[软件测试数据] " + ("证据不足" if outcome == "inconclusive" else f"预设物体：{'、'.join(objects)}")}

    def inspect(self, msg):
        try:
            request = json.loads(msg.data)
            target = request["target"]
            object_name = request.get("object_name", "")
            request_id = request["request_id"]
            if target not in self.config["locations"] or not isinstance(request_id, str):
                return
        except (KeyError, TypeError, ValueError):
            return
        override = self.get_parameter("inspection_delay_seconds").value
        delay = max(0.05, override if override >= 0 else self.config["mock"]["inspection_seconds"])
        if self._closing.wait(delay):
            return
        result = self._observation(request)
        if not self.get_parameter("sensor_available").value:
            result.update(success=False, error="software fixture sensor unavailable")
        self._result_pub.publish(String(data=json.dumps(result, ensure_ascii=False)))

    def inspect_action(self, handle):
        goal = handle.request
        result = self._inspect_type.Result()
        result.request_id, result.target, result.object_name = goal.request_id, goal.target, goal.object_name
        result.simulated = True
        override = self.get_parameter("inspection_delay_seconds").value
        duration = max(0.05, override if override >= 0 else self.config["mock"]["inspection_seconds"])
        started = time.monotonic()
        while time.monotonic()-started < duration:
            if handle.is_cancel_requested:
                handle.canceled()
                return result
            if self._closing.is_set() or time.monotonic()-started > goal.timeout_seconds:
                result.error_code, result.error_message = 3, "fixture observation timeout"
                handle.abort()
                return result
            feedback = self._inspect_type.Feedback()
            feedback.phase, feedback.message = "observing", "[软件测试数据] 正在生成观察结果"
            feedback.progress = min(1.0, (time.monotonic()-started)/duration)
            handle.publish_feedback(feedback)
            self._closing.wait(0.05)
        if handle.is_cancel_requested:
            handle.canceled()
            return result
        if not self.get_parameter("sensor_available").value:
            result.error_code, result.error_message = 2, "fixture sensor unavailable"
            handle.abort()
            return result
        payload = self._observation({"request_id": goal.request_id, "target": goal.target, "object_name": goal.object_name})
        result.outcome = {"found": 1, "not_found": 2, "inconclusive": 3, "observed": 4}[payload["outcome"]]
        now_ns = time.time_ns()
        result.observed_at.sec, result.observed_at.nanosec = divmod(now_ns, 1_000_000_000)
        result.summary = payload["summary"]
        observation = self._observation_type()
        observation.label, observation.sensor = goal.object_name or "scene", "software_fixture"
        observation.confidence = 0.0 if payload["outcome"] == "inconclusive" else 1.0
        observation.details_json = json.dumps(payload["evidence"], ensure_ascii=False)
        result.observations = [observation]
        handle.succeed()
        return result


def main():
    rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
    node = SoftwareFixture()
    signal.signal(signal.SIGTERM, lambda *_: node._closing.set())
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        while rclpy.ok() and not node._closing.is_set():
            executor.spin_once(timeout_sec=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        node._closing.set()
        executor.shutdown(timeout_sec=2)
        node._action.destroy()
        if node._inspect_action is not None:
            node._inspect_action.destroy()
        node._skill_action.destroy()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
