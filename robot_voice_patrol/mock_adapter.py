"""Deterministic interface simulator; this is NOT a physics or perception model."""
from __future__ import annotations

import threading
import time
import math
import uuid
from datetime import datetime, timezone
from typing import Any

from .contracts import ExecutionCancelled, ExecutionError, Feedback, Step


class MockExecutionError(ExecutionError):
    def __init__(self, code, message, *, retryable=False):
        super().__init__(message)
        self.code, self.retryable = code, retryable


from .home_fixture import HomeFixtureMixin
from .home_skills import HOME_SKILLS


class MockAdapter(HomeFixtureMixin):
    mode = "mock"

    def __init__(self, config: dict[str, Any], *, fixture_skills=False):
        self.config = config
        self.fixture_skills = bool(fixture_skills)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._closed = False
        home = config["locations"]["home"]
        self._pose = {"location": "home", "x": home["x"], "y": home["y"],
                      "yaw": home.get("yaw", 0), "pose_valid": True, "simulated": True}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {**self._pose, **self.home_snapshot(), "capabilities": self.capabilities(), "fixture_skills": self.fixture_skills}

    def capabilities(self):
        return {name: {"available": not self._closed and (name in {"navigate", "inspect", "wait"} or self.fixture_skills),
                       "simulated": True, "source": "software_fixture"}
                for name in ("navigate", "inspect", "wait", "capture", "dock", "follow", "turn", *sorted(HOME_SKILLS))}

    def reconcile_mission(self, mission):
        verified = mission.get("mode") == "mock"
        return {"verified": verified, "remote_state": "none" if verified else "unknown",
                "reason": "软件预设任务没有外部硬件动作" if verified else "不能用软件适配器确认 ROS 任务的远端状态"}

    def reconfigure(self, config, reset=False):
        with self._lock:
            self.config = config
            if hasattr(self, "_home_objects"):
                del self._home_objects
            location = "home" if reset else self._pose.get("location")
            target = config["locations"].get(location)
            if reset:
                self._pose.update(location="home", **{k: target.get(k, 0.0) for k in ("x", "y", "yaw")})
            elif target is None or any(abs(self._pose[k] - target.get(k, 0)) > .01 for k in ("x", "y")):
                self._pose["location"] = None

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        self._closed = True
        self.stop()

    def execute(self, step: Step, cancel: threading.Event, feedback: Feedback) -> dict[str, Any]:
        if self._closed:
            raise ExecutionError("模拟接口已关闭")
        if cancel.is_set():
            raise ExecutionCancelled("任务已中断")
        if step.kind in HOME_SKILLS:
            return self.execute_home_fixture(step, cancel, feedback)
        self._stop.clear()
        settings = self.config.get("mock", {})
        if step.kind == "navigate":
            if step.target not in self.config["locations"]:
                raise ExecutionError("导航地点不存在")
            duration = float(settings.get("travel_seconds", 1.5))
        elif step.kind == "inspect":
            if self.snapshot()["location"] != step.target:
                raise ExecutionError("尚未到达巡检地点")
            duration = float(settings.get("inspection_seconds", .6))
        elif step.kind == "wait":
            duration = step.seconds
        elif step.kind in {"capture", "dock", "follow", "turn"}:
            if not self.fixture_skills:
                raise ExecutionError("外部技能接口未连接；软件测试端点需显式开启 fixture_skills")
            if step.kind == "dock" and step.target not in self.config["locations"]:
                raise ExecutionError("回充地点不存在")
            if step.kind == "capture" and step.target is not None and self.snapshot()["location"] != step.target:
                raise MockExecutionError("LOCATION_UNCONFIRMED", "尚未确认到达拍照地点，请先导航")
            duration = step.params.get("duration_seconds", min(.2, step.timeout / 2))
        else:
            raise ExecutionError(f"不支持的技能: {step.kind}")
        started = time.monotonic()
        origin = self.snapshot()
        goal = self.config["locations"].get(step.target, {})
        if step.kind in {"navigate", "dock"}:
            with self._lock:
                self._pose["location"] = None
                self._pose["charging"] = False
        elif step.kind == "follow":
            with self._lock:
                self._pose["location"] = None
        while True:
            if cancel.is_set() or self._stop.is_set():
                raise ExecutionCancelled("模拟接口已确认停止")
            elapsed = time.monotonic() - started
            if elapsed > step.timeout:
                raise MockExecutionError("EXECUTION_TIMEOUT", f"{step.kind} 超时（{step.timeout:g} 秒）", retryable=True)
            fraction = min(elapsed / max(duration, .001), 1)
            if step.kind in {"navigate", "dock"}:
                with self._lock:
                    self._pose.update({key: origin[key] + (float(goal.get(key, 0)) - origin[key]) * fraction
                                       for key in ("x", "y", "yaw")})
            feedback({"progress": round(fraction, 3), "simulated": True})
            if fraction >= 1:
                break
            cancel.wait(min(.04, max(duration - elapsed, .001)))
        observed_at = datetime.now(timezone.utc).isoformat()
        if step.kind in {"capture", "dock", "follow", "turn"}:
            with self._lock:
                if step.kind == "dock":
                    self._pose.update(location=step.target, charging=True)
                elif step.kind == "turn":
                    self._pose["yaw"] = math.atan2(math.sin(self._pose["yaw"] + math.radians(step.params["angle_degrees"])),
                                                   math.cos(self._pose["yaw"] + math.radians(step.params["angle_degrees"])))
            evidence = {"fixture": "explicit_software_skill", "parameters": dict(step.params), "observed_at": observed_at}
            if step.kind == "capture":
                evidence["media_uri"] = f"mock://frames/{uuid.uuid4().hex}.{step.params.get('format', 'jpeg')}"
                evidence["image_generated"] = False
            return {"kind": step.kind, "target": step.target, "status": "succeeded", "outcome": "succeeded",
                    "source": "software_fixture", "simulated": True, "observed_at": observed_at,
                    "evidence": evidence, "message": f"[软件测试数据] {step.kind} 接口执行完成"}
        if step.kind == "navigate":
            with self._lock:
                self._pose["location"] = step.target
            message = f"已模拟到达{goal['label']}"
            return {"kind": step.kind, "target": step.target, "status": "succeeded",
                    "message": message, "simulated": True}
        if step.kind == "inspect":
            objects = list(self.home_objects().get(step.target, []))
            found = step.object_name in objects if step.object_name else None
            if step.object_name:
                detail = f"发现{step.object_name}" if found else f"本次模拟观测未发现{step.object_name}"
            else:
                detail = "模拟观测到：" + ("、".join(objects) or "无预设目标")
            return {"kind": step.kind, "target": step.target, "status": "succeeded",
                    "object_name": step.object_name, "found": found, "objects": objects,
                    "observed_at": observed_at, "evidence": "config.mock.objects（预设数据）",
                    "message": detail, "simulated": True}
        return {"kind": "wait", "status": "succeeded", "message": f"等待 {step.seconds:g} 秒完成",
                "simulated": True}
