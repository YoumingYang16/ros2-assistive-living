"""Durable conditional mission execution shared by software and ROS adapters."""
from __future__ import annotations
import copy
import json
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from .contracts import CommandError, ExecutionCancelled, ExecutionError, Plan, PlanningResult
from .plan_validation import validate_plan, condition_matches, plan_from_dict, evaluate_goal
from .planner import parse_command
from .store import MissionStore, ACTIVE_STATES

# A cancelled or failed physical operation can already have had side effects.
# Retrying these skills requires a new, explicitly planned task after inspection.
NON_REPLAYABLE_SKILLS = frozenset({"capture", "dock", "follow", "turn", "home_control",
                                  "pick_object", "place_object", "handover_object"})
PHYSICAL_DISPATCH_SKILLS = NON_REPLAYABLE_SKILLS | {"navigate"}
HOME_SKILLS = frozenset({"home_control", "pick_object", "place_object", "handover_object"})
_NO_DISPATCH_CODES = frozenset({"CAPABILITY_UNAVAILABLE", "GOAL_REJECTED", "SERVER_UNAVAILABLE",
    "LOCATION_UNCONFIRMED", "UNKNOWN_LOCATION", "UNSUPPORTED_SKILL", "INTERFACE_VERSION_MISMATCH",
    "OBJECT_UNCONFIRMED", "OBJECT_EVIDENCE_STALE"})
