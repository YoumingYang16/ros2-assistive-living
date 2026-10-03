"""Consistent backups, reversible archival and staged offline database restore."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid
from .contracts import CommandError
from .store import DatabaseLease


class DataManager:
    def __init__(self, store):
        self.store = store
        self.directory = Path(store.path).parent / "backups" if store.path != ":memory:" else None

    def _directory(self):
        if self.directory is None:
            raise CommandError("内存数据库没有持久化备份目录；请使用 --db 文件路径")
        self.directory.mkdir(parents=True, exist_ok=True)
        return self.directory

    def _path(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"backup-[0-9a-f]{32}", identifier):
            raise CommandError("备份 ID 格式无效")
        path = self._directory() / (identifier + ".sqlite3")
        if not path.is_file():
            raise CommandError("备份不存在")
        return path

    def list_backups(self):
        if self.directory is None or not self.directory.exists():
            return {"ok": True, "backups": []}
        rows = []
        for path in sorted(self.directory.glob("backup-*.json"), reverse=True):
            try:
                rows.append(json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                continue
        return {"ok": True, "backups": sorted(rows, key=lambda row: row["created_at"], reverse=True)}

    def backup(self):
        identifier = "backup-" + uuid.uuid4().hex
        path = self._directory() / (identifier + ".sqlite3")
        self.store.backup_to(path)
        record = {"id": identifier, "created_at": datetime.now(timezone.utc).isoformat(),
                  "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                  "schema_version": 3, "consistent_sqlite_backup": True}
        path.with_suffix(".json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        self.store.event("info", "已创建一致性数据库备份", data={"backup_id": identifier})
        return {"ok": True, "backup": record}

    def verify(self, identifier):
        path = self._path(identifier)
        metadata = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != metadata["sha256"]:
            raise CommandError("备份哈希不匹配，拒绝恢复")
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if integrity != "ok" or version not in {2, 3} or not {"missions", "requests", "sessions"} <= tables:
                raise CommandError("备份结构或完整性检查未通过")
            count = connection.execute("SELECT COUNT(*) FROM missions").fetchone()[0]
        finally:
            connection.close()
        return {"ok": True, "backup_id": identifier, "integrity": integrity, "sha256": digest, "schema_version": version, "missions": count}

    def stage_restore(self, identifier, confirmed=False):
        if confirmed is not True:
            raise CommandError("请核对备份后明确确认恢复")
        result = self.verify(identifier)
        self.store.set_setting("pending_restore", {"backup_id": identifier, "sha256": result["sha256"]})
        self.store.event("warning", "已准备数据库恢复，需关闭服务并执行离线恢复命令", data={"backup_id": identifier})
        return {"ok": True, "restart_required": True, "staged_only": True, "backup_id": identifier,
                "message": "当前数据库未替换。关闭服务后，使用 --restore-backup 备份ID --db 原数据库路径执行离线恢复。"}

    def policy(self):
        return {"ok": True, **(self.store.get_setting("data_policy") or {"retention_days": 90, "auto_archive": False})}

    def set_policy(self, value):
        if not isinstance(value, dict) or set(value) != {"retention_days", "auto_archive"}:
            raise CommandError("策略必须包含 retention_days 与 auto_archive")
        days = value["retention_days"]
        if type(days) is not int or not 1 <= days <= 36500 or type(value["auto_archive"]) is not bool:
            raise CommandError("保留天数需为 1..36500，auto_archive 需为布尔值")
        self.store.set_setting("data_policy", value)
        return self.policy()

    def archive(self, before, states=None, dry_run=True):
        if type(dry_run) is not bool:
            raise CommandError("dry_run 必须为布尔值")
        count = self.store.archive(before, states or ["succeeded", "failed", "cancelled"], dry_run=dry_run)
        if not dry_run and count:
            self.store.event("info", f"已归档 {count} 条任务；可在归档历史查询", data={"before": before})
        return {"ok": True, "count": count, "dry_run": dry_run, "physical_deletion": False}

    def maintenance(self):
        policy = self.policy()
        if policy["auto_archive"]:
            before = datetime.now(timezone.utc) - timedelta(days=policy["retention_days"])
            return self.archive(before.isoformat(), dry_run=False)
        return {"ok": True, "count": 0}


def restore_offline(database, identifier):
    """Restore under the same OS lease; save a pre-restore backup. Never hot-swap."""
    from .store import MissionStore
    database = Path(database).resolve()
    if not database.is_file():
        raise CommandError("待恢复数据库不存在")
    # Opening MissionStore requires exclusive ownership and also validates schema.
    store = MissionStore(database)
    try:
        manager = DataManager(store)
        verified = manager.verify(identifier)
        before = manager.backup()["backup"]
        current_interlock = store.get_setting("hardware_interlock")
        source = sqlite3.connect(manager._path(identifier).as_uri() + "?mode=ro", uri=True)
        staged = sqlite3.connect(":memory:")
        try:
            # Merge unresolved hardware states BEFORE replacing the destination.
            # A crash between backup() and a later settings write must not erase
            # a live uncertainty marker from the pre-restore database.
            source.backup(staged)
            row = staged.execute("SELECT value FROM settings WHERE key='hardware_interlock'").fetchone()
            restored_interlock = json.loads(row[0]) if row else None
            sources = {}
            for record in (restored_interlock, current_interlock):
                if record and record.get("status") in {"pending", "unknown"}:
                    for original in record.get("sources", [record]):
                        sources[original["id"]] = original
            if len(sources) > 100:
                raise CommandError("恢复将合并过多未核实硬件状态，请先完成可信人工核实")
            if sources:
                originals = list(sources.values())
                merged = json.loads(json.dumps(originals[0]))
                merged.update(status="unknown", updated_at=datetime.now(timezone.utc).isoformat(),
                    reason="离线恢复保留恢复前及备份内全部未核实硬件状态；恢复数据库不能解除互锁")
                if len(originals) > 1:
                    merged.update(id=uuid.uuid4().hex, sources=originals,
                        related_mission_ids=sorted({identifier for item in originals for identifier in
                            item.get("related_mission_ids", [item["mission_id"]])}))
                with staged:
                    staged.execute("INSERT INTO settings(key,value) VALUES('hardware_interlock',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                   (json.dumps(merged, ensure_ascii=False, allow_nan=False),))
            with store._lock:
                staged.backup(store._connection)
        finally:
            source.close()
            staged.close()
        store._migrate()
        store.set_setting("queue_paused", True)
        store.set_setting("pending_restore", None)
        store.event("warning", "已完成离线备份恢复；排队任务保持暂停", data={"backup_id": identifier, "pre_restore_backup": before["id"]})
        return {"ok": True, "restored": identifier, "pre_restore_backup": before["id"], "verified": verified}
    finally:
        store.close()
