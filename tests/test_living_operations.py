"""Service continuity tests: records and deadlines never imply physical care."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from robot_voice_patrol.assistive_service import AssistiveService
from robot_voice_patrol.contracts import CommandError
from robot_voice_patrol.store import MissionStore
from robot_voice_patrol.living_operations import INCIDENTS


class LivingOperationsTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,10,3,tzinfo=timezone.utc)
        self.store=MissionStore()
        self.service=AssistiveService(self.store,start=False,clock=lambda:self.now)
        self.addCleanup(self.store.close);self.addCleanup(self.service.close)

    def act(self,op,**fields):
        return self.service.action({'op':op,**fields})['assistive']

    def test_every_domain_has_unique_registered_scenarios(self):
        c=self.service.catalog();audit=c['coverage_audit']
        self.assertEqual(len(audit['domains']),12)
        self.assertTrue(all(d['scenario_ids'] for d in audit['domains']))
        ids=[i for d in audit['domains'] for i in d['scenario_ids']]
        self.assertEqual(len(ids),len(set(ids)))
        self.assertEqual(set(ids),{r['id'] for r in c['categories']})
        self.assertFalse(audit['universal_exhaustiveness_claim'])

    def test_unfinished_wait_remains_visible_among_newer_completed_records(self):
        waiting=self.act('wellbeing.start',seconds=60)['record']
        self.now+=timedelta(seconds=1)
        for _ in range(251):
            record=self.act('wellbeing.start',seconds=60)['record']
            self.act('wellbeing.confirm',id=record['id'])
        visible=self.service.snapshot()['wellbeing']
        self.assertEqual(len(visible),250)
        self.assertEqual(visible[0]['id'],waiting['id'])

    def test_acknowledged_help_still_counts_as_unfinished(self):
        with patch('robot_voice_patrol.assistive_service.MAX_RECORDS',1):
            record=self.act('assistance.create',category='general')['record']
            self.act('assistance.report',id=record['id'],status='acknowledged')
            with self.assertRaises(CommandError):self.act('assistance.create',category='general')

    def test_full_help_ledger_does_not_starve_reminders_and_retries_after_resolution(self):
        with patch('robot_voice_patrol.assistive_service.MAX_RECORDS',1):
            help_record=self.act('assistance.create',category='general')['record']
            waiting=self.act('wellbeing.start',seconds=30)['record']
            reminder=self.act('reminder.create',title='待确认事项',delay_seconds=30)['record']
            self.now+=timedelta(seconds=31)
            self.service.tick()
            record=self.service._load(waiting['id'])
            self.assertEqual(record['state'],'overdue')
            self.assertIsNone(record['assistance_id'])
            self.assertTrue(record['escalation_error'])
            self.assertEqual(self.service._load(reminder['id'])['state'],'due')
            self.assertEqual(self.service.tick(),0)
            self.act('assistance.report',id=help_record['id'],status='resolved')
            self.service.tick()
            record=self.service._load(waiting['id'])
            self.assertIsNotNone(record['assistance_id'])
            self.assertIsNone(record['escalation_error'])

    def test_all_interruption_types_create_local_help_and_truthful_provenance(self):
        for category in INCIDENTS:
            record=self.act('incident.create',category=category)['record']
            request=self.service._load(record['assistance_id'],'assistance')
            self.assertEqual(request['delivery_status'],'not_sent')
            self.assertFalse(record['sensor_confirmed'])
            self.assertFalse(record['physical_recovery_confirmed'])

    def test_duplicate_active_incident_does_not_duplicate_help(self):
        a=self.act('incident.create',category='power_failure')['record']
        b=self.act('incident.create',category='power_failure')['record']
        self.assertEqual(a['id'],b['id'])
        self.assertEqual(len(self.service.snapshot()['assistance']),1)

    def test_acknowledged_incident_is_unresolved_in_handover(self):
        a=self.act('incident.create',category='power_failure')['record']
        self.act('incident.ack',id=a['id'])
        report=self.act('handover.build')['report']
        self.assertEqual(report['incidents'][0]['state'],'acknowledged')
        self.act('assistance.report',id=a['assistance_id'],status='acknowledged')
        self.assertEqual(self.act('handover.build')['report']['counts']['assistance'],1)

    def test_incident_resolve_does_not_forge_linked_help_completion(self):
        a=self.act('incident.create',category='caregiver_absent')['record']
        r=self.act('incident.resolve',id=a['id'],note='本人已处理')['record']
        self.assertFalse(r['physical_recovery_confirmed'])
        self.assertEqual(self.service._load(a['assistance_id'])['state'],'created')
        new=self.act('incident.create',category='caregiver_absent')['record']
        self.assertNotEqual(a['id'],new['id'])

    def test_bad_incident_cannot_create_help(self):
        for data in [{'category':'medical_diagnosis'},{'category':'power_failure','sensor_confirmed':True},{'category':'power_failure','detail':False}]:
            with self.assertRaises(CommandError):self.act('incident.create',**data)
        self.assertEqual(self.service.snapshot()['assistance'],[])

    def test_equipment_creates_linked_maintenance_reminder(self):
        due=(self.now+timedelta(days=1)).isoformat()
        r=self.act('equipment.add',title='呼叫器',category='communication',service_due_at=due)['record']
        reminder=self.service._load(r['reminder_id'],'reminder')
        self.assertEqual(reminder['due_at'],due)
        self.assertFalse(r['hardware_verified'])

    def test_equipment_service_replaces_future_reminder_atomically(self):
        r=self.act('equipment.add',title='轮椅',category='mobility_aid',service_due_at=(self.now+timedelta(days=1)).isoformat())['record']
        revised=self.act('equipment.service',id=r['id'],next_due_at=(self.now+timedelta(days=7)).isoformat(),note='本人安排了检查')['record']
        self.assertEqual(self.service._load(r['reminder_id'])['state'],'cancelled')
        self.assertNotEqual(revised['reminder_id'],r['reminder_id'])
        self.assertFalse(revised['service_reports'][0]['independently_verified'])

    def test_invalid_next_date_rolls_back_previous_reminder_cancellation(self):
        r=self.act('equipment.add',title='轮椅',category='mobility_aid',service_due_at=(self.now+timedelta(days=1)).isoformat())['record']
        with self.assertRaises(CommandError):
            self.act('equipment.service',id=r['id'],next_due_at=(self.now+timedelta(days=999)).isoformat())
        self.assertEqual(self.service._load(r['reminder_id'])['state'],'pending')
        self.assertEqual(self.service._load(r['id'])['service_reports'],[])

    def test_retirement_cancels_reminder_without_claiming_device_stop(self):
        r=self.act('equipment.add',title='轮椅',category='mobility_aid',service_due_at=(self.now+timedelta(days=1)).isoformat())['record']
        self.act('equipment.retire',id=r['id'])
        self.assertEqual(self.service._load(r['reminder_id'])['state'],'cancelled')
        with self.assertRaises(CommandError):self.act('equipment.service',id=r['id'])

    def test_equipment_dates_and_fields_are_strict(self):
        for value in [None,0,'bad','2026-10-03T12:00:00']:
            with self.assertRaises(CommandError):self.act('equipment.add',title='轮椅',category='mobility_aid',service_due_at=value)
        with self.assertRaises(CommandError):self.act('equipment.add',title='轮椅',category='mobility_aid',execute_repair=True)

    def test_handover_is_read_only_private_and_upcoming(self):
        contact=self.act('contact.save',name='联系人',contact_hint='PRIVATE_CONTACT')['record']
        self.act('assistance.create',category='bathing',detail='PRIVATE_NOTE',contact_id=contact['id'],consent=True)
        self.act('reminder.create',title='soon',delay_seconds=60)
        self.act('reminder.create',title='later',delay_seconds=172800)
        before=self.service.snapshot()
        report=self.act('handover.build')['report']
        self.assertEqual(before,self.service.snapshot())
        self.assertNotIn('PRIVATE',json.dumps(report))
        self.assertEqual([r['title'] for r in report['reminders']],['soon'])
        self.assertEqual(report['external_messages_sent'],0)

    def test_handover_does_not_hide_rows_beyond_dashboard_limit(self):
        for i in range(255):self.act('need.add',title=str(i))
        self.assertEqual(len(self.service.snapshot()['needs']),250)
        self.assertEqual(self.act('handover.build')['report']['counts']['needs'],255)

    def test_preview_new_routes_has_no_side_effects(self):
        before=self.service.snapshot()
        for command in ['报告停电','登记我的轮椅','开始工作学习准备清单','生成照护交接摘要','开始30分钟平安确认']:
            self.assertIsNotNone(self.service.preview(command))
        self.assertEqual(before,self.service.snapshot())

    def test_negated_and_combined_new_commands_are_not_partially_executed(self):
        for text in ['不要报告停电','如果停电就报告停电','登记我的轮椅然后开始工作','请问是否可以生成照护交接摘要']:
            self.assertIsNone(self.service.command(text))
        self.assertEqual(self.service.snapshot()['incidents'],[])

    def test_new_operations_are_idempotent(self):
        for _ in range(2):self.act('equipment.add',title='轮椅',category='mobility_aid',request_id='same')
        self.assertEqual(len(self.service.snapshot()['equipment']),1)
        with self.assertRaises(CommandError):self.act('equipment.add',title='助行器',category='mobility_aid',request_id='same')

    def test_wellbeing_timeout_creates_one_local_help_without_diagnosis(self):
        r=self.act('wellbeing.start',seconds=30)['record']
        self.now+=timedelta(seconds=31)
        self.assertEqual(self.service.tick(),1)
        self.assertEqual(self.service.tick(),0)
        r=self.service._load(r['id'])
        self.assertEqual(r['state'],'overdue');self.assertIsNone(r['health_inference'])
        self.assertEqual(self.service._load(r['assistance_id'])['delivery_status'],'not_sent')

    def test_wellbeing_early_confirmation_prevents_timeout_help(self):
        r=self.act('wellbeing.start',seconds=30)['record']
        self.act('wellbeing.confirm',id=r['id'])
        self.now+=timedelta(seconds=60);self.service.tick()
        self.assertEqual(self.service.snapshot()['assistance'],[])

    def test_wellbeing_cancel_keeps_prior_help_for_manual_reconciliation(self):
        r=self.act('wellbeing.start',seconds=30)['record']
        self.now+=timedelta(seconds=60);self.service.tick()
        r=self.act('wellbeing.cancel',id=r['id'])['record']
        self.assertEqual(self.service._load(r['assistance_id'])['state'],'created')

    def test_support_records_and_deadlines_survive_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'state.sqlite3'
            store=MissionStore(path);service=AssistiveService(store,start=False,clock=lambda:self.now)
            service.action({'op':'wellbeing.start','seconds':30})
            service.action({'op':'equipment.add','title':'轮椅','category':'mobility_aid'})
            service.close();store.close();self.now+=timedelta(seconds=31)
            store=MissionStore(path);service=AssistiveService(store,start=False,clock=lambda:self.now)
            try:
                self.assertEqual(len(service.snapshot()['equipment']),1)
                self.assertEqual(service.snapshot()['wellbeing'][0]['state'],'overdue')
                self.assertEqual(len(service.snapshot()['assistance']),1)
            finally:service.close();store.close()
