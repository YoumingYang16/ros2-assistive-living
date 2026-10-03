import copy
import threading
import time
import unittest
import contextlib
import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from robot_voice_patrol.config import load_config
from robot_voice_patrol.contracts import CommandError, Plan, Step
from robot_voice_patrol.engine import MissionEngine
from robot_voice_patrol.mock_adapter import MockAdapter
from robot_voice_patrol.preflight import inspect_plan
from robot_voice_patrol.scenarios import ScenarioService, ScenarioBusy
from tests import test_http_v3 as http_fixtures


def search():
    return {"version": 1, "name": "三地查找", "steps": [{"type": "search", "id": "find",
        "locations": ["reception", "storage", "meeting_room"], "object_name": "水杯",
        "continue_on": ["not_found"], "return_home": "found"}]}


class ScenariosTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.adapter = MockAdapter(self.config)
        self.service = ScenarioService()

    def tearDown(self):
        self.adapter.close()

    def test_six_scenarios_different_evidence_and_same_source(self):
        original_config, workflow = copy.deepcopy(self.config), search()
        original_workflow = copy.deepcopy(workflow)
        scenarios = [item["id"] for item in self.service.catalog()["scenarios"]]
        report = self.service.run({"workflow": workflow, "scenario_ids": scenarios}, self.config)
        rows = {run["scenario_id"]: run for run in report["runs"]}
        self.assertEqual(rows["configured"]["report"]["goal_outcome"], "achieved")
        self.assertEqual(rows["empty"]["report"]["goal_outcome"], "not_achieved")
        self.assertEqual(rows["inconclusive"]["report"]["goal_outcome"], "unknown")
        self.assertEqual(rows["sensor_failure"]["state"], "failed")
        self.assertEqual(rows["navigation_timeout"]["results"][0]["outcome"], "timed_out")
        self.assertGreater(rows["transient_navigation"]["retries"], 0)
        self.assertEqual(rows["transient_navigation"]["state"], "succeeded")
        self.assertEqual(self.config, original_config)
        self.assertEqual(workflow, original_workflow)
        self.assertTrue(report["isolated"])
        self.assertFalse(report["live_history_modified"])

    def test_unknown_does_not_enter_not_found_branch(self):
        run = self.service.run({"workflow": search(), "scenario_ids": ["inconclusive"]}, self.config)["runs"][0]
        self.assertEqual(sum(r["kind"] == "inspect" and r["status"] == "succeeded" for r in run["results"]), 1)
        self.assertGreater(run["report"]["skipped_steps"], 0)

    def test_timeout_can_enter_explicit_fallback(self):
        plan = Plan("fallback", [Step("navigate", "meeting_room", step_id="go", on_failure="continue"),
            Step("report", step_id="report", condition={"step_id": "go", "outcome": "timed_out"})], "fallback")
        run = self.service.run({"plan": plan.to_dict(), "scenario_ids": ["navigation_timeout"]}, self.config)["runs"][0]
        self.assertEqual(run["state"], "succeeded")
        self.assertEqual(run["report"]["failed_steps"], 1)
        self.assertEqual(run["results"][1]["status"], "succeeded")

    def test_no_retries_means_transient_failure_stays_failed(self):
        plan = Plan("go", [Step("navigate", "meeting_room", max_retries=0)], "go")
        run = self.service.run({"plan": plan.to_dict(), "scenario_ids": ["transient_navigation"]}, self.config)["runs"][0]
        self.assertEqual(run["state"], "failed")
        self.assertEqual(run["retries"], 0)

    def test_long_wait_accelerated_without_changing_original(self):
        plan = Plan("wait", [Step("wait", seconds=3000, timeout=3001)], "wait")
        run = self.service.run({"plan": plan.to_dict()}, self.config)["runs"][0]
        self.assertLess(run["elapsed_seconds"], 1)
        self.assertEqual(run["results"][0]["evidence"]["requested_seconds"], 3000)

    def test_wait_state_bounded_and_run_time_limit_cancelled(self):
        plan = Plan("wait", [Step("wait_state", timeout=60, params={"field": "missing", "value": True})], "wait")
        self.service.RUN_TIMEOUT = .001
        run = self.service.run({"plan": plan.to_dict()}, self.config)["runs"][0]
        self.assertTrue(run["bounded_stop"])
        self.assertEqual(run["state"], "cancelled")
        self.assertLess(run["elapsed_seconds"], 1)

    def test_confirmed_copy_does_not_change_live_draft(self):
        plan = Plan("go", [Step("navigate", "home")], "go", {"requires_confirmation": True})
        run = self.service.run({"plan": plan.to_dict()}, self.config)["runs"][0]
        self.assertEqual(run["state"], "succeeded")
        self.assertTrue(plan.metadata["requires_confirmation"])
        self.assertFalse(run["preflight"]["ready"])

    def test_reject_invalid_scenarios_and_release_slot_on_exception(self):
        for ids in [[], ["missing"], ["empty", "empty"], "empty", [True]]:
            with self.assertRaises(CommandError):
                self.service.run({"workflow": search(), "scenario_ids": ids}, self.config)
        with patch.object(self.service, "_run_one", side_effect=RuntimeError("fixture exception")):
            with self.assertRaises(RuntimeError):
                self.service.run({"workflow": search()}, self.config)
        self.assertTrue(self.service.run({"workflow": search()}, self.config)["ok"])

    def test_concurrent_run_rejected_not_queued_implicitly(self):
        self.service._slot.acquire()
        try:
            with self.assertRaises(ScenarioBusy):
                self.service.run({"workflow": search()}, self.config)
        finally:
            self.service._slot.release()

    def test_preflight_reports_live_gates_and_no_execution(self):
        before = self.adapter.snapshot()
        plan = Plan("capture", [Step("capture", target="meeting_room")], "capture", {"requires_confirmation": True})
        result = inspect_plan(plan, self.config, self.adapter, {"active": False}, busy=True)
        codes = {item["code"] for item in result["findings"]}
        self.assertTrue({"LIFECYCLE_INACTIVE", "ENGINE_BUSY", "CAPABILITY_UNAVAILABLE", "CONFIRMATION_REQUIRED", "LOCATION_NOT_GUARANTEED"} <= codes)
        self.assertFalse(result["ready"])
        self.assertEqual(before, self.adapter.snapshot())

    def test_preflight_conditional_navigation_does_not_guarantee_arrival(self):
        plan = Plan("inspect", [Step("wait", seconds=.1, timeout=1, step_id="wait"),
            Step("navigate", "meeting_room", step_id="go", condition={"step_id": "wait", "outcome": "succeeded"}),
            Step("inspect", "meeting_room", object_name="水杯")], "inspect")
        result = inspect_plan(plan, self.config, self.adapter)
        self.assertIn("LOCATION_NOT_GUARANTEED", {item["code"] for item in result["findings"]})

    def test_preflight_changed_configuration_and_timeout_budget(self):
        plan = Plan("go", [Step("navigate", "home", timeout=5, max_retries=2)], "go", {"config_fingerprint": "old"})
        result = inspect_plan(plan, self.config, self.adapter)
        self.assertEqual(result["maximum_attempts"], 3)
        self.assertEqual(result["configured_timeout_budget_seconds"], 15)
        self.assertIn("CONFIG_CHANGED", {item["code"] for item in result["findings"]})

    def test_cli_scenario_does_not_open_supplied_database(self):
        from robot_voice_patrol.__main__ import main
        with tempfile.TemporaryDirectory() as directory:
            workflow = Path(directory)/"workflow.json"
            database = Path(directory)/"must-not-create.sqlite3"
            workflow.write_text(json.dumps(search(), ensure_ascii=False), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = main(["--workflow", str(workflow), "--scenario", "empty", "--db", str(database)])
            self.assertEqual(status, 0)
            self.assertEqual(json.loads(output.getvalue())["comparison"][0]["goal_outcome"], "not_achieved")
            self.assertFalse(database.exists())


class ScenarioHttpTests(unittest.TestCase):
    setUp = http_fixtures.HttpV3Tests.setUp
    tearDown = http_fixtures.HttpV3Tests.tearDown
    request = http_fixtures.HttpV3Tests.request

    def test_http_scenario_does_not_modify_running_live_engine(self):
        live = Plan("wait", [Step("wait", seconds=20, timeout=21)], "live")
        self.engine.submit_structured(live)
        mission_id = self.engine.snapshot()["mission"]["id"]
        baseline = self.engine.metrics()["missions_total"]
        status, report = self.request("/api/scenarios/run", {"workflow": search(), "scenario_ids": ["empty", "configured"]})
        self.assertEqual(status, 200)
        self.assertEqual(len(report["comparison"]), 2)
        self.assertEqual(self.engine.snapshot()["state"], "running")
        self.assertEqual(self.engine.snapshot()["mission"]["id"], mission_id)
        self.assertEqual(self.engine.metrics()["missions_total"], baseline)
        _, preflight = self.request("/api/preflight", {"workflow": search()})
        self.assertFalse(preflight["ready"])
        self.engine.control("stop")

    def test_routes_validate_inputs_and_assets_available(self):
        for path in ["/scenarios.js", "/scenarios.css"]:
            status, data = self.request(path, binary=True)
            self.assertEqual(status, 200)
            self.assertTrue(data)
        status, catalog = self.request("/api/scenarios")
        self.assertEqual(status, 200)
        self.assertEqual(len(catalog["scenarios"]), 6)
        status, _ = self.request("/api/scenarios/run", {"workflow": search(), "scenario_ids": ["invalid"]})
        self.assertEqual(status, 400)
        status, _ = self.request("/api/preflight", {"text": "去会议室"})
        self.assertEqual(status, 400)
        status, _ = self.request("/api/scenarios/run", {"workflow": search()}, method="PUT")
        self.assertEqual(status, 405)

    def test_queue_edit_api_and_audit(self):
        _, queued = self.request("/api/queue", {"text": "去会议室", "request_id": "queue-edit"})
        identifier = queued["job"]["id"]
        status, response = self.request(f"/api/queue/{identifier}", {"priority": 85}, method="PUT")
        self.assertEqual(status, 200)
        self.assertEqual(response["job"]["priority"], 85)
        self.request(f"/api/queue/{identifier}/cancel", {})
        status, _ = self.request(f"/api/queue/{identifier}", {"priority": 60}, method="PUT")
        self.assertEqual(status, 400)
