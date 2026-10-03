import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import unittest
from unittest.mock import patch

from robot_voice_patrol.config import load_config
from robot_voice_patrol.contracts import CommandError
from robot_voice_patrol.model_provider import ModelProvider, ProviderError, provider_from_env, validate_model_output
from robot_voice_patrol.natural_language import DialoguePlanner
from robot_voice_patrol.engine import MissionEngine
from robot_voice_patrol.mock_adapter import MockAdapter


def task():
    return {"kind": "task", "message": "前往会议室", "options": [], "plan": {"summary": "前往会议室", "steps": [
        {"kind": "navigate", "target": "meeting_room", "seconds": 0, "object_name": "", "timeout": 30,
         "max_retries": 1, "step_id": "s001", "condition": None}]}}


def openai_response(data=None):
    return {"status": "completed", "output": [{"type": "message", "status": "completed", "content": [
        {"type": "output_text", "text": json.dumps(data or task(), ensure_ascii=False)}]}]}


class ProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.server.last_body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.server.authorization = self.headers.get("Authorization")
                if self.server.delay:
                    time.sleep(self.server.delay)
                self.send_response(self.server.response_code)
                if self.server.response_code == 302:
                    self.send_header("Location", "http://192.0.2.1/private")
                self.end_headers()
                payload = self.server.response
                try:
                    self.wfile.write(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def setUp(self):
        self.config = load_config()
        self.server.response_code = 200
        self.server.response = openai_response()
        self.server.delay = 0
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/api/chat"
        self.provider = ModelProvider("openai", "explicit-test-model", api_key="TEST_ONLY_NOT_A_REAL_KEY",
                                      endpoint=self.endpoint, allow_test_endpoint=True, timeout=1)

    def test_openai_request_uses_responses_strict_schema_and_no_storage(self):
        result = self.provider.generate("请导航至会议室", self.config)
        self.assertEqual("task", result["kind"])
        sent = self.server.last_body
        self.assertEqual("explicit-test-model", sent["model"])
        self.assertEqual("json_schema", sent["text"]["format"]["type"])
        self.assertTrue(sent["text"]["format"]["strict"])
        self.assertFalse(sent["store"])
        self.assertEqual("Bearer TEST_ONLY_NOT_A_REAL_KEY", self.server.authorization)
        self.assertNotIn("TEST_ONLY", str(self.provider.summary()))

    def test_ollama_real_http_shape_with_mock_endpoint(self):
        self.server.response = {"done": True, "done_reason": "stop", "message": {"role": "assistant", "content": json.dumps(task())}}
        provider = ModelProvider("ollama", "explicit-local:test", endpoint=self.endpoint)
        self.assertEqual("task", provider.generate("去会议室", self.config)["kind"])
        self.assertFalse(self.server.last_body["stream"])
        self.assertIn("properties", self.server.last_body["format"])
        self.assertIsNone(self.server.authorization)

    def test_refusal_incomplete_tool_calls_and_missing_outputs_rejected(self):
        for response in ({"status": "incomplete", "output": []}, {"status": "completed", "output": []},
                         {"status": "completed", "output": [{"type": "function_call", "name": "navigate"}]},
                         {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]}):
            with self.subTest(response=response):
                self.server.response = response
                with self.assertRaises(ProviderError):
                    self.provider.generate("去会议室", self.config)

    def test_ollama_incomplete_output_rejected(self):
        self.server.response = {"done": True, "done_reason": "length", "message": {"content": json.dumps(task())}}
        with self.assertRaises(ProviderError):
            ModelProvider("ollama", "test", endpoint=self.endpoint).generate("去会议室", self.config)

    def test_http_error_never_echoes_sensitive_body(self):
        self.server.response_code = 401
        self.server.response = {"error": "SECRET_DATA_FROM_SERVER"}
        with self.assertRaises(ProviderError) as raised:
            self.provider.generate("去会议室", self.config)
        self.assertIn("401", str(raised.exception))
        self.assertNotIn("SECRET", str(raised.exception))

    def test_redirect_is_not_followed(self):
        self.server.response_code = 302
        with self.assertRaises(ProviderError):
            self.provider.generate("去会议室", self.config)

    def test_timeout_produces_no_plan(self):
        self.server.delay = .25
        self.provider.timeout = .1
        with self.assertRaises(ProviderError):
            self.provider.generate("去会议室", self.config)

    def test_response_size_invalid_json_and_duplicate_fields(self):
        for payload in (b"x" * 1_048_577, b"not-json", b'{"status":"completed","status":"incomplete"}'):
            with self.subTest(size=len(payload)):
                self.server.response = payload
                with self.assertRaises(ProviderError):
                    self.provider.generate("去会议室", self.config)

    def test_schema_unknown_skills_targets_future_conditions_and_numeric_types_rejected(self):
        mutations = [("kind", "shell"), ("target", "moon"), ("seconds", True), ("max_retries", True),
                     ("timeout", float("inf")), ("condition", {"step_id": "s999", "outcome": "found"})]
        for key, value in mutations:
            data = task()
            data["plan"]["steps"][0][key] = value
            with self.subTest(key=key), self.assertRaises(CommandError):
                validate_model_output(data, self.config)

    def test_observation_without_prior_navigation_rejected(self):
        data = task()
        data["plan"]["steps"][0].update(kind="inspect", object_name="水杯")
        with self.assertRaises(ProviderError):
            validate_model_output(data, self.config)

    def test_task_cannot_hide_in_answer_or_use_extra_fields(self):
        data = task()
        data["kind"] = "answer"
        with self.assertRaises(ProviderError):
            validate_model_output(data, self.config)
        data = task()
        data["plan"]["steps"][0]["shell"] = "bad"
        with self.assertRaises(ProviderError):
            validate_model_output(data, self.config)

    def test_endpoint_policy_and_missing_model(self):
        cases = [("openai", "test", {"endpoint": "https://evil.example/v1/responses", "api_key": "fake"}),
                 ("ollama", "test", {"endpoint": "http://192.0.2.1:11434/api/chat"}),
                 ("ollama", "test", {"endpoint": "http://name:password@localhost:11434/api/chat"}),
                 ("ollama", "", {}), ("ollama", "test", {"timeout": float("nan")})]
        for provider, model, kwargs in cases:
            with self.subTest(provider=provider, model=model), self.assertRaises(ProviderError):
                ModelProvider(provider, model, **kwargs)

    def test_environment_is_opt_in_and_requires_explicit_model(self):
        self.assertIsNone(provider_from_env({}))
        self.assertIsNone(provider_from_env({"OPENAI_API_KEY": "fake"}))
        with self.assertRaises(ProviderError):
            provider_from_env({"VOICE_PATROL_MODEL_PROVIDER": "openai", "OPENAI_API_KEY": "fake"})
        provider = provider_from_env({"VOICE_PATROL_MODEL_PROVIDER": "ollama", "VOICE_PATROL_MODEL": "chosen:tag"})
        self.assertEqual("chosen:tag", provider.model)

    def test_model_plan_becomes_reviewable_draft_not_implicit_execution(self):
        class FakeProvider:
            def generate(self, *args):
                return task()
        planner = DialoguePlanner(self.config, FakeProvider())
        proposed = planner.interpret("麻烦安排一次会议室访问")
        self.assertTrue(proposed.plan.metadata["requires_confirmation"])
        confirmed = planner.interpret("确认执行", proposed.context)
        self.assertFalse(confirmed.plan.metadata["requires_confirmation"])

    def test_semantic_context_does_not_send_logs_or_observation_database(self):
        self.provider.generate("去会议室", self.config, {"last_target": "home", "database": "DO_NOT_SEND", "events": ["PRIVATE"]})
        payload = self.server.last_body["input"]
        self.assertNotIn("DO_NOT_SEND", payload)
        self.assertNotIn("PRIVATE", payload)


class EngineModelIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict("os.environ", {"VOICE_PATROL_MODEL_PROVIDER": "none"})
        self.environment.start()
        self.config = load_config()
        self.config["mock"].update(travel_seconds=.005, inspection_seconds=.005)

    def tearDown(self):
        self.environment.stop()

    def test_model_confirmation_then_execution_and_idempotent_confirmation(self):
        class FakeProvider:
            def generate(self, *args):
                return task()
        planner = DialoguePlanner(self.config, FakeProvider())
        engine = MissionEngine(self.config, MockAdapter(self.config), planner=planner)
        try:
            draft = engine.submit("安排一次会议室访问", request_id="proposal")
            self.assertTrue(draft["needs_confirmation"])
            self.assertEqual("idle", engine.snapshot()["state"])
            self.assertEqual(0, engine.store.metrics()["missions_total"])
            accepted = engine.submit("确认执行", request_id="confirmation")
            deadline = time.monotonic() + 2
            while engine.snapshot()["state"] in {"running", "pausing", "paused", "cancelling"} and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertEqual("succeeded", engine.snapshot()["state"])
            self.assertEqual("meeting_room", engine.snapshot()["robot"]["location"])
            self.assertNotIn("pending_plan", engine.store.session("default"))
            duplicate = engine.submit("确认执行", request_id="confirmation")
            self.assertTrue(duplicate["duplicate"])
            self.assertEqual(accepted["mission_id"], duplicate["mission_id"])
            self.assertEqual(1, engine.store.metrics()["missions_total"])
        finally:
            engine.close()

    def test_stop_alias_bypasses_blocked_model_and_discards_late_draft(self):
        started, release = threading.Event(), threading.Event()
        errors, responses = [], []
        class DelayedProvider:
            def generate(self, *args):
                started.set()
                release.wait(2)
                return task()
        engine = MissionEngine(self.config, MockAdapter(self.config),
                               planner=DialoguePlanner(self.config, DelayedProvider()))
        def submit():
            try:
                responses.append(engine.submit("安排会议室访问", request_id="slow-proposal"))
            except CommandError as exc:
                errors.append(exc)
        worker = threading.Thread(target=submit)
        worker.start()
        try:
            self.assertTrue(started.wait(1))
            before = time.monotonic()
            engine.submit("请你立即停止", request_id="urgent-stop")
            self.assertLess(time.monotonic() - before, .5)
            release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertTrue(errors)
            self.assertFalse(responses)
            self.assertNotIn("pending_plan", engine.store.session("default"))
            self.assertEqual(0, engine.store.metrics()["missions_total"])
        finally:
            release.set()
            worker.join(2)
            engine.close()

    def test_configuration_change_invalidates_previously_previewed_plan(self):
        engine = MissionEngine(self.config, MockAdapter(self.config), planner=DialoguePlanner(self.config, False))
        try:
            engine.preview("去会议室")
            updated = copy.deepcopy(self.config)
            updated["locations"]["meeting_room"]["x"] += 10
            engine.update_config(updated)
            with self.assertRaises(CommandError):
                engine.submit("确认执行", request_id="old-draft-confirmation")
            self.assertEqual(0, engine.store.metrics()["missions_total"])
            self.assertEqual("idle", engine.snapshot()["state"])
        finally:
            engine.close()


if __name__ == "__main__":
    unittest.main()
