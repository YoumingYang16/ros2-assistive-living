"""Single-resource durable queue with explicit restart and recurrence semantics."""
from __future__ import annotations
import copy
from datetime import datetime, timezone
import hashlib
import json
import threading
import time
import uuid
from .contracts import CommandError
from .plan_validation import plan_from_dict, validate_plan
from .scheduling import parse_instant, validate_repeat, next_occurrence
from .store import ACTIVE_STATES, encode, now


def config_fingerprint(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class TaskScheduler:
    def __init__(self, engine, *, clock=None, start=True):
        self.engine, self.store = engine, engine.store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._shutdown = threading.Event()
        self.store.recover_jobs()
        self.paused = bool(self.store.jobs(include_finished=False, limit=1000)) or bool(self.store.get_setting("queue_paused"))
        self.last_error = None
        self._last_maintenance = time.monotonic()
        self._thread = None
        if start:
            self._thread = threading.Thread(target=self._run, name="task-scheduler", daemon=True)
            self._thread.start()

    def summary(self):
        jobs = self.store.jobs(include_finished=False, limit=1000)
        return {"paused": self.paused, "pending": sum(j["status"] == "queued" for j in jobs),
                "running": sum(j["status"] in {"running", "dispatching"} for j in jobs), "last_error": self.last_error}

    def snapshot(self):
        return {"ok": True, "jobs": self.store.jobs(limit=1000), **self.summary(), "restart_policy": "pause_pending",
                "misfire_policy": "skip_missed_recurrences", "motion_concurrency": 1}

    def add(self, plan, *, request_id=None, session_id="default", priority=50, run_at=None, repeat=None, _generation=None):
        if type(priority) is not int or not 0 <= priority <= 100:
            raise CommandError("优先级必须为 0 到 100 的整数")
        plan = validate_plan(copy.deepcopy(plan), self.engine.config)
        if plan.metadata.get("requires_confirmation"):
            raise CommandError("请先确认计划再加入执行队列")
        repeat = validate_repeat(repeat)
        instant = self.clock()
        due = parse_instant(run_at) if run_at else next_occurrence(repeat, instant) if repeat and "daily_at" in repeat else instant
        payload = {"plan": plan.to_dict(), "priority": priority, "run_at": run_at, "repeat": repeat}
        fingerprint = hashlib.sha256(encode(payload).encode()).hexdigest()
        key = self.store.request_key(session_id, request_id or uuid.uuid4().hex)
        job = {"id": uuid.uuid4().hex, "status": "queued", "created_at": now(), "run_at": due.isoformat(),
               "priority": priority, "repeat": repeat, "plan": plan.to_dict(), "summary": plan.summary,
               "config_fingerprint": config_fingerprint(self.engine.config), "occurrence": 0,
               "mission_id": None, "last_runs": [], "error": None, "session_id": session_id}
        with self._lock:
            with self.engine._condition:
                if self.engine._closed or (_generation is not None and _generation != self.engine._planning_generation):
                    raise CommandError("规划期间收到停止或关闭请求，未加入队列")
                job, duplicate = self.store.create_job(job, key, fingerprint)
        return {"ok": True, "job": job, "duplicate": duplicate, "message": "任务已加入队列", "state": self.engine.snapshot()}

    def control(self, action):
        if not isinstance(action, str) or action not in {"pause", "resume"}:
            raise CommandError("队列控制仅支持 pause / resume")
        with self._lock:
            self.paused = action == "pause"
            self.store.set_setting("queue_paused", self.paused)
        return self.snapshot()

    def update(self, identifier, payload):
        if not isinstance(payload, dict) or not payload or set(payload) - {"priority", "run_at", "repeat"}:
            raise CommandError("调度修改仅接受 priority、run_at、repeat，且至少提供一项")
        changes = copy.deepcopy(payload)
        if "priority" in changes and (type(changes["priority"]) is not int or not 0 <= changes["priority"] <= 100):
            raise CommandError("优先级必须为 0 到 100 的整数")
        if "run_at" in changes:
            changes["run_at"] = parse_instant(changes["run_at"]).isoformat()
        if "repeat" in changes:
            changes["repeat"] = validate_repeat(changes["repeat"])
        with self._lock, self.engine._condition:
            if self.engine._closed:
                raise CommandError("服务已关闭")
            job = self.store.job(identifier)
            if job is None:
                raise CommandError("排队任务不存在")
            if job["status"] != "queued":
                raise CommandError("只能修改尚未派发的排队任务")
            before = {key: copy.deepcopy(job.get(key)) for key in changes}
            job.update(changes, updated_at=now(), revision=job.get("revision", 0) + 1)
            self.store.save_job(job, audit={"action": "schedule_updated", "job_id": identifier,
                "revision": job["revision"], "before": before, "after": changes})
        return {"ok": True, "job": job, "message": "排队任务的调度设置已更新"}

    def cancel(self, identifier):
        with self._lock, self.engine._condition:
            job = self.store.job(identifier)
            if job is None:
                raise CommandError("排队任务不存在")
            if job["status"] in {"running", "dispatching"}:
                job["repeat"] = None
                job["cancel_requested"] = True
                mission = self.store.mission(job.get("mission_id", ""))
                if mission and mission["state"] not in ACTIVE_STATES:
                    job["last_runs"] = (job.get("last_runs", []) + [{"mission_id": mission["id"],
                        "status": mission["state"], "finished_at": mission.get("finished_at")}])[-50:]
                    job.update(status=mission["state"], finished_at=mission.get("finished_at"))
                self.store.save_job(job)
                # A completed queue mission may await its bookkeeping tick
                # while a newer manual mission runs. Cancel only this job's
                # own live mission, never the unrelated current mission.
                current = self.engine._mission
                if current and current["id"] == job.get("mission_id") and self.engine._state in ACTIVE_STATES:
                    self.engine.control("stop")
            elif job["status"] == "queued":
                job.update(status="cancelled", repeat=None, finished_at=now())
                self.store.save_job(job)
            return {"ok": True, "job": self.store.job(identifier), "message": "已取消排队或请求停止当前任务"}

    def tick(self):
        # Serialize readiness, config verification and dispatch with direct
        # submissions/config changes; no gap may turn queued work into a stale
        # dispatch or a permanent "busy" rejection.
        with self._lock, self.engine._condition:
            jobs = self.store.jobs(include_finished=False, limit=1000)
            for job in jobs:
                if job["status"] != "running":
                    continue
                mission = self.store.mission(job.get("mission_id", ""))
                if not mission or mission["state"] in ACTIVE_STATES:
                    return
                job["last_runs"] = (job.get("last_runs", []) + [{"mission_id": mission["id"], "status": mission["state"], "finished_at": mission.get("finished_at")}])[-50:]
                from .home_skills import SKILL_CODES
                if mission["state"] != "succeeded" and any(step.get("kind") in SKILL_CODES for step in mission.get("steps", [])):
                    job.update(repeat=None, error="外部机械任务未成功结束，已停止周期重放；核实后请创建新任务")
                if job.get("repeat") and not job.get("cancel_requested"):
                    job.update(status="queued", occurrence=job["occurrence"] + 1, mission_id=None,
                               run_at=next_occurrence(job["repeat"], self.clock(), anchor=parse_instant(job["run_at"])).isoformat())
                else:
                    job.update(status=mission["state"], finished_at=mission.get("finished_at"))
                self.store.save_job(job)
            if self.paused or self.engine._closed or not self.engine.can_dispatch():
                return
            due = [j for j in self.store.jobs(include_finished=False, limit=1000) if j["status"] == "queued" and parse_instant(j["run_at"]) <= self.clock()]
            if not due:
                return
            # Priority wins among ready work; timestamps preserve ordering at equal priority.
            job = sorted(due, key=lambda j: (-j["priority"], j["run_at"], j["created_at"]))[0]
            if job["config_fingerprint"] != config_fingerprint(self.engine.config):
                job.update(status="blocked", error="地点或任务配置已变化，请重新预览并加入队列")
                self.store.save_job(job)
                return
            job.update(status="dispatching")
            self.store.save_job(job)
            try:
                plan = plan_from_dict(job["plan"], self.engine.config)
                receipt = self.engine.submit_structured(plan, request_id=f"{job['id']}:{job['occurrence']}", session_id="queue")
                if not receipt.get("ok") or not receipt.get("mission_id"):
                    raise CommandError(receipt.get("message", "排队任务派发未确认"))
                job.update(status="running", mission_id=receipt["mission_id"], error=None)
            except CommandError as exc:
                job.update(status="blocked", error=str(exc))
            except Exception as exc:
                job.update(status="interrupted", error="派发异常，需核对任务历史", recovery_required=True)
                self.last_error = type(exc).__name__
            self.store.save_job(job)

    def _run(self):
        while not self._shutdown.wait(.1):
            try:
                self.tick()
                if time.monotonic() - self._last_maintenance >= 60:
                    self.engine.data.maintenance()
                    self._last_maintenance = time.monotonic()
            except Exception as exc:
                self.last_error = type(exc).__name__
                self.paused = True

    def close(self):
        self._shutdown.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)
