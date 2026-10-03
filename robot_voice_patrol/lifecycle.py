"""ROS-independent managed lifecycle policy used by HTTP and ROS nodes."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import threading

from .contracts import CommandError


class LifecycleController:
    TRANSITIONS = {"configure": ("unconfigured", "inactive"), "activate": ("inactive", "active"),
                   "deactivate": ("active", "inactive"), "cleanup": ("inactive", "unconfigured"),
                   "reset_error": ("error", "unconfigured")}

    def __init__(self, adapter=None, *, busy=None, stop=None, initial_state="active", hooks=None):
        if initial_state not in {"unconfigured", "inactive", "active", "error", "finalized"}:
            raise ValueError("invalid lifecycle state")
        self.adapter, self._busy, self._stop = adapter, busy, stop
        self._state, self._last_error = initial_state, None
        self._hooks = hooks or {}
        self._lock = threading.RLock()
        self._history = deque(maxlen=100)

    def snapshot(self):
        with self._lock:
            return {"state": self._state, "active": self._state == "active",
                    "configured": self._state in {"inactive", "active"},
                    "last_error": self._last_error, "transitions": list(self._history),
                    "allowed_actions": [k for k, (source, _) in self.TRANSITIONS.items() if source == self._state]}

    def ensure_active(self):
        with self._lock:
            if self._state != "active":
                raise CommandError(f"服务生命周期状态为 {self._state}，请先配置并激活")

    def _pending(self):
        if self._busy and self._busy():
            return True
        if self.adapter is not None:
            status = self.adapter.snapshot()
            return any(status.get(key) for key in ("action_pending", "navigation_pending", "cancellation_pending"))
        return False

    def transition(self, action):
        with self._lock:
            if action not in self.TRANSITIONS:
                raise CommandError("未知生命周期操作")
            source, target = self.TRANSITIONS[action]
            if self._state != source:
                raise CommandError(f"{self._state} 状态不能执行 {action}")
            if action in {"deactivate", "cleanup", "reset_error"} and self._pending():
                raise CommandError("任务或外部动作尚未结束，请停止并确认终态后再切换生命周期")
            try:
                hook = self._hooks.get(action)
                if hook:
                    hook()
            except Exception as exc:
                self.fail(str(exc) or type(exc).__name__)
                raise CommandError(f"生命周期操作失败：{exc}") from exc
            self._state, self._last_error = target, None
            self._history.append({"action": action, "from": source, "to": target,
                                  "at": datetime.now(timezone.utc).isoformat()})
            return self.snapshot()

    def fail(self, message):
        with self._lock:
            source = self._state
            self._state, self._last_error = "error", str(message)[:1000]
            self._history.append({"action": "error", "from": source, "to": "error",
                                  "at": datetime.now(timezone.utc).isoformat()})
            if self._stop:
                self._stop()

    def finalize(self):
        with self._lock:
            if self._pending():
                raise CommandError("外部动作尚未确认终止")
            self._state = "finalized"
            return self.snapshot()