_CONFIRMED_TERMINAL_CODES = frozenset({"ACTION_ABORTED", "NAVIGATION_FAILED", "SKILL_FAILED", "HOME_RECONCILIATION_REQUIRED",
                                      "EXTERNAL_RECONCILIATION_REQUIRED", "NAVIGATION_CANCEL_CONFIRMED"})


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class MissionEngine:
    def __init__(self, config, adapter, journal_path=None, *, db_path=None, planner=None, start_scheduler=True):
        self.config, self.adapter = config, adapter
        self._condition = threading.Condition(threading.RLock())
        self._planning_lock = threading.Lock()
        self.store = MissionStore(db_path)
        self._state, self._mission, self._worker = "idle", None, None
        self._step_cancel = threading.Event()
        self._cancel_requested = self._pause_requested = self._closed = False
        self._planning_generation = 0
        self._events = deque(self.store.events(), maxlen=200)
        self._journal_path = Path(journal_path) if journal_path else None
        self._started_monotonic = time.monotonic()
        self._recoveries = self.store.recover_interrupted()
        self._hardware_interlock = self.store.get_setting("hardware_interlock")
        if self._hardware_interlock and self._hardware_interlock.get("status") == "pending":
            self._hardware_interlock.update(status="unknown", updated_at=timestamp(),
                reason="进程结束前没有持久确认硬件终态；不能因重启而解除互锁")
            self.store.set_setting("hardware_interlock", self._hardware_interlock)
        # Upgrade protection for older journals without the V6 intent marker.
        if not self._hardware_locked():
            resolved = self._hardware_interlock or {}
            uncertain = [m for m in self.store.recoveries() if m.get("mode") == "ros2"
                and not m.get("hardware_reconciliation")
                and any(s.get("kind") in PHYSICAL_DISPATCH_SKILLS
                    and not (resolved.get("status") == "resolved" and resolved.get("mission_id") == m["id"]
                             and resolved.get("step_id") == s.get("step_id")) and any(
                    t.get("step_id") == s.get("step_id") and t.get("status") == "interrupted"
                    for t in m.get("step_states", [])) for s in m.get("steps", []))]
            if uncertain:
                first = uncertain[0]
                step = first["steps"][first.get("step_index", 0)]
                self._hardware_interlock = {"id": uuid.uuid4().hex, "status": "unknown",
                    "mission_id": first["id"], "related_mission_ids": [m["id"] for m in uncertain],
                    "step_id": step.get("step_id"), "kind": step.get("kind"), "created_at": timestamp(),
                    "updated_at": timestamp(), "reason": "历史外部动作在派发期间中断，必须核实实际硬件状态"}
                self.store.set_setting("hardware_interlock", self._hardware_interlock)
        try:
            saved = self.store.get_setting("config")
            if saved and {**config.get("ros", {}), **saved.get("ros", {})} == config.get("ros", {}):
                from .config import validate_config
                saved["ros"] = copy.deepcopy(config.get("ros", {}))
                self.config = validate_config(saved)
                self.adapter.config = self.config
                if hasattr(self.adapter, "reconfigure"):
                    self.adapter.reconfigure(self.config, reset=True)
            if planner is None:
                from .natural_language import DialoguePlanner
                planner = DialoguePlanner(self.config)
            self._planner = planner
            from .skills import get_registry
            from .lifecycle import LifecycleController
            from .memory import ObservationMemory
            from .data_management import DataManager
            self.registry = get_registry()
            self.lifecycle = LifecycleController(adapter, busy=lambda: self._worker is not None and self._worker.is_alive())
            self.memory_service = ObservationMemory(self.store, self.config)
            self.data = DataManager(self.store)
            self.data.maintenance()
            self.scheduler = None
        except Exception:
            self.store.close()
            raise
        with self._condition:
            self._event("info", "软件模拟模式就绪" if adapter.mode == "mock" else "ROS 2 接口模式就绪")
            if self._recoveries:
                self._event("warning", f"发现 {len(self._recoveries)} 项未确认结束的历史任务；不会自动恢复执行")
        from .scheduler import TaskScheduler
        self.scheduler = TaskScheduler(self, start=start_scheduler)
        from .assistive_service import AssistiveService
        self.assistive = AssistiveService(self.store, start=start_scheduler)
        from .assistive_dialogue import AssistiveDialogue
        self.assistive_dialogue = AssistiveDialogue(self.assistive)

    def _event(self, level, message, data=None):
        with self._condition:
            mission_id = self._mission["id"] if self._mission else None
            self._events.append(self.store.event(level, message, mission_id, data))

    def _persist(self):
        if self._mission:
            self._mission["state"] = self._state
            self.store.save_mission(self._mission)

    def _hardware_locked(self):
        return bool(self._hardware_interlock and self._hardware_interlock.get("status") in {"pending", "unknown"})

    def _begin_hardware_step(self, step):
        if self.adapter.mode != "ros2" or step.kind not in PHYSICAL_DISPATCH_SKILLS:
            return
        with self._condition:
            if self._hardware_locked():
                raise ExecutionError("存在未核实的硬件执行记录，不能派发下一步")
            self._hardware_interlock = {"id": uuid.uuid4().hex, "status": "pending",
                "mission_id": self._mission["id"], "related_mission_ids": [self._mission["id"]],
                "step_id": step.step_id, "kind": step.kind, "created_at": timestamp(),
                "updated_at": timestamp(), "reason": "已记录派发意图，等待物理执行终态"}
            # Durable BEFORE dispatch, including the crash window before ROS ACK.
            self.store.set_setting("hardware_interlock", self._hardware_interlock)

    def _settle_hardware_step(self, *, succeeded=False, error_code=None):
        if (not self._hardware_locked() or self._hardware_interlock.get("status") == "unknown"
                or self._hardware_interlock.get("mission_id") != self._mission["id"]):
            return
        robot = self.adapter.snapshot()
        pending = any(robot.get(key) for key in ("hardware_uncertain", "action_pending", "navigation_pending", "cancellation_pending"))
        confirmed = not pending and (succeeded or error_code in _NO_DISPATCH_CODES | _CONFIRMED_TERMINAL_CODES)
        updated = copy.deepcopy(self._hardware_interlock)
        updated.update(status="resolved" if confirmed else "unknown", updated_at=timestamp(),
            reason="已确认执行终态或未派发；本次派发意图已结清" if confirmed else "外部操作物理状态未确认，需可信人工核实",
            error_code=error_code)
        self.store.set_setting("hardware_interlock", updated)
        self._hardware_interlock = updated
        if succeeded and not confirmed:
            from .skills import SkillExecutionError
            raise SkillExecutionError("UNKNOWN_HARDWARE_STATE", "步骤虽返回成功，但接口仍有未决硬件操作；不能继续下一步")

    def reconcile_hardware(self, verifier, *, note):
        """Trusted local integration only; deliberately absent from HTTP APIs.

        Restart/reconnect the adapter after physical reconciliation if it still
        holds an unresolved ROS lease. A web acknowledgement never calls this.
        """
        if not callable(verifier) or not isinstance(note, str) or not 8 <= len(note.strip()) <= 1000:
            raise CommandError("需要可信本机核实器及具体人工核实说明")
        with self._condition:
            if not self._hardware_locked():
                raise CommandError("没有待核实的硬件互锁")
            if self._worker and self._worker.is_alive():
                raise CommandError("任务线程尚未结束，不能核实解除互锁")
            robot = self.adapter.snapshot()
            if any(robot.get(key) for key in ("hardware_uncertain", "action_pending", "navigation_pending", "cancellation_pending")):
                raise CommandError("执行接口仍持有未决目标；先核实设备并重新连接任务节点，再使用可信本机核实器")
            record = copy.deepcopy(self._hardware_interlock)
        receipt = verifier(copy.deepcopy(record))
        try:
            if not isinstance(receipt, dict) or len(json.dumps(receipt, allow_nan=False)) > 65536:
                raise ValueError("Invalid receipt")
            if (receipt.get("interlock_id") != record["id"] or receipt.get("all_resources_terminal") is not True
                    or receipt.get("terminal_confirmed") is not True or not isinstance(receipt.get("evidence"), dict)
                    or not receipt["evidence"] or not isinstance(receipt.get("verified_by"), str)
                    or not 1 <= len(receipt["verified_by"].strip()) <= 200):
                raise ValueError("Incomplete verification")
            observed = datetime.fromisoformat(receipt["observed_at"].replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc)-observed).total_seconds()
            if observed.tzinfo is None or observed < datetime.fromisoformat(record["created_at"]) or not -1 <= age <= 30:
                raise ValueError("Stale verification")
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
            raise CommandError("核实回执需匹配互锁 ID、覆盖所有相关机械终态，并提供新鲜证据和核实者") from exc
        with self._condition:
            if not self._hardware_locked() or self._hardware_interlock["id"] != record["id"]:
                raise CommandError("核实期间互锁状态已变化")
            audit = {"interlock_id": record["id"], "verified_at": timestamp(), "note": note.strip(), "receipt": copy.deepcopy(receipt)}
            for identifier in record.get("related_mission_ids", [record["mission_id"]]):
                mission = self.store.mission(identifier)
                if mission:
                    mission["hardware_reconciliation"] = audit
                    self.store.save_mission(mission)
                    if self._mission and self._mission["id"] == identifier:
                        self._mission["hardware_reconciliation"] = copy.deepcopy(audit)
            updated = copy.deepcopy(self._hardware_interlock)
            updated.update(status="resolved", updated_at=timestamp(), reconciliation=audit,
                reason="可信本机核实器确认相关机械均已到达终态；历史动作不会重放")
            self.store.set_setting("hardware_interlock", updated)
            self._hardware_interlock = updated
            self._event("warning", "已通过可信本机核实解除硬件互锁；没有重放任何动作", {"interlock_id": record["id"]})
            return {"ok": True, "hardware_interlock": copy.deepcopy(self._hardware_interlock)}

    def snapshot(self):
        with self._condition:
            result = {"version": "7.0.0", "mode": self.adapter.mode, "state": self._state,
                "mission": copy.deepcopy(self._mission), "events": list(self._events),
                "locations": copy.deepcopy(self.config["locations"]), "memory": self.store.observations(),
                "recoveries": self.store.recoveries(), "history_count": self.store.metrics()["missions_total"],
                "hardware_interlock": copy.deepcopy(self._hardware_interlock),
                "capabilities": {"planner": "hybrid", "navigation": True, "patrol": True,
                    "conditional_tasks": True, "persistent_tasks": self.store.path != ":memory:",
                    "clarification": True, "inspection": "preset_data" if self.adapter.mode == "mock" else "external_interface",
                    "physics_simulation": False}}
        result["robot"] = self.adapter.snapshot()
        result["planner"] = self.planner_info()
        result["queue"] = self.scheduler.summary() if self.scheduler else {"paused": False, "pending": 0, "running": 0}
        result["lifecycle"] = self.lifecycle.snapshot()
        result["capabilities"]["skills"] = self.registry.catalog(self.adapter)
        return result

    def planner_info(self):
        provider = getattr(self._planner, "provider", None) or getattr(self._planner, "_provider", None)
        return {"rules_available": True, "model_configured": provider is not None,
                "provider": getattr(provider, "name", type(provider).__name__ if provider else "none")}

    def _response(self, message, **extra):
        return {"ok": True, "message": message, "state": self.snapshot(), **extra}

    def _interpret(self, text, session_id, *, context=None):
        if not isinstance(text, str) or not text.strip() or len(text) > 500:
            raise CommandError("指令须为 1 到 500 个字符的文字")
        context = copy.deepcopy(self.store.session(session_id) if context is None else context)
        with self._condition:
            context["active_mission"] = self._state in ACTIVE_STATES
        result = self.memory_service.interpret(text, context=context) or self._planner.interpret(text, context=context)
        if not isinstance(result, PlanningResult):
            raise CommandError("语言规划器返回了不支持的结果")
        if result.plan:
            result = PlanningResult(result.kind, validate_plan(result.plan, self.config), result.message,
                                    result.context, result.options)
        return result

    def _schedule_continuation(self, text, context):
        """A schedule belongs to its draft, never to the next unrelated task."""
        from .planner import _normalize, _location_names, _OBJECT_SYNONYMS
        import re
        normalized = _normalize(text)
        if context.get("pending_plan") and (
                normalized in {"确认", "确认执行", "执行计划", "就这样"} or
                re.match(r"^(?:改去|改成|改为|把.+改成)", normalized)):
            return True
        pending = context.get("clarification")
        if isinstance(pending, dict):
            chosen = (_location_names(self.config).get(normalized) if pending.get("slot", "destination") == "destination"
                      else _OBJECT_SYNONYMS.get(normalized, normalized))
            return chosen in pending.get("options", [])
        # Free-form model replies cannot reliably establish draft identity.
        # Preserve scheduling only for an explicit offered option; other new
        # proposals will require their usual confirmation as a new draft.
        pending = context.get("model_clarification")
        return isinstance(pending, dict) and normalized in {_normalize(x) for x in pending.get("options", [])}

    def _interpret_scheduled(self, text, session_id):
        from .natural_language import parse_schedule_intent
        schedule = parse_schedule_intent(text)
        context = self.store.session(session_id)
        continuation = self._schedule_continuation(text, context) if "pending_schedule" in context and not schedule else False
        # Planning is side-effect free. A rejected schedule must not leave a
        # durable scheduling marker that a later command might inherit.
        if schedule:
            context["pending_schedule"] = {k: schedule[k] for k in ("run_at", "repeat") if schedule.get(k) is not None}
        result = self._interpret(schedule["text"] if schedule else text, session_id, context=context)
        result_context = copy.deepcopy(result.context)
        if schedule and result.kind in {"task", "clarify"}:
            result_context["pending_schedule"] = copy.deepcopy(context["pending_schedule"])
        elif result.kind in {"task", "clarify"} and not continuation:
            result_context.pop("pending_schedule", None)
        elif schedule and result.kind not in {"task", "clarify"}:
            raise CommandError("调度必须生成任务或任务澄清，不能安排查询或对话回答")
        return result, result_context

    def preview(self, text, session_id="default"):
        if self._closed:
            raise CommandError("服务已关闭")
        assisted = self.assistive_dialogue.preview(text, session_id)
        if assisted is not None:
            return assisted
        with self._planning_lock:
            with self._condition:
                if self._closed:
                    raise CommandError("服务已关闭")
                generation = self._planning_generation
            result, context = self._interpret_scheduled(text, session_id)
            if isinstance(context.get("assistive_dialogue"), dict):
                context["assistive_dialogue"]["pending"] = None
                context["assistive_dialogue"]["awaiting_detail"] = False
            if result.plan:
                context["pending_plan"] = result.plan.to_dict()
            with self._condition:
                if generation != self._planning_generation:
                    raise CommandError("规划期间收到停止请求，已放弃该草稿")
                self.store.save_session(session_id, context)
        return {"ok": True, "kind": result.kind, "plan": result.plan.to_dict() if result.plan else None,
                "message": result.message, "options": result.options,
                "needs_clarification": result.kind == "clarify",
                "needs_confirmation": bool(result.plan and result.plan.metadata.get("requires_confirmation"))}

    def submit(self, text, request_id=None, session_id="default"):
        if self._closed:
            raise CommandError("服务已关闭")
        if not isinstance(text, str) or not text.strip() or len(text) > 500:
            raise CommandError("指令须为 1 到 500 个字符的文字")
        from .natural_language import detect_control
        if detect_control(text) is None and self.assistive_dialogue.preview(text, session_id) is not None:
            import hashlib
            if not isinstance(session_id, str) or not 1 <= len(session_id) <= 128:
                raise CommandError("session_id 必须为 1 到 128 字符的字符串")
            for name, value in (("session_id", session_id), ("request_id", request_id)):
                if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 128):
                    raise CommandError(name + " 必须为 1 到 128 字符的字符串")
            request_id = request_id or uuid.uuid4().hex
            key = hashlib.sha256(json.dumps([session_id, request_id]).encode()).hexdigest()
            existing = self.store.reserve_request(session_id, request_id, text)
            if existing:
                response = json.loads(existing["response"]) if existing["response"] else {
                    "ok": True, "message": "该请求正在处理，请查看生活记录", "pending": True}
                return {**response, "duplicate": True, "request_id": request_id, "state": self.snapshot()}
            try:
                with self._planning_lock:
                    response = self.assistive_dialogue.command(text, session_id, request_id=key)
                    if response is None:
                        raise CommandError("生活对话上下文已变化，请重新发出完整指令")
                response.update(state=self.snapshot(), request_id=request_id)
                self.store.finish_request(session_id, request_id, response)
                return response
            except Exception as exc:
                self.store.finish_request(session_id, request_id, {"ok": False, "message": str(exc)})
                raise
        request_id = request_id or uuid.uuid4().hex
        existing = self.store.reserve_request(session_id, request_id, text)
        if existing:
            response = json.loads(existing["response"]) if existing["response"] else {
                "ok": True, "message": "该请求正在处理，请查看状态", "pending": True}
            return {**response, "duplicate": True, "request_id": request_id,
                    "mission_id": existing["mission_id"], "state": self.snapshot()}
        mission_id = None
        try:
            try:
                direct = parse_command(text, self.config)
            except CommandError:
                direct = None
            if direct and direct.kind in {"stop", "pause", "resume", "status"}:
                response = self.control(direct.kind)
            else:
                with self._planning_lock:
                    with self._condition:
                        generation = self._planning_generation
                    result, context = self._interpret_scheduled(text, session_id)
                    # A new robot topic owns subsequent short answers. Keep
                    # living focus history, but never reuse its old choices.
                    if isinstance(context.get("assistive_dialogue"), dict):
                        context["assistive_dialogue"]["pending"] = None
                        context["assistive_dialogue"]["awaiting_detail"] = False
                    with self._condition:
                        if generation != self._planning_generation:
                            raise CommandError("规划期间收到停止请求，已放弃该计划")
                    if result.kind == "task":
                        if result.plan.metadata.get("requires_confirmation"):
                            context["pending_plan"] = result.plan.to_dict()
                            response = self._response(result.message or "计划已生成，请检查后确认执行", kind="task",
                                needs_confirmation=True, plan=result.plan.to_dict(), options=["确认执行", "取消计划"])
                        else:
                            with self._condition:
                                if generation != self._planning_generation:
                                    raise CommandError("规划期间收到停止请求，已放弃该计划")
                            if "pending_schedule" in context:
                                response = self.scheduler.add(result.plan, request_id=request_id, session_id="scheduled:" + session_id[:110], _generation=generation, **context.pop("pending_schedule"))
                            else:
                                with self._condition:
                                    if generation != self._planning_generation:
                                        raise CommandError("规划期间收到停止请求，已放弃该计划")
                                    response = self.submit_plan(result.plan, _request=(session_id, request_id))
                                mission_id = response["state"]["mission"]["id"]
                            context.pop("pending_plan", None)
                    elif result.kind in {"clarify", "answer"}:
                        response = self._response(result.message, kind=result.kind,
                            needs_clarification=result.kind == "clarify", options=result.options)
                    elif result.kind in {"stop", "pause", "resume", "status"}:
                        response = self.control(result.kind)
                    else:
                        raise CommandError("规划器返回未知指令类型")
                    with self._condition:
                        if generation != self._planning_generation:
                            context.pop("pending_plan", None)
                            context.pop("clarification", None)
                            context.pop("model_clarification", None)
                            context.pop("pending_schedule", None)
                        self.store.save_session(session_id, context)
            response.update(request_id=request_id, mission_id=mission_id)
            self.store.finish_request(session_id, request_id, response, mission_id)
            return response
        except Exception as exc:
            self.store.finish_request(session_id, request_id, {"ok": False, "message": str(exc), "code": type(exc).__name__})
            raise

    def _validate_plan(self, plan):
        return validate_plan(plan, self.config)

    def can_dispatch(self):
        with self._condition:
            if self._closed or self._hardware_locked() or (self._worker and self._worker.is_alive()):
                return False
            if not self.lifecycle.snapshot()["active"]:
                return False
            robot = self.adapter.snapshot()
            return not any(robot.get(k) for k in ("action_pending", "navigation_pending", "cancellation_pending"))

    def submit_structured(self, plan, *, request_id=None, session_id="workflow", _recovery_from=None):
        plan = validate_plan(copy.deepcopy(plan), self.config)
        if plan.metadata.get("requires_confirmation"):
            raise CommandError("此计划需要明确确认，不能通过结构化入口绕过确认")
        request_id = request_id or uuid.uuid4().hex
        original = self.store.reserve_request(session_id, request_id, json.dumps(plan.to_dict(), ensure_ascii=False, sort_keys=True))
        if original:
            response = json.loads(original["response"]) if original["response"] else {"ok": True, "pending": True}
            return {**response, "duplicate": True, "request_id": request_id, "mission_id": original["mission_id"], "state": self.snapshot()}
        try:
            result = self.submit_plan(plan, _request=(session_id, request_id), _recovery_from=_recovery_from)
            identifier = result["state"]["mission"]["id"]
            result.update(request_id=request_id, mission_id=identifier)
            self.store.finish_request(session_id, request_id, result, identifier)
            return result
        except Exception as exc:
            self.store.finish_request(session_id, request_id, {"ok": False, "message": str(exc)})
            raise

    def submit_plan(self, plan, *, _request=None, _recovery_from=None):
        plan = validate_plan(copy.deepcopy(plan), self.config)
        with self._condition:
            if self._closed:
                raise CommandError("服务已关闭")
            if self._hardware_locked():
                raise CommandError("未知硬件状态互锁尚未解除；包括导航在内的新机械任务均被阻止，需可信本机核实")
            self.lifecycle.ensure_active()
            if self._worker is not None and self._worker.is_alive():
                raise CommandError("当前任务尚未结束，请先停止或等待完成")
            robot = self.adapter.snapshot()
            if robot.get("action_pending") or robot.get("navigation_pending") or robot.get("cancellation_pending"):
                raise CommandError("外部执行接口仍有未决目标，不能开始新任务")
            mission = {"id": uuid.uuid4().hex, **plan.to_dict(), "state": "running", "mode": self.adapter.mode,
                "step_index": 0, "results": [], "started_at": timestamp(), "finished_at": None,
                "error": None, "error_code": None, "feedback": {}, "report": None,
                "step_states": [{"step_id": s.step_id, "status": "pending", "attempt": 0} for s in plan.steps]}
            self.store.save_mission(mission, request=_request, recovery_from=_recovery_from)
            self._cancel_requested = self._pause_requested = False
            self._step_cancel.clear()
            self._state, self._mission = "running", mission
            self._event("info", f"开始任务：{plan.summary}")
            self._worker = threading.Thread(target=self._run, args=(plan,), daemon=True, name="mission-worker")
            self._worker.start()
        return self._response("任务已接受：" + plan.summary)

    def control(self, action, *, clear_dialogues=True):
        if action == "status":
            return self._response("当前任务状态：" + self.snapshot()["state"])
        if action not in {"stop", "pause", "resume"}:
            raise CommandError("不支持的控制指令")
        if action == "stop" and self.scheduler:
            self.scheduler.control("pause")
        with self._condition:
            if action == "stop":
                self._planning_generation += 1
                if clear_dialogues:
                    self.store.clear_pending_dialogues()
            active = self._worker is not None and self._worker.is_alive() and self._state in ACTIVE_STATES
            if not active:
                return self._response("当前没有执行中的任务；待处理规划已取消" if action == "stop" else "当前没有执行中的任务")
            if action == "stop":
                self._cancel_requested, self._pause_requested = True, False
                self._state = "cancelling"
                self._step_cancel.set()
                message = "已请求停止，等待执行接口确认"
            elif action == "pause":
                if self._cancel_requested:
                    raise CommandError("任务正在停止，不能暂停")
                self._pause_requested = True
                if self._state != "paused":
                    self._state = "pausing"
                self._step_cancel.set()
                message = "已请求暂停，等待执行接口确认"
            else:
                if self._cancel_requested:
                    raise CommandError("任务正在停止，不能继续")
                if not self._pause_requested:
                    return self._response("任务正在执行")
                self._pause_requested = False
                message = "已请求继续，将重新执行未完成步骤"
            self._persist()
            self._event("info", message)
            self._condition.notify_all()
            if action in {"stop", "pause"}:
                try:
                    self.adapter.stop()
                except Exception as exc:
                    self._event("error", f"停止请求接口异常：{exc}")
        return self._response(message)

    def _ready(self):
        with self._condition:
            if self._pause_requested and not self._cancel_requested:
                self._state = "paused"
                self._persist()
                self._event("info", "执行接口已确认暂停")
            while self._pause_requested and not self._cancel_requested:
                self._condition.wait()
            if self._cancel_requested:
                return False
            self._state = "running"
            self._step_cancel.clear()
            return True

    def _feedback(self, value):
        with self._condition:
            if self._mission:
                self._mission["feedback"].update(copy.deepcopy(value))
                if isinstance(value.get("execution_ref"), dict):
                    reference = {**copy.deepcopy(value["execution_ref"]), "step_id": self._mission["steps"][self._mission["step_index"]]["step_id"]}
                    refs = self._mission.setdefault("execution_refs", [])
                    refs[:] = [r for r in refs if not (r.get("step_id") == reference["step_id"] and not r.get("goal_id"))]
                    if reference not in refs:
                        refs.append(reference)
                    if self._hardware_locked() and self._hardware_interlock.get("mission_id") == self._mission["id"]:
                        self._hardware_interlock["execution_ref"] = copy.deepcopy(reference)
                        self.store.set_setting("hardware_interlock", self._hardware_interlock)
                    self._persist()

    def _emit_speech(self, text):
        with self._condition:
            self._event("speech", text)

    @staticmethod
    def _normalize_result(step, result):
        if not isinstance(result, dict) or result.get("status") != "succeeded":
            raise ExecutionError("执行接口未提供成功结果")
        result = copy.deepcopy(result)
        if result.get("kind", step.kind) != step.kind or result.get("target", step.target) != step.target:
            raise ExecutionError("执行结果与当前技能/地点不匹配")
        result.update(kind=step.kind, target=step.target, step_id=step.step_id)
        if step.kind == "inspect":
            if result.get("object_name", step.object_name) != step.object_name:
                raise ExecutionError("观察结果与请求目标不匹配")
            if "found" in result and result["found"] is not None and type(result["found"]) is not bool:
                raise ExecutionError("found 必须是布尔值或 null")
            outcome = result.get("outcome")
            if outcome is None:
                outcome = ("found" if result.get("found") is True else "not_found" if result.get("found") is False
                           else "inconclusive" if step.object_name else "observed")
            if not isinstance(outcome, str) or outcome not in {"found", "not_found", "inconclusive", "observed"}:
                raise ExecutionError("感知接口返回未知结论")
            if (outcome == "found" and result.get("found") is False or
                    outcome == "not_found" and result.get("found") is True or
                    outcome == "inconclusive" and type(result.get("found")) is bool):
                raise ExecutionError("观察结果中的 outcome 与 found 相互矛盾")
            if not result.get("evidence"):
                raise ExecutionError("观察结果缺少证据")
            try:
                observed = datetime.fromisoformat(str(result["observed_at"]).replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - observed).total_seconds()
                if observed.tzinfo is None or not -5 <= age <= step.timeout + 5:
                    raise ValueError("stale observation")
            except (KeyError, TypeError, ValueError) as exc:
                raise ExecutionError("观察结果时间戳无效或已过期") from exc
            if step.object_name and outcome == "observed":
                outcome = "inconclusive"
            if outcome == "inconclusive":
                result["found"] = None
            result["outcome"] = outcome
        else:
            result["outcome"] = "succeeded"
        try:
            json.dumps(result, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ExecutionError("执行结果不是有效 JSON") from exc
        return result

    def _run(self, plan):
        try:
            for index, step in enumerate(plan.steps):
                attempt = 0
                while True:
                    if not self._ready():
                        self._finish("cancelled", "任务已停止，执行接口已返回")
                        return
                    with self._condition:
                        self._mission["step_index"] = index
                        matches, reason = condition_matches(step, self._mission["results"])
                        if not matches:
                            self._mission["results"].append({"step_index": index, "step_id": step.step_id,
                                "kind": step.kind, "target": step.target, "status": "skipped", "message": reason})
                            self._mission["step_states"][index].update(status="skipped", reason=reason)
                            self._persist()
                            self._event("info", f"跳过步骤 {index + 1}：{reason}")
                            break
                        self._mission["feedback"] = {"attempt": attempt + 1}
                        self._mission["step_states"][index].update(status="running", attempt=attempt + 1, started_at=timestamp())
                        self._persist()
                        target = self.config["locations"].get(step.target, {}).get("label", "")
                        self._event("info", f"步骤 {index + 1}/{len(plan.steps)}：{step.kind} {target}")
                    try:
                        self._begin_hardware_step(step)
                        result = self._normalize_result(step, self.registry.execute(step, self.adapter, self._step_cancel, self._feedback,
                            {"config": self.config, "results": copy.deepcopy(self._mission["results"]),
                             "emit_speech": self._emit_speech, "mission_id": self._mission["id"]}))
                    except ExecutionCancelled:
                        if step.kind == "navigate":
                            with self._condition:
                                self._settle_hardware_step(error_code="NAVIGATION_CANCEL_CONFIRMED")
                                if self._hardware_locked():
                                    from .skills import SkillExecutionError
                                    raise SkillExecutionError("UNKNOWN_HARDWARE_STATE", "导航取消后仍存在未决物理状态，不能恢复派发")
                        if not self._step_cancel.is_set():
                            raise ExecutionError("执行接口意外取消了任务")
                        if step.kind in NON_REPLAYABLE_SKILLS:
                            from .skills import SkillExecutionError
                            raise SkillExecutionError("HOME_RECONCILIATION_REQUIRED" if step.kind in HOME_SKILLS else "EXTERNAL_RECONCILIATION_REQUIRED",
                                "外部机械操作已中断；取消终态不证明没有物理副作用。请核实设备、载荷与交接状态后创建新任务，不能暂停后自动重放")
                        continue
                    except ExecutionError as exc:
                        if step.kind == "navigate":
                            with self._condition:
                                self._settle_hardware_step(error_code=getattr(exc, "code", None))
                                # Retry an absolute target only after the prior
                                # dispatch has a durably confirmed terminal/no-send.
                                if self._hardware_locked():
                                    raise
                        if self._step_cancel.is_set() and step.kind not in NON_REPLAYABLE_SKILLS:
                            raise ExecutionError(f"停止或暂停未获执行接口确认：{exc}") from exc
                        if step.kind in NON_REPLAYABLE_SKILLS or attempt >= step.max_retries or not getattr(exc, "retryable", True):
                            code = str(getattr(exc, "code", None) or "EXECUTION_FAILED")
                            pending = self.adapter.snapshot()
                            uncertain_external = any(pending.get(k) for k in ("action_pending", "navigation_pending", "cancellation_pending"))
                            failed = {"step_id": step.step_id, "step_index": index, "kind": step.kind, "target": step.target,
                                      "object_name": step.object_name, "status": "failed", "error_code": code,
                                      "outcome": "timed_out" if "TIMEOUT" in code or "TIMED_OUT" in code else "failed",
                                      "message": str(exc), "handled_failure": step.on_failure == "continue" and not uncertain_external and step.kind not in NON_REPLAYABLE_SKILLS}
                            with self._condition:
                                self._mission["results"].append(failed)
                                self._mission["step_states"][index].update(status="failed", error=str(exc), error_code=code, finished_at=timestamp())
                                self._persist()
                            if step.on_failure == "continue" and not uncertain_external and step.kind not in NON_REPLAYABLE_SKILLS:
                                self._event("warning", "步骤失败，进入显式补救分支：" + str(exc))
                                break
                            raise
                        attempt += 1
                        with self._condition:
                            self._mission["step_states"][index].update(status="retrying", error=str(exc))
                            self._persist()
                            self._event("warning", f"步骤失败，重试 {attempt}/{step.max_retries}：{exc}",
                                        {"error_code": getattr(exc, "code", "EXECUTION_FAILED")})
                        self._step_cancel.wait(min(.1 * 2 ** (attempt - 1), .5))
                        continue
                    with self._condition:
                        self._mission["results"].append({"step_index": index, **result})
                        self._mission["step_states"][index].update(status="succeeded", finished_at=timestamp())
                        self._persist()
                        self._settle_hardware_step(succeeded=True)
                        if self._cancel_requested:
                            self._finish("cancelled", "任务已停止，执行接口已返回；已完成步骤已记录")
                            return
                        self._event("warning" if result.get("outcome") == "inconclusive" else "info", result.get("message", "步骤完成"))
                    break
            self._finish("succeeded", "任务完成")
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            try:
                self.adapter.stop()
            except Exception as stop_error:
                message += f"；停止请求接口异常：{stop_error}"
            self._finish("failed", message, getattr(exc, "code", "EXECUTION_FAILED"))

    def _finish(self, state, message, error_code=None):
        with self._condition:
            if self._cancel_requested and state == "succeeded":
                state, message = "cancelled", "任务已停止"
            self._state = state
            self._mission.update(finished_at=timestamp(), error=message if state == "failed" else None, error_code=error_code)
            index = self._mission["step_index"]
            if self._mission["step_states"][index]["status"] in {"running", "retrying"}:
                self._mission["step_states"][index]["status"] = state
            observations = [r for r in self._mission["results"] if r.get("kind") == "inspect" and r.get("status") == "succeeded"]
            uncertain = sum(r.get("outcome") == "inconclusive" for r in observations)
            if state == "succeeded" and uncertain:
                message = f"任务执行完成，{uncertain} 次观察无法得出明确结论"
            self._mission["report"] = {"status": state, "message": message,
                "completed_steps": sum(r.get("status") == "succeeded" for r in self._mission["results"]),
                "skipped_steps": sum(r.get("status") == "skipped" for r in self._mission["results"]),
                "total_steps": len(self._mission["steps"]), "observations": observations,
                "inconclusive_observations": uncertain,
                "simulated": self.adapter.mode == "mock" or any(r.get("simulated", False) for r in self._mission["results"])}
            plan = plan_from_dict({k: self._mission[k] for k in ("command", "steps", "summary", "metadata", "version")}, self.config)
            goal = evaluate_goal(plan, self._mission["results"])
            self._mission["report"].update(goal=goal, goal_outcome=goal["outcome"],
                failed_steps=sum(r.get("status") == "failed" for r in self._mission["results"]))
            self._persist()
            self._settle_hardware_step(error_code=error_code)
            self._event("error" if state == "failed" else "info", message)
            if self._journal_path:
                try:
                    self._journal_path.parent.mkdir(parents=True, exist_ok=True)
                    with self._journal_path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(self._mission, ensure_ascii=False, allow_nan=False) + "\n")
                except (OSError, ValueError) as exc:
                    self._event("warning", f"任务已结束，但 JSONL 导出失败：{exc}")
            self._condition.notify_all()

    def history(self, limit=20, offset=0, **filters):
        return {"ok": True, "missions": self.store.history(limit, offset, **filters), "total": self.store.history_count(**filters)}

    def mission_detail(self, mission_id):
        mission = self.store.mission(mission_id)
        if mission is None:
            raise CommandError("任务不存在")
        return {"ok": True, "mission": mission, "events": self.store.events(mission_id, 1000)}

    def health(self):
        robot = self.adapter.snapshot()
        live = not self._closed
        ready = live and self._state not in ACTIVE_STATES and robot.get("health", {}).get("status", "ready") == "ready"
        ready = ready and not any(robot.get(k) for k in ("action_pending", "navigation_pending", "cancellation_pending"))
        ready = ready and self.lifecycle.snapshot()["active"]
        ready = ready and not self._hardware_locked()
        return {"ok": live, "live": live, "ready": ready, "mode": self.adapter.mode, "state": self._state,
                "uptime_seconds": round(time.monotonic() - self._started_monotonic, 1),
                "database": {"available": True, "persistent": self.store.path != ":memory:", "schema_version": 3},
                "planner": self.planner_info(), "robot": robot,
                "hardware_interlock": copy.deepcopy(self._hardware_interlock),
                "recovery_required": len(self.store.recoveries()), "lifecycle": self.lifecycle.snapshot(),
                "queue": self.scheduler.summary() if self.scheduler else None}

    def metrics(self):
        return {"ok": True, **self.store.metrics(), "uptime_seconds": round(time.monotonic()-self._started_monotonic, 1)}

    def dismiss_recovery(self, mission_id):
        mission = self.store.dismiss_recovery(mission_id)
        with self._condition:
            self._event("info", "已确认历史中断任务；未重新执行", {"mission_id": mission_id})
        return {"ok": True, "mission": mission, "message": "已标记核对完成，没有重新执行任务", "state": self.snapshot()}

    def lifecycle_transition(self, action):
        if not isinstance(action, str):
            raise CommandError("生命周期 action 必须为字符串")
        if action in {"deactivate", "cleanup", "reset_error"}:
            self.scheduler.control("pause")
        with self._condition:
            state = self.lifecycle.transition(action)
            if not self._closed:
                self._event("info", "生命周期切换：" + action)
        return {"ok": True, **state}

    def plan_input(self, payload, *, session_id="workflow"):
        if not isinstance(payload, dict):
            raise CommandError("任务输入必须为对象")
        selected = [name for name in ("text", "plan", "workflow") if payload.get(name) is not None]
        if len(selected) != 1:
            raise CommandError("请且只提供 text、plan、workflow 中的一种")
        if selected[0] == "workflow":
            from .workflow import compile_workflow
            return compile_workflow(payload["workflow"], self.config, parameters=payload.get("parameters"))
        if selected[0] == "plan":
            return plan_from_dict(payload["plan"], self.config)
        with self._planning_lock:
            result = self._interpret(payload["text"], session_id)
        if result.kind != "task" or result.plan is None:
            raise CommandError(result.message or "该输入尚未生成完整任务，请在任务控制台完成澄清")
        if result.plan.metadata.get("requires_confirmation"):
            self.store.save_session(session_id, result.context)
            raise CommandError("模型或修改计划需要先在任务控制台确认")
        return result.plan

    def enqueue(self, payload):
        allowed = {"text", "plan", "workflow", "parameters", "priority", "run_at", "repeat", "request_id", "session_id"}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise CommandError("排队请求包含未知字段")
        with self._condition:
            generation = self._planning_generation
        plan = self.plan_input(payload, session_id=payload.get("session_id", "queue-planning"))
        return self.scheduler.add(plan, _generation=generation, **{k: payload[k] for k in ("request_id", "session_id", "priority", "run_at", "repeat") if k in payload})

    def save_template(self, payload, identifier=None):
        if not isinstance(payload, dict) or set(payload) - {"name", "description", "workflow"}:
            raise CommandError("模板字段应为 name、description、workflow")
        name, description = payload.get("name"), payload.get("description", "")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100 or not isinstance(description, str) or len(description) > 2000:
            raise CommandError("模板名称或说明无效")
        from .workflow import compile_workflow
        plan = compile_workflow(payload.get("workflow"), self.config)
        old = self.store.template(identifier) if identifier else None
        if identifier and old is None:
            raise CommandError("模板不存在")
        item = {"id": identifier or uuid.uuid4().hex, "name": name.strip(), "description": description,
                "workflow": copy.deepcopy(payload["workflow"]), "compiled_steps": len(plan.steps),
                "created_at": old["created_at"] if old else timestamp(), "updated_at": timestamp(),
                "revision": old.get("revision", 0) + 1 if old else 1}
        self.store.save_template(item)
        return {"ok": True, "template": item}

    def run_template(self, identifier, payload):
        item = self.store.template(identifier)
        if item is None:
            raise CommandError("模板不存在")
        if not isinstance(payload, dict) or set(payload) - {"parameters", "priority", "run_at", "repeat", "request_id", "session_id"}:
            raise CommandError("模板执行参数无效")
        return self.enqueue({"workflow": item["workflow"], **payload})

    def preview_recovery(self, identifier):
        if not isinstance(identifier, str) or not identifier.isalnum() or len(identifier) > 64:
            raise CommandError("任务 ID 格式无效")
        from .recovery import recovery_preview
        with self._condition:
            return recovery_preview(self, identifier)

    def resume_recovery(self, identifier, *, confirmed=False, request_id=None):
        if not isinstance(identifier, str) or not identifier.isalnum() or len(identifier) > 64:
            raise CommandError("任务 ID 格式无效")
        if confirmed is not True:
            raise CommandError("恢复前必须核对剩余计划并明确确认")
        with self._planning_lock, self._condition:
            if request_id:
                existing = self.store.request("recovery:" + str(identifier), request_id)
                if existing:
                    value = json.loads(existing["response"]) if existing["response"] else {"ok": True, "pending": True}
                    return {**value, "duplicate": True, "mission_id": existing["mission_id"], "state": self.snapshot()}
            preview = self.preview_recovery(identifier)
            if preview.get("blocked") or not preview.get("plan"):
                raise CommandError(preview.get("message", "没有可恢复的步骤"))
            plan = plan_from_dict(preview["plan"], self.config)
            from dataclasses import replace
            plan = replace(plan, metadata={**plan.metadata, "requires_confirmation": False, "explicitly_confirmed": True})
            return self.submit_structured(plan, request_id=request_id, session_id="recovery:" + identifier,
                                          _recovery_from=identifier)

    def update_config(self, data):
        from .config import validate_config
        config = validate_config(data)
        with self._planning_lock, self._condition:
            if self._worker and self._worker.is_alive():
                raise CommandError("请先结束任务，再修改配置")
            if config.get("ros") != self.config.get("ros"):
                raise CommandError("ROS 通信端点需修改配置文件并重启；控制台只允许修改地点、路线和任务参数")
            robot = self.adapter.snapshot()
            if any(robot.get(k) for k in ("action_pending", "navigation_pending", "cancellation_pending")):
                raise CommandError("外部执行目标未决，不能修改配置")
            self.store.set_setting("config", config)
            self.config, self.adapter.config = config, config
            self.memory_service.config = config
            if hasattr(self.adapter, "reconfigure"):
                self.adapter.reconfigure(config)
            from .natural_language import DialoguePlanner
            provider = getattr(self._planner, "provider", None) or getattr(self._planner, "_provider", None)
            self._planner = DialoguePlanner(config, provider=provider)
            self._event("info", "地点与任务配置已保存")
        return {"ok": True, "message": "配置已保存", "config": copy.deepcopy(config), "state": self.snapshot()}

    def close(self):
        with self._condition:
            if self._closed:
                return
            self._closed = True
        self.assistive.close()
        if self.scheduler:
            self.scheduler.close()
        self.control("stop", clear_dialogues=False)
        if self._worker and self._worker is not threading.current_thread():
            self._worker.join(timeout=max(8, self.config.get("ros", {}).get("cancel_timeout", 2) + 3))
        self.adapter.close()
        if self._worker and self._worker.is_alive():
            return
        self.store.close()
