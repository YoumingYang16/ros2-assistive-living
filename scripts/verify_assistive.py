"""Execute every catalog example in an isolated mock engine; never use ROS."""
from pathlib import Path
import argparse
import json
import sys
import time
from datetime import datetime, timezone
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from robot_voice_patrol.config import load_config
from robot_voice_patrol.engine import MissionEngine
from robot_voice_patrol.mock_adapter import MockAdapter
from robot_voice_patrol.natural_language import DialoguePlanner

def run():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',default='docs/ASSISTIVE_COVERAGE.json')
    args=parser.parse_args()
    config=load_config(Path(__file__).resolve().parents[1]/'config/home.json')
    config['mock'].update(travel_seconds=.001,inspection_seconds=.001)
    engine=MissionEngine(config,MockAdapter(config,fixture_skills=True),start_scheduler=False,
                         planner=DialoguePlanner(config,provider=False))
    results=[]
    try:
        for category in engine.assistive.catalog()['categories']:
            row={**category,'passed':False}
            try:
                preview=engine.preview(category['example'],'coverage')
                value=engine.submit(category['example'],request_id=category['id'],session_id='coverage')
                if value.get('needs_confirmation'):
                    value=engine.submit('确认执行',session_id='coverage')
                deadline=time.monotonic()+3
                while engine.snapshot()['state'] in {'running','pausing','cancelling'}:
                    if time.monotonic()>deadline:raise AssertionError('execution timeout')
                    time.sleep(.005)
                assert value.get('ok',True),value
                if category['mode']=='hardware_interface' and category['id']!='stop':
                    assert engine.snapshot()['state']=='succeeded',engine.snapshot()['mission']
                    row['simulated']=True
                row.update(passed=True,message=value.get('message'),dispatch_kind=preview.get('kind'))
            except Exception as error:
                row['error']=str(error)
            results.append(row)
        report={'version':'7.0.0','generated_at':datetime.now(timezone.utc).isoformat(),
                'scope':'curated catalog examples; isolated software mock; no physical care or external communications',
                'total':len(results),'passed':sum(r['passed'] for r in results),'results':results,
                'external_messages_sent':engine.assistive.snapshot()['delivery']['external_messages_sent']}
    finally:engine.close()
    Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='results'},ensure_ascii=False))
    return 0 if report['passed']==report['total'] else 1
if __name__=='__main__':raise SystemExit(run())
