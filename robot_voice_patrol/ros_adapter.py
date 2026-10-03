"""ROS 2 navigation/inspection actions with conservative cancellation ownership.

Imports remain ROS-independent until construction. A transport error never
releases an unconfirmed action: late acknowledgements are cancelled, and no
new operation is sent until a terminal action result is known.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import threading
import time
import uuid
from typing import Any

from .contracts import ExecutionCancelled, ExecutionError, Feedback, Step


class RosExecutionError(ExecutionError):
    def __init__(self, code: str, message: str, *, retryable: bool = False,
                 stage: str = "", details: dict | None = None):
        super().__init__(message)
        self.code, self.retryable, self.stage = code, retryable, stage
        self.details = details or {}

    def to_dict(self):
        return {"code": self.code, "message": str(self), "retryable": self.retryable,
                "stage": self.stage, "details": self.details}


@dataclass
class _ActionOperation:
    target: str
    kind: str = "navigate"
    phase: str = "sending"
    goal_id: str | None = None
    abort: bool = False
    cancel_sent: bool = False
    cancel_response: str = "not_requested"
    handle: Any = None
    send_future: Any = None
    result_future: Any = None
    outcome: Any = None
    error: RosExecutionError | None = None
    started: float = field(default_factory=time.monotonic)
    done: threading.Event = field(default_factory=threading.Event)
    action_name: str = ""
    request_id: str | None = None


class Ros2Adapter:
    mode = "ros2"

    def __init__(self, config: dict, node: Any = None, *, start_executor: bool = True):
        try:
            import rclpy
            from rclpy.action import ActionClient
            from rclpy.callback_groups import ReentrantCallbackGroup
            from rclpy.executors import MultiThreadedExecutor
            from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
            from geometry_msgs.msg import PoseWithCovarianceStamped
            from nav2_msgs.action import NavigateToPose
            from std_msgs.msg import String
            from action_msgs.msg import GoalStatusArray
        except ImportError as exc:
            raise RuntimeError("ROS 模式需要已 source 的 ROS 2 Jazzy 环境及 nav2_msgs。") from exc
        self.config = config
        self._settings = config.get("ros", {})
        self._perception_backend = self._settings.get("perception_backend", "action")
        self._inspect_type = None
        if self._perception_backend == "action":
            try:
                from voice_patrol_interfaces.action import Inspect
                self._inspect_type = Inspect
            except ImportError as exc:
                raise RuntimeError("请先构建并 source voice_patrol_interfaces；兼容旧接口可设置 ros.perception_backend=json。") from exc
        self._rclpy = rclpy
        self._owns_context = not rclpy.ok()
        if self._owns_context:
            rclpy.init()
        self._owns_node = node is None
        self.node = node or rclpy.create_node("voice_patrol_adapter")
        self._goal_type, self._string_type = NavigateToPose, String
        self._lock = threading.RLock()
        self._execute_lock = threading.Lock()
        self._closed = False
        self._stop_generation = 0
        self._active: _ActionOperation | None = None
        self._pending_inspections: dict[str, dict] = {}
        self._pose = {"x": None, "y": None, "yaw": None}
        self._location = None
        self._pose_observed_at = None
        self._pose_received_monotonic = None
        self._distance_remaining = None
        self._executor_failure = None
        self._last_error = None
        self._error_counts = Counter()
        self._status_cache = {}
        self._status_subscriptions = []
        self._frame = self._settings.get("map_frame", "map")
        self._cancel_timeout = float(self._settings.get("cancel_timeout", 2.0))
        self._discovery_timeout = float(self._settings.get("service_discovery_timeout", 5.0))
        self._pose_stale_seconds = float(self._settings.get("pose_stale_seconds", 5.0))
        self._callback_group = ReentrantCallbackGroup()
        self._navigation = ActionClient(self.node, NavigateToPose,
            self._settings.get("navigation_action", "/navigate_to_pose"), callback_group=self._callback_group)
        self._inspection_client = None
        self._skill_client = self._capabilities_client = self._skill_type = self._capabilities_type = None
        self._capability_future = None
        self._capability_checked = self._capability_requested = 0.0
        self._advertised_skills = set()
        self._capabilities_simulated = False
        self._capabilities_provider = None
        try:
            from voice_patrol_interfaces.action import ExecuteSkill
            from voice_patrol_interfaces.srv import GetCapabilities
            self._skill_type, self._capabilities_type = ExecuteSkill, GetCapabilities
            self._skill_client = ActionClient(self.node, ExecuteSkill,
                self._settings.get("skill_action", "/voice_patrol/execute_skill"), callback_group=self._callback_group)
            self._capabilities_client = self.node.create_client(GetCapabilities,
                self._settings.get("capabilities_service", "/voice_patrol/get_capabilities"), callback_group=self._callback_group)
        except ImportError:
            # Older interface installations keep navigation/inspection usable;
            # new abilities remain unavailable until V3 interfaces are built.
            pass
        self._inspection_pub = self._inspection_sub = None
        if self._perception_backend == "action":
            self._inspection_client = ActionClient(self.node, self._inspect_type,
                self._settings.get("inspection_action", "/voice_patrol/inspect"), callback_group=self._callback_group)
        else:
            self._inspection_pub = self.node.create_publisher(String,
                self._settings.get("inspection_request_topic", "/voice_patrol/inspection/request"), 10)
            self._inspection_sub = self.node.create_subscription(String,
                self._settings.get("inspection_result_topic", "/voice_patrol/inspection/result"),
                self._on_inspection_result, 10, callback_group=self._callback_group)
        pose_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT,
                              durability=DurabilityPolicy.VOLATILE)
        self._pose_sub = self.node.create_subscription(PoseWithCovarianceStamped,
            self._settings.get("pose_topic", "/amcl_pose"), self._on_pose, pose_qos,
            callback_group=self._callback_group)
        status_qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE,
                                durability=DurabilityPolicy.TRANSIENT_LOCAL)
        for action_name in {self._settings.get("navigation_action", "/navigate_to_pose"),
                            self._settings.get("inspection_action", "/voice_patrol/inspect"),
                            self._settings.get("skill_action", "/voice_patrol/execute_skill")}:
            subscription = self.node.create_subscription(GoalStatusArray, action_name.rstrip("/") + "/_action/status",
                lambda msg, name=action_name: self._on_action_status(name, msg), status_qos,
                callback_group=self._callback_group)
            self._status_subscriptions.append(subscription)
        self._executor = self._spin_thread = None
        if start_executor:
            self._executor = MultiThreadedExecutor(num_threads=4, context=self.node.context)
            self._executor.add_node(self.node)
            self._spin_thread = threading.Thread(target=self._spin, daemon=True, name="ros2-executor")
            self._spin_thread.start()

    def _error(self, code, message, *, retryable=False, stage="", details=None):
        error = RosExecutionError(code, message, retryable=retryable, stage=stage, details=details)
        with self._lock:
            self._last_error = {**error.to_dict(), "at": datetime.now(timezone.utc).isoformat()}
            self._error_counts[code] += 1
        return error

    def _spin(self):
        try:
            self._executor.spin()
        except Exception as exc:
            if not self._closed:
                self._executor_failure = str(exc) or type(exc).__name__
                self.node.get_logger().error(f"ROS executor stopped: {self._executor_failure}")
                self.stop()
        finally:
            if not self._closed and self._executor_failure is None:
                self._executor_failure = "ROS executor exited unexpectedly"

    def _on_pose(self, msg):
        if msg.header.frame_id != self._frame:
            return
        p, q = msg.pose.pose.position, msg.pose.pose.orientation
        values = [p.x, p.y, q.x, q.y, q.z, q.w]
        norm = q.x*q.x + q.y*q.y + q.z*q.z + q.w*q.w
        if not all(math.isfinite(float(v)) for v in values) or not 0.99 <= norm <= 1.01:
            return
        yaw = math.atan2(2*(q.w*q.z + q.x*q.y), 1-2*(q.y*q.y + q.z*q.z))
        with self._lock:
            self._pose = {"x": p.x, "y": p.y, "yaw": yaw}
            self._pose_observed_at = datetime.now(timezone.utc).isoformat()
            self._pose_received_monotonic = time.monotonic()

    def _check(self, cancel, generation, deadline, *, stage="execution"):
        with self._lock:
            stopped = self._closed or generation != self._stop_generation
            executor_failure = self._executor_failure
        if executor_failure:
            raise self._error("ROS_EXECUTOR_FAILED", "ROS 执行器已停止，需要重新启动服务", stage=stage,
                              details={"reason": executor_failure})
        if stopped or cancel.is_set():
            raise ExecutionCancelled("任务已取消")
        if time.monotonic() >= deadline:
            codes = {"discovery": "SERVER_UNAVAILABLE", "goal_ack": "GOAL_ACK_TIMEOUT",
                     "inspection": "INSPECTION_TIMEOUT"}
            raise self._error(codes.get(stage, "EXECUTION_TIMEOUT"), f"执行超时（{stage}）",
                              retryable=stage == "discovery", stage=stage)

    def execute(self, step: Step, cancel: threading.Event, feedback: Feedback) -> dict:
        if not self._execute_lock.acquire(blocking=False):
            raise self._error("OPERATION_BUSY", "机器人正在执行另一项操作")
        try:
            with self._lock:
                generation = self._stop_generation
                if self._active is not None:
                    raise self._error("UNKNOWN_ACTION_STATE", "上一次操作尚未确认终止，拒绝执行重叠任务")
            deadline = time.monotonic() + max(0.001, float(step.timeout))
            self._check(cancel, generation, deadline)
            if step.kind == "navigate":
                return self._navigate(step, cancel, feedback, generation, deadline)
            if step.kind == "inspect":
                if self._perception_backend == "action":
                    return self._inspect_action(step, cancel, feedback, generation, deadline)
                return self._inspect_json(step, cancel, feedback, generation, deadline)
            if step.kind == "wait":
                until = time.monotonic() + step.seconds
                while time.monotonic() < until:
                    self._check(cancel, generation, deadline)
                    cancel.wait(min(0.05, max(0.001, until-time.monotonic())))
                self._check(cancel, generation, deadline)
                return {"kind": "wait", "seconds": step.seconds, "source": "ros2", "status": "succeeded",
                        "outcome": "succeeded", "message": f"已等待 {step.seconds:g} 秒"}
            if step.kind in {"capture", "dock", "follow", "turn", "home_control", "pick_object", "place_object", "handover_object"}:
                return self._external_skill(step, cancel, feedback, generation, deadline)
            raise self._error("UNSUPPORTED_SKILL", f"不支持的 ROS 技能：{step.kind}")
        finally:
            self._execute_lock.release()

    def _invoke_action(self, step, client, goal, cancel, feedback, generation, deadline):
        discovery_deadline = min(deadline, time.monotonic()+self._discovery_timeout)
        while not client.server_is_ready():
            self._check(cancel, generation, discovery_deadline, stage="discovery")
            cancel.wait(0.05)
        action_name = self._settings.get("navigation_action", "/navigate_to_pose") if step.kind == "navigate" else (
            self._settings.get("inspection_action", "/voice_patrol/inspect") if step.kind == "inspect" else
            self._settings.get("skill_action", "/voice_patrol/execute_skill"))
        lease = _ActionOperation(target=step.target, kind=step.kind, action_name=action_name,
                                 request_id=getattr(goal, "request_id", None))
        feedback({"kind": step.kind, "execution_ref": {"goal_id": None, "action_name": action_name,
                  "kind": step.kind, "request_id": lease.request_id, "step_id": step.step_id}})
        with self._lock:
            self._check(cancel, generation, deadline)
            self._active = lease
            if step.kind in {"navigate", "dock", "follow"}:
                self._location = None
            try:
                lease.send_future = client.send_goal_async(goal,
                    feedback_callback=lambda msg: self._on_action_feedback(lease, msg, feedback))
                lease.send_future.add_done_callback(lambda future: self._on_goal_response(lease, future))
            except Exception as exc:
                lease.abort = True
                lease.error = self._error("UNKNOWN_ACTION_STATE", f"目标请求发送状态不明：{exc}", stage="send")
                raise lease.error from exc
        try:
            reference_sent = False
            while not lease.done.wait(0.05):
                if lease.goal_id and not reference_sent:
                    feedback({"kind": step.kind, "execution_ref": {"goal_id": lease.goal_id, "action_name": action_name,
                              "kind": step.kind, "request_id": lease.request_id, "step_id": step.step_id}})
                    reference_sent = True
                if lease.error:
                    raise lease.error
                self._check(cancel, generation, deadline, stage="goal_ack" if lease.handle is None else step.kind)
            if lease.goal_id and not reference_sent:
                feedback({"kind": step.kind, "execution_ref": {"goal_id": lease.goal_id, "action_name": action_name,
                          "kind": step.kind, "request_id": lease.request_id, "step_id": step.step_id}})
            self._check(cancel, generation, deadline, stage=step.kind)
            if lease.error:
                raise lease.error
            if lease.outcome.status == 5:
                raise ExecutionCancelled("ROS Action 已确认取消")
            if lease.outcome.status != 4:
                result = lease.outcome.result
                retryable = step.kind in {"navigate", "inspect"} and not (step.kind == "inspect" and getattr(result, "error_code", None) == 1)
                raise self._error("ACTION_ABORTED", "ROS Action 执行失败", retryable=retryable,
                                  stage=step.kind, details={"status": lease.outcome.status,
                                  "error_code": getattr(result, "error_code", None),
                                  "error_message": getattr(result, "error_message", getattr(result, "error_msg", ""))})
            return lease.outcome.result
        except (ExecutionCancelled, ExecutionError) as exc:
            with self._lock:
                lease.abort = True
                lease.phase = "hardware_unknown" if getattr(lease.error, "code", None) == "UNKNOWN_HARDWARE_STATE" else "cancelling"
                self._request_cancel_locked(lease)
            if isinstance(exc, ExecutionCancelled) and not lease.done.wait(self._cancel_timeout):
                raise self._error("STOP_UNCONFIRMED", "停止/暂停未获 ROS Action 终态确认；禁止后续操作",
                                  stage="cancel", details={"goal_id": lease.goal_id}) from exc
            raise

    def _on_goal_response(self, lease, future):
        with self._lock:
            try:
                handle = future.result()
                if handle is None:
                    raise RuntimeError("empty goal response")
                lease.handle = handle
                if not handle.accepted:
                    lease.error = self._error("GOAL_REJECTED", "ROS Action 拒绝了目标", stage=lease.kind)
                    self._finish_locked(lease)
                    return
                lease.phase = "executing"
                if hasattr(handle, "goal_id"):
                    lease.goal_id = bytes(handle.goal_id.uuid).hex()
                lease.result_future = handle.get_result_async()
                lease.result_future.add_done_callback(lambda result: self._on_action_result(lease, result))
                if lease.abort or self._closed:
                    self._request_cancel_locked(lease)
            except Exception as exc:
                lease.error = self._error("UNKNOWN_ACTION_STATE", f"无法确认目标请求状态：{exc}", stage="goal_ack")
                lease.abort = True
                self._request_cancel_locked(lease)

    def _on_action_result(self, lease, future):
        with self._lock:
            try:
                result = future.result()
                if result is None or result.status not in (4, 5, 6):
                    raise RuntimeError("result lacks terminal action status")
                lease.outcome = result
                # A gateway can abort the ROS request after losing contact with
                # a device. The ROS terminal state is not a physical stop ack.
                # Preserve global adapter ownership so even Nav2 cannot start.
                if lease.kind not in {"navigate", "inspect"}:
                    raw = getattr(result.result, "evidence_json", "")
                    evidence = json.loads(raw) if isinstance(raw, str) and 0 < len(raw) <= 65536 else {}
                    if isinstance(evidence, dict) and evidence.get("gateway_terminal_confirmed") is False:
                        lease.error = self._error("UNKNOWN_HARDWARE_STATE", "硬件网关未确认物理终态；包括导航在内的后续机械操作已锁止，需人工核实",
                                                  stage=lease.kind, details={"goal_id": lease.goal_id,
                                                  "request_id": lease.request_id, "ros_status": result.status})
                        lease.abort = True
                        lease.phase = "hardware_unknown"
                        return
                self._finish_locked(lease)
            except Exception as exc:
                lease.error = self._error("UNKNOWN_ACTION_STATE", f"无法确认 Action 终止状态：{exc}", stage="result")
                lease.abort = True
                self._request_cancel_locked(lease)

    def _finish_locked(self, lease):
        lease.phase = "terminal"
        if self._active is lease:
            self._active = None
            self._distance_remaining = None
        lease.done.set()

    def _request_cancel_locked(self, lease):
        if lease.handle is None or not lease.handle.accepted or lease.cancel_sent or lease.done.is_set():
            return
        try:
            future = lease.handle.cancel_goal_async()
            lease.cancel_sent = True
            lease.cancel_response = "pending"
            future.add_done_callback(lambda reply: self._on_cancel_reply(lease, reply))
        except Exception as exc:
            lease.error = self._error("CANCEL_SEND_FAILED", f"取消请求发送失败：{exc}", stage="cancel")

    def _on_cancel_reply(self, lease, future):
        with self._lock:
            try:
                reply = future.result()
                lease.cancel_response = "accepted" if reply and reply.goals_canceling else "rejected"
            except Exception:
                lease.cancel_response = "transport_error"
            # This service acknowledgement never releases action ownership.

    def _on_action_feedback(self, lease, msg, callback):
        with self._lock:
            if self._active is not lease or lease.abort:
                return
            if lease.kind == "navigate":
                remaining = float(msg.feedback.distance_remaining)
                if not math.isfinite(remaining):
                    return
                self._distance_remaining = remaining
                value = {"kind": "navigate", "target": lease.target, "distance_remaining": round(remaining, 2)}
            else:
                progress = float(msg.feedback.progress)
                value = {"kind": lease.kind, "target": lease.target, "phase": msg.feedback.phase,
                         "progress": max(0.0, min(1.0, progress)) if math.isfinite(progress) else 0.0,
                         "message": msg.feedback.message}
        callback(value)

    def _on_action_status(self, action_name, msg):
        with self._lock:
            for entry in msg.status_list:
                try:
                    identifier = bytes(entry.goal_info.goal_id.uuid).hex()
                    if len(identifier) == 32 and entry.status in range(1, 7):
                        self._status_cache[(action_name, identifier)] = {"status": int(entry.status), "received": time.monotonic()}
                except (TypeError, ValueError, AttributeError):
                    continue
            if len(self._status_cache) > 2000:
                oldest = sorted(self._status_cache, key=lambda key: self._status_cache[key]["received"])
                for key in oldest[:-1000]:
                    self._status_cache.pop(key, None)

    def reconcile_mission(self, mission):
        with self._lock:
            active = self._active
            if active is not None and getattr(active.error, "code", None) == "UNKNOWN_HARDWARE_STATE":
                return {"verified": False, "remote_state": "unknown", "reason": "网关明确报告物理终态未知，不能凭 ROS 成功/取消/失败状态解除互锁"}
        refs = list(mission.get("execution_refs") or [])
        ref = (mission.get("feedback") or {}).get("execution_ref")
        if isinstance(ref, dict):
            refs.append(ref)
        if mission.get("mode") != "ros2" or not refs:
            return {"verified": False, "remote_state": "unknown", "reason": "历史任务缺少可核对的 ROS Goal UUID"}
        external_steps = {s.get("step_id") for s in mission.get("steps", [])
                          if s.get("kind") in {"navigate", "inspect", "capture", "dock", "follow", "turn", "home_control", "pick_object", "place_object", "handover_object"}}
        referenced = {r.get("step_id") for r in refs if isinstance(r, dict) and r.get("goal_id")}
        unresolved = {s.get("step_id") for s in mission.get("step_states", [])
                      if s.get("status") in {"running", "retrying", "interrupted"}} & external_steps
        if unresolved - referenced:
            return {"verified": False, "remote_state": "unknown", "reason": "未完成步骤缺少可关联的 Goal UUID，不能凭其他步骤终态恢复"}
        checks = []
        with self._lock:
            for ref in refs:
                if not isinstance(ref, dict):
                    continue
                cached = getattr(self, "_status_cache", {}).get((ref.get("action_name"), ref.get("goal_id")))
                if cached is None:
                    return {"verified": False, "remote_state": "unknown", "reason": "未收到旧目标的 ROS 状态，不能证明已经终止"}
                checks.append({"goal_id": ref.get("goal_id"), "status": cached["status"]})
                if cached["status"] not in {4, 5, 6}:
                    return {"verified": False, "remote_state": "active", "reason": "旧目标仍未达到终态", "checks": checks}
        return {"verified": bool(checks), "remote_state": "terminal" if checks else "unknown",
                "reason": "已收到所记录目标的成功、取消或失败终态" if checks else "没有有效目标引用", "checks": checks}

    def _refresh_capabilities(self):
        client, action = getattr(self, "_capabilities_client", None), getattr(self, "_skill_client", None)
        if client is None or action is None:
            return
        with self._lock:
            now = time.monotonic()
            if self._closed or not client.service_is_ready() or not action.server_is_ready():
                self._advertised_skills = set()
                return
            if self._capability_future is not None and now - self._capability_requested < 3:
                return
            if self._capability_future is None and now - self._capability_checked < 1:
                return
            try:
                future = client.call_async(self._capabilities_type.Request())
                self._capability_future, self._capability_requested = future, now
                future.add_done_callback(self._on_capabilities)
            except Exception:
                self._advertised_skills = set()
                self._capability_future = None

    def _on_capabilities(self, future):
        with self._lock:
            if self._closed or future is not self._capability_future:
                return
            self._capability_future = None
            self._capability_checked = time.monotonic()
            try:
                response = future.result()
                if response.protocol_version not in {"3", "5"} or len(response.skills) > 32:
                    raise ValueError("unsupported capabilities protocol")
                supported = {"capture", "dock", "follow", "turn"}
                if response.protocol_version == "5":
                    supported.update({"home_control", "pick_object", "place_object", "handover_object"})
                self._advertised_skills = set(response.skills) & supported
                self._capabilities_simulated = bool(response.simulated)
                self._capabilities_provider = str(response.provider)[:200]
            except Exception:
                self._advertised_skills = set()

    def capabilities(self):
        try:
            self._refresh_capabilities()
        except Exception:
            with self._lock:
                self._advertised_skills = set()
        navigation, inspection = self._readiness()
        with self._lock:
            fresh = time.monotonic() - getattr(self, "_capability_checked", 0) <= 5
            blocked = self._closed or bool(self._executor_failure) or bool(self._active and getattr(self._active.error, "code", None) == "UNKNOWN_HARDWARE_STATE")
            result = {"navigate": {"available": navigation and not blocked},
                      "inspect": {"available": inspection and not blocked}, "wait": {"available": not blocked}}
            for name in ("capture", "dock", "follow", "turn", "home_control", "pick_object", "place_object", "handover_object"):
                result[name] = {"available": fresh and name in getattr(self, "_advertised_skills", set()) and not blocked,
                                "simulated": getattr(self, "_capabilities_simulated", False),
                                "provider": getattr(self, "_capabilities_provider", None)}
            return result

    def _external_skill(self, step, cancel, feedback, generation, deadline):
        if not self.capabilities().get(step.kind, {}).get("available"):
            raise self._error("CAPABILITY_UNAVAILABLE", "外部技能未连接或能力声明已失效", stage=step.kind)
        if step.kind == "capture" and step.target is not None and self._location != step.target:
            raise self._error("LOCATION_UNCONFIRMED", "尚未确认到达拍照地点，请先导航", stage=step.kind)
        if step.kind in {"pick_object", "place_object", "handover_object"} and self._location != step.target:
            raise self._error("LOCATION_UNCONFIRMED", "尚未确认到达取放或交接地点，请先导航", stage=step.kind)
        requested_at = datetime.now(timezone.utc)
        goal = self._skill_type.Goal()
        from .home_skills import SKILL_CODES, HOME_SKILLS
        goal.request_id, goal.skill = uuid.uuid4().hex, SKILL_CODES[step.kind]
        goal.target, goal.timeout_seconds = step.target or "", float(step.timeout)
        for name, default in (("camera", ""), ("subject", ""), ("duration_seconds", 0.0),
                              ("distance_meters", 0.0), ("angle_degrees", 0.0)):
            value = step.params.get(name, default)
            setattr(goal, name, float(value) if isinstance(default, float) else value)
        goal.image_format = step.params.get("format", "")
        if step.kind in HOME_SKILLS:
            if not hasattr(goal, "parameters_json"):
                raise self._error("INTERFACE_VERSION_MISMATCH", "请重新构建 V5 ExecuteSkill 接口", stage=step.kind)
            goal.parameters_json = json.dumps(step.params, ensure_ascii=False, allow_nan=False)
        result = self._invoke_action(step, self._skill_client, goal, cancel, feedback, generation, deadline)
        try:
            if result.request_id != goal.request_id or int(result.skill) != goal.skill:
                raise ValueError("request/skill identity mismatch")
            if not result.success or result.error_code != 0:
                raise self._error("SKILL_FAILED", str(result.error_message)[:1000] or "外部技能执行失败", stage=step.kind,
                                  details={"interface_code": result.error_code})
            if not isinstance(result.evidence_json, str) or not 1 <= len(result.evidence_json) <= 65536:
                raise ValueError("missing or oversized evidence")
            evidence = json.loads(result.evidence_json)
            if not isinstance(evidence, dict) or not evidence:
                raise ValueError("evidence must be a nonempty object")
            json.dumps(evidence, allow_nan=False)
            observed_at = datetime.fromtimestamp(result.observed_at.sec + result.observed_at.nanosec / 1e9, timezone.utc)
            age = (datetime.now(timezone.utc) - observed_at).total_seconds()
            if observed_at < requested_at or not -5 <= age <= step.timeout + 5:
                raise ValueError("stale skill evidence")
            if step.kind == "capture" and (not isinstance(result.media_uri, str) or not result.media_uri or len(result.media_uri) > 2000):
                raise ValueError("capture result must identify produced media")
            if result.media_uri:
                evidence["media_uri"] = result.media_uri
        except (ValueError, TypeError, AttributeError, OverflowError, RecursionError) as exc:
            raise self._error("INVALID_SKILL_RESULT", f"外部技能结果无效：{exc}", stage=step.kind) from exc
        if step.kind in HOME_SKILLS:
            from .home_skills import validate_evidence
            validate_evidence(step, evidence)
        if step.kind == "dock":
            with self._lock:
                self._location = step.target
        simulated = bool(result.simulated or self._capabilities_simulated)
        return {"kind": step.kind, "target": step.target, "status": "succeeded", "outcome": "succeeded",
                "simulated": simulated, "source": "ros_fixture" if simulated else "ros2_skill",
                "evidence": evidence, "observed_at": observed_at.isoformat(),
                "message": f"{'[软件测试数据] ' if simulated else ''}{step.kind} 接口已返回成功终态"}

    def _navigate(self, step, cancel, feedback, generation, deadline):
        if step.target not in self.config["locations"]:
            raise self._error("UNKNOWN_LOCATION", f"未知地点：{step.target}")
        place = self.config["locations"][step.target]
        goal = self._goal_type.Goal()
        goal.pose.header.frame_id = self._frame
        goal.pose.header.stamp = self.node.get_clock().now().to_msg()
        goal.pose.pose.position.x, goal.pose.pose.position.y = float(place["x"]), float(place["y"])
        goal.pose.pose.orientation.z = math.sin(float(place.get("yaw", 0))/2)
        goal.pose.pose.orientation.w = math.cos(float(place.get("yaw", 0))/2)
        result = self._invoke_action(step, self._navigation, goal, cancel, feedback, generation, deadline)
        if int(getattr(result, "error_code", 0)) != 0:
            raise self._error("NAVIGATION_FAILED", f"Nav2 导航失败：{getattr(result, 'error_msg', '')}",
                              retryable=True, stage="navigate", details={"nav2_code": result.error_code})
        with self._lock:
            self._location = step.target
        simulated = getattr(result, "error_msg", "") == "VOICE_PATROL_SOFTWARE_FIXTURE"
        return {"kind": "navigate", "target": step.target, "source": "ros_fixture" if simulated else "nav2",
                "simulated": simulated, "status": "succeeded", "outcome": "succeeded",
                "message": f"{'[软件测试数据] ' if simulated else ''}已到达{place.get('label', step.target)}"}

    def _inspect_action(self, step, cancel, feedback, generation, deadline):
        requested_at = datetime.now(timezone.utc)
        goal = self._inspect_type.Goal()
        goal.request_id = str(uuid.uuid4())
        goal.target, goal.object_name = step.target, step.object_name
        goal.timeout_seconds = float(max(0.001, deadline-time.monotonic()))
        result = self._invoke_action(step, self._inspection_client, goal, cancel, feedback, generation, deadline)
        if result.error_code != 0:
            raise self._error("INSPECTION_FAILED", result.error_message or "感知 Action 报告失败",
                              retryable=result.error_code in (2, 3, 4), stage="inspect",
                              details={"inspection_code": result.error_code})
        try:
            evidence = []
            for observation in result.observations:
                confidence = float(observation.confidence)
                bounds = list(observation.bbox_xywh)
                if (not math.isfinite(confidence) or not 0 <= confidence <= 1 or len(bounds) != 4 or
                    not all(math.isfinite(v) for v in bounds) or bounds[2] < 0 or bounds[3] < 0):
                    raise ValueError("invalid confidence/bounding box")
                details = self._decode_json(observation.details_json) if observation.details_json else {}
                if not isinstance(details, dict):
                    raise ValueError("observation metadata must be a JSON object")
                evidence.append({"label": observation.label, "confidence": confidence, "sensor": observation.sensor,
                                 "media_uri": observation.media_uri, "bbox_xywh": bounds, "details": details})
            outcomes = {1: "found", 2: "not_found", 3: "inconclusive", 4: "observed"}
            payload = {"request_id": result.request_id, "target": result.target, "object_name": result.object_name,
                       "success": True, "outcome": outcomes[result.outcome], "summary": result.summary,
                       "observed_at": datetime.fromtimestamp(result.observed_at.sec + result.observed_at.nanosec/1e9,
                                                              timezone.utc).isoformat(),
                       "evidence": evidence, "simulated": result.simulated}
            checked = self._validate_observation(payload, goal.request_id, step.target, step.object_name, requested_at)
        except (KeyError, ValueError, TypeError, OverflowError, RecursionError) as exc:
            raise self._error("INVALID_OBSERVATION", f"感知结果无效：{exc}", stage="inspect") from exc
        return self._observation_result(checked, "ros2_inspection_action")

    @staticmethod
    def _decode_json(text):
        def reject_nonfinite(value):
            raise ValueError(f"non-finite JSON value: {value}")
        if not isinstance(text, str) or len(text.encode("utf-8")) > 262144:
            raise ValueError("JSON response is too large or not text")
        return json.loads(text, parse_constant=reject_nonfinite)

    def _validate_observation(self, result, request_id, target, object_name, requested_at):
        if result.get("request_id") != request_id or result.get("target") != target or result.get("object_name", "") != object_name:
            raise ValueError("request/target/object correlation mismatch")
        if result.get("success") is not True:
            raise ValueError(result.get("error", "perception failed"))
        observed = datetime.fromisoformat(str(result["observed_at"]).replace("Z", "+00:00"))
        if observed.tzinfo is None or (observed-requested_at).total_seconds() < -5 or (observed-datetime.now(timezone.utc)).total_seconds() > 5:
            raise ValueError("observation is stale, future-dated or lacks timezone")
        if not isinstance(result.get("evidence"), (dict, list)) or not result["evidence"]:
            raise ValueError("non-empty observation evidence is required")
        if "simulated" in result and not isinstance(result["simulated"], bool):
            raise ValueError("simulated must be boolean")
        outcome = result.get("outcome")
        if outcome is None:
            if object_name and isinstance(result.get("found"), bool):
                outcome = "found" if result["found"] else "not_found"
            elif not object_name:
                outcome = "observed"
        allowed = {"found", "not_found", "inconclusive"} if object_name else {"observed", "inconclusive"}
        if outcome not in allowed:
            raise ValueError("invalid or missing observation outcome")
        found = True if outcome == "found" else False if outcome == "not_found" else None
        if "found" in result and (not isinstance(result["found"], (bool, type(None))) or result["found"] is not found):
            raise ValueError("found contradicts outcome")
        return {"request_id": request_id, "target": target, "object_name": object_name, "outcome": outcome,
                "found": found, "observed_at": observed.isoformat(), "evidence": result["evidence"],
                "summary": result.get("summary", ""), "simulated": result.get("simulated", False)}

    @staticmethod
    def _observation_result(result, source):
        messages = {"found": "检测到目标物体", "not_found": "本次观察未检测到目标物体",
                    "inconclusive": "当前证据不足，无法确定", "observed": "巡检观察已完成"}
        summary = result.get("summary")
        message = summary if isinstance(summary, str) and summary.strip() else messages[result["outcome"]]
        return {"kind": "inspect", "source": source, "status": "succeeded", "message": message, **result}

    def _inspect_json(self, step, cancel, feedback, generation, deadline):
        request_id, requested_at = str(uuid.uuid4()), datetime.now(timezone.utc)
        pending = {"event": threading.Event(), "result": None, "error": "", "target": step.target,
                   "object_name": step.object_name, "requested_at": requested_at}
        with self._lock:
            self._pending_inspections[request_id] = pending
        try:
            self._check(cancel, generation, deadline)
            payload = {"request_id": request_id, "target": step.target, "object_name": step.object_name,
                       "requested_at": requested_at.isoformat(), "timeout_seconds": max(0, deadline-time.monotonic())}
            self._inspection_pub.publish(self._string_type(data=json.dumps(payload, ensure_ascii=False)))
            feedback({"kind": "inspect", "target": step.target, "message": "等待外部感知节点返回证据"})
            while not pending["event"].wait(0.05):
                self._check(cancel, generation, deadline, stage="inspection")
            self._check(cancel, generation, deadline, stage="inspection")
            if pending["error"]:
                raise self._error("INVALID_OBSERVATION", pending["error"], stage="inspection")
            return self._observation_result(pending["result"], "ros2_perception")
        finally:
            with self._lock:
                self._pending_inspections.pop(request_id, None)

    def _on_inspection_result(self, msg):
        try:
            result = self._decode_json(msg.data)
            if not isinstance(result, dict) or not isinstance(result.get("request_id"), str):
                return
        except (ValueError, TypeError, RecursionError):
            return
        with self._lock:
            pending = self._pending_inspections.get(result["request_id"])
            if pending is None or pending["event"].is_set():
                return
            if result.get("target") != pending["target"] or result.get("object_name", "") != pending["object_name"]:
                return
            try:
                pending["result"] = self._validate_observation(result, result["request_id"], pending["target"],
                                                              pending["object_name"], pending["requested_at"])
            except (KeyError, TypeError, ValueError) as exc:
                pending["error"] = f"感知结果无效：{exc}"
            pending["event"].set()

    def stop(self):
        with self._lock:
            self._stop_generation += 1
            if self._active is not None:
                self._active.abort = True
                self._active.phase = "cancelling"
                self._request_cancel_locked(self._active)

    def _readiness(self):
        try:
            navigation = bool(self._navigation.server_is_ready())
            if self._perception_backend == "action":
                inspection = bool(self._inspection_client and self._inspection_client.server_is_ready())
            else:
                request = self._settings.get("inspection_request_topic", "/voice_patrol/inspection/request")
                result = self._settings.get("inspection_result_topic", "/voice_patrol/inspection/result")
                inspection = self.node.count_subscribers(request) > 0 and self.node.count_publishers(result) > 0
            return navigation, inspection
        except Exception:
            return False, False

    def snapshot(self):
        navigation_ready, inspection_ready = self._readiness()
        capabilities = self.capabilities()
        with self._lock:
            active = self._active
            pose_age = time.monotonic()-self._pose_received_monotonic if self._pose_received_monotonic is not None else None
            pose_valid = pose_age is not None and pose_age <= self._pose_stale_seconds
            blocked = bool(self._executor_failure or (active and (active.abort or active.error)))
            health = {"status": "closed" if self._closed else "error" if blocked else "ready" if
                      navigation_ready and inspection_ready and pose_valid else "degraded",
                      "executor_alive": not self._closed and not bool(self._executor_failure),
                      "navigation_ready": navigation_ready, "inspection_ready": inspection_ready,
                      "perception_backend": self._perception_backend, "pose_valid": pose_valid,
                      "pose_age_seconds": round(pose_age, 2) if pose_age is not None else None,
                      "blocked": blocked, "executor_error": self._executor_failure,
                      "last_error": self._last_error, "error_counts": dict(self._error_counts)}
            return {"mode": self.mode, "location": self._location, **self._pose, "health": health,
                    "hardware_uncertain": bool(active and getattr(active.error, "code", None) == "UNKNOWN_HARDWARE_STATE"),
                    "pose_valid": pose_valid, "pose_observed_at": self._pose_observed_at, "pose_frame": self._frame,
                    "distance_remaining": self._distance_remaining, "navigation_pending": bool(active and active.kind == "navigate"),
                    "action_pending": active is not None, "cancellation_pending": bool(active and active.abort),
                    "navigation_error": str(active.error) if active and active.error else None,
                    "inspection_pending": len(self._pending_inspections)+int(bool(active and active.kind == "inspect")),
                    "action": {"kind": active.kind, "phase": active.phase, "goal_id": active.goal_id,
                               "cancel_response": active.cancel_response, "elapsed_seconds": round(time.monotonic()-active.started, 2)} if active else None,
                    "capabilities": capabilities, "closed": self._closed}

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.stop()
        deadline = time.monotonic()+self._cancel_timeout
        while time.monotonic() < deadline:
            with self._lock:
                active = self._active
            if active is None:
                break
            active.done.wait(0.05)
        if self._active is not None:
            self.node.get_logger().error("关闭时仍无法确认 Action 已终止；恢复连接并确认旧目标终止后再重启。")
        if self._executor is not None:
            self._executor.shutdown(timeout_sec=2.0, wait_for_threads=False)
        if self._spin_thread is not None:
            self._spin_thread.join(timeout=2.0)
        self._navigation.destroy()
        if self._inspection_client is not None:
            self._inspection_client.destroy()
        if getattr(self, "_skill_client", None) is not None:
            self._skill_client.destroy()
        if getattr(self, "_capabilities_client", None) is not None:
            self.node.destroy_client(self._capabilities_client)
        for name in ("_pose_sub", "_inspection_sub"):
            entity = getattr(self, name, None)
            if entity is not None:
                self.node.destroy_subscription(entity)
        for entity in getattr(self, "_status_subscriptions", []):
            self.node.destroy_subscription(entity)
        if self._inspection_pub is not None:
            self.node.destroy_publisher(self._inspection_pub)
        if self._owns_node:
            self.node.destroy_node()
        if self._owns_context and self._rclpy.ok():
            self._rclpy.shutdown()
