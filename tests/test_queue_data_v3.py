import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
import os
import subprocess
import sys

from robot_voice_patrol.config import load_config
from robot_voice_patrol.contracts import CommandError, Plan, Step
from robot_voice_patrol.engine import MissionEngine
from robot_voice_patrol.mock_adapter import MockAdapter
from robot_voice_patrol.data_management import restore_offline
from robot_voice_patrol.scheduling import next_occurrence, parse_instant, validate_repeat
from robot_voice_patrol.store import MissionStore


def finish(engine):
    worker = engine._worker
    if worker:
        worker.join(3)
        if worker.is_alive():
            raise AssertionError("mission did not finish")
    return engine.snapshot()


class QueueDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "missions.sqlite3"
        self.config = load_config()
        self.config["mock"].update(travel_seconds=.01, inspection_seconds=.01)
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

    def plan(self, target="home"):
        return Plan("go " + target, [Step("navigate", target)], "go " + target)

    def test_ready_queue_priority_serial_dispatch_and_completion(self):
        scheduler = self.engine.scheduler
        low = scheduler.add(self.plan("meeting_room"), priority=1)["job"]
        high = scheduler.add(self.plan("reception"), priority=90)["job"]
        scheduler.tick()
        self.assertEqual(self.engine.store.job(high["id"])["status"], "running")
        self.assertEqual(self.engine.store.job(low["id"])["status"], "queued")
        finish(self.engine)
        scheduler.tick()
        self.assertEqual(self.engine.store.job(high["id"])["status"], "succeeded")
        self.assertEqual(self.engine.store.job(low["id"])["status"], "running")
        finish(self.engine)
        scheduler.tick()
        self.assertEqual(self.engine.metrics()["missions_total"], 2)

    def test_queued_requests_deduplicate_and_conflict(self):
        first = self.engine.scheduler.add(self.plan(), request_id="repeat")
        second = self.engine.scheduler.add(self.plan(), request_id="repeat")
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["job"]["id"], second["job"]["id"])
        with self.assertRaises(CommandError):
            self.engine.scheduler.add(self.plan("meeting_room"), request_id="repeat")

    def test_restart_preserves_pending_queue_and_requires_resume(self):
        job = self.engine.scheduler.add(self.plan())["job"]
        self.engine.close()
        replacement = self.make()
        self.assertTrue(replacement.scheduler.paused)
        replacement.scheduler.tick()
        self.assertEqual(replacement.metrics()["missions_total"], 0)
        replacement.scheduler.control("resume")
        replacement.scheduler.tick()
        finish(replacement)
        replacement.scheduler.tick()
        self.assertEqual(replacement.store.job(job["id"])["status"], "succeeded")

    def test_future_schedule_and_cancel_do_not_dispatch(self):
        job = self.engine.scheduler.add(self.plan(), run_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat())["job"]
        self.engine.scheduler.tick()
        self.assertEqual(self.engine.metrics()["missions_total"], 0)
        self.engine.scheduler.cancel(job["id"])
        self.assertEqual(self.engine.store.job(job["id"])["status"], "cancelled")

    def test_repeat_skips_missed_occurrences_without_burst(self):
        instant = datetime(2026, 10, 3, tzinfo=timezone.utc)
        scheduler = self.engine.scheduler
        scheduler.clock = lambda: instant
        job = scheduler.add(self.plan(), repeat={"interval_seconds": 10})["job"]
        scheduler.tick()
        finish(self.engine)
        instant += timedelta(seconds=95)
        scheduler.tick()
        saved = self.engine.store.job(job["id"])
        self.assertEqual(saved["status"], "queued")
        self.assertEqual(saved["occurrence"], 1)
        self.assertEqual(parse_instant(saved["run_at"]), datetime(2026, 10, 3, 0, 1, 40, tzinfo=timezone.utc))
        self.assertEqual(self.engine.metrics()["missions_total"], 1)

    def test_configuration_change_blocks_queued_draft(self):
        job = self.engine.scheduler.add(self.plan())["job"]
        config = copy.deepcopy(self.config)
        config["locations"]["home"]["x"] = 6
        self.engine.update_config(config)
        self.engine.scheduler.tick()
        self.assertEqual(self.engine.store.job(job["id"])["status"], "blocked")
        self.assertEqual(self.engine.metrics()["missions_total"], 0)

    def test_stop_pauses_following_queue(self):
        self.engine.scheduler.add(self.plan())
        self.engine.submit("立即停止")
        self.engine.scheduler.tick()
        self.assertTrue(self.engine.scheduler.paused)
        self.assertEqual(self.engine.metrics()["missions_total"], 0)

    def test_natural_schedule_is_persistent_and_can_clarify(self):
        response = self.engine.submit("每天九点去会议室", request_id="natural-schedule")
        self.assertEqual(response["job"]["repeat"]["daily_at"], "09:00")
        self.assertEqual(self.engine.metrics()["missions_total"], 0)
        first = self.engine.submit("完成当前任务后找水杯", session_id="clarify-schedule")
        self.assertTrue(first["needs_clarification"])
        second = self.engine.submit("会议室", session_id="clarify-schedule")
        self.assertIn("job", second)

    def test_template_parameters_crud_and_execution(self):
        workflow = {"version": 1, "name": "参数模板", "parameters": {"room": {"type": "location", "default": "home"}},
                    "steps": [{"type": "step", "id": "go", "kind": "navigate", "target": "${room}"}]}
        item = self.engine.save_template({"name": "arrival", "workflow": workflow})["template"]
        response = self.engine.run_template(item["id"], {"parameters": {"room": "meeting_room"}})
        self.assertEqual(response["job"]["plan"]["steps"][0]["target"], "meeting_room")
        with self.assertRaises(CommandError):
            self.engine.run_template(item["id"], {"parameters": {"room": "moon"}})
        self.engine.store.delete_template(item["id"])
        self.assertEqual(self.engine.store.templates(), [])

    def test_backup_verify_tamper_and_path_rejection(self):
        backup = self.engine.data.backup()["backup"]
        self.assertEqual(self.engine.data.verify(backup["id"])["integrity"], "ok")
        with self.assertRaises(CommandError):
            self.engine.data.verify("../missions")
        path = self.engine.data._path(backup["id"])
        with path.open("ab") as stream:
            stream.write(b"tamper")
        with self.assertRaisesRegex(CommandError, "哈希"):
            self.engine.data.verify(backup["id"])

    def test_offline_restore_requires_exclusive_database_and_preserves_pre_restore(self):
        self.engine.submit("去会议室")
        finish(self.engine)
        backup = self.engine.data.backup()["backup"]
        self.engine.submit("返回起点")
        finish(self.engine)
        stage = self.engine.data.stage_restore(backup["id"], confirmed=True)
        self.assertTrue(stage["staged_only"])
        self.assertEqual(self.engine.metrics()["missions_total"], 2)
        with self.assertRaises(RuntimeError):
            restore_offline(self.db, backup["id"])
        self.engine.close()
        result = restore_offline(self.db, backup["id"])
        self.assertNotEqual(result["pre_restore_backup"], backup["id"])
        replacement = self.make()
        self.assertEqual(replacement.metrics()["missions_total"], 1)
        self.assertTrue(replacement.scheduler.paused)

    def test_archive_is_reversible_and_queries_keep_audit(self):
        self.engine.submit("去会议室")
        finish(self.engine)
        identifier = self.engine.snapshot()["mission"]["id"]
        before = (datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
        self.assertEqual(self.engine.data.archive(before)["count"], 1)
        self.assertEqual(self.engine.history()["total"], 1)
        self.engine.data.archive(before, dry_run=False)
        self.assertEqual(self.engine.history()["total"], 0)
        self.assertEqual(self.engine.history(archived=True)["total"], 1)
        self.assertTrue(self.engine.mission_detail(identifier)["events"])
        self.engine.store.unarchive([identifier])
        self.assertEqual(self.engine.history(query="会议室")["total"], 1)

    def test_event_cursor_and_policy_validation(self):
        events = self.engine.store.events_page(after=0, limit=1)
        self.engine.store.event("info", "next")
        after = self.engine.store.events_page(after=events[-1]["id"])
        self.assertTrue(all(e["id"] > events[-1]["id"] for e in after))
        with self.assertRaises(CommandError):
            self.engine.data.set_policy({"retention_days": True, "auto_archive": True})
        self.engine.data.set_policy({"retention_days": 30, "auto_archive": True})
        self.assertEqual(self.engine.data.policy()["retention_days"], 30)

    def test_invalid_schedules_and_daily_timezone(self):
        for bad in ({"interval_seconds": True}, {"interval_seconds": float("nan")}, {"daily_at": "25:00", "timezone": "UTC"}):
            with self.assertRaises(CommandError):
                validate_repeat(bad)
        with self.assertRaises(CommandError):
            parse_instant("2026-10-03T09:00:00")
        instant = datetime(2026, 10, 3, 0, 30, tzinfo=timezone.utc)
        self.assertEqual(next_occurrence({"daily_at": "09:00", "timezone": "Asia/Hong_Kong"}, instant).hour, 1)

    def test_lifecycle_blocks_dispatch_until_active(self):
        self.engine.lifecycle_transition("deactivate")
        with self.assertRaises(CommandError):
            self.engine.submit_plan(self.plan())
        self.engine.lifecycle_transition("cleanup")
        self.engine.lifecycle_transition("configure")
        self.engine.lifecycle_transition("activate")
        self.engine.submit_plan(self.plan())
        self.assertEqual(finish(self.engine)["state"], "succeeded")

    def test_actual_crash_preserves_job_to_mission_link_without_replay(self):
        self.engine.close()
        program = """
import os,sys,time
from robot_voice_patrol.config import load_config
from robot_voice_patrol.engine import MissionEngine
from robot_voice_patrol.mock_adapter import MockAdapter
c=load_config()
e=MissionEngine(c,MockAdapter(c),db_path=sys.argv[1],start_scheduler=False)
e.scheduler.control('resume')
e.enqueue({'text':'等待60秒','request_id':'crash-job'})
e.scheduler.tick()
time.sleep(.1)
os._exit(23)
"""
        run = subprocess.run([sys.executable, "-c", program, str(self.db)], cwd=Path(__file__).resolve().parents[1],
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), capture_output=True, timeout=10)
        self.assertEqual(run.returncode, 23, run.stderr)
        replacement = self.make()
        job = replacement.store.jobs()[0]
        self.assertEqual(job["status"], "interrupted")
        self.assertEqual(replacement.store.mission(job["mission_id"])["state"], "interrupted")
        replacement.scheduler.tick()
        self.assertEqual(replacement.snapshot()["state"], "idle")
        self.assertEqual(replacement.metrics()["missions_total"], 1)

    def test_v2_database_migrates_history_and_saved_configuration(self):
        source_v2 = Path(__file__).resolve().parents[2] / "ros2_voice_patrol_v2"
        if not source_v2.is_dir():
            # A release checkout need not contain V2; construct a genuine V2 schema fixture.
            self.engine.close()
            connection = sqlite3.connect(self.db)
            connection.execute("PRAGMA user_version=2")
            connection.execute("DROP TABLE jobs")
            connection.execute("DROP TABLE templates")
            connection.commit()
            connection.close()
        else:
            self.engine.close()
            old_db = Path(self.temp.name) / "legacy.sqlite3"
            program = """
import sys,time
from robot_voice_patrol.config import load_config
from robot_voice_patrol.engine import MissionEngine
from robot_voice_patrol.mock_adapter import MockAdapter
c=load_config();c['mock'].update(travel_seconds=.01,inspection_seconds=.01)
e=MissionEngine(c,MockAdapter(c),db_path=sys.argv[1]);c['locations']['home']['x']=7;e.update_config(c)
e.submit('去会议室找水杯');e._worker.join(3);e.close()
"""
            run = subprocess.run([sys.executable, "-c", program, str(old_db)], cwd=source_v2,
                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), capture_output=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.db = old_db
        replacement = self.make()
        self.assertEqual(replacement.metrics()["schema_version"], 3)
        if source_v2.is_dir():
            self.assertEqual(replacement.config["locations"]["home"]["x"], 7)
            self.assertEqual(replacement.memory_service.query(object_name="水杯")["observations"][0]["outcome"], "found")

    def test_recurring_completed_mission_recovers_future_occurrence(self):
        job = self.engine.scheduler.add(self.plan(), repeat={"interval_seconds": 60})["job"]
        self.engine.scheduler.tick()
        finish(self.engine)
        # No final scheduler tick: simulate process ending after mission receipt but before recurrence update.
        self.engine.close()
        replacement = self.make()
        saved = replacement.store.job(job["id"])
        self.assertEqual(saved["status"], "queued")
        self.assertEqual(saved["occurrence"], 1)
        self.assertTrue(replacement.scheduler.paused)
        self.assertGreater(parse_instant(saved["run_at"]), datetime.now(timezone.utc))


if __name__ == "__main__":
    unittest.main()
