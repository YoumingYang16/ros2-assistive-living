"""Meaningful domain tests; these do not represent physical care validation."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest

from robot_voice_patrol.assistive_service import AssistiveService
from robot_voice_patrol.assistive_connectors import DeliveryReceiptVerifier, sign_receipt
from robot_voice_patrol.contracts import CommandError
from robot_voice_patrol.store import MissionStore


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 3, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class AssistiveServiceTests(unittest.TestCase):
    def setUp(self):
        self.store = MissionStore()
        self.clock = Clock()
        self.service = AssistiveService(self.store, start=False, clock=self.clock)

    def tearDown(self):
        self.service.close()
        self.store.close()

    def create(self, **overrides):
        return self.service.action({"op": "reminder.create", "title": "喝水", "delay_seconds": 60, **overrides})["assistive"]["record"]

    def test_preview_has_no_persistent_effect(self):
        before = self.service.snapshot()
        preview = self.service.preview("十分钟后提醒我喝水")
        self.assertEqual(preview["action"]["delay_seconds"], 600)
        self.assertEqual(before, self.service.snapshot())

    def test_due_ack_is_not_physical_completion_claim(self):
        reminder = self.create()
        self.clock.advance(60)
        self.assertEqual(self.service.tick(), 1)
        self.assertEqual(self.service.tick(), 0)
        due = self.service.snapshot()["reminders"][0]
        self.assertEqual(due["state"], "due")
        self.assertFalse(due["notification"]["audible_confirmed"])
        result = self.service.action({"op": "reminder.ack", "id": reminder["id"]})
        record = result["assistive"]["record"]
        self.assertEqual(record["state"], "acknowledged")
        self.assertIn("不证明", record["completion_claim"])

    def test_due_to_missed_transitions_once(self):
        self.create(grace_seconds=30)
        self.clock.advance(60)
        self.service.tick()
        self.clock.advance(30)
        self.assertEqual(self.service.tick(), 1)
        self.assertEqual(self.service.snapshot()["reminders"][0]["state"], "missed")
        count = len(self.service.snapshot()["events"])
        self.assertEqual(self.service.tick(), 0)
        self.assertEqual(len(self.service.snapshot()["events"]), count)

    def test_snooze_missed_then_due(self):
        reminder = self.create(grace_seconds=30)
        self.clock.advance(100)
        self.service.tick()
        self.service.action({"op": "reminder.snooze", "id": reminder["id"], "seconds": 300})
        self.assertEqual(self.service.snapshot()["reminders"][0]["state"], "pending")
        self.clock.advance(300)
        self.service.tick()
        self.assertEqual(self.service.snapshot()["reminders"][0]["state"], "due")

    def test_cannot_ack_future_reminder(self):
        record = self.create()
        with self.assertRaises(CommandError):
            self.service.action({"op": "reminder.ack", "id": record["id"]})

    def test_cancel_prevents_due_notification(self):
        record = self.create()
        self.service.action({"op": "reminder.cancel", "id": record["id"]})
        self.clock.advance(999)
        self.assertEqual(self.service.tick(), 0)
        with self.assertRaises(CommandError):
            self.service.action({"op": "reminder.snooze", "id": record["id"]})

    def test_repeat_skips_missed_intervals_without_burst(self):
        record = self.create(repeat_seconds=120)
        self.clock.advance(1000)
        self.service.tick()
        updated = self.service.action({"op": "reminder.ack", "id": record["id"]})["assistive"]["record"]
        self.assertEqual(updated["state"], "pending")
        self.assertGreater(datetime.fromisoformat(updated["due_at"]), self.clock())
        self.assertEqual(updated["acknowledged_count"], 1)
        self.assertEqual(updated["occurrence"], 9)
        self.assertEqual(len(self.service.snapshot()["reminders"]), 1)

    def test_validation_rejects_ambiguous_time(self):
        for overrides in ({"due_at": "2026-10-03T12:00:00Z"}, {"delay_seconds": True},
                          {"delay_seconds": -1}, {"delay_seconds": float("nan")},
                          {"repeat_seconds": 1}, {"grace_seconds": 0}):
            with self.subTest(overrides=overrides), self.assertRaises(CommandError):
                self.create(**overrides)
        self.assertEqual(self.service.snapshot()["reminders"], [])

    def test_timezone_required_for_absolute_due(self):
        with self.assertRaises(CommandError):
            self.service.action({"op": "reminder.create", "title": "预约", "due_at": "2026-10-03T10:00:00"})

    def test_request_id_deduplicates_and_detects_collision(self):
        body = {"op": "reminder.create", "title": "喝水", "delay_seconds": 30, "request_id": "session:1"}
        first = self.service.action(body)
        second = self.service.action(body)
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["assistive"], second["assistive"])
        with self.assertRaises(CommandError):
            self.service.action({**body, "title": "用餐"})
        self.assertEqual(len(self.service.snapshot()["reminders"]), 1)

    def test_concurrent_request_deduplication(self):
        body = {"op": "need.add", "title": "纸巾", "request_id": "same"}
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: self.service.action(body), range(24)))
        self.assertEqual(len({r["assistive"]["record"]["id"] for r in responses}), 1)
        self.assertEqual(len(self.service.snapshot()["needs"]), 1)
        self.assertEqual(sum(bool(r.get("deduplicated")) for r in responses), 23)

    def test_atomic_record_receipt_and_event(self):
        self.store._connection.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON assistive_events BEGIN SELECT RAISE(ABORT,'fail'); END")
        with self.assertRaises(Exception):
            self.service.action({"op": "need.add", "title": "纸巾", "request_id": "retry"})
        self.assertEqual(self.service.snapshot()["needs"], [])
        self.assertEqual(self.store._connection.execute("SELECT COUNT(*) FROM assistive_receipts").fetchone()[0], 0)
        self.store._connection.execute("DROP TRIGGER fail_audit")
        self.assertTrue(self.service.action({"op": "need.add", "title": "纸巾", "request_id": "retry"})["ok"])

    def test_help_request_has_no_external_delivery(self):
        result = self.service.command("我需要洗澡帮助")
        record = result["assistive"]["record"]
        self.assertEqual(record["category"], "bathing")
        self.assertEqual(record["delivery_status"], "not_sent")
        self.assertFalse(record["physical_action_executed"])
        self.assertIn("尚未发送", result["message"])

    def test_emergency_local_escalation_is_not_dispatched(self):
        record = self.service.command("紧急求助")["assistive"]["record"]
        self.assertEqual(record["urgency"], "urgent")
        self.clock.advance(60)
        self.service.tick()
        request = self.service.snapshot()["assistance"][0]
        self.assertEqual(request["state"], "escalated")
        self.assertFalse(request["external_delivery_confirmed"])
        self.assertEqual(self.service.snapshot()["delivery"]["external_messages_sent"], 0)

    def test_local_ack_and_resolution_have_report_provenance(self):
        request = self.service.command("我需要移乘帮助")["assistive"]["record"]
        self.service.action({"op": "assistance.report", "id": request["id"], "status": "acknowledged", "note": "我已联系家人"})
        self.clock.advance(1000)
        self.assertEqual(self.service.tick(), 0)
        resolved = self.service.action({"op": "assistance.report", "id": request["id"], "status": "resolved"})["assistive"]["record"]
        self.assertEqual(len(resolved["reports"]), 2)
        self.assertFalse(resolved["reports"][1]["independently_verified"])
        self.assertEqual(resolved["delivery_status"], "not_sent")
        with self.assertRaises(CommandError):
            self.service.action({"op": "assistance.report", "id": request["id"], "status": "acknowledged"})

    def test_cannot_claim_transport_delivered(self):
        request = self.service.command("我需要洗澡帮助")["assistive"]["record"]
        with self.assertRaises(CommandError):
            self.service.action({"op": "assistance.report", "id": request["id"], "status": "delivered"})

    def test_contact_consent_required_and_removed_contact_rejected(self):
        contact = self.service.action({"op": "contact.save", "name": "家人", "contact_hint": "手动拨号"})["assistive"]["record"]
        body = {"op": "assistance.create", "category": "transfer", "contact_id": contact["id"]}
        with self.assertRaises(CommandError): self.service.action(body)
        request = self.service.action({**body, "consent": True})["assistive"]["record"]
        self.assertEqual(request["delivery_status"], "not_sent")
        self.service.action({"op": "contact.remove", "id": contact["id"]})
        with self.assertRaises(CommandError): self.service.action({**body, "consent": True})

    def test_wrong_record_type_is_rejected(self):
        reminder = self.create()
        with self.assertRaises(CommandError):
            self.service.action({"op": "assistance.cancel", "id": reminder["id"]})

    def test_profile_bounded_and_cannot_remove_confirmation(self):
        self.service.command("打开大字模式")
        self.assertEqual(self.service.snapshot()["profile"]["text_scale"], 1.5)
        for changes in ({"text_scale": 100}, {"speech_enabled": "false"}, {"confirmation_required": False}, {"secret": True}):
            with self.subTest(changes=changes), self.assertRaises(CommandError):
                self.service.action({"op": "profile.update", "changes": changes})

    def test_partial_profile_change_rolls_back_if_invalid(self):
        before = self.service.snapshot()["profile"]
        with self.assertRaises(CommandError):
            self.service.action({"op": "profile.update", "changes": {"text_scale": 1.8, "high_contrast": "yes"}})
        self.assertEqual(self.service.snapshot()["profile"], before)

    def test_checklist_daily_reuse_and_item_completion(self):
        first = self.service.command("开始晨间清单")["assistive"]["record"]
        second = self.service.command("开始晨间清单")["assistive"]["record"]
        self.assertEqual(first["id"], second["id"])
        for item in first["items"]:
            response = self.service.action({"op": "checklist.check", "id": first["id"], "item_id": item["id"], "checked": True})
        self.assertEqual(response["assistive"]["record"]["state"], "completed")
        reset = self.service.action({"op": "checklist.reset", "id": first["id"]})["assistive"]["record"]
        self.assertTrue(all(not item["checked"] for item in reset["items"]))

    def test_checklist_next_day_new_instance(self):
        first = self.service.command("开始睡前清单")["assistive"]["record"]
        self.clock.advance(86400)
        second = self.service.command("开始睡前清单")["assistive"]["record"]
        self.assertNotEqual(first["id"], second["id"])

    def test_custom_checklist_validation(self):
        result = self.service.action({"op": "checklist.start", "title": "我的准备", "items": ["确认手机", "确认钥匙"]})
        self.assertEqual(len(result["assistive"]["record"]["items"]), 2)
        for body in ({"routine": "night", "items": ["a"]}, {"title": "t", "items": ["a", "a"]}, {"title": "t", "items": []}):
            with self.subTest(body=body), self.assertRaises(CommandError):
                self.service.action({"op": "checklist.start", **body})

    def test_shopping_check_is_user_record_not_purchase(self):
        need = self.service.command("把纸巾加入购物清单")["assistive"]["record"]
        updated = self.service.action({"op": "need.check", "id": need["id"], "checked": True})["assistive"]["record"]
        self.assertEqual(updated["state"], "completed")
        self.assertFalse(updated["purchase_made"])
        self.service.action({"op": "need.remove", "id": need["id"]})
        self.assertEqual(self.service.snapshot()["needs"], [])

    def test_checkin_help_is_linked_atomic_local_request(self):
        result = self.service.action({"op": "checkin.create", "feeling": "uncomfortable", "note": "本人表示不适"})
        checkin = result["assistive"]["record"]
        self.assertIsNone(checkin["diagnosis"])
        request = self.service.snapshot()["assistance"][0]
        self.assertEqual(checkin["assistance_id"], request["id"])
        self.assertEqual(request["delivery_status"], "not_sent")

    def test_good_checkin_does_not_generate_help_request(self):
        self.service.command("记录我现在感觉良好")
        self.assertEqual(self.service.snapshot()["assistance"], [])

    def test_natural_language_fallback_and_ambiguity(self):
        for text in ("去会议室", "立即停止", "不要提醒我喝水", "如果我洗澡就帮我", "我需要洗澡还是穿衣",
                     "我需要洗澡帮助吗", "帮我洗澡并移乘", "别人说我需要洗澡帮助", "帮我调整药量", "如果紧急求助"):
            with self.subTest(text=text):
                self.assertIsNone(self.service.command(text))
        self.assertEqual(self.service.snapshot()["assistance"], [])

    def test_chinese_and_daily_time_parsing(self):
        cases = {"半小时后提醒我喝水": 1800, "两分钟后提醒我吃饭": 120, "二十三分钟后提醒我活动": 1380}
        for text, seconds in cases.items():
            self.assertEqual(self.service.preview(text)["action"]["delay_seconds"], seconds)
        daily = self.service.preview("每天9点30分提醒我查看预约")["action"]
        self.assertEqual(daily["due_at"], "2026-10-03T01:30:00+00:00")
        self.assertEqual(daily["repeat_seconds"], 86400)
        with self.assertRaises(CommandError): self.service.preview("每天25点提醒我喝水")

    def test_daily_timezone_preference(self):
        self.service.action({"op": "profile.update", "changes": {"timezone": "UTC"}})
        action = self.service.preview("每天9点提醒我查看安排")["action"]
        self.assertEqual(action["due_at"], "2026-10-03T09:00:00+00:00")

    def test_catalog_software_and_human_examples_execute(self):
        entries = self.service.catalog()["categories"]
        self.assertGreaterEqual(len(entries), 40)
        self.assertEqual(len(entries), len({entry["id"] for entry in entries}))
        for entry in entries:
            if entry["mode"] == "hardware_interface": continue
            with self.subTest(category=entry["id"]):
                self.assertIsNotNone(self.service.preview(entry["examples"][0]))
                self.assertTrue(self.service.command(entry["examples"][0])["ok"])

    def test_strict_unknown_fields_and_bad_objects(self):
        for body in ([], {"op": "need.add", "title": "纸", "execute_payment": True}, {"op": "unknown"},
                     {"op": "contact.save", "name": "a\x00b"}, {"op": "need.add", "title": "x" * 1000},
                     {"op": "need.add", "title": "x", "quantity": 1}, {"op": "checkin.create", "feeling": "diagnosed"}):
            with self.subTest(body=body), self.assertRaises(CommandError): self.service.action(body)

    def test_id_length_supports_engine_session_prefix(self):
        result = self.service.command("把牛奶加入购物清单", request_id="a" * 240)
        self.assertTrue(result["ok"])

    def test_closed_service_rejects_new_mutations(self):
        self.service.close()
        with self.assertRaises(CommandError): self.service.command("把牛奶加入购物清单")
        self.assertEqual(self.service.snapshot()["needs"], [])

    def test_invalid_update_id_does_not_create_contact(self):
        for identifier in (None, 0, False, ""):
            with self.subTest(identifier=identifier), self.assertRaises(CommandError):
                self.service.action({"op": "contact.save", "name": "家人", "id": identifier})
        self.assertEqual(self.service.snapshot()["contacts"], [])

    def test_no_mission_or_robot_jobs_created_by_daily_services(self):
        self.service.command("我需要穿衣帮助")
        self.service.command("10分钟后提醒我喝水")
        self.service.command("开始出门清单")
        self.assertEqual(self.store._connection.execute("SELECT COUNT(*) FROM missions").fetchone()[0], 0)
        self.assertEqual(self.store._connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_worker_advances_independently_of_motion_dispatch(self):
        background = AssistiveService(self.store, start=True, clock=self.clock)
        try:
            self.create()
            self.clock.advance(61)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and background.snapshot()["reminders"][0]["state"] == "pending":
                time.sleep(0.03)
            self.assertEqual(background.snapshot()["reminders"][0]["state"], "due")
        finally:
            background.close()


class AssistivePersistenceTests(unittest.TestCase):
    def test_restart_overdue_and_receipts_survive(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.sqlite3"
            clock = Clock()
            store = MissionStore(path)
            service = AssistiveService(store, start=False, clock=clock)
            first = service.command("一分钟后提醒我喝水", request_id="persisted")
            service.command("我需要如厕帮助")
            service.close()
            store.close()
            clock.advance(2000)
            store = MissionStore(path)
            service = AssistiveService(store, start=False, clock=clock)
            try:
                snapshot = service.snapshot()
                self.assertEqual(snapshot["reminders"][0]["state"], "missed")
                self.assertEqual(snapshot["assistance"][0]["state"], "escalated")
                retry = service.command("一分钟后提醒我喝水", request_id="persisted")
                self.assertTrue(retry["deduplicated"])
                self.assertEqual(first["assistive"], retry["assistive"])
                self.assertEqual(len(service.snapshot()["reminders"]), 1)
                self.assertEqual(service.tick(), 0)
            finally:
                service.close()
                store.close()


class AssistiveConnectorTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = MissionStore()
        self.service = AssistiveService(self.store, start=False, clock=self.clock)
        self.key = b"unit-test-provider-key-32-bytes-long"
        self.verifier = DeliveryReceiptVerifier("fixture", self.key, clock=self.clock)
        self.contact = self.service.action({"op": "contact.save", "name": "家人"})["assistive"]["record"]
        self.request = self.service.action({"op": "assistance.create", "category": "toileting",
                                           "contact_id": self.contact["id"], "consent": True})["assistive"]["record"]

    def tearDown(self):
        self.service.close()
        self.store.close()

    def receipt(self, **changes):
        body = {"version": 1, "receipt_id": "receipt-1", "request_id": self.request["id"],
                "contact_id": self.contact["id"], "provider": "fixture", "status": "delivered",
                "occurred_at": self.clock().isoformat(), "delivery_id": "delivery-1", **changes}
        return sign_receipt(body, self.key)

    def test_envelope_requires_consent_and_does_not_dispatch(self):
        envelope = self.service.delivery_envelope(self.request["id"])
        self.assertTrue(envelope["consent"])
        self.assertEqual(envelope["contact_id"], self.contact["id"])
        self.assertEqual(self.service.snapshot()["assistance"][0]["delivery_status"], "not_sent")
        no_consent = self.service.command("我需要洗澡帮助")["assistive"]["record"]
        with self.assertRaises(CommandError): self.service.delivery_envelope(no_consent["id"])

    def test_signed_delivery_and_provider_acknowledgment(self):
        result = self.service.record_delivery_receipt(self.receipt(), self.verifier)
        self.assertEqual(result["assistive"]["record"]["state"], "delivered")
        self.assertTrue(result["assistive"]["record"]["external_delivery_confirmed"])
        result = self.service.record_delivery_receipt(self.receipt(receipt_id="receipt-2", status="acknowledged"), self.verifier)
        request = result["assistive"]["record"]
        self.assertEqual(request["state"], "acknowledged")
        self.assertEqual(request["acknowledgement_source"], "authenticated_provider_report")
        self.assertFalse(request["physical_action_executed"])

    def test_delivery_without_response_still_escalates(self):
        self.service.record_delivery_receipt(self.receipt(), self.verifier)
        self.clock.advance(901)
        self.service.tick()
        self.assertEqual(self.service.snapshot()["assistance"][0]["state"], "escalated")

    def test_receipt_replay_is_idempotent_and_order_independent(self):
        first = self.receipt()
        self.service.record_delivery_receipt(first, self.verifier)
        second = dict(reversed(list(first.items())))
        self.assertTrue(self.service.record_delivery_receipt(second, self.verifier)["deduplicated"])
        self.assertEqual(self.service.snapshot()["delivery"]["authenticated_receipts"], 1)

    def test_receipt_id_collision_is_rejected(self):
        self.service.record_delivery_receipt(self.receipt(), self.verifier)
        with self.assertRaises(CommandError):
            self.service.record_delivery_receipt(self.receipt(status="acknowledged"), self.verifier)

    def test_tampered_signature_is_rejected_without_mutation(self):
        forged = {**self.receipt(), "status": "acknowledged"}
        with self.assertRaises(CommandError): self.service.record_delivery_receipt(forged, self.verifier)
        self.assertEqual(self.service.snapshot()["assistance"][0]["state"], "created")

    def test_wrong_identity_or_contact_is_rejected(self):
        for changes in ({"provider": "other"}, {"contact_id": "wrong-contact"}, {"request_id": "wrong-request"}):
            with self.subTest(changes=changes), self.assertRaises(CommandError):
                self.service.record_delivery_receipt(self.receipt(**changes), self.verifier)

    def test_old_or_future_receipts_rejected(self):
        for seconds in (-301, 31):
            with self.subTest(seconds=seconds), self.assertRaises(CommandError):
                self.service.record_delivery_receipt(self.receipt(occurred_at=(self.clock() + timedelta(seconds=seconds)).isoformat()), self.verifier)

    def test_failure_escalates_and_cannot_overwrite_proven_delivery(self):
        result = self.service.record_delivery_receipt(self.receipt(status="failed"), self.verifier)
        self.assertEqual(result["assistive"]["record"]["delivery_status"], "failed")
        self.assertEqual(result["assistive"]["record"]["state"], "escalated")
        self.service.record_delivery_receipt(self.receipt(receipt_id="receipt-2"), self.verifier)
        with self.assertRaises(CommandError):
            self.service.record_delivery_receipt(self.receipt(receipt_id="receipt-3", status="failed"), self.verifier)

    def test_cancelled_request_cannot_accept_new_receipts(self):
        self.service.action({"op": "assistance.cancel", "id": self.request["id"]})
        with self.assertRaises(CommandError): self.service.record_delivery_receipt(self.receipt(), self.verifier)

    def test_removed_contact_blocks_dispatch_envelope(self):
        self.service.action({"op": "contact.remove", "id": self.contact["id"]})
        with self.assertRaises(CommandError): self.service.delivery_envelope(self.request["id"])

    def test_protocol_rejects_extra_fields_bad_version_and_status(self):
        for changes in ({"version": True}, {"version": 1.0}, {"status": []}, {"status": "resolved"}, {"occurred_at": "no-timezone"}):
            with self.subTest(changes=changes), self.assertRaises(CommandError):
                self.verifier.verify(self.receipt(**changes))
        with self.assertRaises(CommandError): self.verifier.verify({**self.receipt(), "untrusted": "field"})

    def test_backup_includes_assistive_state_without_schema_bump(self):
        with tempfile.TemporaryDirectory() as folder:
            store = MissionStore()
            service = AssistiveService(store, start=False)
            try:
                service.command("把纸巾加入购物清单")
                destination = Path(folder) / "backup.sqlite3"
                store.backup_to(destination)
                restored = MissionStore(destination)
                restored_service = AssistiveService(restored, start=False)
                try:
                    self.assertEqual(restored._connection.execute("PRAGMA user_version").fetchone()[0], 3)
                    self.assertEqual(restored_service.snapshot()["needs"][0]["title"], "纸巾")
                finally:
                    restored_service.close()
                    restored.close()
            finally:
                service.close()
                store.close()


if __name__ == "__main__":
    unittest.main()
