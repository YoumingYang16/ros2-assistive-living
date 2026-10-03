"""Explicit software-only household endpoints; no actual device control."""
from __future__ import annotations
from datetime import datetime, timezone
import uuid
import copy
from .contracts import ExecutionCancelled
from .skills import SkillExecutionError


class HomeFixtureMixin:
    def home_objects(self):
        if not hasattr(self, "_home_objects"):
            self._home_objects = copy.deepcopy(self.config["mock"].get("objects", {}))
        return self._home_objects

    def home_snapshot(self):
        return {"held_payload": getattr(self, "_held_payload", None),
                "home_devices": dict(getattr(self, "_home_devices", {}))}

    def execute_home_fixture(self, step, cancel, feedback):
        if not self.fixture_skills:
            raise SkillExecutionError("CAPABILITY_UNAVAILABLE", "生活辅助软件端点未启用")
        with self._lock:
            if cancel.is_set() or self._closed:
                raise ExecutionCancelled("生活辅助接口已停止")
            self._stop.clear()
            if step.kind != "home_control" and self._pose.get("location") != step.target:
                raise SkillExecutionError("LOCATION_UNCONFIRMED", "尚未到达操作地点")
            params = step.params
            evidence = {"target": step.target, "fixture": "explicit_household_software"}
            item = params.get("item")
            held = getattr(self, "_held_payload", None)
            fault = getattr(self, "home_fault", None)
            if step.kind == "home_control":
                if fault == "device_offline":
                    raise SkillExecutionError("DEVICE_OFFLINE", "软件场景：设备离线，未确认状态")
                if not hasattr(self, "_home_devices"):
                    self._home_devices = {}
                self._home_devices[f"{step.target}:{params['device']}"] = params["state"]
                evidence.update(device=params["device"], reported_state=params["state"], readback_confirmed=True)
            elif step.kind == "pick_object":
                if held or fault == "grasp_failed":
                    raise SkillExecutionError("PAYLOAD_UNAVAILABLE", "载荷已占用或抓取未确认")
                if item not in self.home_objects().get(step.target, []):
                    raise SkillExecutionError("OBJECT_UNCONFIRMED", "预设物品不在此处")
                self._held_payload = item
                self.home_objects()[step.target].remove(item)
                evidence.update(item=item, payload_confirmed=True)
            else:
                if held != item:
                    raise SkillExecutionError("PAYLOAD_UNCONFIRMED", "持有物品与请求不一致")
                if fault == "recipient_absent" or fault == "handover_rejected":
                    raise SkillExecutionError("RECIPIENT_UNCONFIRMED", "接收人未确认，载荷保留，等待人工处理")
                if step.kind == "handover_object":
                    evidence.update(recipient=params["recipient"], recipient_acknowledged=True,
                                    receipt_id="fixture-" + uuid.uuid4().hex)
                else:
                    evidence.update(surface=params["surface"], surface_confirmed=True)
                evidence.update(item=item, released=True)
                self._held_payload = None
                self.home_objects().setdefault(step.target, []).append(item)
            observed_at = datetime.now(timezone.utc).isoformat()
            feedback({"kind": step.kind, "simulated": True, "evidence": evidence})
            return {"kind": step.kind, "target": step.target, "status": "succeeded", "outcome": "succeeded",
                    "simulated": True, "source": "software_fixture", "evidence": evidence,
                    "observed_at": observed_at, "message": "[软件预设] 家居接口返回完成，未执行任何真实物理动作"}
