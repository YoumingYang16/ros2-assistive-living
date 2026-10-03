"""HTTP contracts for complete living history, including malformed URL inputs."""
import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from robot_voice_patrol.config import load_config
from robot_voice_patrol.engine import MissionEngine
from robot_voice_patrol.mock_adapter import MockAdapter
from robot_voice_patrol.server import create_server


class AssistiveHistoryHTTPTests(unittest.TestCase):
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

    def request(self, path, headers=None):
        request = Request(self.url + path, headers=headers or {})
        try:
            response = urlopen(request, timeout=2)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    def act(self, op, **fields):
        return self.engine.assistive.action({"op": op, **fields})["assistive"]["record"]

    def test_empty_meta_parameters_are_rejected_as_well_as_unknown_ones(self):
        for suffix in ["?kind=", "?query=", "?unknown", "?offset=1", "?a=&a=", "?&"]:
            with self.subTest(suffix=suffix):
                status, body = self.request("/api/assistive/history/meta" + suffix)
                self.assertEqual(status, 400)
                self.assertFalse(body["ok"])
        status, data = self.request("/api/assistive/history/meta")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["kinds"]), 9)

    def test_list_rejects_duplicates_unknown_fields_and_nondecimal_page_sizes(self):
        suffixes = ["kind=", "kind=need&kind=reminder", "offset=", "limit=", "unused=1",
                    "query=a&query=b", "since=", "limit=1_0", "limit=%2B10", "limit=%2010",
                    "limit=1.0", "limit=true", "offset=-1", "limit=101", "offset=1000001"]
        for suffix in suffixes:
            with self.subTest(suffix=suffix):
                status, body = self.request("/api/assistive/history?" + suffix)
                self.assertEqual(status, 400)
                self.assertFalse(body["ok"])
        self.assertEqual(self.request("/api/assistive/history?query=")[0], 200)

    def test_pagination_search_detail_and_removed_history_work_over_http(self):
        records = [self.act("need.add", title=f"用品{i}") for i in range(3)]
        self.act("need.remove", id=records[0]["id"])
        status, first = self.request("/api/assistive/history?kind=need&limit=2")
        status2, second = self.request("/api/assistive/history?kind=need&limit=2&offset=2")
        self.assertEqual((status, status2), (200, 200))
        self.assertEqual(first["total"], 3)
        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])
        self.assertEqual({item["id"] for item in first["records"] + second["records"]}, {r["id"] for r in records})
        status, found = self.request("/api/assistive/history?" + urlencode({"query": "用品0", "state": "removed"}))
        self.assertEqual((status, found["total"]), (200, 1))
        status, detail = self.request("/api/assistive/history/" + records[0]["id"] + "?event_limit=1")
        self.assertEqual((status, detail["record"]["state"], detail["event_total"]), (200, "removed", 2))
        self.assertTrue(detail["events_has_more"])

    def test_detail_parameters_are_strict_and_routes_cannot_access_parent_paths(self):
        record = self.act("need.add", title="记录")
        path = "/api/assistive/history/" + record["id"]
        for suffix in ["?event_limit=", "?event_limit=1&event_limit=2", "?event_offset=", "?query=x", "?event_limit=1_0", "?event_limit=0"]:
            with self.subTest(suffix=suffix):
                self.assertEqual(self.request(path + suffix)[0], 400)
        self.assertEqual(self.request("/api/assistive/history/../config")[0], 404)
        self.assertEqual(self.request("/api/assistive/history/%27%20OR%201=1")[0], 404)

    def test_history_retains_existing_origin_and_host_guards(self):
        for headers in [{"Origin": "https://other.example"}, {"Host": "other.example:8773"}, {"Sec-Fetch-Site": "cross-site"}]:
            with self.subTest(headers=headers):
                self.assertEqual(self.request("/api/assistive/history", headers)[0], 403)


if __name__ == "__main__":
    unittest.main()
