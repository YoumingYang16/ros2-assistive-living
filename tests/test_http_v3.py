import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from robot_voice_patrol.config import load_config
from robot_voice_patrol.engine import MissionEngine
from robot_voice_patrol.mock_adapter import MockAdapter
from robot_voice_patrol.server import create_server
from robot_voice_patrol.voice_sessions import VoiceSessionService
from tests.test_queue_data_v3 import finish


class FixtureRecognizer:
    def accept(self, data):
        return {"is_final": True, "text": "去会议室"}
    def finish(self):
        return {"is_final": True, "text": ""}
    def close(self):
        pass


class FixtureASR:
    name = "test-fixture"
    simulated = True
    local = True
    def available(self):
        return True
    def create_recognizer(self, rate):
        return FixtureRecognizer()


class HttpV3Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        config = load_config()
        config["mock"].update(travel_seconds=.01, inspection_seconds=.01)
        self.engine = MissionEngine(config, MockAdapter(config), db_path=Path(self.temp.name)/"data.sqlite3", start_scheduler=False)
        self.server = create_server(self.engine, "127.0.0.1", 0, voice_service=VoiceSessionService(asr_provider=FixtureASR()))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(1)
        self.engine.close()
        self.temp.cleanup()

    def request(self, path, body=None, method=None, headers=None, binary=False):
        data = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else None
        request = Request(self.url+path, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as exc:
            response = exc
        with response:
            data = response.read()
            return response.status, data if binary else json.loads(data)

    def workflow(self):
        return {"version": 1, "name": "repeat", "steps": [{"type": "repeat", "id": "repeat", "count": 2,
            "body": [{"type": "step", "id": "report", "kind": "report"}]}]}

    def test_workflow_preview_submit_and_idempotency(self):
        status, preview = self.request("/api/workflow/preview", {"workflow": self.workflow()})
        self.assertEqual(status, 200)
        self.assertEqual(len(preview["plan"]["steps"]), 2)
        self.assertEqual(self.engine.metrics()["missions_total"], 0)
        payload = {"workflow": self.workflow(), "request_id": "once"}
        status, response = self.request("/api/workflow/submit", payload)
        self.assertEqual(status, 200)
        finish(self.engine)
        _, duplicate = self.request("/api/workflow/submit", payload)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(response["mission_id"], duplicate["mission_id"])

    def test_queue_template_routes_and_explicit_cancel(self):
        _, saved = self.request("/api/templates", {"name": "report", "workflow": self.workflow()})
        identifier = saved["template"]["id"]
        status, queued = self.request(f"/api/templates/{identifier}/run", {"request_id": "queued-once"})
        self.assertEqual(status, 200)
        job_id = queued["job"]["id"]
        _, queue = self.request("/api/queue")
        self.assertEqual(queue["pending"], 1)
        self.request("/api/queue/control", {"action": "pause"})
        self.request(f"/api/queue/{job_id}/cancel", {})
        _, queue = self.request("/api/queue")
        self.assertEqual(queue["jobs"][0]["status"], "cancelled")
        self.request(f"/api/templates/{identifier}", method="DELETE")
        _, templates = self.request("/api/templates")
        self.assertEqual(templates["templates"], [])

    def test_voice_stream_sequence_correction_finish_never_executes(self):
        _, created = self.request("/api/voice/sessions", {"sample_rate": 16000, "channels": 1})
        sid = created["session_id"]
        status, partial = self.request(f"/api/voice/sessions/{sid}/chunk", b"\0\0"*1600,
            headers={"Content-Type": "audio/pcm", "X-Audio-Sequence": "0"})
        self.assertEqual(status, 200)
        self.assertEqual(partial["text"], "去会议室")
        status, _ = self.request(f"/api/voice/sessions/{sid}/chunk", b"\0\0", headers={"Content-Type": "audio/pcm", "X-Audio-Sequence": "0"})
        self.assertEqual(status, 409)
        _, corrected = self.request(f"/api/voice/sessions/{sid}/correct", {"segment_id": "seg001", "text": "去前台"})
        self.assertEqual(corrected["corrections"][0]["before"], "去会议室")
        _, final = self.request(f"/api/voice/sessions/{sid}/finish", {})
        self.assertEqual(final["text"], "去前台")
        self.assertEqual(self.engine.metrics()["missions_total"], 0)
        self.request(f"/api/voice/sessions/{sid}", method="DELETE")
        status, _ = self.request(f"/api/voice/sessions/{sid}/finish", {})
        self.assertEqual(status, 409)

    def test_backup_download_integrity_staged_restore_and_policy(self):
        status, result = self.request("/api/data/backup", {})
        self.assertEqual(status, 200)
        identifier = result["backup"]["id"]
        status, data = self.request(f"/api/data/backups/{identifier}/download", binary=True)
        self.assertEqual(status, 200)
        self.assertTrue(data.startswith(b"SQLite format 3"))
        status, manifest = self.request(f"/api/data/backups/{identifier}/manifest")
        self.assertEqual(status, 200)
        self.assertEqual(manifest, result["backup"])
        _, verified = self.request("/api/data/verify", {"backup_id": identifier})
        self.assertEqual(verified["integrity"], "ok")
        status, _ = self.request("/api/data/restore", {"backup_id": identifier})
        self.assertEqual(status, 400)
        _, staged = self.request("/api/data/restore", {"backup_id": identifier, "confirmed": True})
        self.assertTrue(staged["staged_only"])
        self.request("/api/data/policy", {"retention_days": 60, "auto_archive": False}, method="PUT")
        _, policy = self.request("/api/data/policy")
        self.assertEqual(policy["retention_days"], 60)

    def test_lifecycle_skills_and_memory(self):
        _, skills = self.request("/api/skills")
        self.assertEqual(len(skills["skills"]), 14)
        self.request("/api/lifecycle", {"action": "deactivate"})
        status, _ = self.request("/api/command", {"text": "去会议室"})
        self.assertEqual(status, 400)
        self.request("/api/lifecycle", {"action": "activate"})
        self.request("/api/command", {"text": "去会议室找水杯"})
        finish(self.engine)
        _, memory = self.request("/api/memory?outcome=found")
        self.assertEqual(memory["observations"][0]["outcome"], "found")
        self.assertTrue(memory["observations"][0]["historical"])

    def test_bad_input_and_cross_site_do_not_mutate(self):
        for path, body in [("/api/queue", {"text": "去会议室", "priority": True}),
                           ("/api/queue/control", {"action": []}), ("/api/lifecycle", {"action": []}),
                           ("/api/recovery/preview", {"mission_id": []}),
                           ("/api/workflow/preview", {"workflow": {"name": "bad"}})]:
            with self.subTest(path=path):
                status, result = self.request(path, body)
                self.assertEqual(status, 400, result)
        status, _ = self.request("/api/queue", {"text": "去会议室"}, headers={"Origin": "https://outside.invalid"})
        self.assertEqual(status, 403)
        self.assertEqual(self.engine.metrics()["missions_total"], 0)


if __name__ == "__main__":
    unittest.main()
