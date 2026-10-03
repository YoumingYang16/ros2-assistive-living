"""Trusted-host hardware dispatch, independent of ROS native libraries.

The provider is responsible for closed-loop control and physical stop evidence.
This module never interprets a cancel request or elapsed deadline as a stop ack.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import copy
import hashlib
import importlib
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Protocol

from .config import validate_config
from .contracts import CommandError, Step
from .home_skills import HOME_SKILLS, SKILL_CODES, validate_evidence
from .skills import get_registry
from .store import DatabaseLease


class GatewayError(CommandError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DriverCommand:
    request_id: str
    step: Step
    requested_at: str


class HardwareDriver(Protocol):
    """execute returns only after hardware is terminal; stop must not block.

    A failed/raised call is NOT automatically a physical terminal ack. Return
    terminal_confirmed=True with a failed/cancelled receipt once actually safe.
    """
    def capabilities(self) -> dict: ...
    def execute(self, command: DriverCommand, cancel: threading.Event, feedback) -> dict: ...
    def stop(self) -> None: ...
    def snapshot(self) -> dict: ...
    def close(self) -> None: ...


class UnconfiguredDriver:
    def capabilities(self):
        return {}

    def execute(self, command, cancel, feedback):
        raise GatewayError("UNCONFIGURED", "No hardware provider is configured")

    def stop(self):
        pass

    def snapshot(self):
        return {"configured": False}

    def close(self):
        pass


def load_driver(reference, config):
    """Explicit administrator CLI configuration only; never expose via HTTP."""
    if not reference:
        return UnconfiguredDriver()
    if not isinstance(reference, str) or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", reference):
        raise GatewayError("PROVIDER_INVALID", "Provider must be a trusted module:factory")
    module, factory = reference.split(":")
    driver = getattr(importlib.import_module(module), factory)(copy.deepcopy(config))
    if not all(callable(getattr(driver, name, None)) for name in ("capabilities", "execute", "stop", "snapshot", "close")):
        raise GatewayError("PROVIDER_INVALID", "Provider does not implement HardwareDriver")
    return driver


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def parse_goal(goal, config):
    """Validate the typed ExecuteSkill wire fields before creating a Step."""
    fields = {"request_id", "skill", "target", "timeout_seconds", "parameters_json", "camera", "image_format",
              "subject", "duration_seconds", "distance_meters", "angle_degrees"}
    if not isinstance(goal, dict) or set(goal) - fields:
        raise GatewayError("INVALID_GOAL", "Unknown or invalid goal fields")
    request_id = goal.get("request_id")
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_:-]{1,128}", request_id):
        raise GatewayError("INVALID_GOAL", "Invalid request ID")
    code = goal.get("skill")
    if type(code) is not int or code not in SKILL_CODES.values():
        raise GatewayError("INVALID_GOAL", "Unsupported skill code")
    kind = next(name for name, value in SKILL_CODES.items() if value == code)
    timeout = goal.get("timeout_seconds")
    if type(timeout) not in (int, float) or not 0 < timeout <= 3605 or not math.isfinite(timeout):
        raise GatewayError("INVALID_GOAL", "Timeout must be finite and within (0, 3605]")
    target = goal.get("target", "")
    if not isinstance(target, str):
        raise GatewayError("INVALID_GOAL", "Target must be text")
    raw = goal.get("parameters_json", "")
    if not isinstance(raw, str) or len(raw) > 8192:
        raise GatewayError("INVALID_GOAL", "Invalid parameters JSON")
    try:
        params = json.loads(raw, object_pairs_hook=_unique, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value))) if raw else {}
        if not isinstance(params, dict):
            raise ValueError("Parameters must be an object")
        _json(params)
    except (ValueError, TypeError, RecursionError) as exc:
        raise GatewayError("INVALID_GOAL", "Malformed parameters JSON") from exc
    defaults = {"camera": "", "image_format": "", "subject": "", "duration_seconds": 0.0,
                "distance_meters": 0.0, "angle_degrees": 0.0}
    used = {"capture": {"camera", "image_format"}, "follow": {"subject", "duration_seconds", "distance_meters"},
            "turn": {"angle_degrees"}}.get(kind, set())
    for name, default in defaults.items():
        value = goal.get(name, default)
        if name not in used and (type(value) is bool or value != default):
            raise GatewayError("INVALID_GOAL", f"Irrelevant field: {name}")
    if kind not in HOME_SKILLS:
        if params:
            raise GatewayError("INVALID_GOAL", "Legacy skills use typed fields only")
        params = {("format" if name == "image_format" else name): goal.get(name, defaults[name]) for name in used}
    step = Step(kind, target or None, timeout=float(timeout), max_retries=0, step_id=request_id, params=params)
    try:
        step = get_registry().validate_step(step, config)
    except CommandError as exc:
        raise GatewayError("INVALID_GOAL", str(exc)) from exc
    return request_id, step


class HardwareGateway:
    """One physical operation, durable receipts, fail-closed uncertainty.

    execute waits for the provider even after cancellation/timeout. A stuck
    provider therefore keeps the lease and cannot overlap with a second action.
    A crash or invalid provider receipt blocks later actions until reconciled.
    """
    def __init__(self, config, driver=None, *, journal_path=None, provider="unconfigured"):
        self.config = validate_config(copy.deepcopy(config))
        self.driver = driver or UnconfiguredDriver()
        self.provider = str(provider)[:200]
        self._lock = threading.RLock()
        self._active = None
        self._closed = False
        self._closing = False
        self._lease = None
        path = ":memory:"
        if journal_path:
            target = Path(journal_path).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            self._lease = DatabaseLease(target)
            path = str(target)
        try:
            self._db = sqlite3.connect(path, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            tables = {row[0] for row in self._db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if tables - {"hardware_receipts"}:
                raise GatewayError("WRONG_JOURNAL", "Hardware receipts require a separate database, not a mission or other application database")
            if self._db.execute("PRAGMA user_version").fetchone()[0] > 1:
                raise GatewayError("JOURNAL_VERSION", "Hardware receipt database was created by a newer gateway")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            with self._db:
                self._db.execute("CREATE TABLE IF NOT EXISTS hardware_receipts (request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, command TEXT NOT NULL, status TEXT NOT NULL, receipt TEXT, note TEXT NOT NULL DEFAULT '')")
                self._db.execute("UPDATE hardware_receipts SET status='unknown', note='Process ended before a confirmed terminal receipt' WHERE status='running'")
                self._db.execute("PRAGMA user_version=1")
        except Exception:
            if hasattr(self, "_db"):
                self._db.close()
            if self._lease:
                self._lease.close()
            raise

    def capabilities(self):
        try:
            raw = self.driver.capabilities()
            if not isinstance(raw, dict):
                return {}
            result = {name: {"available": True, "simulated": item["simulated"]}
                      for name, item in raw.items() if name in SKILL_CODES and isinstance(item, dict)
                      and item.get("available") is True and type(item.get("simulated")) is bool}
            with self._lock:
                if self._closed or self._closing or self._db.execute("SELECT 1 FROM hardware_receipts WHERE status='unknown' LIMIT 1").fetchone():
                    return {}
            return result
        except Exception:
            return {}

    def _identity(self, goal):
        request_id, step = parse_goal(goal, self.config)
        fingerprint = hashlib.sha256(_json(step.to_dict()).encode()).hexdigest()
        return request_id, step, fingerprint

    def check_goal(self, goal):
        """Read-only admission check; execute acquires the authoritative lease."""
        request_id, step, fingerprint = self._identity(goal)
        with self._lock:
            if self._closed or self._closing:
                raise GatewayError("CLOSED", "Hardware gateway is closed")
            row = self._db.execute("SELECT * FROM hardware_receipts WHERE request_id=?", (request_id,)).fetchone()
            if row and row["fingerprint"] != fingerprint:
                raise GatewayError("REQUEST_CONFLICT", "Request ID reused with different parameters")
            if row and row["status"] not in {"running", "unknown"}:
                return {"replay": True}
            if (row and row["status"] == "unknown") or self._db.execute("SELECT 1 FROM hardware_receipts WHERE status='unknown' LIMIT 1").fetchone():
                raise GatewayError("UNCERTAIN_HARDWARE", "An earlier operation needs physical reconciliation")
            if self._active is not None:
                raise GatewayError("BUSY", "A hardware operation has not returned")
            if step.kind not in self.capabilities():
                raise GatewayError("CAPABILITY_UNAVAILABLE", "Driver has not declared this capability")
        return {"replay": False}

    def _validate_receipt(self, command, receipt, *, simulated):
        if not isinstance(receipt, dict):
            raise GatewayError("INVALID_RECEIPT", "Driver receipt must be an object")
        try:
            encoded = _json(receipt)
        except (ValueError, TypeError, RecursionError) as exc:
            raise GatewayError("INVALID_RECEIPT", "Driver receipt is not finite JSON") from exc
        step = command.step
        valid = (len(encoded) <= 65536 and receipt.get("request_id") == command.request_id
                 and receipt.get("kind") == step.kind and receipt.get("target") == step.target
                 and receipt.get("status") in {"succeeded", "failed", "cancelled"}
                 and receipt.get("terminal_confirmed") is True and type(receipt.get("simulated")) is bool
                 and receipt["simulated"] == simulated and isinstance(receipt.get("evidence"), dict)
                 and bool(receipt["evidence"]))
        if not valid:
            raise GatewayError("INVALID_RECEIPT", "Missing terminal evidence or mismatched receipt identity")
        try:
            observed = datetime.fromisoformat(receipt["observed_at"].replace("Z", "+00:00"))
            started = datetime.fromisoformat(command.requested_at)
            age = (datetime.now(timezone.utc) - observed).total_seconds()
            if observed.tzinfo is None or observed < started or not -1 <= age <= 5:
                raise ValueError("Stale timestamp")
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise GatewayError("INVALID_RECEIPT", "Driver must provide fresh observation time") from exc
        if receipt["status"] == "succeeded":
            evidence = receipt["evidence"]
            if step.kind in HOME_SKILLS:
                validate_evidence(step, evidence)
            elif step.kind == "capture":
                if (not isinstance(receipt.get("media_uri"), str) or not 1 <= len(receipt["media_uri"]) <= 2000
                        or evidence.get("media_created") is not True):
                    raise GatewayError("INVALID_RECEIPT", "Capture needs produced-media evidence")
            elif step.kind == "dock":
                if evidence.get("target") != step.target or evidence.get("docked") is not True or evidence.get("charging_confirmed") is not True:
                    raise GatewayError("INVALID_RECEIPT", "Dock needs docking and charging feedback")
            elif step.kind == "follow":
                if evidence.get("subject") != step.params["subject"] or evidence.get("tracking_confirmed") is not True or evidence.get("motion_complete") is not True:
                    raise GatewayError("INVALID_RECEIPT", "Follow needs tracking and motion-complete feedback")
            elif step.kind == "turn":
                angle = evidence.get("angle_degrees")
                if type(angle) not in (int, float) or not math.isfinite(angle) or abs(angle-step.params["angle_degrees"]) > 2 or evidence.get("motion_complete") is not True:
                    raise GatewayError("INVALID_RECEIPT", "Turn needs measured-angle feedback")
        return copy.deepcopy(receipt)

    def _stop(self):
        try:
            self.driver.stop()
        except Exception:
            # A failed stop call must never release the physical operation lease.
            pass

    def execute(self, goal, cancel=None, feedback=None):
        request_id, step, fingerprint = self._identity(goal)
        with self._lock:
            if cancel is not None and cancel.is_set():
                raise GatewayError("CANCELLED_BEFORE_DISPATCH", "Goal cancelled before hardware dispatch")
            admission = self.check_goal(goal)
            if admission["replay"]:
                row = self._db.execute("SELECT receipt FROM hardware_receipts WHERE request_id=?", (request_id,)).fetchone()
                return {**json.loads(row["receipt"]), "replayed": True}
            capability = self.capabilities().get(step.kind)
            if capability is None:
                raise GatewayError("CAPABILITY_UNAVAILABLE", "Driver capability changed before dispatch")
            command = DriverCommand(request_id, step, datetime.now(timezone.utc).isoformat())
            stored = {"requested_at": command.requested_at, "step": step.to_dict(), "simulated": capability["simulated"]}
            with self._db:
                self._db.execute("INSERT INTO hardware_receipts(request_id,fingerprint,command,status) VALUES(?,?,?,'running')", (request_id, fingerprint, _json(stored)))
            local_cancel, done = threading.Event(), threading.Event()
            self._active = {"request_id": request_id, "cancel": local_cancel}
        value = {}

        def emit(item):
            if feedback is not None and isinstance(item, dict):
                try:
                    if len(_json(item)) <= 4096:
                        feedback(copy.deepcopy(item))
                except Exception:
                    pass

        def invoke():
            try:
                value["receipt"] = self.driver.execute(command, local_cancel, emit)
            except BaseException as exc:
                value["error"] = str(exc)[:1000]
            finally:
                done.set()

        deadline = time.monotonic() + step.timeout
        worker = threading.Thread(target=invoke, name="hardware-driver", daemon=True)
        worker.start()
        interruption = None
        while not done.wait(.01):
            requested = cancel.is_set() if cancel is not None else False
            if interruption is None and (requested or local_cancel.is_set() or time.monotonic() >= deadline):
                interruption = "cancelled" if requested or local_cancel.is_set() else "timed_out"
                local_cancel.set()
                self._stop()
                emit({"phase": "awaiting_stop_ack", "message": "Cancellation requested; waiting for driver terminal evidence"})
        # Cancellation received immediately before a fast completion is still
        # recorded, along with the original driver result (never silently lost).
        if interruption is None and (local_cancel.is_set() or cancel is not None and cancel.is_set()):
            interruption = "cancelled"
        if interruption is None and time.monotonic() >= deadline:
            interruption = "timed_out"
        try:
            if "error" in value:
                raise GatewayError("DRIVER_UNCERTAIN", value["error"])
            receipt = self._validate_receipt(command, value.get("receipt"), simulated=capability["simulated"])
            if interruption:
                receipt = {**receipt, "status": interruption, "driver_status": receipt["status"],
                           "error_code": interruption.upper(), "message": "Driver returned after cancellation/deadline; inspect evidence before another action"}
        except Exception as exc:
            receipt = {"request_id": request_id, "kind": step.kind, "target": step.target,
                       "status": "unknown", "terminal_confirmed": False, "simulated": capability["simulated"],
                       "error_code": getattr(exc, "code", "INVALID_RECEIPT"), "message": str(exc)[:1000],
                       "evidence": {}, "observed_at": datetime.now(timezone.utc).isoformat()}
            self._stop()
        with self._lock:
            # If persistence fails, leave _active and the running row intact.
            with self._db:
                self._db.execute("UPDATE hardware_receipts SET status=?,receipt=? WHERE request_id=?", (receipt["status"], _json(receipt), request_id))
            self._active = None
        return copy.deepcopy(receipt)

    def reconcile(self, request_id, receipt, note):
        """Trusted operator only, after observing hardware; no automatic replay."""
        if not isinstance(note, str) or not 8 <= len(note.strip()) <= 1000:
            raise GatewayError("RECONCILIATION_NOTE", "Record how hardware terminal state was verified")
        with self._lock:
            if self._active is not None:
                raise GatewayError("BUSY", "A live driver cannot be manually reconciled")
            row = self._db.execute("SELECT * FROM hardware_receipts WHERE request_id=?", (request_id,)).fetchone()
            if row is None or row["status"] != "unknown":
                raise GatewayError("NOT_UNCERTAIN", "Only uncertain receipts can be reconciled")
            saved = json.loads(row["command"])
            command = DriverCommand(request_id, Step(**saved["step"]), saved["requested_at"])
            checked = self._validate_receipt(command, receipt, simulated=saved["simulated"])
            checked["reconciled"] = True
            with self._db:
                self._db.execute("UPDATE hardware_receipts SET status=?,receipt=?,note=? WHERE request_id=?", (checked["status"], _json(checked), note.strip(), request_id))
            return checked

    def snapshot(self):
        with self._lock:
            unknown = [row[0] for row in self._db.execute("SELECT request_id FROM hardware_receipts WHERE status='unknown' ORDER BY rowid")]
            return {"provider": self.provider, "active_request_id": self._active["request_id"] if self._active else None,
                    "uncertain_requests": unknown, "closed": self._closed}

    def close(self):
        with self._lock:
            if self._closed and self._active is None:
                return True
            if self._active:
                self._closing = True
                self._active["cancel"].set()
                self._stop()
                return False
            self._closed = True
            self._db.close()
            if self._lease:
                self._lease.close()
        self.driver.close()
        return True


def main():
    """Read-only commissioning report, usable without ROS native libraries."""
    import argparse
    from .config import load_config
    parser = argparse.ArgumentParser(description="Inspect configured hardware provider; never dispatch an operation")
    parser.add_argument("--config", default=str(Path(__file__).parent / "config" / "home.json"))
    parser.add_argument("--provider", default="", help="Explicit trusted module:factory, empty means unavailable")
    args = parser.parse_args()
    config = load_config(args.config)
    driver = load_driver(args.provider, config)
    gateway = HardwareGateway(config, driver, provider=args.provider or "unconfigured")
    try:
        print(json.dumps({"protocol_version": "5", "capabilities": gateway.capabilities(),
                          "gateway": gateway.snapshot(), "native_ros_tested": False,
                          "physical_operation_dispatched": False}, ensure_ascii=False, indent=2))
    finally:
        gateway.close()


if __name__ == "__main__":
    main()
