import copy
import io
import json
import unittest
import wave
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from tests import test_server as server_test_support
from tests.test_persistence import finish


class HttpV2Tests(unittest.TestCase):
    setUp = server_test_support.ServerTests.setUp
    tearDown = server_test_support.ServerTests.tearDown

    def request(self, path, payload=None, *, method=None, content_type="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode() if payload is not None else None
        req = Request(self.url + path, data=data, method=method, headers={"Content-Type": content_type})
        try:
            response = urlopen(req, timeout=3)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read()), response.headers

    def test_receipt_history_detail_and_download_are_consistent(self):
        payload = {"text": "等待0.01秒", "request_id": "http-receipt", "session_id": "http"}
        status, receipt, _ = self.request("/api/command", payload)
        self.assertEqual(status, 200)
        finish(self.engine)
        _, duplicate, _ = self.request("/api/command", payload)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["mission_id"], receipt["mission_id"])
        _, history, _ = self.request("/api/history?limit=1&offset=0")
        self.assertEqual(history["total"], 1)
        identifier = receipt["mission_id"]
        _, detail, _ = self.request(f"/api/missions/{identifier}")
        _, exported, headers = self.request(f"/api/missions/{identifier}/export")
        self.assertEqual(exported, detail)
        self.assertEqual(detail["mission"]["state"], "succeeded")
        self.assertTrue(detail["events"])
        self.assertIn("attachment", headers["Content-Disposition"])
        status, rejected, _ = self.request("/api/command", {**payload, "text": "去会议室"})
        self.assertEqual(status, 400)
        self.assertFalse(rejected["ok"])

    def test_clarification_and_preview_do_not_execute(self):
        _, answer, _ = self.request("/api/command", {"text": "找水杯", "session_id": "dialogue"})
        self.assertTrue(answer["needs_clarification"])
        status, preview, _ = self.request("/api/plan", {"text": "会议室", "session_id": "dialogue"})
        self.assertEqual(status, 200)
        self.assertEqual(preview["plan"]["steps"][0]["target"], "meeting_room")
        self.assertEqual(self.engine.metrics()["missions_total"], 0)
        status, _, _ = self.request("/api/command", {"text": "确认执行", "session_id": "other"})
        self.assertEqual(status, 400)

    def test_configuration_validation_and_active_task_exclusion(self):
        _, payload, _ = self.request("/api/config")
        config = payload["config"]
        config["locations"]["home"]["x"] = 4
        status, _, _ = self.request("/api/config", {"config": config}, method="PUT")
        self.assertEqual(status, 200)
        self.assertEqual(self.engine.config["locations"]["home"]["x"], 4)
        bad = copy.deepcopy(config)
        bad["locations"]["home"]["x"] = "bad"
        status, _, _ = self.request("/api/config", bad, method="PUT")
        self.assertEqual(status, 400)
        self.request("/api/command", {"text": "等待10秒"})
        status, _, _ = self.request("/api/config", config, method="PUT")
        self.assertEqual(status, 400)
        _, health, _ = self.request("/api/health")
        self.assertTrue(health["live"])
        self.assertFalse(health["ready"])

    def test_audio_errors_never_create_missions(self):
        stream = io.BytesIO()
        with wave.open(stream, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\0\0" * 1600)
        with patch.dict("os.environ", {"VOICE_PATROL_VOSK_MODEL": ""}):
            status, payload, _ = self.request("/api/voice/transcribe", stream.getvalue(), content_type="audio/wav")
        self.assertEqual(status, 503)
        self.assertFalse(payload["ok"])
        status, _, _ = self.request("/api/voice/transcribe", b"bad", content_type="audio/wav")
        self.assertEqual(status, 400)
        status, _, _ = self.request("/api/voice/transcribe", b"bad", content_type="audio/mpeg")
        self.assertEqual(status, 415)
        self.assertEqual(self.engine.metrics()["missions_total"], 0)

    def test_recovery_acknowledgement_does_not_dispatch(self):
        self.engine.store.save_mission({"id": "crashed123", "state": "running", "started_at": "2026-10-03T00:00:00Z",
            "mode": "mock", "steps": [], "step_states": [], "results": []})
        self.engine.store.recover_interrupted()
        _, state, _ = self.request("/api/state")
        self.assertEqual(len(state["recoveries"]), 1)
        status, ack, _ = self.request("/api/recovery", {"mission_id": "crashed123", "action": "dismiss"})
        self.assertEqual(status, 200)
        self.assertFalse(ack["mission"]["recovery_required"])
        self.assertEqual(ack["state"]["state"], "idle")

    def test_invalid_http_methods_and_pagination(self):
        status, _, _ = self.request("/api/command", {"text": "去会议室"}, method="PUT")
        self.assertEqual(status, 405)
        status, _, _ = self.request("/api/history?limit=bad")
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
