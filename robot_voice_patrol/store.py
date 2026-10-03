"""SQLite mission journal, idempotency receipts and explicit crash recovery.

The database records a step as running BEFORE dispatch. On process restart,
unfinished missions become interrupted; they are never silently replayed.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any

from .contracts import CommandError

ACTIVE_STATES = {"running", "pausing", "paused", "cancelling"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class DatabaseLease:
    """OS releases this exclusive process lock even after an unexpected crash."""
    def __init__(self, database: Path):
        self.path = Path(str(database) + ".lock")
        self.stream = self.path.open("a+b")
        try:
            self.stream.seek(0, 2)
            if self.stream.tell() == 0:
                self.stream.write(b"0")
                self.stream.flush()
            self.stream.seek(0)
            import os
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            self.stream.close()
            raise RuntimeError("同一任务数据库已被另一个服务占用，请使用不同 --db 路径或关闭原服务") from exc

    def close(self):
        if not self.stream.closed:
            self.stream.close()


class MissionStore:
    def __init__(self, path: str | Path | None = None):
        self.path = str(path) if path else ":memory:"
        self._lock = threading.RLock()
        self._closed = False
        self._lease = None
        self._recorded_observations = {}
        if self.path != ":memory:":
            target = Path(self.path).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            self.path = str(target)
            self._lease = DatabaseLease(target)
        try:
            self._connection = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._migrate()
        except Exception:
            if self._lease:
                self._lease.close()
            raise

    def _migrate(self):
        with self._lock, self._connection:
            version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            if version > 3:
                raise RuntimeError("数据库版本比程序更新，拒绝降级打开")
            self._connection.executescript("""
                CREATE TABLE IF NOT EXISTS missions (
                    id TEXT PRIMARY KEY, state TEXT NOT NULL, snapshot TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS missions_created ON missions(created_at DESC);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, mission_id TEXT,
                    time TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL,
                    data TEXT NOT NULL DEFAULT '{}');
                CREATE INDEX IF NOT EXISTS events_mission ON events(mission_id,id);
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, mission_id TEXT NOT NULL,
                    step_id TEXT NOT NULL, observed_at TEXT NOT NULL, snapshot TEXT NOT NULL,
                    UNIQUE(mission_id,step_id));
                CREATE TABLE IF NOT EXISTS requests (
                    request_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL, mission_id TEXT, response TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, context TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, run_at TEXT NOT NULL,
                    priority INTEGER NOT NULL, snapshot TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    request_key TEXT UNIQUE, fingerprint TEXT);
                CREATE INDEX IF NOT EXISTS jobs_due ON jobs(status,run_at,priority);
                CREATE TABLE IF NOT EXISTS templates (
                    id TEXT PRIMARY KEY,name TEXT NOT NULL,snapshot TEXT NOT NULL,
                    created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS migrations (
                    version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL);
            """)
            columns = {r[1] for r in self._connection.execute("PRAGMA table_info(missions)")}
            if "archived" not in columns:
                self._connection.execute("ALTER TABLE missions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
            columns = {r[1] for r in self._connection.execute("PRAGMA table_info(observations)")}
            for name in ("object_name", "target", "outcome"):
                if name not in columns:
                    self._connection.execute(f"ALTER TABLE observations ADD COLUMN {name} TEXT")
            for row in self._connection.execute("SELECT id,snapshot FROM observations WHERE outcome IS NULL").fetchall():
                value = json.loads(row["snapshot"])
                self._connection.execute("UPDATE observations SET object_name=?,target=?,outcome=? WHERE id=?",
                    (value.get("object_name", ""), value.get("target"), value.get("outcome", "observed"), row["id"]))
            self._connection.execute("CREATE INDEX IF NOT EXISTS observations_search ON observations(object_name,target,observed_at)")
            self._connection.execute("INSERT OR IGNORE INTO migrations VALUES(3,?)", (now(),))
            self._connection.execute("PRAGMA user_version=3")

    def recover_interrupted(self) -> list[dict]:
        recovered = []
        with self._lock, self._connection:
            rows = self._connection.execute("SELECT snapshot FROM missions WHERE state IN ('running','pausing','paused','cancelling')").fetchall()
            for row in rows:
                mission = json.loads(row["snapshot"])
                previous = mission["state"]
                mission.update(state="interrupted", finished_at=now(),
                               error="上次进程在任务结束前退出，执行状态需要核对；不会自动重放",
                               recovery_required=True, previous_state=previous)
                for step in mission.get("step_states", []):
                    if step.get("status") in {"running", "retrying"}:
                        step["status"] = "interrupted"
                mission["report"] = {"status": "interrupted", "message": mission["error"],
                                     "completed_steps": sum(r.get("status") == "succeeded" for r in mission.get("results", [])),
                                     "total_steps": len(mission.get("steps", [])), "observations": [],
                                     "simulated": mission.get("mode") == "mock"}
                self._save_mission_locked(mission)
                recovered.append(mission)
            self._connection.execute("UPDATE requests SET status='interrupted',response=?,updated_at=? WHERE status='reserved'",
                                     (encode({"ok": False, "code": "request_interrupted", "message": "请求处理期间服务重启，请查询任务历史后重新决定"}), now()))
        return recovered

    def _save_mission_locked(self, mission: dict):
        self._connection.execute("""INSERT INTO missions(id,state,snapshot,created_at,updated_at) VALUES(?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET state=excluded.state,snapshot=excluded.snapshot,updated_at=excluded.updated_at""",
                                 (mission["id"], mission["state"], encode(mission), mission["started_at"], now()))

    def save_mission(self, mission: dict, *, request=None, recovery_from=None):
        with self._lock, self._connection:
            if recovery_from is not None:
                row = self._connection.execute("SELECT snapshot FROM missions WHERE id=?", (recovery_from,)).fetchone()
                if not row:
                    raise CommandError("恢复来源任务不存在")
                original = json.loads(row[0])
                if original.get("resumed_as"):
                    raise CommandError("该任务已创建恢复任务，请查看关联任务：" + original["resumed_as"])
                if original["state"] not in {"interrupted", "failed", "cancelled"}:
                    raise CommandError("恢复来源任务必须已中断、失败或取消")
                original.update(recovery_required=False, resumed_as=mission["id"])
                self._save_mission_locked(original)
            self._save_mission_locked(mission)
            if request is not None:
                self._connection.execute("UPDATE requests SET mission_id=?,updated_at=? WHERE request_key=? AND status='reserved'",
                                         (mission["id"], now(), self.request_key(*request)))
            identifiers = self._recorded_observations.get(mission["id"])
            if identifiers is None:
                identifiers = {r[0] for r in self._connection.execute("SELECT step_id FROM observations WHERE mission_id=?", (mission["id"],))}
            else:
                identifiers = set(identifiers)
            for result in mission.get("results", []):
                if result.get("kind") == "inspect" and result.get("status") == "succeeded":
                    if result["step_id"] in identifiers:
                        continue
                    self._connection.execute("""INSERT OR IGNORE INTO observations
                        (mission_id,step_id,observed_at,snapshot,object_name,target,outcome) VALUES(?,?,?,?,?,?,?)""",
                        (mission["id"], result["step_id"], result.get("observed_at", now()), encode(result),
                         result.get("object_name", ""), result.get("target"), result.get("outcome", "observed")))
                    identifiers.add(result["step_id"])
        # Publish the cache only after commit; a rolled-back observation must
        # remain eligible for a later insert.
        with self._lock:
            self._recorded_observations[mission["id"]] = identifiers
            if len(self._recorded_observations) > 128:
                self._recorded_observations.pop(next(iter(self._recorded_observations)))

    def event(self, level: str, message: str, mission_id=None, data=None) -> dict:
        event = {"time": now(), "level": level, "message": message}
        with self._lock, self._connection:
            cursor = self._connection.execute("INSERT INTO events(mission_id,time,level,message,data) VALUES(?,?,?,?,?)",
                                              (mission_id, event["time"], level, message, encode(data or {})))
            event["id"] = cursor.lastrowid
        return event

    def events(self, mission_id=None, limit=200):
        limit = max(1, min(int(limit), 1000))
        with self._lock:
            if mission_id:
                rows = self._connection.execute("SELECT * FROM events WHERE mission_id=? ORDER BY id DESC LIMIT ?", (mission_id, limit)).fetchall()
            else:
                rows = self._connection.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [{"id": r["id"], "time": r["time"], "level": r["level"], "message": r["message"],
                     "mission_id": r["mission_id"], "data": json.loads(r["data"])} for r in reversed(rows)]

    @staticmethod
    def request_key(session_id: str, request_id: str) -> str:
        for value in (session_id, request_id):
            if not isinstance(value, str) or not value or len(value) > 128 or any(ord(c) < 32 for c in value):
                raise CommandError("session_id / request_id 格式无效")
        return encode([session_id, request_id])

    def reserve_request(self, session_id, request_id, text):
        key = self.request_key(session_id, request_id)
        fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self._lock, self._connection:
            existing = self._connection.execute("SELECT * FROM requests WHERE request_key=?", (key,)).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise CommandError("同一 request_id 不能用于不同指令")
                return dict(existing)
            self._connection.execute("INSERT INTO requests VALUES(?,?,?,?,?,?,?)",
                                     (key, fingerprint, "reserved", None, None, now(), now()))
        return None

    def finish_request(self, session_id, request_id, response, mission_id=None):
        # Persist only receipt metadata, not a stale full robot snapshot.
        response = {k: v for k, v in response.items() if k != "state"}
        with self._lock, self._connection:
            self._connection.execute("UPDATE requests SET status=?,mission_id=COALESCE(?,mission_id),response=?,updated_at=? WHERE request_key=?",
                                     ("accepted" if response.get("ok") else "rejected", mission_id, encode(response), now(),
                                      self.request_key(session_id, request_id)))

    def session(self, session_id):
        self.request_key(session_id, "context")
        with self._lock:
            row = self._connection.execute("SELECT context FROM sessions WHERE id=?", (session_id,)).fetchone()
            return json.loads(row[0]) if row else {}

    def save_session(self, session_id, context):
        self.request_key(session_id, "context")
        content = encode(context)
        if len(content) > 131072:
            raise CommandError("对话上下文过大")
        with self._lock, self._connection:
            self._connection.execute("INSERT INTO sessions VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET context=excluded.context,updated_at=excluded.updated_at",
                                     (session_id, content, now()))

    def clear_pending_dialogues(self):
        with self._lock, self._connection:
            rows = self._connection.execute("SELECT id,context FROM sessions").fetchall()
            for row in rows:
                context = json.loads(row["context"])
                for key in ("pending_plan", "clarification", "model_clarification", "pending_schedule"):
                    context.pop(key, None)
                self._connection.execute("UPDATE sessions SET context=?,updated_at=? WHERE id=?", (encode(context), now(), row["id"]))

    def _history_where(self, *, query="", state="", since="", until="", archived=False):
        clauses, values = ["archived=?"], [int(bool(archived))]
        if state:
            if state not in ACTIVE_STATES | {"succeeded", "failed", "cancelled", "interrupted"}:
                raise CommandError("未知任务状态")
            clauses.append("state=?")
            values.append(state)
        for name, value, op in (("since", since, ">="), ("until", until, "<=")):
            if value:
                from .scheduling import parse_instant
                value = parse_instant(value).isoformat()
                clauses.append(f"julianday(created_at) {op} julianday(?)")
                values.append(value)
        if query:
            if not isinstance(query, str) or len(query) > 200:
                raise CommandError("搜索文本最长 200 字")
            clauses.append("snapshot LIKE ? ESCAPE '\\'")
            values.append("%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        return " AND ".join(clauses), values

    def history(self, limit=20, offset=0, **filters):
        limit, offset = max(1, min(int(limit), 200)), max(0, int(offset))
        where, values = self._history_where(**filters)
        with self._lock:
            rows = self._connection.execute(f"SELECT snapshot,archived FROM missions WHERE {where} ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", (*values, limit, offset)).fetchall()
        return [{**json.loads(r[0]), "archived": bool(r[1])} for r in rows]

    def history_count(self, **filters):
        where, values = self._history_where(**filters)
        with self._lock:
            return self._connection.execute(f"SELECT COUNT(*) FROM missions WHERE {where}", values).fetchone()[0]

    def search_observations(self, *, object_name="", target="", outcome="", since="", until="", limit=100):
        clauses, values = [], []
        for name, value in (("object_name", object_name), ("target", target), ("outcome", outcome)):
            if value:
                if not isinstance(value, str) or len(value) > 100:
                    raise CommandError("观察查询参数格式错误")
                clauses.append(name + "=?")
                values.append(value)
        for value, op in ((since, ">="), (until, "<=")):
            if value:
                from .scheduling import parse_instant
                clauses.append("julianday(observed_at) " + op + " julianday(?)")
                values.append(parse_instant(value).isoformat())
        where = " AND ".join(clauses) or "1=1"
        with self._lock:
            rows = self._connection.execute(f"SELECT * FROM observations WHERE {where} ORDER BY observed_at DESC,id DESC LIMIT ?",
                (*values, max(1, min(int(limit), 500)))).fetchall()
        return [{**json.loads(r["snapshot"]), "observation_id": r["id"], "mission_id": r["mission_id"]} for r in rows]

    def jobs(self, *, include_finished=True, limit=200):
        where = "1=1" if include_finished else "status IN ('queued','dispatching','running')"
        with self._lock:
            rows = self._connection.execute(f"SELECT snapshot FROM jobs WHERE {where} ORDER BY run_at,priority DESC,created_at LIMIT ?", (max(1, min(limit, 1000)),)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def job(self, identifier):
        with self._lock:
            row = self._connection.execute("SELECT snapshot FROM jobs WHERE id=?", (identifier,)).fetchone()
        return json.loads(row[0]) if row else None

    def create_job(self, job, request_key, fingerprint):
        with self._lock, self._connection:
            existing = self._connection.execute("SELECT snapshot,fingerprint FROM jobs WHERE request_key=?", (request_key,)).fetchone()
            if existing:
                if existing[1] != fingerprint:
                    raise CommandError("同一排队请求 ID 不能用于不同任务")
                return json.loads(existing[0]), True
            pending = self._connection.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running','dispatching')").fetchone()[0]
            if pending >= 500:
                raise CommandError("待执行任务已达到 500 项上限，请先处理已有任务")
            self._connection.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)",
                (job["id"], job["status"], job["run_at"], job["priority"], encode(job), job["created_at"], now(), request_key, fingerprint))
        return job, False

    def save_job(self, job, *, audit=None):
        with self._lock, self._connection:
            self._connection.execute("UPDATE jobs SET status=?,run_at=?,priority=?,snapshot=?,updated_at=? WHERE id=?",
                (job["status"], job["run_at"], job["priority"], encode(job), now(), job["id"]))
            if audit is not None:
                self._connection.execute("INSERT INTO events(mission_id,time,level,message,data) VALUES(?,?,?,?,?)",
                    (job.get("mission_id"), now(), "info", "已更新排队任务的调度设置", encode(audit)))

    def recover_jobs(self):
        with self._lock, self._connection:
            rows = self._connection.execute("SELECT snapshot FROM jobs WHERE status IN ('dispatching','running')").fetchall()
            for row in rows:
                job = json.loads(row[0])
                # dispatch request is deterministic; retain linked mission even if process died before job receipt.
                key = self.request_key("queue", f"{job['id']}:{job.get('occurrence', 0)}")
                request = self._connection.execute("SELECT mission_id FROM requests WHERE request_key=?", (key,)).fetchone()
                if request and request[0]:
                    job["mission_id"] = request[0]
                mission = self.mission(job.get("mission_id", ""))
                state = mission["state"] if mission and mission["state"] not in ACTIVE_STATES else "interrupted"
                from .home_skills import SKILL_CODES
                if mission and state != "succeeded" and any(step.get("kind") in SKILL_CODES for step in mission.get("steps", [])):
                    job["repeat"] = None
                if mission and state in {"succeeded", "failed", "cancelled"} and job.get("repeat") and not job.get("cancel_requested"):
                    from .scheduling import next_occurrence, parse_instant
                    job["last_runs"] = (job.get("last_runs", []) + [{"mission_id": mission["id"], "status": state, "finished_at": mission.get("finished_at")}])[-50:]
                    job.update(status="queued", occurrence=job.get("occurrence", 0) + 1, mission_id=None,
                               run_at=next_occurrence(job["repeat"], datetime.now(timezone.utc), anchor=parse_instant(job["run_at"])).isoformat(),
                               error=None, recovery_required=False)
                else:
                    job.update(status=state, error="服务重启，已核对已有任务记录；不会自动重复派发",
                               recovery_required=state == "interrupted")
                self.save_job(job)

    def templates(self):
        with self._lock:
            rows = self._connection.execute("SELECT snapshot FROM templates ORDER BY updated_at DESC").fetchall()
        return [json.loads(r[0]) for r in rows]

    def template(self, identifier):
        with self._lock:
            row = self._connection.execute("SELECT snapshot FROM templates WHERE id=?", (identifier,)).fetchone()
        return json.loads(row[0]) if row else None

    def request(self, session_id, request_id):
        with self._lock:
            row = self._connection.execute("SELECT * FROM requests WHERE request_key=?", (self.request_key(session_id, request_id),)).fetchone()
        return dict(row) if row else None

    def save_template(self, value):
        with self._lock, self._connection:
            self._connection.execute("INSERT INTO templates VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,snapshot=excluded.snapshot,updated_at=excluded.updated_at",
                (value["id"], value["name"], encode(value), value["created_at"], now()))

    def delete_template(self, identifier):
        with self._lock, self._connection:
            count = self._connection.execute("DELETE FROM templates WHERE id=?", (identifier,)).rowcount
        if not count:
            raise CommandError("模板不存在")

    def archive(self, before, states, *, dry_run=True):
        from .scheduling import parse_instant
        before = parse_instant(before).isoformat()
        permitted = {"succeeded", "failed", "cancelled", "interrupted"}
        if not isinstance(states, list) or not states or any(s not in permitted for s in states):
            raise CommandError("只能归档已结束任务")
        placeholders = ",".join("?" for _ in states)
        where = f"archived=0 AND created_at<? AND state IN ({placeholders}) AND COALESCE(json_extract(snapshot,'$.recovery_required'),0)=0"
        with self._lock, self._connection:
            count = self._connection.execute(f"SELECT COUNT(*) FROM missions WHERE {where}", (before, *states)).fetchone()[0]
            if not dry_run:
                self._connection.execute(f"UPDATE missions SET archived=1 WHERE {where}", (before, *states))
        return count

    def backup_to(self, destination):
        with self._lock:
            connection = sqlite3.connect(str(destination))
            try:
                self._connection.backup(connection)
                result = connection.execute("PRAGMA integrity_check").fetchone()[0]
                if result != "ok":
                    raise RuntimeError("备份完整性检查失败")
            finally:
                connection.close()

    def unarchive(self, identifiers):
        if not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 200 or any(not isinstance(x, str) or not x.isalnum() for x in identifiers):
            raise CommandError("请提供 1..200 个任务 ID")
        with self._lock, self._connection:
            cursor = self._connection.execute(f"UPDATE missions SET archived=0 WHERE id IN ({','.join('?' for _ in identifiers)}) AND archived=1", identifiers)
            return cursor.rowcount

    def events_page(self, *, mission_id=None, after=0, limit=100):
        with self._lock:
            rows = self._connection.execute("SELECT * FROM events WHERE id>? AND (? IS NULL OR mission_id=?) ORDER BY id LIMIT ?",
                (max(0, int(after)), mission_id, mission_id, max(1, min(int(limit), 1000)))).fetchall()
        return [{"id": r["id"], "time": r["time"], "level": r["level"], "message": r["message"],
                 "mission_id": r["mission_id"], "data": json.loads(r["data"])} for r in rows]

    def mission(self, mission_id):
        with self._lock:
            row = self._connection.execute("SELECT snapshot FROM missions WHERE id=?", (mission_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def observations(self, limit=100):
        with self._lock:
            rows = self._connection.execute("SELECT snapshot FROM observations ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 1000)),)).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

    def dismiss_recovery(self, mission_id):
        with self._lock, self._connection:
            row = self._connection.execute("SELECT snapshot FROM missions WHERE id=?", (mission_id,)).fetchone()
            if not row:
                raise CommandError("任务不存在")
            mission = json.loads(row[0])
            if mission["state"] != "interrupted":
                raise CommandError("只能核对被中断的历史任务")
            mission.update(recovery_required=False, recovery_acknowledged_at=now())
            self._save_mission_locked(mission)
            return mission

    def recoveries(self):
        with self._lock:
            rows = self._connection.execute("SELECT snapshot FROM missions WHERE state='interrupted'").fetchall()
        return [m for m in (json.loads(r[0]) for r in rows) if m.get("recovery_required")]

    def get_setting(self, key):
        with self._lock:
            row = self._connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_setting(self, key, value):
        with self._lock, self._connection:
            self._connection.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, encode(value)))

    def metrics(self):
        with self._lock:
            counts = {r[0]: r[1] for r in self._connection.execute("SELECT state,COUNT(*) FROM missions GROUP BY state")}
            observations = self._connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            requests = self._connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
            archived = self._connection.execute("SELECT COUNT(*) FROM missions WHERE archived=1").fetchone()[0]
            durations = self._connection.execute("SELECT AVG((julianday(json_extract(snapshot,'$.finished_at'))-julianday(created_at))*86400) FROM missions WHERE state NOT IN ('running','paused','pausing','cancelling')").fetchone()[0]
            failures = {r[0] or "UNKNOWN": r[1] for r in self._connection.execute("SELECT json_extract(snapshot,'$.error_code'),COUNT(*) FROM missions WHERE state='failed' GROUP BY 1")}
        return {"missions_total": sum(counts.values()), "missions_by_state": counts,
                "observations_total": observations, "requests_total": requests,
                "archived_total": archived, "mean_duration_seconds": round(durations or 0, 3), "failures_by_code": failures,
                "storage": "memory" if self.path == ":memory:" else "sqlite", "schema_version": 3}

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._connection.close()
            if self._lease:
                self._lease.close()
