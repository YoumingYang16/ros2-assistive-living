"""Cross-domain dispatch and HTTP boundaries, using only local mock data."""
import json
from pathlib import Path
import threading
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from robot_voice_patrol.config import load_config
from robot_voice_patrol.contracts import CommandError
from robot_voice_patrol.engine import MissionEngine
from robot_voice_patrol.mock_adapter import MockAdapter
from robot_voice_patrol.server import create_server


class AssistiveIntegrationTests(unittest.TestCase):
    def setUp(self):
        config=load_config(Path(__file__).parents[1]/'config/home.json')
        self.engine=MissionEngine(config,MockAdapter(config,fixture_skills=True),start_scheduler=False)
        self.addCleanup(self.engine.close)

    def test_preview_does_not_create_assistance_or_reminder(self):
        for text in ['我需要洗澡帮助','十分钟后提醒我喝水']:
            self.assertEqual(self.engine.preview(text)['kind'],'assistive')
        state=self.engine.assistive.snapshot()
        self.assertEqual(state['assistance'],[])
        self.assertEqual(state['reminders'],[])

    def test_new_assistive_action_preserves_explicit_robot_confirmation(self):
        self.engine.submit('把手机从客厅送到卧室',session_id='same')
        pending=self.engine.store.session('same')['pending_plan']
        self.engine.submit('十分钟后提醒我喝水',session_id='same')
        self.assertEqual(self.engine.store.session('same')['pending_plan'],pending)
        response=self.engine.submit('确认执行',session_id='same')
        self.assertNotIn('assistive',response)
        self.assertEqual(response['state']['mission']['steps'],pending['steps'])

    def test_same_assistive_request_deduplicates_and_conflicting_text_rejects(self):
        for _ in range(2):self.engine.submit('十分钟后提醒我喝水','r1','s1')
        self.assertEqual(len(self.engine.assistive.snapshot()['reminders']),1)
        with self.assertRaises(CommandError):self.engine.submit('十分钟后提醒我吃饭','r1','s1')

    def test_sessions_have_distinct_assistive_receipt_keys(self):
        for session in ['first','second']:self.engine.submit('我需要如厕帮助','same',session)
        self.assertEqual(len(self.engine.assistive.snapshot()['assistance']),2)

    def test_cross_domain_request_identifier_cannot_change_meaning(self):
        self.engine.submit('十分钟后提醒我喝水','cross','same')
        with self.assertRaises(CommandError):self.engine.submit('打开卧室灯','cross','same')
        self.engine.submit('把手机从客厅送到卧室','reverse','same')
        with self.assertRaises(CommandError):self.engine.submit('我需要如厕帮助','reverse','same')
        self.assertEqual(self.engine.assistive.snapshot()['assistance'],[])

    def test_bad_request_identifier_rejected_before_mutation(self):
        with self.assertRaises(CommandError):self.engine.submit('我需要如厕帮助',{},'s')
        self.assertEqual(self.engine.assistive.snapshot()['assistance'],[])

    def test_unsupported_negative_request_creates_nothing(self):
        with self.assertRaises(CommandError):self.engine.submit('不要帮我洗澡')
        self.assertEqual(self.engine.assistive.snapshot()['assistance'],[])

    def test_http_catalog_action_and_cors(self):
        server=create_server(self.engine,port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        try:
            with urlopen(base+'/api/assistive/catalog') as response:
                self.assertGreaterEqual(len(json.load(response)['categories']),50)
            req=Request(base+'/api/assistive/action',json.dumps({'op':'assistance.create','category':'bathing'}).encode(),{'Content-Type':'application/json'})
            with urlopen(req) as response:self.assertTrue(json.load(response)['ok'])
            with urlopen(base+'/api/assistive') as response:
                result=json.load(response);self.assertEqual(result['delivery']['external_messages_sent'],0)
                self.assertEqual(result['assistance'][0]['delivery_status'],'not_sent')
            req=Request(base+'/api/assistive/action',b'{}',{'Content-Type':'application/json','Origin':'https://example.org'})
            with self.assertRaises(HTTPError) as caught:urlopen(req)
            self.assertEqual(caught.exception.code,403)
        finally:server.shutdown();server.server_close();thread.join()
