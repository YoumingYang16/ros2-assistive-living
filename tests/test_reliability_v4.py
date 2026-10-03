"""Deterministic regression checks for draft, dispatch and recovery boundaries."""
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from robot_voice_patrol.config import load_config
from robot_voice_patrol.contracts import CommandError, Plan, Step
from robot_voice_patrol.engine import MissionEngine
from robot_voice_patrol.mock_adapter import MockAdapter
from robot_voice_patrol.plan_validation import validate_plan
from robot_voice_patrol.store import encode
from tests.test_queue_data_v3 import finish


class ReliabilityV4Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = load_config()
        self.config["mock"].update(travel_seconds=.01, inspection_seconds=.01)
        self.db = Path(self.temp.name) / "missions.sqlite3"
        self.engines = []
        self.engine = self.make()

    def make(self):
        engine = MissionEngine(self.config, MockAdapter(self.config), db_path=self.db, start_scheduler=False)
        self.engines.append(engine)
        return engine

    def tearDown(self):
        for engine in self.engines:
            engine.close()
        self.temp.cleanup()

    def plan(self):
        return Plan("go", [Step("navigate", "home")], "go home")

    def checkpoint(self):
        plan = validate_plan(Plan("go and return", [Step("navigate", "meeting_room", step_id="go"),
            Step("navigate", "home", step_id="back")], "go and return"), self.config)
        mission = {"id": "source1", **plan.to_dict(), "state": "interrupted", "mode": "mock",
            "started_at": datetime.now(timezone.utc).isoformat(), "results": [], "step_states": [],
            "recovery_required": True}
        self.engine.store.save_mission(mission)
        return mission

    def test_failed_schedule_does_not_attach_to_later_command(self):
        with self.assertRaises(CommandError):
            self.engine.submit("每天九点删除数据")
        self.assertNotIn("pending_schedule", self.engine.store.session("default"))
        response = self.engine.submit("去会议室")
        self.assertNotIn("job", response)
        self.assertEqual(finish(self.engine)["robot"]["location"], "meeting_room")
        self.assertEqual(self.engine.scheduler.summary()["pending"], 0)

    def test_failed_new_schedule_preserves_existing_unscheduled_draft(self):
        self.engine.preview("去会议室")
        before = self.engine.store.session("default")
        with self.assertRaises(CommandError):
            self.engine.submit("每天九点删除数据")
        self.assertEqual(self.engine.store.session("default"), before)
        self.assertNotIn("job", self.engine.submit("确认执行"))
        finish(self.engine)

    def test_new_task_replaces_scheduled_clarification_without_inheriting_schedule(self):
        self.assertTrue(self.engine.submit("每天九点找水杯")["needs_clarification"])
        self.assertNotIn("job", self.engine.submit("去前台"))
        self.assertNotIn("pending_schedule", self.engine.store.session("default"))
        self.assertEqual(finish(self.engine)["robot"]["location"], "reception")

    def test_preview_replaces_scheduled_draft_without_inheriting_schedule(self):
        self.engine.preview("每天九点去会议室")
        self.engine.preview("去前台")
        self.assertNotIn("pending_schedule", self.engine.store.session("default"))
        self.assertNotIn("job", self.engine.submit("确认执行"))
        self.assertEqual(finish(self.engine)["robot"]["location"], "reception")

    def test_schedule_survives_explicit_draft_edit_and_confirmation(self):
        self.engine.preview("每天九点去会议室")
        self.assertTrue(self.engine.submit("改去前台")["needs_confirmation"])
        response = self.engine.submit("确认执行")
        self.assertEqual(response["job"]["repeat"]["daily_at"], "09:00")
        self.assertEqual(response["job"]["plan"]["steps"][0]["target"], "reception")
        self.assertEqual(self.engine.metrics()["missions_total"], 0)

    def test_schedule_requires_task_not_a_help_answer(self):
        with self.assertRaises(CommandError):
            self.engine.submit("每天九点帮助")
        self.assertNotIn("pending_schedule", self.engine.store.session("default"))

    def test_job_update_persists_and_original_request_replay_does_not_revert_it(self):
        scheduler = self.engine.scheduler
        original = scheduler.add(self.plan(), request_id="original")["job"]
        later = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        updated = scheduler.update(original["id"], {"priority": 99, "run_at": later,
            "repeat": {"interval_seconds": 60}})["job"]
        self.assertEqual(updated["revision"], 1)
        self.assertEqual(scheduler.add(self.plan(), request_id="original")["job"], updated)
        scheduler.tick()
        self.assertEqual(self.engine.metrics()["missions_total"], 0)
        self.engine.close()
        replacement = self.make()
        self.assertEqual(replacement.store.job(original["id"]), updated)
        audit = [e["data"] for e in replacement.store.events() if e["data"].get("action") == "schedule_updated"]
        self.assertEqual(audit[0]["before"]["priority"], 50)
        self.assertEqual(audit[0]["after"]["priority"], 99)

    def test_job_update_can_disable_recurrence(self):
        job = self.engine.scheduler.add(self.plan(), repeat={"interval_seconds": 60})["job"]
        value = self.engine.scheduler.update(job["id"], {"repeat": None})
        self.assertIsNone(value["job"]["repeat"])
        self.engine.scheduler.tick()
        finish(self.engine)
        self.engine.scheduler.tick()
        self.assertEqual(self.engine.store.job(job["id"])["status"], "succeeded")

    def test_invalid_job_edit_is_atomic(self):
        job = self.engine.scheduler.add(self.plan())["job"]
        for payload in ({}, [], {"priority": True}, {"priority": 101}, {"run_at": None},
                        {"run_at": "2026-10-03T09:00:00"}, {"repeat": {"interval_seconds": 0}},
                        {"priority": 99, "plan": {}}, {"status": "queued"}):
            with self.subTest(payload=payload), self.assertRaises(CommandError):
                self.engine.scheduler.update(job["id"], payload)
            self.assertEqual(self.engine.store.job(job["id"]), job)

    def test_only_queued_jobs_can_be_edited(self):
        job = self.engine.scheduler.add(self.plan())["job"]
        for status in ("dispatching", "running", "blocked", "interrupted", "succeeded", "cancelled"):
            job["status"] = status
            self.engine.store.save_job(job)
            with self.subTest(status=status), self.assertRaises(CommandError):
                self.engine.scheduler.update(job["id"], {"priority": 90})
            self.assertEqual(self.engine.store.job(job["id"])["priority"], 50)

    def test_job_and_audit_commit_together(self):
        job = self.engine.scheduler.add(self.plan())["job"]
        def fail_audit(value):
            if isinstance(value, dict) and value.get("action") == "schedule_updated":
                raise RuntimeError("audit storage failed")
            return encode(value)
        with patch("robot_voice_patrol.store.encode", side_effect=fail_audit):
            with self.assertRaisesRegex(RuntimeError, "audit storage failed"):
                self.engine.scheduler.update(job["id"], {"priority": 99})
        self.assertEqual(self.engine.store.job(job["id"]), job)

    def test_update_racing_dispatch_is_rejected_after_dispatch(self):
        scheduler = self.engine.scheduler
        job = scheduler.add(self.plan())["job"]
        entered, release = threading.Event(), threading.Event()
        original = self.engine.submit_structured
        def blocked_submit(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(*args, **kwargs)
        errors = []
        def update():
            try:
                scheduler.update(job["id"], {"priority": 90})
            except Exception as exc:
                errors.append(exc)
        with patch.object(self.engine, "submit_structured", side_effect=blocked_submit):
            dispatch = threading.Thread(target=scheduler.tick)
            dispatch.start()
            self.assertTrue(entered.wait(2))
            editor = threading.Thread(target=update)
            editor.start()
            release.set()
            dispatch.join(3)
            editor.join(3)
        self.assertFalse(dispatch.is_alive() or editor.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], CommandError)
        self.assertEqual(self.engine.store.job(job["id"])["priority"], 50)
        finish(self.engine)

    def test_priority_and_counts_cover_more_than_200_pending_jobs(self):
        scheduler = self.engine.scheduler
        base = scheduler.add(self.plan(), priority=1,
            run_at=(datetime.now(timezone.utc) - timedelta(seconds=3)).isoformat())["job"]
        for index in range(1, 201):
            job = copy.deepcopy(base)
            job.update(id=f"bulk{index:03d}", priority=100 if index == 200 else 1,
                       run_at=(datetime.now(timezone.utc) - timedelta(seconds=2) + timedelta(microseconds=index)).isoformat())
            self.engine.store.create_job(job, f"bulk-request-{index}", f"payload-{index}")
        # Force the highest-priority job beyond the legacy first-200 window.
        highest = self.engine.store.job("bulk200")
        highest["run_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.engine.store.save_job(highest)
        self.assertEqual(scheduler.summary()["pending"], 201)
        scheduler.tick()
        self.assertEqual(self.engine.store.job("bulk200")["status"], "running")
        finish(self.engine)

    def test_cancel_completed_queue_job_does_not_stop_new_manual_mission(self):
        job = self.engine.scheduler.add(self.plan(), repeat={"interval_seconds": 60})["job"]
        self.engine.scheduler.tick()
        finish(self.engine)
        # Its queue bookkeeping still says "running" until the next tick.
        current = self.engine.submit("等待60秒")["mission_id"]
        self.engine.scheduler.cancel(job["id"])
        state = self.engine.snapshot()
        self.assertEqual(state["mission"]["id"], current)
        self.assertEqual(state["state"], "running")
        saved = self.engine.store.job(job["id"])
        self.assertEqual(saved["status"], "succeeded")
        self.assertIsNone(saved["repeat"])
        self.engine.control("stop")
        finish(self.engine)

    def test_success_return_racing_stop_keeps_completed_checkpoint(self):
        entered, release = threading.Event(), threading.Event()
        def completed_after_stop(step, cancel, feedback):
            entered.set()
            self.assertTrue(release.wait(3))
            return {"status": "succeeded", "kind": step.kind, "target": step.target, "simulated": True}
        plan = Plan("go and return", [Step("navigate", "meeting_room", step_id="go"),
            Step("navigate", "home", step_id="back")], "go and return")
        with patch.object(self.engine.adapter, "execute", side_effect=completed_after_stop):
            self.engine.submit_plan(plan)
            self.assertTrue(entered.wait(2))
            self.engine.control("stop")
            release.set()
            result = finish(self.engine)
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(result["mission"]["step_states"][0]["status"], "succeeded")
        self.assertEqual([r["step_id"] for r in result["mission"]["results"]], ["go"])
        preview = self.engine.preview_recovery(result["mission"]["id"])
        self.assertEqual([s["step_id"] for s in preview["plan"]["steps"]], ["back"])

    def test_recovery_parent_is_claimed_with_mission_before_final_receipt(self):
        source = self.checkpoint()
        with patch.object(self.engine.store, "finish_request", side_effect=RuntimeError("receipt unavailable")):
            with self.assertRaisesRegex(RuntimeError, "receipt unavailable"):
                self.engine.resume_recovery(source["id"], confirmed=True, request_id="recover1")
        child = finish(self.engine)["mission"]["id"]
        old = self.engine.store.mission(source["id"])
        self.assertEqual(old["resumed_as"], child)
        self.assertFalse(old["recovery_required"])
        self.engine.close()
        replacement = self.make()
        duplicate = replacement.resume_recovery(source["id"], confirmed=True, request_id="recover1")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["mission_id"], child)
        with self.assertRaisesRegex(CommandError, "关联任务"):
            replacement.resume_recovery(source["id"], confirmed=True, request_id="recover2")
        self.assertEqual(replacement.metrics()["missions_total"], 2)

    def test_parent_claim_rolls_back_if_child_mission_cannot_persist(self):
        source = self.checkpoint()
        original = self.engine.store._save_mission_locked
        def fail_child(mission):
            if mission["id"] != source["id"]:
                raise RuntimeError("child write failed")
            return original(mission)
        with patch.object(self.engine.store, "_save_mission_locked", side_effect=fail_child):
            with self.assertRaisesRegex(RuntimeError, "child write failed"):
                self.engine.resume_recovery(source["id"], confirmed=True, request_id="recover1")
        self.assertNotIn("resumed_as", self.engine.store.mission(source["id"]))
        self.assertEqual(self.engine.snapshot()["state"], "idle")
        self.assertEqual(self.engine.metrics()["missions_total"], 1)

    def test_concurrent_same_recovery_request_returns_same_child(self):
        source = self.checkpoint()
        barrier = threading.Barrier(3)
        results, errors = [], []
        def recover():
            barrier.wait()
            try:
                results.append(self.engine.resume_recovery(source["id"], confirmed=True, request_id="same"))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=recover) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(3)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["mission_id"], results[1]["mission_id"])
        self.assertEqual(sum(bool(r.get("duplicate")) for r in results), 1)
        finish(self.engine)
        self.assertEqual(self.engine.metrics()["missions_total"], 2)


if __name__ == "__main__":
    unittest.main()
