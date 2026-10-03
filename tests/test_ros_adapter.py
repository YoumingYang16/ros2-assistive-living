"""Protocol/lifecycle tests using action futures; these do not validate ROS DDS."""
from concurrent.futures import Future
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import json
import threading
import time
from types import SimpleNamespace as NS
import unittest

from robot_voice_patrol.contracts import ExecutionCancelled, ExecutionError, Step
from robot_voice_patrol.engine import MissionEngine
from robot_voice_patrol.plan_validation import condition_matches
from robot_voice_patrol.ros_adapter import Ros2Adapter, RosExecutionError


class FakeHandle:
    def __init__(self, accepted=True):
        self.accepted = accepted
        self.result = Future()
        self.cancel_calls = 0

    def get_result_async(self):
        return self.result

    def cancel_goal_async(self):
        self.cancel_calls += 1
        future = Future()
        future.set_result(NS(goals_canceling=[1]))
        return future

    def finish(self, status=4, error_code=0, result=None):
        self.result.set_result(NS(status=status, result=result or NS(error_code=error_code, error_msg="test")))


class FakeClient:
    def __init__(self):
        self.sent = []
        self.ready = True

    def server_is_ready(self):
        return self.ready

    def send_goal_async(self, goal, feedback_callback):
        future = Future()
        self.sent.append((goal, future))
        return future


def adapter_fixture():
    adapter = Ros2Adapter.__new__(Ros2Adapter)
    adapter.config = {"locations": {"home": {"x": 1, "y": 2, "yaw": 0}}}
    adapter._lock = threading.RLock()
    adapter._execute_lock = threading.Lock()
    adapter._closed = False
    adapter._stop_generation = 0
    adapter._active = None
    adapter._pending_inspections = {}
    adapter._pose = {"x": None, "y": None, "yaw": None}
    adapter._location = None
    adapter._pose_observed_at = None
    adapter._pose_received_monotonic = None
    adapter._pose_stale_seconds = 5.0
    adapter._settings = {}
    adapter._perception_backend = "json"
    adapter._inspection_client = None
    adapter._discovery_timeout = 5.0
    adapter._executor_failure = None
    adapter._last_error = None
    adapter._error_counts = Counter()
    adapter._distance_remaining = None
    adapter._frame = "map"
    adapter._cancel_timeout = 0.1
    adapter.node = NS(get_clock=lambda: NS(now=lambda: NS(to_msg=lambda: "stamp")),
                      count_subscribers=lambda _: 1, count_publishers=lambda _: 1)
    adapter._goal_type = NS(Goal=lambda: NS(pose=NS(header=NS(), pose=NS(position=NS(), orientation=NS()))))
    adapter._navigation = FakeClient()
    adapter._string_type = lambda **kwargs: NS(**kwargs)
    adapter.published = []
    adapter._inspection_pub = NS(publish=lambda msg: adapter.published.append(json.loads(msg.data)))
    return adapter


def wait_until(predicate, timeout=1):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for test thread")
        time.sleep(0.002)


def run_step(adapter, step, cancel=None):
    outcome = {}
    def run():
        try:
            outcome["result"] = adapter.execute(step, cancel or threading.Event(), lambda _: None)
        except Exception as exc:
            outcome["error"] = exc
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


