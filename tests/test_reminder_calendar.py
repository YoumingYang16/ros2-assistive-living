"""Calendar semantics and atomic reminder edits; no hardware or network."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

from robot_voice_patrol.assistive_service import AssistiveService
from robot_voice_patrol.contracts import CommandError
from robot_voice_patrol.reminder_schedule import (
    calendar_timezone, next_calendar_occurrence, parse_clock_text,
    parse_weekly_reminder, validate_calendar,
)
from robot_voice_patrol.store import MissionStore


def instant(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def weekly(days=(1, 3, 5), at="08:00", zone="Asia/Hong_Kong"):
    return {"weekdays": list(days), "local_time": at, "timezone": zone}


class CalendarHelperTests(unittest.TestCase):
    def test_timezone_conversion_and_strict_future(self):
        calendar = weekly()
        self.assertEqual(next_calendar_occurrence(calendar, instant("2026-10-03T00:00:00Z")), instant("2026-10-05T00:00:00Z"))
        self.assertEqual(next_calendar_occurrence(calendar, instant("2026-10-05T00:00:00Z")), instant("2026-10-07T00:00:00Z"))

    def test_every_day_is_local_calendar_and_crosses_dst(self):
        calendar = weekly(range(1, 8), "08:00", "America/New_York")
        first = next_calendar_occurrence(calendar, instant("2026-03-06T20:00:00Z"))
        second = next_calendar_occurrence(calendar, first)
        self.assertEqual(first, instant("2026-03-07T13:00:00Z"))
        self.assertEqual(second, instant("2026-03-08T12:00:00Z"))
        self.assertEqual((second-first).total_seconds(), 23*3600)

    def test_nonexistent_spring_wall_time_skips_day(self):
        value = next_calendar_occurrence(weekly([7], "02:30", "America/New_York"), instant("2026-03-07T00:00:00Z"))
        self.assertEqual(value, instant("2026-03-15T06:30:00Z"))

    def test_ambiguous_fall_wall_time_uses_first_only(self):
        calendar = weekly([7], "01:30", "America/New_York")
        first = next_calendar_occurrence(calendar, instant("2026-10-31T00:00:00Z"))
        self.assertEqual(first, instant("2026-11-01T05:30:00Z"))
        self.assertEqual(next_calendar_occurrence(calendar, first+timedelta(minutes=1)), instant("2026-11-08T06:30:00Z"))

    def test_end_is_inclusive_but_no_next_after_end(self):
        end = instant("2026-10-05T00:00:00Z")
        self.assertEqual(next_calendar_occurrence(weekly(), end-timedelta(seconds=1), end_at=end), end)
        self.assertIsNone(next_calendar_occurrence(weekly(), end, end_at=end))
        self.assertIsNone(next_calendar_occurrence(weekly(), end-timedelta(seconds=1), end_at=end-timedelta(microseconds=1)))

    def test_strict_calendar_schema(self):
        for changes in ({"weekdays": []}, {"weekdays": [1,1]}, {"weekdays": [True]}, {"weekdays": [0]},
                        {"weekdays": [8]}, {"local_time": "8:00"}, {"local_time": "24:00"},
                        {"timezone": "Missing/Invalid"}, {"timezone": None}, {"extra": True}):
            with self.subTest(changes=changes), self.assertRaises(CommandError):
                validate_calendar({**weekly(), **changes})
        self.assertEqual(validate_calendar(weekly([5, 1, 3]))["weekdays"], [1,3,5])

    def test_missing_timezone_data_has_only_explicit_fallbacks(self):
        with patch("robot_voice_patrol.reminder_schedule.ZoneInfo", side_effect=ZoneInfoNotFoundError):
            self.assertEqual(calendar_timezone("Asia/Hong_Kong").utcoffset(None), timedelta(hours=8))
            self.assertEqual(calendar_timezone("UTC").utcoffset(None), timedelta())
            with self.assertRaisesRegex(CommandError, "不可用"):
                calendar_timezone("America/New_York")

    def test_naive_datetimes_rejected(self):
        with self.assertRaises(CommandError):
            next_calendar_occurrence(weekly(), datetime(2026, 10, 3))
        with self.assertRaises(CommandError):
            next_calendar_occurrence(weekly(), instant("2026-10-03T00:00:00Z"), end_at=datetime(2026, 10, 9))

    def test_weekly_chinese_parser_and_clock(self):
        for text in ("每周一三五8点提醒我喝水", "每周一、三、五08:00提醒我喝水"):
            self.assertEqual(parse_weekly_reminder(text)["calendar"], weekly())
        self.assertEqual(parse_weekly_reminder("每周一和周三上午八点提醒我喝水")["calendar"], weekly([1,3]))
        self.assertEqual(parse_weekly_reminder("每星期一、星期三下午两点三十分提醒我活动")["calendar"], weekly([1,3], "14:30"))
        self.assertEqual(parse_clock_text("下午十二点"), "12:00")
        self.assertEqual(parse_clock_text("上午十二点"), "00:00")
        self.assertIsNone(parse_weekly_reminder("不要每周一8点提醒我喝水"))
        with self.assertRaises(CommandError):
            parse_clock_text("下午十三点")

    def test_incomplete_clock_is_not_assumed_to_mean_whole_hour(self):
        for value in ("8:", None, 8, "上午8:"):
            self.assertIsNone(parse_clock_text(value))
        self.assertIsNone(parse_weekly_reminder("每周一8:提醒我喝水"))


class ReminderCalendarTests(unittest.TestCase):
    def setUp(self):
        self.now = instant("2026-10-03T00:00:00Z")
        self.store = MissionStore()
        self.service = AssistiveService(self.store, start=False, clock=lambda: self.now)
        self.addCleanup(self.store.close)
        self.addCleanup(self.service.close)

    def act(self, op, **fields):
        return self.service.action({"op": op, **fields})["assistive"]["record"]

    def create(self, **fields):
        return self.act("reminder.create", title="喝水", **fields)

    def test_calendar_create_and_late_ack_skip_missed_cycles(self):
        r = self.create(calendar=weekly())
        self.assertEqual(r["revision"], 1)
        self.now = instant("2026-10-14T04:00:00Z")
        self.assertEqual(self.service.tick(), 1)
        self.assertEqual(self.service.tick(), 0)
        missed = self.service._load(r["id"])
        self.assertEqual(missed["state"], "missed")
        self.assertEqual(missed["due_at"], "2026-10-05T00:00:00+00:00")
        updated = self.act("reminder.ack", id=r["id"])
        self.assertEqual(updated["due_at"], "2026-10-16T00:00:00+00:00")
        self.assertEqual(updated["revision"], 3)
        self.assertEqual(len(self.service.snapshot()["reminders"]), 1)

    def test_calendar_cutoff_does_not_clear_unacknowledged_history(self):
        r = self.create(calendar=weekly(), end_at="2026-10-05T00:00:00Z")
        self.now = instant("2026-10-20T00:00:00Z")
        self.service.tick()
        self.assertEqual(self.service._load(r["id"])["state"], "missed")
        finished = self.act("reminder.ack", id=r["id"])
        self.assertEqual(finished["state"], "acknowledged")
        self.assertEqual(finished["due_at"], r["due_at"])

    def test_interval_cutoff_and_snooze_limit(self):
        r = self.create(delay_seconds=60, repeat_seconds=120, end_at=(self.now+timedelta(seconds=180)).isoformat())
        self.now += timedelta(seconds=60)
        with self.assertRaises(CommandError):
            self.act("reminder.snooze", id=r["id"], seconds=121)
        next_one = self.act("reminder.ack", id=r["id"])
        self.assertEqual(next_one["due_at"], "2026-10-03T00:03:00+00:00")
        self.now += timedelta(seconds=120)
        self.assertEqual(self.act("reminder.ack", id=r["id"])["state"], "acknowledged")

    def test_snooze_keeps_interval_calendar_anchor(self):
        r = self.create(delay_seconds=60, repeat_seconds=120)
        self.now += timedelta(seconds=60)
        self.act("reminder.snooze", id=r["id"], seconds=180)
        self.now += timedelta(seconds=180)
        updated = self.act("reminder.ack", id=r["id"])
        self.assertEqual(updated["due_at"], "2026-10-03T00:05:00+00:00")

    def test_early_snooze_does_not_repeat_same_calendar_slot(self):
        r = self.create(calendar=weekly())
        self.act("reminder.snooze", id=r["id"], seconds=60)
        self.now += timedelta(seconds=60)
        updated = self.act("reminder.ack", id=r["id"])
        self.assertEqual(updated["due_at"], "2026-10-07T00:00:00+00:00")

    def test_title_edit_preserves_due_state_and_audit(self):
        r = self.create(delay_seconds=60)
        self.now += timedelta(seconds=61)
        self.service.tick()
        edited = self.act("reminder.update", id=r["id"], title="饮水提醒", expected_revision=2)
        self.assertEqual(edited["state"], "due")
        self.assertEqual(edited["revision"], 3)
        self.assertIsNotNone(edited["notification"])
        event = self.service.snapshot()["events"][0]
        self.assertEqual(event["data"]["previous"]["title"], "喝水")
        self.assertEqual(event["data"]["current"]["title"], "饮水提醒")

    def test_title_and_end_only_edits_preserve_calendar_occurrence(self):
        r = self.create(calendar=weekly())
        self.now = instant("2026-10-05T00:01:00Z")
        self.service.tick()
        before = self.service._load(r["id"])
        titled = self.act("reminder.update", id=r["id"], title="新标题")
        ended = self.act("reminder.update", id=r["id"], end_at="2026-10-09T00:00:00Z")
        cleared = self.act("reminder.update", id=r["id"], end_at=None)
        for value in (titled, ended, cleared):
            for field in ("due_at", "scheduled_due_at", "state", "notification", "occurrence"):
                self.assertEqual(value[field], before[field], field)

    def test_unchanged_calendar_in_full_form_does_not_skip_missed_reminder(self):
        r = self.create(calendar=weekly())
        self.now = instant("2026-10-06T00:00:00Z")
        self.service.tick()
        before = self.service._load(r["id"])
        changed = self.act("reminder.update", id=r["id"], title="已改标题", calendar=weekly([5,3,1]),
                           end_at="2026-10-07T00:00:00Z", expected_revision=before["revision"])
        self.assertEqual(changed["state"], "missed")
        self.assertEqual(changed["due_at"], before["due_at"])
        self.assertEqual(changed["notification"], before["notification"])
        next_one = self.act("reminder.ack", id=r["id"])
        self.assertEqual(next_one["due_at"], "2026-10-07T00:00:00+00:00")

    def test_weekly_to_once_retains_due_but_never_repeats_after_ack(self):
        r = self.create(calendar=weekly())
        once = self.act("reminder.update", id=r["id"], calendar=None, repeat_seconds=None)
        self.assertEqual(once["due_at"], r["due_at"])
        self.assertIsNone(once["calendar"])
        self.assertIsNone(once["repeat_seconds"])
        self.now = instant(r["due_at"])
        self.assertEqual(self.act("reminder.ack", id=r["id"])["state"], "acknowledged")

    def test_changed_interval_after_snooze_starts_from_current_occurrence(self):
        r = self.create(delay_seconds=60, repeat_seconds=120)
        self.now += timedelta(seconds=60)
        snoozed = self.act("reminder.snooze", id=r["id"], seconds=180)
        changed = self.act("reminder.update", id=r["id"], repeat_seconds=100)
        self.assertEqual(changed["due_at"], snoozed["due_at"])
        self.assertEqual(changed["scheduled_due_at"], changed["due_at"])
        self.now = instant(changed["due_at"])
        self.assertEqual(self.act("reminder.ack", id=r["id"])["due_at"], "2026-10-03T00:05:40+00:00")

    def test_unchanged_interval_form_edit_preserves_snooze_anchor(self):
        r = self.create(delay_seconds=60, repeat_seconds=120)
        self.now += timedelta(seconds=60)
        snoozed = self.act("reminder.snooze", id=r["id"], seconds=180)
        changed = self.act("reminder.update", id=r["id"], title="new", repeat_seconds=120)
        self.assertEqual(changed["scheduled_due_at"], r["due_at"])
        self.assertEqual(changed["due_at"], snoozed["due_at"])
        self.now = instant(changed["due_at"])
        self.assertEqual(self.act("reminder.ack", id=r["id"])["due_at"], "2026-10-03T00:05:00+00:00")

    def test_service_calendar_crosses_dst_and_stops_exactly_at_end(self):
        self.now = instant("2026-03-06T20:00:00Z")
        r = self.create(calendar=weekly(range(1,8), "08:00", "America/New_York"), end_at="2026-03-08T08:00:00-04:00")
        self.now = instant(r["due_at"])
        next_one = self.act("reminder.ack", id=r["id"])
        self.assertEqual(next_one["due_at"], "2026-03-08T12:00:00+00:00")
        self.now = instant(next_one["due_at"])
        self.assertEqual(self.act("reminder.ack", id=r["id"])["state"], "acknowledged")

    def test_fall_overlap_second_clock_time_cannot_be_next_before_cutoff(self):
        self.now = instant("2026-10-31T00:00:00Z")
        r = self.create(calendar=weekly([7], "01:30", "America/New_York"), end_at="2026-11-01T07:00:00Z")
        self.assertEqual(r["due_at"], "2026-11-01T05:30:00+00:00")
        self.now = instant("2026-11-01T05:45:00Z")
        self.assertEqual(self.act("reminder.ack", id=r["id"])["state"], "acknowledged")

    def test_expired_calendar_can_be_acknowledged_without_timezone_data(self):
        self.now = instant("2026-10-03T00:00:00Z")
        r = self.create(calendar=weekly([1], "08:00", "America/New_York"), end_at="2026-10-05T12:00:00Z")
        self.now = instant("2026-10-06T00:00:00Z")
        with patch("robot_voice_patrol.reminder_schedule.ZoneInfo", side_effect=ZoneInfoNotFoundError):
            self.assertEqual(self.act("reminder.ack", id=r["id"])["state"], "acknowledged")

    def test_missing_timezone_during_recurrence_leaves_ack_and_receipt_uncommitted(self):
        r = self.create(calendar=weekly([1], "08:00", "America/New_York"))
        self.now = instant(r["due_at"])
        before = self.service._load(r["id"])
        with patch("robot_voice_patrol.reminder_schedule.ZoneInfo", side_effect=ZoneInfoNotFoundError):
            with self.assertRaises(CommandError):
                self.service.action({"op":"reminder.ack", "id":r["id"], "request_id":"ack-missing-zone"})
        self.assertEqual(self.service._load(r["id"]), before)
        self.assertIsNone(self.store._connection.execute("SELECT 1 FROM assistive_receipts WHERE request_id='ack-missing-zone'").fetchone())

    def test_edit_reschedules_due_reminder_and_changes_rule(self):
        r = self.create(delay_seconds=60)
        self.now += timedelta(seconds=61)
        self.service.tick()
        edited = self.act("reminder.update", id=r["id"], calendar=weekly([2,4]), end_at="2026-10-08T00:00:00Z")
        self.assertEqual(edited["state"], "pending")
        self.assertEqual(edited["due_at"], "2026-10-06T00:00:00+00:00")
        self.assertIsNone(edited["notification"])
        self.assertIsNone(edited["repeat_seconds"])
        single = self.act("reminder.update", id=r["id"], calendar=None, delay_seconds=90, end_at=None)
        self.assertIsNone(single["calendar"])
        self.assertIsNone(single["repeat_seconds"])
        self.assertIsNone(single["end_at"])

    def test_explicit_switch_from_calendar_to_interval(self):
        r = self.create(calendar=weekly())
        updated = self.act("reminder.update", id=r["id"], repeat_seconds=600)
        self.assertIsNone(updated["calendar"])
        self.assertEqual(updated["due_at"], r["due_at"])

    def test_invalid_changes_and_empty_update_roll_back(self):
        r = self.create(calendar=weekly())
        invalid = [{}, {"delay_seconds": 60}, {"calendar": weekly(), "repeat_seconds": 600},
                   {"due_at": "2026-10-09T00:00:00Z", "delay_seconds": 60},
                   {"end_at": "2026-10-04T00:00:00Z"}, {"end_at": "2026-10-08"},
                   {"title": "changed", "calendar": weekly([1,1])}, {"expected_revision": True, "title": "changed"},
                   {"state": "completed"}, {"note": "unsupported update field"}]
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaises(CommandError):
                self.act("reminder.update", id=r["id"], **fields)
            self.assertEqual(self.service._load(r["id"]), r)

    def test_invalid_creation_is_atomic(self):
        for fields in ({"calendar": weekly(), "repeat_seconds": 60}, {"calendar": None},
                       {"calendar": weekly(), "delay_seconds": 60},
                       {"calendar": weekly(), "end_at": "2026-10-04T00:00:00Z"},
                       {"delay_seconds": 60, "end_at": "2026-10-03T00:00:00Z"}):
            with self.subTest(fields=fields), self.assertRaises(CommandError):
                self.create(**fields)
        self.assertEqual(self.service.snapshot()["reminders"], [])

    def test_revision_conflict_and_idempotent_retry(self):
        r = self.create(delay_seconds=60)
        body = {"op": "reminder.update", "id": r["id"], "title": "new", "expected_revision": 1, "request_id": "edit1"}
        result = self.service.action(body)
        retried = self.service.action(body)
        self.assertTrue(retried["deduplicated"])
        self.assertEqual(result["assistive"], retried["assistive"])
        with self.assertRaises(CommandError):
            self.act("reminder.update", id=r["id"], title="stale", expected_revision=1)
        with self.assertRaises(CommandError):
            self.act("reminder.cancel", id=r["id"], expected_revision=1)
        self.now += timedelta(seconds=60)
        self.service.tick()
        with self.assertRaises(CommandError):
            self.act("reminder.ack", id=r["id"], expected_revision=2)

    def test_concurrent_cas_edit_has_one_winner(self):
        r = self.create(delay_seconds=60)
        def edit(title):
            try:
                self.act("reminder.update", id=r["id"], title=title, expected_revision=1)
                return True
            except CommandError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(edit, ["one", "two"])), 1)
        self.assertEqual(self.service._load(r["id"])["revision"], 2)

    def test_cancelled_reminder_cannot_be_reactivated_by_update(self):
        r = self.create(delay_seconds=60)
        self.act("reminder.cancel", id=r["id"])
        with self.assertRaises(CommandError):
            self.act("reminder.update", id=r["id"], delay_seconds=60)

    def test_legacy_record_without_revision_or_calendar_remains_editable(self):
        r = self.create(delay_seconds=60, repeat_seconds=120)
        for field in ("revision", "calendar", "scheduled_due_at", "end_at"):
            r.pop(field, None)
        with self.store._connection:
            self.store._connection.execute("UPDATE assistive_records SET snapshot=? WHERE id=?", (json.dumps(r), r["id"]))
        self.assertEqual(self.service.snapshot()["reminders"][0]["revision"], 1)
        self.assertEqual(self.act("reminder.update", id=r["id"], title="new", expected_revision=1)["revision"], 2)

    def test_calendar_and_edit_survive_restart_without_duplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"reminders.sqlite3"
            store = MissionStore(path)
            service = AssistiveService(store, start=False, clock=lambda: self.now)
            r = service.action({"op": "reminder.create", "title": "weekly", "calendar": weekly()})["assistive"]["record"]
            service.close(); store.close()
            self.now = instant("2026-10-14T04:00:00Z")
            store = MissionStore(path)
            service = AssistiveService(store, start=False, clock=lambda: self.now)
            try:
                self.assertEqual(service.snapshot()["reminders"][0]["state"], "missed")
                self.assertEqual(service.tick(), 0)
                updated = service.action({"op": "reminder.ack", "id": r["id"]})["assistive"]["record"]
                self.assertEqual(updated["due_at"], "2026-10-16T00:00:00+00:00")
                self.assertEqual(len(service.snapshot()["reminders"]), 1)
            finally:
                service.close(); store.close()

    def test_new_language_previews_do_not_write_or_schedule_motion(self):
        before = deepcopy(self.service.snapshot())
        proposal = self.service.preview("每周一和周三上午八点提醒我喝水")
        self.assertEqual(proposal["action"]["calendar"], weekly([1,3]))
        self.assertEqual(self.service.preview("开始20分钟平安确认")["action"], {"op":"wellbeing.start", "seconds":1200})
        self.assertEqual(self.service.snapshot(), before)
        self.assertIsNone(self.service.preview("不要每周一8点提醒我喝水"))


if __name__ == "__main__":
    unittest.main()
