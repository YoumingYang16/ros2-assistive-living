"""Real local HTTP routes; no ROS, audio, contacts or physical side effects."""
import json
from datetime import datetime, timezone
from pathlib import Path
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from robot_voice_patrol.config import load_config
from robot_voice_patrol.engine import MissionEngine
from robot_voice_patrol.mock_adapter import MockAdapter
from robot_voice_patrol.server import create_server


class DailyManagementHTTPTests(unittest.TestCase):
    def setUp(self):
        config=load_config(Path(__file__).parents[1]/'config/home.json')
        self.engine=MissionEngine(config,MockAdapter(config),start_scheduler=False)
        fixed_now=datetime.now(timezone.utc)
        self.engine.assistive._clock=lambda:fixed_now
        self.server=create_server(self.engine,port=0)
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.01},daemon=True)
        self.thread.start()
        self.base='http://127.0.0.1:'+str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown();self.thread.join(2);self.server.server_close();self.engine.close()

    def get(self,path):
        with urlopen(self.base+path,timeout=3) as response:return json.load(response)

    def action(self,body):
        request=Request(self.base+'/api/assistive/action',json.dumps(body).encode(),{'Content-Type':'application/json'})
        with urlopen(request,timeout=3) as response:return json.load(response)

    def test_history_routes_page_all_records_and_read_only_detail(self):
        for i in range(31):self.action({'op':'need.add','title':'历史条目'+str(i)})
        before=self.engine.assistive.snapshot()
        metadata=self.get('/api/assistive/history/meta')
        self.assertIn('wellbeing',metadata['kinds'])
        first=self.get('/api/assistive/history?kind=need&query=%E5%8E%86%E5%8F%B2&limit=20')
        second=self.get('/api/assistive/history?kind=need&limit=20&offset=20')
        self.assertEqual(first['total'],31);self.assertTrue(first['has_more'])
        self.assertEqual(len(first['records'])+len(second['records']),31)
        self.assertFalse(second['has_more'])
        self.assertFalse({r['id'] for r in first['records']} & {r['id'] for r in second['records']})
        detail=self.get('/api/assistive/history/'+first['records'][0]['id']+'?event_limit=1')
        self.assertEqual(detail['event_total'],1)
        self.assertEqual(detail['record']['id'],first['records'][0]['id'])
        self.assertEqual(before,self.engine.assistive.snapshot())

    def test_invalid_or_duplicate_filters_return_400_without_modification(self):
        before=self.engine.assistive.snapshot()
        for query in ['limit=0','limit=101','offset=-1','kind=invalid','kind=need&state=due',
                      'limit=1&limit=2','limit=','unknown=','since=2026-10-03','until=not-a-date']:
            with self.subTest(query=query), self.assertRaises(HTTPError) as caught:
                self.get('/api/assistive/history?'+query)
            self.assertEqual(caught.exception.code,400)
        self.assertEqual(before,self.engine.assistive.snapshot())

    def test_calendar_update_revision_conflict_is_not_a_silent_overwrite(self):
        result=self.action({'op':'reminder.create','title':'每周安排','calendar':{'weekdays':[1,3,5],'local_time':'08:00','timezone':'Asia/Hong_Kong'}})
        record=result['assistive']['record'];revision=record['revision']
        self.action({'op':'reminder.update','id':record['id'],'title':'已修改','expected_revision':revision})
        with self.assertRaises(HTTPError) as caught:
            self.action({'op':'reminder.update','id':record['id'],'title':'过期覆盖','expected_revision':revision})
        self.assertEqual(caught.exception.code,400)
        detail=self.get('/api/assistive/history/'+record['id'])
        self.assertEqual(detail['record']['title'],'已修改')
        self.assertEqual(detail['record']['calendar']['weekdays'],[1,3,5])

    def test_new_asset_is_served_and_history_rejects_cross_origin(self):
        with urlopen(self.base+'/daily-management.js') as response:self.assertIn(b'reminder.update',response.read())
        request=Request(self.base+'/api/assistive/history',headers={'Origin':'https://example.org'})
        with self.assertRaises(HTTPError) as caught:urlopen(request,timeout=3)
        self.assertEqual(caught.exception.code,403)


if __name__=='__main__':unittest.main()
