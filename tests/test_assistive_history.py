"""History pagination, privacy, exact time boundaries and read-only behavior."""
from datetime import datetime, timedelta, timezone
import json
import unittest

from robot_voice_patrol.assistive_history import history_metadata, query_history, record_detail
from robot_voice_patrol.assistive_service import AssistiveService
from robot_voice_patrol.contracts import CommandError
from robot_voice_patrol.store import MissionStore, encode


class AssistiveHistoryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 3, tzinfo=timezone.utc)
        self.store = MissionStore()
        self.service = AssistiveService(self.store, start=False, clock=lambda: self.now)
        self.addCleanup(self.store.close)
        self.addCleanup(self.service.close)

    def act(self, op, **fields):
        return self.service.action({"op": op, **fields})["assistive"]["record"]

    def test_empty_history_and_metadata_are_useful(self):
        result = query_history(self.service)
        self.assertEqual((result["records"], result["total"], result["has_more"]), ([], 0, False))
        self.assertEqual(result["meta"]["date_field"], "updated_at")
        self.assertTrue(result["meta"]["includes_removed"])
        data = history_metadata()
        data["kinds"].clear()
        self.assertEqual(len(history_metadata()["kinds"]), 9)

    def test_more_than_dashboard_limit_can_be_retrieved_without_loss(self):
        ids = []
        for i in range(263):
            ids.append(self.act("need.add", title=f"需求{i:03d}")["id"])
        observed = []
        for offset in range(0, 275, 25):
            page = query_history(self.service, kind="need", limit=25, offset=offset)
            self.assertEqual(page["total"], 263)
            observed.extend(item["id"] for item in page["records"])
            self.assertEqual(page["has_more"], offset + len(page["records"]) < 263)
        self.assertEqual(observed, sorted(ids))  # Stable tie order for equal update times.
        self.assertEqual(query_history(self.service, offset=999)["records"], [])

    def test_newest_updates_precede_older_records(self):
        first = self.act("need.add", title="早创建")
        self.now += timedelta(seconds=1)
        second = self.act("need.add", title="晚创建")
        self.assertEqual(query_history(self.service)["records"][0]["id"], second["id"])
        self.now += timedelta(seconds=1)
        self.act("need.check", id=first["id"], checked=True)
        self.assertEqual(query_history(self.service)["records"][0]["id"], first["id"])

    def test_removed_records_are_preserved_and_searchable(self):
        removed = self.act("need.add", title="历史需求")
        self.act("need.remove", id=removed["id"])
        contact = self.act("contact.save", name="家人", contact_hint="不应进入历史的地址")
        self.act("contact.remove", id=contact["id"])
        result = query_history(self.service, state="removed")
        self.assertEqual(result["total"], 2)
        self.assertEqual(record_detail(self.service, removed["id"])["record"]["state"], "removed")

    def test_supported_kinds_include_new_continuity_records(self):
        records = [
            self.act("contact.save", name="家人"),
            self.act("reminder.create", title="喝水", delay_seconds=60),
            self.act("assistance.create", category="general"),
            self.act("checklist.start", title="准备", items=["衣物"]),
            self.act("need.add", title="牛奶"),
            self.act("checkin.create", feeling="good"),
            self.act("incident.create", category="power_failure"),
            self.act("equipment.add", title="助行器", category="mobility_aid"),
            self.act("wellbeing.start", seconds=60),
        ]
        for record in records:
            with self.subTest(kind=record["kind"]):
                page = query_history(self.service, kind=record["kind"], state=record["state"])
                self.assertIn(record["id"], {r["id"] for r in page["records"]})

    def test_search_matches_only_human_fields(self):
        a = self.act("need.add", title="Buy Milk", note="有盖的小瓶")
        b = self.act("assistance.create", category="general", detail="整理蓝色书本")
        c = self.act("contact.save", name="王阿姨", relationship="UNSEARCHABLE_RELATION", contact_hint="UNSEARCHABLE_CONTACT")
        for text, expected in [("milk", a["id"]), ("小瓶", a["id"]), ("蓝色", b["id"]), ("王阿姨", c["id"])]:
            self.assertEqual(query_history(self.service, query=text)["records"][0]["id"], expected)
        for text in [a["id"], "UNSEARCHABLE_CONTACT", "UNSEARCHABLE_RELATION", "contact_hint"]:
            self.assertEqual(query_history(self.service, query=text)["total"], 0)

    def test_sql_fragments_and_wildcards_are_literal_text(self):
        self.act("need.add", title="100%_done\\end")
        malicious = "' OR 1=1 --"
        record = self.act("need.add", title=malicious)
        self.assertEqual(query_history(self.service, query=malicious)["records"][0]["id"], record["id"])
        self.assertEqual(query_history(self.service, query="%_")["total"], 1)
        self.assertEqual(query_history(self.service, query="DROP TABLE")["total"], 0)
        self.assertEqual(query_history(self.service)["total"], 2)

    def test_dates_are_inclusive_updated_times_not_creation_times(self):
        record = self.act("need.add", title="跨日更新")
        self.now += timedelta(days=1)
        self.act("need.check", id=record["id"], checked=True)
        page = query_history(self.service, since="2026-10-04T08:00:00+08:00", until="2026-10-04T00:00:00Z")
        self.assertEqual(page["total"], 1)
        self.assertEqual(query_history(self.service, until="2026-10-03T23:59:59.999999Z")["total"], 0)

    def test_microsecond_boundaries_do_not_round(self):
        a = self.act("need.add", title="整数秒")
        self.now += timedelta(microseconds=1)
        b = self.act("need.add", title="一微秒")
        self.now += timedelta(microseconds=1)
        c = self.act("need.add", title="二微秒")
        self.assertEqual([r["id"] for r in query_history(self.service, until="2026-10-03T00:00:00Z")["records"]], [a["id"]])
        page = query_history(self.service, since="2026-10-03T00:00:00.000001Z", until="2026-10-03T00:00:00.000001Z")
        self.assertEqual([r["id"] for r in page["records"]], [b["id"]])
        self.assertEqual(query_history(self.service, since="2026-10-03T00:00:00.000002Z")["records"][0]["id"], c["id"])

    def test_invalid_filter_types_are_rejected(self):
        cases = [
            {"kind": "mission"}, {"kind": []}, {"kind": ""}, {"state": "invalid"},
            {"state": []}, {"kind": "contact", "state": "due"}, {"query": False},
            {"query": "x" * 201}, {"query": "bad\nquery"}, {"limit": True},
            {"limit": 0}, {"limit": 101}, {"limit": "25"}, {"limit": 2.0},
            {"offset": -1}, {"offset": 1_000_001}, {"offset": False},
            {"since": "2026-10-03"}, {"until": "2026-10-03T08:00:00"},
            {"until": "2026-99-03T08:00:00Z"}, {"since": 123},
            {"since": "2026-10-04T00:00:00Z", "until": "2026-10-03T00:00:00Z"},
        ]
        for fields in cases:
            with self.subTest(fields=fields), self.assertRaises(CommandError):
                query_history(self.service, **fields)

    def test_query_and_details_do_not_advance_due_work_or_write_audit(self):
        reminder = self.act("reminder.create", title="等待显示", delay_seconds=60)
        before = list(self.store._connection.iterdump())
        self.now += timedelta(seconds=120)
        page = query_history(self.service)
        detail = record_detail(self.service, reminder["id"])
        self.assertEqual(page["records"][0]["state"], "pending")
        self.assertEqual(detail["record"]["state"], "pending")
        self.assertEqual(before, list(self.store._connection.iterdump()))

    def test_detail_audit_pages_are_complete_and_separate_records(self):
        a = self.act("need.add", title="多次核对")
        self.act("need.add", title="其他记录")
        for index in range(60):
            self.act("need.check", id=a["id"], checked=bool(index % 2))
        first = record_detail(self.service, a["id"], event_limit=50)
        second = record_detail(self.service, a["id"], event_limit=50, event_offset=50)
        self.assertEqual((first["event_total"], len(first["events"]), first["events_has_more"]), (61, 50, True))
        self.assertEqual((len(second["events"]), second["events_has_more"]), (11, False))
        events = first["events"] + second["events"]
        self.assertEqual(len({event["id"] for event in events}), 61)
        self.assertTrue(all(event["record_id"] == a["id"] for event in events))
        self.assertEqual([event["id"] for event in events], sorted((event["id"] for event in events), reverse=True))

    def test_detail_redacts_structured_transport_credentials_at_every_level(self):
        contact = self.act("contact.save", name="家人", contact_hint="PRIVATE_ADDRESS")
        with self.store._lock, self.store._connection:
            contact["future_provider"] = {"api_key": "PRIVATE_API", "nested": [{"signature": "PRIVATE_SIGNATURE", "status": "未发送"}]}
            self.service._save(contact)
            self.service._audit("test.local", contact, receipt={"body": "PRIVATE_BODY"}, extra={"access_token": "PRIVATE_TOKEN", "status": "local"})
        detail = record_detail(self.service, contact["id"])
        page = query_history(self.service, kind="contact")
        for result in (detail, page):
            self.assertNotIn("PRIVATE_", json.dumps(result))
        self.assertEqual(detail["record"]["name"], "家人")
        self.assertEqual(detail["record"]["future_provider"]["nested"][0]["status"], "未发送")
        self.assertEqual(detail["events"][0]["data"]["extra"]["status"], "local")
        self.assertEqual(self.service._load(contact["id"])["contact_hint"], "PRIVATE_ADDRESS")

    def test_details_validate_id_pagination_and_return_not_found(self):
        for record_id in [None, [], "", "x" * 101, "bad\nname"]:
            with self.subTest(record_id=record_id), self.assertRaises(CommandError):
                record_detail(self.service, record_id)
        with self.assertRaisesRegex(CommandError, "记录不存在"):
            record_detail(self.service, "missing")
        with self.assertRaisesRegex(CommandError, "记录不存在"):
            record_detail(self.service, "' OR 1=1 --")
        for fields in [{"event_limit": 0}, {"event_limit": True}, {"event_offset": -1}, {"event_offset": 1_000_001}]:
            with self.subTest(fields=fields), self.assertRaises(CommandError):
                record_detail(self.service, "missing", **fields)

    def test_database_failures_do_not_expose_sql_or_corrupt_payload(self):
        record = self.act("need.add", title="损坏记录")
        with self.store._connection:
            self.store._connection.execute("UPDATE assistive_records SET snapshot=? WHERE id=?", ("PRIVATE_CORRUPT_JSON", record["id"]))
        for call in [lambda: query_history(self.service), lambda: record_detail(self.service, record["id"])]:
            with self.assertRaises(CommandError) as caught:
                call()
            self.assertNotIn("PRIVATE_CORRUPT_JSON", str(caught.exception))
            self.assertNotIn("SELECT", str(caught.exception))

    def test_old_reminder_revision_default_is_exposed_without_database_write(self):
        reminder = self.act("reminder.create", title="旧版提醒", delay_seconds=60)
        reminder.pop("revision", None)
        with self.store._connection:
            self.store._connection.execute("UPDATE assistive_records SET snapshot=? WHERE id=?", (encode(reminder), reminder["id"]))
        before = list(self.store._connection.iterdump())
        self.assertEqual(query_history(self.service)["records"][0]["revision"], 1)
        self.assertEqual(record_detail(self.service, reminder["id"])["record"]["revision"], 1)
        self.assertEqual(before, list(self.store._connection.iterdump()))


if __name__ == "__main__":
    unittest.main()
