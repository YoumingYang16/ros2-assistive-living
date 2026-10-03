import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from robot_voice_patrol.config import load_config
from robot_voice_patrol.engine import MissionEngine
from robot_voice_patrol.mock_adapter import MockAdapter
from robot_voice_patrol.server import create_server


class ServerTests(unittest.TestCase):
    def setUp(self):
        config = load_config()
        self.engine = MissionEngine(config, MockAdapter(config))
        self.server = create_server(self.engine, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(1)
        self.engine.close()

    def request(self, path, payload=None, extra_headers=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(self.url + path, data=data, headers={"Content-Type": "application/json", **(extra_headers or {})})
        try:
            response = urlopen(request, timeout=2)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    def test_read_state_and_preview_without_execution(self):
        status, state = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["mode"], "mock")
        status, preview = self.request("/api/plan", {"text": "巡逻两圈"})
        self.assertEqual(status, 200)
        self.assertGreater(len(preview["plan"]["steps"]), 3)
        self.assertEqual(self.engine.snapshot()["state"], "idle")

    def test_submit_then_stop(self):
        status, result = self.request("/api/command", {"text": "等待两秒"})
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        status, result = self.request("/api/control", {"action": "stop"})
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])

    def test_malformed_or_unknown_commands_rejected(self):
        for payload in (["bad"], {"text": None}, {"text": "去月球"}):
            status, result = self.request("/api/command", payload)
            self.assertEqual(status, 400)
            self.assertFalse(result["ok"])

    def test_cross_origin_and_rebinding_rejected(self):
        for headers in ({"Origin": "https://evil.example"}, {"Host": "evil.example:8765"}):
            status, _ = self.request("/api/command", {"text": "去会议室"}, headers)
            self.assertEqual(status, 403)
        self.assertEqual(self.engine.snapshot()["state"], "idle")

    def test_static_path_cannot_read_configuration_or_parent_file(self):
        for path in ("/config/default.json", "/../setup.py", "/api/missing"):
            status, _ = self.request(path)
            self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
