"""Language → engine → durable domain actions; no microphone or hardware."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from robot_voice_patrol.config import load_config
from robot_voice_patrol.contracts import CommandError
from robot_voice_patrol.engine import MissionEngine
from robot_voice_patrol.mock_adapter import MockAdapter


class AssistiveDialogueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "dialogue.db"
        self.config = load_config(Path(__file__).parents[1] / "config/home.json")
        self.config["mock"].update(travel_seconds=.001, inspection_seconds=.001)
        self.now = datetime(2026, 10, 3, 1, 0, tzinfo=timezone.utc)
        self.engines = []
        self.engine = self.make()

    def tearDown(self):
        for engine in self.engines:
            engine.close()
        self.temp.cleanup()

    def make(self):
        engine = MissionEngine(self.config, MockAdapter(self.config, fixture_skills=True), db_path=self.db, start_scheduler=False)
        engine.assistive._clock = lambda: self.now
        self.engines.append(engine)
        return engine

    def reminder(self, title="喝水", **extra):
        return self.engine.assistive.action({"op": "reminder.create", "title": title, "delay_seconds": 0, **extra})["assistive"]["record"]

    def record(self, identifier):
        with self.engine.store._lock:
            return self.engine.assistive._load(identifier)

    def say(self, text, request_id=None, session="voice-one"):
        return self.engine.submit(text, request_id=request_id, session_id=session)

    def test_due_reminder_acknowledge_via_engine(self):
        reminder = self.reminder()
        response = self.say("确认喝水提醒")
        self.assertEqual(response["assistive"]["record"]["state"], "acknowledged")
        self.assertEqual(self.record(reminder["id"])["acknowledged_count"], 1)
        self.assertFalse(response["needs_confirmation"])

    def test_user_word_order_alias_acknowledges_focused_reminder_only(self):
        first = self.say("0分钟后提醒我喝水")["assistive"]["record"]
        second = self.reminder("吃饭")
        response = self.say("这条提醒我知道了")
        self.assertEqual(response["assistive"]["record"]["id"], first["id"])
        self.assertEqual(self.record(second["id"])["acknowledged_count"], 0)
        repeated = self.say("这条提醒我知道了")
        self.assertIn("不会自动处理下一条", repeated["message"])
        self.assertEqual(self.record(second["id"])["acknowledged_count"], 0)

    def test_user_word_order_alias_requires_choice_without_matching_focus(self):
        self.reminder(); self.reminder("吃饭")
        response = self.say("这条提醒我知道了", session="without-focus")
        self.assertTrue(response["needs_clarification"])
        self.assertEqual(len(response["options"]), 2)
        self.assertTrue(all(r["acknowledged_count"] == 0 for r in self.engine.assistive.snapshot()["reminders"]))

    def test_cancel_recent_timed_confirmation_alias_preserves_other_waiting_record(self):
        first = self.say("开始30分钟平安确认")["assistive"]["record"]
        second = self.engine.assistive.action({"op":"wellbeing.start", "seconds":1800, "title":"另一条等待"})["assistive"]["record"]
        preview = self.engine.preview("取消刚才的定时确认", "voice-one")
        self.assertEqual(preview["action"]["id"], first["id"])
        self.assertEqual(self.record(first["id"])["state"], "waiting")
        response = self.say("取消刚才的定时确认")
        self.assertEqual(response["assistive"]["record"]["id"], first["id"])
        self.assertEqual(response["assistive"]["record"]["state"], "cancelled")
        self.assertEqual(self.record(second["id"])["state"], "waiting")
        self.assertIn("不会自动处理下一条", self.say("取消刚才的定时确认")["message"])

    def test_multiple_candidates_clarify_before_mutation_and_spoken_reply_selects(self):
        first, second = self.reminder(), self.reminder("吃饭")
        before = deepcopy(self.engine.assistive.snapshot())
        response = self.say("确认提醒")
        self.assertTrue(response["needs_clarification"])
        self.assertEqual(len(response["options"]), 2)
        self.assertEqual(len(response["option_labels"]), 2)
        self.assertTrue(response["option_labels"][1].startswith("第2个"))
        self.assertTrue(all("〔" not in label for label in response["option_labels"]))
        self.assertIn("第1个", response["message"])
        self.assertIn("喝水", response["message"])
        self.assertIn("吃饭", response["message"])
        self.assertNotIn("〔", response["message"])
        self.assertEqual(self.engine.assistive.snapshot(), before)
        chosen = self.say("第二个")
        self.assertEqual(chosen["assistive"]["record"]["state"], "acknowledged")
        self.assertEqual(sum(self.record(item["id"])["state"] == "acknowledged" for item in (first,second)), 1)

    def test_preview_is_read_only_and_its_choice_command_is_self_contained(self):
        self.reminder(); self.reminder()
        records = deepcopy(self.engine.assistive.snapshot())
        sessions = self.engine.store._connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        preview = self.engine.preview("确认提醒", "preview-session")
        self.assertTrue(preview["needs_clarification"])
        self.assertEqual(self.engine.assistive.snapshot(), records)
        self.assertEqual(self.engine.store._connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], sessions)
        result = self.say(preview["options"][0], session="preview-session")
        self.assertEqual(result["assistive"]["record"]["state"], "acknowledged")

    def test_single_record_preview_does_not_acknowledge(self):
        item = self.reminder()
        response = self.engine.preview("确认提醒", "readonly")
        self.assertEqual(response["action"]["op"], "reminder.ack")
        self.assertEqual(self.record(item["id"])["acknowledged_count"], 0)
        self.assertEqual(self.engine.store.session("readonly"), {})

    def test_single_preview_command_rejects_changed_record_instead_of_rebinding(self):
        item = self.reminder()
        preview = self.engine.preview("确认提醒", "readonly")
        self.engine.assistive.action({"op":"reminder.cancel", "id":item["id"]})
        replacement = self.reminder("吃饭")
        response = self.say(preview["execution_command"])
        self.assertIn("旧选择", response["message"])
        self.assertEqual(self.record(replacement["id"])["acknowledged_count"], 0)

    def test_choice_does_not_cross_sessions(self):
        self.reminder(); self.reminder("吃饭")
        self.say("确认提醒", session="a")
        try:
            response = self.say("第二个", session="b")
            self.assertNotIn("record", response.get("assistive", {}))
        except CommandError:
            pass
        self.assertTrue(all(r["acknowledged_count"] == 0 for r in self.engine.assistive.snapshot()["reminders"]))
        self.assertEqual(self.say("第二个", session="a")["assistive"]["record"]["state"], "acknowledged")

    def test_pending_choice_persists_across_restart_within_ttl(self):
        self.reminder(); self.reminder("吃饭")
        self.say("确认提醒")
        self.engine.close()
        # Initialization uses the real clock for its one startup tick. Freeze
        # the test time to a future due stage already missed so it stays stable.
        self.engine = self.make()
        # Re-issue after startup tick revision; this demonstrates persistence of
        # a fresh choice while keeping test clock deterministic.
        self.say("确认提醒")
        pending = self.engine.store.session("voice-one")["assistive_dialogue"]["pending"]
        self.engine.close()
        self.engine = self.make()
        self.assertEqual(self.engine.store.session("voice-one")["assistive_dialogue"]["pending"], pending)
        response = self.say("第一个")
        self.assertIn(response["kind"], {"assistive", "clarify"})
        self.assertEqual(response["assistive"]["record"]["state"], "acknowledged")

    def test_expired_choice_does_not_mutate(self):
        self.reminder(); self.reminder("吃饭")
        self.say("确认提醒")
        self.now += timedelta(seconds=301)
        response = self.say("第二个")
        self.assertIn("五分钟", response["message"])
        self.assertTrue(all(r["acknowledged_count"] == 0 for r in self.engine.assistive.snapshot()["reminders"]))

    def test_candidate_change_invalidates_old_number(self):
        first = self.reminder(); self.reminder("吃饭")
        self.say("确认提醒")
        self.engine.assistive.action({"op":"reminder.snooze", "id":first["id"], "seconds":60})
        response = self.say("第二个")
        self.assertIn("旧序号未执行", response["message"])
        self.assertTrue(all(r["acknowledged_count"] == 0 for r in self.engine.assistive.snapshot()["reminders"]))

    def test_new_candidate_invalidates_old_number(self):
        self.reminder(); self.reminder("吃饭")
        self.say("确认提醒")
        self.reminder("活动")
        response = self.say("第一个")
        self.assertIn("旧序号未执行", response["message"])
        self.assertTrue(response["needs_clarification"])

    def test_duplicate_request_cannot_acknowledge_the_remaining_reminder(self):
        self.reminder(); self.reminder("吃饭")
        self.say("确认提醒")
        original = self.say("第一个", "pick-once")
        again = self.say("第一个", "pick-once")
        self.assertTrue(again["duplicate"])
        self.assertEqual(original["assistive"]["record"]["id"], again["assistive"]["record"]["id"])
        self.assertEqual(sum(r["acknowledged_count"] for r in self.engine.assistive.snapshot()["reminders"]), 1)

    def test_old_choice_and_generic_repeat_do_not_acknowledge_next_occurrence(self):
        item = self.reminder(repeat_seconds=60)
        self.reminder("吃饭")
        preview = self.engine.preview("确认提醒")
        option = next(value for value in preview["options"] if "喝水" in value)
        self.say(option)
        self.now += timedelta(seconds=61)
        self.engine.assistive.tick()
        old = self.say(option)
        self.assertIn("旧选择", old["message"])
        self.assertEqual(self.record(item["id"])["acknowledged_count"], 1)
        # An explicit exact title is a fresh instruction for the new occurrence.
        self.say("确认喝水提醒")
        self.assertEqual(self.record(item["id"])["acknowledged_count"], 2)

    def test_consumed_focus_does_not_silently_bind_to_next_reminder(self):
        first = self.reminder()
        self.say("确认提醒")
        second = self.reminder("吃饭")
        response = self.say("确认提醒")
        self.assertIn("不会自动处理下一条", response["message"])
        self.assertEqual(self.record(second["id"])["acknowledged_count"], 0)

    def test_snooze_then_explicit_cancel(self):
        item = self.reminder()
        response = self.say("稍后五分钟")
        self.assertEqual(datetime.fromisoformat(response["assistive"]["record"]["due_at"]), self.now+timedelta(minutes=5))
        self.assertEqual(self.say("取消喝水提醒")["assistive"]["record"]["state"], "cancelled")

    def test_wellbeing_i_am_here_and_cancellation(self):
        first = self.say("开始30分钟平安确认")["assistive"]["record"]
        self.assertEqual(self.say("我在")["assistive"]["record"]["state"], "completed")
        self.say("开始30分钟平安确认")
        self.assertEqual(self.say("取消平安确认")["assistive"]["record"]["state"], "cancelled")
        self.assertEqual(self.record(first["id"])["state"], "completed")

    def test_assistance_response_resolution_and_cancellation_are_local_reports(self):
        self.say("我需要如厕帮助")
        response = self.say("有人回应了")
        self.assertEqual(response["assistive"]["record"]["state"], "acknowledged")
        self.assertFalse(response["assistive"]["record"]["reports"][-1]["independently_verified"])
        self.assertEqual(self.say("协助已解决")["assistive"]["record"]["state"], "resolved")
        self.say("我需要洗澡帮助")
        self.assertEqual(self.say("取消协助请求")["assistive"]["record"]["state"], "cancelled")

    def test_checklist_item_check_and_uncheck_and_need_completion(self):
        self.say("开始晨间清单")
        record = self.say("完成晨间清单第一个项目")["assistive"]["record"]
        self.assertTrue(record["items"][0]["checked"])
        record = self.say("取消勾选晨间清单第一项")["assistive"]["record"]
        self.assertFalse(record["items"][0]["checked"])
        self.say("把纸巾加入购物清单")
        self.assertEqual(self.say("纸巾已备好")["assistive"]["record"]["state"], "completed")

    def test_checklist_reset_invalidates_effect_dedup_but_original_request_stays_deduplicated(self):
        record = self.say("开始晨间清单")["assistive"]["record"]
        original = self.say("完成这个第一个项目", "check-first")
        self.assertTrue(original["assistive"]["record"]["items"][0]["checked"])
        self.engine.assistive.action({"op":"checklist.reset", "id":record["id"]})
        retry = self.say("完成这个第一个项目", "check-first")
        self.assertTrue(retry["duplicate"])
        self.assertFalse(self.record(record["id"])["items"][0]["checked"])
        fresh = self.say("完成这个第一个项目", "check-after-reset")
        self.assertTrue(fresh["assistive"]["record"]["items"][0]["checked"])
        self.assertTrue(self.record(record["id"])["items"][0]["checked"])

    def test_reminder_update_title_date_and_weekly_rule(self):
        item = self.reminder()
        self.say("把喝水提醒改名为饮水")
        self.assertEqual(self.record(item["id"])["title"], "饮水")
        self.say("把饮水提醒改到明天上午八点")
        self.assertEqual(self.record(item["id"])["due_at"], "2026-10-04T00:00:00+00:00")
        response = self.say("把饮水提醒改成每周一和周三上午八点")
        self.assertEqual(response["assistive"]["record"]["calendar"]["weekdays"], [1,3])
        self.assertEqual(response["assistive"]["record"]["calendar"]["local_time"], "08:00")

    def test_duplicate_title_update_clarifies(self):
        self.reminder(); self.reminder()
        response = self.say("把喝水提醒改名为饮水")
        self.assertTrue(response["needs_clarification"])
        self.say("第二个")
        self.assertEqual(sorted(r["title"] for r in self.engine.assistive.snapshot()["reminders"]), ["喝水", "饮水"])

    def test_excess_candidates_request_narrowing_without_long_spoken_list(self):
        for i in range(6): self.reminder("提醒"+str(i))
        response = self.say("确认提醒")
        self.assertTrue(response["needs_clarification"])
        self.assertEqual(response["options"], [])
        self.assertLess(len(response["message"]), 100)

    def test_robot_plan_confirmation_is_not_hijacked_or_deleted(self):
        draft = self.say("把手机从客厅送到卧室")
        self.assertTrue(draft["needs_confirmation"])
        self.say("10分钟后提醒我喝水")
        response = self.say("确认执行")
        self.assertNotIn("assistive", response)
        self.engine._worker.join(2)
        self.assertEqual(self.engine.snapshot()["state"], "succeeded")

    def test_bare_assent_during_living_clarification_cannot_dispatch_old_robot_plan(self):
        self.say("把手机从客厅送到卧室")
        self.reminder(); self.reminder("吃饭")
        self.say("确认提醒")
        for text in ("确认", "就这样"):
            preview = self.engine.preview(text, "voice-one")
            self.assertTrue(preview["needs_clarification"])
            response = self.say(text)
            self.assertTrue(response["needs_clarification"])
            self.assertIn("请明确说确认执行", response["message"])
            self.assertEqual(self.engine.snapshot()["state"], "idle")
            self.assertIsNone(self.engine.snapshot()["mission"])
        response = self.say("确认执行")
        self.assertNotIn("assistive", response)
        self.engine._worker.join(2)
        self.assertEqual(self.engine.snapshot()["state"], "succeeded")
        self.assertTrue(all(r["acknowledged_count"] == 0 for r in self.engine.assistive.snapshot()["reminders"]))

    def test_excess_candidates_keep_short_assents_and_numbers_out_of_old_robot_plan(self):
        self.say("把手机从客厅送到卧室")
        for i in range(6): self.reminder("提醒"+str(i))
        response = self.say("确认提醒")
        self.assertEqual(response["options"], [])
        self.assertIsNone(self.engine.store.session("voice-one")["assistive_dialogue"]["pending"])
        for text in ("确认", "第一个", "就这样"):
            response = self.say(text)
            self.assertIsNone(self.engine.snapshot()["mission"])
            self.assertTrue(response["needs_clarification"])
            self.assertEqual(response["options"], [])
        self.assertTrue(all(r["acknowledged_count"] == 0 for r in self.engine.assistive.snapshot()["reminders"]))

    def test_no_match_requires_detail_but_explicit_robot_confirmation_still_works(self):
        self.say("把手机从客厅送到卧室")
        self.assertIn("没有匹配", self.say("确认提醒")["message"])
        for text in ("确认", "就这样"):
            response = self.say(text)
            self.assertIsNone(self.engine.snapshot()["mission"])
            self.assertTrue(response["needs_clarification"])
        response = self.say("确认执行")
        self.assertNotIn("assistive", response)
        self.engine._worker.join(2)
        self.assertEqual(self.engine.snapshot()["state"], "succeeded")

    def test_unresolved_living_topic_survives_restart_and_choice_expiry(self):
        self.say("把手机从客厅送到卧室")
        self.say("确认提醒")
        self.engine.close()
        self.now += timedelta(seconds=301)
        self.engine = self.make()
        response = self.say("确认")
        self.assertTrue(response["needs_clarification"])
        self.assertEqual(response["options"], [])
        self.assertIsNone(self.engine.snapshot()["mission"])

    def test_explicit_new_robot_preview_replaces_unresolved_living_topic(self):
        self.say("确认提醒")
        response = self.engine.preview("把手机从客厅送到卧室", "voice-one")
        self.assertTrue(response["needs_confirmation"])
        response = self.say("确认")
        self.assertNotIn("assistive", response)
        self.engine._worker.join(2)
        self.assertEqual(self.engine.snapshot()["state"], "succeeded")

    def test_stop_has_priority_over_pending_assistive_choice(self):
        self.reminder(); self.reminder("吃饭")
        self.say("确认提醒")
        response = self.say("立即停止")
        self.assertNotIn("assistive", response)
        self.assertTrue(self.engine.scheduler.paused)
        self.assertTrue(all(r["acknowledged_count"] == 0 for r in self.engine.assistive.snapshot()["reminders"]))

    def test_new_robot_topic_clears_old_living_choice_but_keeps_records(self):
        self.reminder(); self.reminder("吃饭")
        self.say("确认提醒")
        self.assertIsNotNone(self.engine.store.session("voice-one")["assistive_dialogue"]["pending"])
        response = self.say("把手机从客厅送到卧室")
        self.assertTrue(response["needs_confirmation"])
        self.assertIsNone(self.engine.store.session("voice-one")["assistive_dialogue"]["pending"])
        try:
            response = self.say("第二个")
            self.assertNotIn("record", response.get("assistive", {}))
        except CommandError:
            pass
        self.assertTrue(all(r["acknowledged_count"] == 0 for r in self.engine.assistive.snapshot()["reminders"]))

    def test_future_reminder_cannot_be_acknowledged_early(self):
        item = self.reminder(delay_seconds=600)
        response = self.say("确认喝水提醒")
        self.assertIn("没有匹配", response["message"])
        self.assertEqual(self.record(item["id"])["acknowledged_count"], 0)

    def test_ros_text_callback_uses_same_dialogue_and_speaks_candidates(self):
        from tests.test_managed_node import modules
        from robot_voice_patrol.ros_node import managed_node_class
        self.reminder(); self.reminder("吃饭")
        spoken, errors = [], []
        node = NS(engine=self.engine, control=NS(ensure_active=lambda: None), speak=spoken.append,
                  get_logger=lambda: NS(error=errors.append))
        with patch.dict(sys.modules, modules()):
            callback = managed_node_class().command_received
            callback(node, NS(data="确认提醒"))
            self.assertIn("第1个", spoken[-1])
            self.assertIn("第2个", spoken[-1])
            callback(node, NS(data="第二个"))
        self.assertEqual(errors, [])
        self.assertEqual(sum(r["acknowledged_count"] for r in self.engine.assistive.snapshot()["reminders"]), 1)
        self.assertGreaterEqual(len(spoken), 2)

    def test_cli_process_reopens_dialogue_and_deduplicates_confirmation(self):
        root = Path(__file__).parents[1]
        command = [sys.executable, "-m", "robot_voice_patrol", "--home", "--db", str(Path(self.temp.name)/"cli.db"),
                   "--journal", str(Path(self.temp.name)/"cli.jsonl"), "--session-id", "cli-life"]
        environment = {**os.environ, "PYTHONPATH": str(root), "PYTHONIOENCODING":"utf-8", "VOICE_PATROL_MODEL_PROVIDER":"none"}
        def run(text, request_id, preview=False):
            result = subprocess.run(command+["--command", text, "--request-id", request_id]+(["--preview"] if preview else []),
                                    cwd=root, env=environment, encoding="utf-8", capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        preview = run("开始30分钟平安确认", "preview", preview=True)
        self.assertEqual(preview["action"]["op"], "wellbeing.start")
        created = run("开始30分钟平安确认", "create")
        confirmed = run("我在", "confirm")
        repeated = run("我在", "confirm")
        self.assertEqual(created["assistive"]["record"]["id"], confirmed["assistive"]["record"]["id"])
        self.assertEqual(confirmed["assistive"]["record"]["state"], "completed")
        self.assertTrue(repeated["duplicate"])
        self.assertEqual(repeated["assistive"]["record"]["id"], confirmed["assistive"]["record"]["id"])

    def test_concurrent_same_request_acks_only_once(self):
        item = self.reminder()
        responses = []
        threads = [threading.Thread(target=lambda: responses.append(self.say("确认提醒", "same-command"))) for _ in range(3)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(2)
        self.assertEqual(len(responses), 3)
        self.assertEqual(self.record(item["id"])["acknowledged_count"], 1)


if __name__ == "__main__":
    unittest.main()