class RosAdapterTests(unittest.TestCase):
    def typed_fixture(self):
        adapter = adapter_fixture()
        adapter._perception_backend = "action"
        adapter._inspection_client = FakeClient()
        adapter._inspect_type = NS(Goal=lambda: NS())
        return adapter

    def typed_result(self, goal, outcome=1):
        sec, nanosec = divmod(time.time_ns(), 1_000_000_000)
        observation = NS(label="水杯", confidence=0.9, sensor="fixture", media_uri="",
                         bbox_xywh=[1.0, 2.0, 3.0, 4.0], details_json='{"reason":"software_test"}')
        return NS(request_id=goal.request_id, target=goal.target, object_name=goal.object_name,
                  outcome=outcome, error_code=0, error_message="", summary="", simulated=True,
                  observed_at=NS(sec=sec, nanosec=nanosec), observations=[observation])

    def test_typed_inspection_preserves_all_three_outcomes(self):
        for outcome, expected, found in [(1, "found", True), (2, "not_found", False), (3, "inconclusive", None)]:
            with self.subTest(outcome=outcome):
                adapter = self.typed_fixture()
                thread, response = run_step(adapter, Step("inspect", "home", object_name="水杯", timeout=2))
                wait_until(lambda: adapter._inspection_client.sent)
                goal, future = adapter._inspection_client.sent[0]
                handle = FakeHandle()
                future.set_result(handle)
                handle.finish(result=self.typed_result(goal, outcome))
                thread.join(1)
                self.assertEqual(response["result"]["outcome"], expected)
                self.assertIs(response["result"]["found"], found)
                self.assertEqual(response["result"]["status"], "succeeded")
                self.assertTrue(response["result"]["simulated"])
                normalized = MissionEngine._normalize_result(
                    Step("inspect", "home", object_name="水杯", timeout=2, step_id="s001"), response["result"])
                self.assertEqual(normalized["outcome"], expected)
                conditional = Step("navigate", "home", step_id="s002",
                                   condition={"step_id": "s001", "outcome": "not_found"})
                self.assertEqual(condition_matches(conditional, [normalized])[0], outcome == 2)

    def test_typed_scene_result_matches_engine_contract(self):
        adapter = self.typed_fixture()
        step = Step("inspect", "home", timeout=2, step_id="s001")
        thread, response = run_step(adapter, step)
        wait_until(lambda: adapter._inspection_client.sent)
        goal, future = adapter._inspection_client.sent[0]
        handle = FakeHandle()
        future.set_result(handle)
        handle.finish(result=self.typed_result(goal, outcome=4))
        thread.join(1)
        result = MissionEngine._normalize_result(step, response["result"])
        self.assertEqual(result["outcome"], "observed")
        self.assertIsNone(result["found"])

    def test_typed_bbox_numeric_scalars_survive_engine_json_serialization(self):
        # ROS float32[4] arrays contain non-native numeric scalars. Decimal
        # exercises the same conversion boundary without a NumPy dependency.
        coordinates = [Decimal("1.25"), Decimal("-2.5"), Decimal("3.75"), Decimal("0")]
        with self.assertRaises(TypeError):
            json.dumps(coordinates)
        for outcome, expected in ((1, "found"), (3, "inconclusive")):
            with self.subTest(outcome=expected):
                adapter = self.typed_fixture()
                step = Step("inspect", "home", object_name="水杯", timeout=2, step_id="s001")
                thread, response = run_step(adapter, step)
                wait_until(lambda: adapter._inspection_client.sent)
                goal, future = adapter._inspection_client.sent[0]
                handle = FakeHandle()
                future.set_result(handle)
                result = self.typed_result(goal, outcome=outcome)
                result.observations[0].bbox_xywh = list(coordinates)
                handle.finish(result=result)
                thread.join(1)
                self.assertFalse(thread.is_alive())
                self.assertNotIn("error", response)
                normalized = MissionEngine._normalize_result(step, response["result"])
                bbox = normalized["evidence"][0]["bbox_xywh"]
                self.assertTrue(all(type(value) is float for value in bbox))
                self.assertEqual(bbox, [1.25, -2.5, 3.75, 0.0])
                restored = json.loads(json.dumps(normalized, allow_nan=False))
                self.assertEqual(restored["evidence"][0]["bbox_xywh"], bbox)
                self.assertEqual(restored["outcome"], expected)

    def test_typed_bbox_scalar_conversion_still_rejects_invalid_geometry(self):
        for coordinates in (
            [Decimal("NaN"), Decimal("0"), Decimal("1"), Decimal("1")],
            [Decimal("0"), Decimal("0"), Decimal("Infinity"), Decimal("1")],
            [Decimal("0"), Decimal("0"), Decimal("-1"), Decimal("1")],
            [Decimal("0"), Decimal("0"), Decimal("1"), Decimal("-1")],
        ):
            with self.subTest(coordinates=coordinates):
                adapter = self.typed_fixture()
                step = Step("inspect", "home", object_name="水杯", timeout=2)
                thread, response = run_step(adapter, step)
                wait_until(lambda: adapter._inspection_client.sent)
                goal, future = adapter._inspection_client.sent[0]
                handle = FakeHandle()
                future.set_result(handle)
                result = self.typed_result(goal)
                result.observations[0].bbox_xywh = coordinates
                handle.finish(result=result)
                thread.join(1)
                self.assertFalse(thread.is_alive())
                self.assertNotIn("result", response)
                self.assertIsInstance(response["error"], RosExecutionError)
                self.assertEqual(response["error"].code, "INVALID_OBSERVATION")
                self.assertFalse(response["error"].retryable)

    def test_typed_inspection_can_be_cancelled_with_terminal_ack(self):
        adapter = self.typed_fixture()
        thread, response = run_step(adapter, Step("inspect", "home", object_name="水杯", timeout=2))
        wait_until(lambda: adapter._inspection_client.sent)
        handle = FakeHandle()
        adapter._inspection_client.sent[0][1].set_result(handle)
        adapter.stop()
        self.assertEqual(handle.cancel_calls, 1)
        self.assertEqual(adapter.snapshot()["action"]["cancel_response"], "accepted")
        handle.finish(status=5)
        thread.join(1)
        self.assertIsInstance(response["error"], ExecutionCancelled)
        self.assertFalse(adapter.snapshot()["action_pending"])

    def test_typed_invalid_evidence_fails_without_retry(self):
        adapter = self.typed_fixture()
        thread, response = run_step(adapter, Step("inspect", "home", object_name="水杯", timeout=2))
        wait_until(lambda: adapter._inspection_client.sent)
        goal, future = adapter._inspection_client.sent[0]
        handle = FakeHandle()
        future.set_result(handle)
        result = self.typed_result(goal)
        result.observations[0].confidence = float("nan")
        handle.finish(result=result)
        thread.join(1)
        self.assertEqual(response["error"].code, "INVALID_OBSERVATION")
        self.assertFalse(response["error"].retryable)

    def test_executor_failure_is_latched_and_blocks_new_action(self):
        adapter = adapter_fixture()
        adapter._executor_failure = "test executor fault"
        with self.assertRaises(RosExecutionError) as caught:
            adapter.execute(Step("navigate", "home"), threading.Event(), lambda _: None)
        self.assertEqual(caught.exception.code, "ROS_EXECUTOR_FAILED")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(adapter._navigation.sent, [])
        self.assertEqual(adapter.snapshot()["health"]["status"], "error")

    def test_json_inconclusive_never_becomes_not_found(self):
        adapter = adapter_fixture()
        thread, response = run_step(adapter, Step("inspect", "home", object_name="水杯", timeout=2))
        wait_until(lambda: adapter.published)
        result = {**adapter.published[0], "success": True, "outcome": "inconclusive",
                  "observed_at": datetime.now(timezone.utc).isoformat(), "evidence": {"reason": "occluded"}}
        adapter._on_inspection_result(NS(data=json.dumps(result)))
        thread.join(1)
        self.assertEqual(response["result"]["outcome"], "inconclusive")
        self.assertIsNone(response["result"]["found"])

    def test_discovery_timeout_has_distinct_retryable_code(self):
        adapter = adapter_fixture()
        adapter._navigation.ready = False
        adapter._discovery_timeout = 0.01
        with self.assertRaises(RosExecutionError) as caught:
            adapter.execute(Step("navigate", "home", timeout=5), threading.Event(), lambda _: None)
        self.assertEqual(caught.exception.code, "SERVER_UNAVAILABLE")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(adapter.snapshot()["health"]["error_counts"]["SERVER_UNAVAILABLE"], 1)

    def test_deep_invalid_json_does_not_kill_callback(self):
        adapter = adapter_fixture()
        adapter._on_inspection_result(NS(data="["*2000 + "0" + "]"*2000))
        self.assertIsNone(adapter._executor_failure)
        self.assertEqual(adapter.snapshot()["inspection_pending"], 0)

    def test_unknown_initial_pose_is_not_fabricated(self):
        snapshot = adapter_fixture().snapshot()
        self.assertIsNone(snapshot["location"])
        self.assertIsNone(snapshot["x"])
        self.assertFalse(snapshot["pose_valid"])

    def test_pose_valid_expires_after_five_seconds(self):
        adapter = adapter_fixture()
        adapter._pose_received_monotonic = time.monotonic()
        self.assertTrue(adapter.snapshot()["pose_valid"])
        adapter._pose_received_monotonic -= 6
        self.assertFalse(adapter.snapshot()["pose_valid"])

    def test_wait_has_explicit_success_status(self):
        adapter = adapter_fixture()
        result = adapter.execute(Step("wait", seconds=0.01, timeout=1), threading.Event(), lambda _: None)
        self.assertEqual(result["status"], "succeeded")

    def test_server_discovery_is_bounded(self):
        adapter = adapter_fixture()
        adapter._navigation.ready = False
        with self.assertRaises(ExecutionError):
            adapter.execute(Step("navigate", "home", timeout=0.02), threading.Event(), lambda _: None)
        self.assertEqual(adapter._navigation.sent, [])

    def test_late_acceptance_is_cancelled_and_blocks_overlap_until_terminal(self):
        adapter = adapter_fixture()
        with self.assertRaises(ExecutionError):
            adapter.execute(Step("navigate", "home", timeout=0.02), threading.Event(), lambda _: None)
        self.assertTrue(adapter.snapshot()["cancellation_pending"])
        with self.assertRaisesRegex(ExecutionError, "重叠"):
            adapter.execute(Step("navigate", "home"), threading.Event(), lambda _: None)
        handle = FakeHandle()
        adapter._navigation.sent[0][1].set_result(handle)
        self.assertEqual(handle.cancel_calls, 1)
        self.assertTrue(adapter.snapshot()["navigation_pending"])
        # The cancel service already replied, but the action has not terminated.
        with self.assertRaises(ExecutionError):
            adapter.execute(Step("inspect", "home"), threading.Event(), lambda _: None)
        handle.finish(status=5)
        self.assertFalse(adapter.snapshot()["navigation_pending"])
        self.assertEqual(len(adapter._navigation.sent), 1)

    def test_stop_returns_promptly_and_cancels_late_goal(self):
        adapter = adapter_fixture()
        thread, outcome = run_step(adapter, Step("navigate", "home", timeout=2))
        wait_until(lambda: adapter._navigation.sent)
        before = time.monotonic()
        adapter.stop()
        self.assertLess(time.monotonic() - before, 0.1)
        handle = FakeHandle()
        adapter._navigation.sent[0][1].set_result(handle)
        self.assertEqual(handle.cancel_calls, 1)
        handle.finish(status=5)
        thread.join(1)
        self.assertIsInstance(outcome.get("error"), ExecutionCancelled)

    def test_unacknowledged_stop_is_failure_not_claimed_stopped(self):
        adapter = adapter_fixture()
        thread, outcome = run_step(adapter, Step("navigate", "home", timeout=2))
        wait_until(lambda: adapter._navigation.sent)
        adapter.stop()
        thread.join(1)
        self.assertIsInstance(outcome.get("error"), ExecutionError)
        self.assertNotIsInstance(outcome.get("error"), ExecutionCancelled)
        self.assertTrue(adapter.snapshot()["cancellation_pending"])

    def test_success_requires_terminal_success_and_zero_error_code(self):
        for status, error_code, should_succeed in [(4, 0, True), (4, 3, False), (6, 0, False)]:
            with self.subTest(status=status, error_code=error_code):
                adapter = adapter_fixture()
                thread, outcome = run_step(adapter, Step("navigate", "home", timeout=2))
                wait_until(lambda: adapter._navigation.sent)
                handle = FakeHandle()
                adapter._navigation.sent[0][1].set_result(handle)
                handle.finish(status=status, error_code=error_code)
                thread.join(1)
                self.assertEqual("result" in outcome, should_succeed)
                self.assertFalse(adapter.snapshot()["navigation_pending"])

    def test_result_transport_error_blocks_future_navigation(self):
        adapter = adapter_fixture()
        thread, outcome = run_step(adapter, Step("navigate", "home", timeout=2))
        wait_until(lambda: adapter._navigation.sent)
        handle = FakeHandle()
        adapter._navigation.sent[0][1].set_result(handle)
        handle.result.set_exception(RuntimeError("lost server"))
        thread.join(1)
        self.assertIsInstance(outcome.get("error"), ExecutionError)
        self.assertTrue(adapter.snapshot()["navigation_pending"])
        self.assertEqual(handle.cancel_calls, 1)

    def test_rejected_goal_releases_reservation(self):
        adapter = adapter_fixture()
        thread, outcome = run_step(adapter, Step("navigate", "home", timeout=2))
        wait_until(lambda: adapter._navigation.sent)
        adapter._navigation.sent[0][1].set_result(FakeHandle(accepted=False))
        thread.join(1)
        self.assertIsInstance(outcome.get("error"), ExecutionError)
        self.assertFalse(adapter.snapshot()["navigation_pending"])

    def test_inspection_timeout_is_failure_and_late_results_are_ignored(self):
        adapter = adapter_fixture()
        with self.assertRaises(ExecutionError):
            adapter.execute(Step("inspect", "home", timeout=0.02), threading.Event(), lambda _: None)
        request = adapter.published[0]
        self.assertEqual(adapter.snapshot()["inspection_pending"], 0)
        adapter._on_inspection_result(NS(data=json.dumps({**request, "success": True})))
        self.assertEqual(adapter.snapshot()["inspection_pending"], 0)

    def test_inspection_requires_correlated_fresh_evidence(self):
        adapter = adapter_fixture()
        thread, outcome = run_step(adapter, Step("inspect", "home", object_name="水杯", timeout=2))
        wait_until(lambda: adapter.published)
        request = adapter.published[0]
        result = {"request_id": request["request_id"], "target": "home", "object_name": "水杯",
                  "success": True, "found": False, "observed_at": datetime.now(timezone.utc).isoformat(),
                  "evidence": {"detections": [], "image_uri": "file:///frames/42.jpg"}}
        adapter._on_inspection_result(NS(data=json.dumps({**result, "request_id": "wrong"})))
        self.assertTrue(thread.is_alive())
        adapter._on_inspection_result(NS(data=json.dumps(result)))
        thread.join(1)
        self.assertFalse(outcome["result"]["found"])
        self.assertEqual(outcome["result"]["source"], "ros2_perception")
        self.assertEqual(outcome["result"]["status"], "succeeded")

    def test_stale_or_evidenceless_inspection_is_failure(self):
        for bad_fields in [{"evidence": {}}, {"observed_at": "2020-01-01T00:00:00Z"}, {"found": "yes"}]:
            with self.subTest(fields=bad_fields):
                adapter = adapter_fixture()
                thread, outcome = run_step(adapter, Step("inspect", "home", object_name="水杯", timeout=2))
                wait_until(lambda: adapter.published)
                request = adapter.published[0]
                result = {**request, "success": True, "found": True,
                          "observed_at": datetime.now(timezone.utc).isoformat(), "evidence": {"camera": "front"},
                          **bad_fields}
                adapter._on_inspection_result(NS(data=json.dumps(result)))
                thread.join(1)
                self.assertIsInstance(outcome.get("error"), ExecutionError)


if __name__ == "__main__":
    unittest.main()
