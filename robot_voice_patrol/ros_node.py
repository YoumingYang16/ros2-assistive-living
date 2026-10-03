"""Managed ROS lifecycle bridge. No ROS native imports during plain Python use."""
from __future__ import annotations

import json
import signal
import threading

from .config import load_config
from .contracts import CommandError
from .engine import MissionEngine
from .lifecycle import LifecycleController
from .ros_adapter import Ros2Adapter


class RosLifecycleBridge:
    """Make HTTP lifecycle transitions invoke the real ROS state machine."""
    def __init__(self, node):
        self.node = node
        self._lock = threading.RLock()

    def snapshot(self):
        return {**self.node.control.snapshot(), "managed_ros_node": True}

    def ensure_active(self):
        self.node.control.ensure_active()

    def transition(self, action):
        with self._lock:
            if action == "reset_error":
                raise CommandError("ROS 错误处理完成后会回到 unconfigured；若已 finalized 请重启节点")
            if action not in LifecycleController.TRANSITIONS:
                raise CommandError("未知生命周期操作")
            source, target = LifecycleController.TRANSITIONS[action]
            if self.node.control.snapshot()["state"] != source:
                raise CommandError(f"当前状态不能执行 {action}")
            getattr(self.node, "trigger_" + action)()
            if self.node.control.snapshot()["state"] != target:
                raise CommandError(self.node.control.snapshot().get("last_error") or "ROS 生命周期切换被拒绝")
            return self.snapshot()


def managed_node_class():
    # These imports deliberately remain in a factory. Local mock mode needs no
    # rclpy installation, and native platform import failures stay diagnosable.
    from rclpy.lifecycle import LifecycleNode, TransitionCallbackReturn
    from rclpy.callback_groups import ReentrantCallbackGroup
    from rclpy.clock import Clock, ClockType
    from std_msgs.msg import String

    class VoicePatrolLifecycleNode(LifecycleNode):
        def __init__(self):
            super().__init__("voice_patrol")
            for name, default in (("config", ""), ("dashboard", True), ("host", "127.0.0.1"),
                                  ("port", 8773), ("db_path", ".runtime/missions.sqlite3"),
                                  ("map_frame", ""), ("autostart", True)):
                self.declare_parameter(name, default)
            self.engine = self.adapter = self.http_server = self.http_thread = None
            self.state_pub = self.speech_pub = self.command_sub = self.status_timer = None
            self._resources_lock = threading.RLock()
            self._last_event_id = 0
            self._commands_group = ReentrantCallbackGroup()
            self.control = LifecycleController(initial_state="unconfigured", busy=self._busy)
            self.bridge = RosLifecycleBridge(self)

        def _busy(self):
            if self.engine is None:
                return False
            return self.engine.snapshot()["state"] in {"running", "pausing", "paused", "cancelling"}

        def on_configure(self, state):
            try:
                with self._resources_lock:
                    if self.engine is not None:
                        raise CommandError("上一轮资源尚未释放")
                    config = load_config(self.get_parameter("config").value or None)
                    frame = self.get_parameter("map_frame").value
                    if frame:
                        config.setdefault("ros", {})["map_frame"] = str(frame)
                    self.adapter = Ros2Adapter(config, node=self, start_executor=False)
                    self.control.adapter = self.adapter
                    self.engine = MissionEngine(config, self.adapter, db_path=self.get_parameter("db_path").value or None)
                    self.engine.lifecycle = self.bridge
                    self.state_pub = self.create_publisher(String, "/voice_patrol/state", 10)
                    self.speech_pub = self.create_lifecycle_publisher(String, "/voice_patrol/speech_text", 10)
                    self.command_sub = self.create_subscription(String, "/voice_patrol/command", self.command_received, 10,
                                                                 callback_group=self._commands_group)
                    self.status_timer = self.create_timer(.5, self.publish_state, clock=Clock(clock_type=ClockType.STEADY_TIME))
                    self.control.transition("configure")
                    if self.get_parameter("dashboard").value:
                        from .server import create_server
                        host, port = str(self.get_parameter("host").value), int(self.get_parameter("port").value)
                        self.http_server = create_server(self.engine, host, port)
                        self.http_thread = threading.Thread(target=self.http_server.serve_forever, daemon=True, name="voice-http")
                        self.http_thread.start()
                        self.get_logger().info(f"Voice Patrol dashboard: http://{host}:{self.http_server.server_port}")
                return super().on_configure(state)
            except Exception as exc:
                self.control.fail(str(exc))
                self.get_logger().error(f"Configure failed: {exc}")
                return TransitionCallbackReturn.ERROR

        def on_activate(self, state):
            try:
                with self.engine._condition:
                    outcome = super().on_activate(state)
                    if outcome == TransitionCallbackReturn.SUCCESS:
                        self.control.transition("activate")
                    return outcome
            except CommandError as exc:
                self.get_logger().warning(str(exc))
                return TransitionCallbackReturn.FAILURE

        def on_deactivate(self, state):
            try:
                # Serialize with task acceptance. Queue dispatch checks this
                # same lifecycle gate, so no new task can start after it.
                with self.engine._condition:
                    if self.control._pending():
                        raise CommandError("活动任务或外部目标尚未结束")
                    outcome = super().on_deactivate(state)
                    if outcome == TransitionCallbackReturn.SUCCESS:
                        self.control.transition("deactivate")
                    return outcome
            except CommandError as exc:
                self.get_logger().warning(str(exc))
                return TransitionCallbackReturn.FAILURE

        def on_cleanup(self, state):
            try:
                with self.engine._condition:
                    self.control.transition("cleanup")
                self._release_resources()
                return super().on_cleanup(state)
            except CommandError as exc:
                self.get_logger().warning(str(exc))
                return TransitionCallbackReturn.FAILURE
            except Exception as exc:
                self.control.fail(str(exc))
                return TransitionCallbackReturn.ERROR

        def on_error(self, state):
            # ROS successful error processing returns to unconfigured. Retain
            # the error in logs; never pretend the old external goal is done.
            error = self.control.snapshot().get("last_error") or "ROS lifecycle callback failed"
            self.get_logger().error(error)
            if self.adapter is not None:
                self.adapter.stop()
                if self.adapter.snapshot().get("action_pending"):
                    return TransitionCallbackReturn.FAILURE
            self._release_resources()
            self.control = LifecycleController(initial_state="unconfigured", busy=self._busy)
            return TransitionCallbackReturn.SUCCESS

        def on_shutdown(self, state):
            self._release_resources()
            self.control.finalize()
            return super().on_shutdown(state)

        def _release_resources(self):
            with self._resources_lock:
                server, thread = self.http_server, self.http_thread
                self.http_server = self.http_thread = None
                # Cleanup can originate in its own HTTP handler. Give the
                # lifecycle receipt time to flush before closing the listener.
                if server:
                    def stop_http():
                        server.shutdown()
                        server.server_close()
                        if thread and thread is not threading.current_thread():
                            thread.join(timeout=2)
                    timer = threading.Timer(.2, stop_http)
                    timer.daemon = True
                    timer.start()
                if self.status_timer is not None:
                    self.destroy_timer(self.status_timer)
                    self.status_timer = None
                if self.command_sub is not None:
                    self.destroy_subscription(self.command_sub)
                    self.command_sub = None
                if self.engine is not None:
                    self.engine.close()
                    self.engine = None
                elif self.adapter is not None:
                    self.adapter.close()
                self.adapter = None
                self.control.adapter = None
                if self.state_pub is not None:
                    self.destroy_publisher(self.state_pub)
                    self.state_pub = None
                if self.speech_pub is not None:
                    self.destroy_lifecycle_publisher(self.speech_pub)
                    self.speech_pub = None

        def speak(self, message):
            if self.speech_pub is not None:
                self.speech_pub.publish(String(data=message))

        def command_received(self, msg):
            engine = self.engine
            if engine is None:
                return
            try:
                self.control.ensure_active()
                events = engine.snapshot()["events"]
                before = events[-1]["id"] if events else 0
                response = engine.submit(msg.data)
                events = response["state"]["events"]
                if not events or events[-1]["id"] == before:
                    self.speak(response["message"])
            except CommandError as exc:
                self.speak(f"指令未执行：{exc}")
            except Exception as exc:
                self.get_logger().error(f"Command failed: {exc}")

        def publish_state(self):
            engine = self.engine
            if engine is None or self.state_pub is None:
                return
            try:
                snapshot = engine.snapshot()
                snapshot["lifecycle"] = self.bridge.snapshot()
                self.state_pub.publish(String(data=json.dumps(snapshot, ensure_ascii=False, allow_nan=False)))
                if self.control.snapshot()["active"]:
                    for event in snapshot["events"]:
                        if event["id"] > self._last_event_id:
                            self.speak(event.get("data", {}).get("speech_text") or event["message"])
                            self._last_event_id = event["id"]
            except Exception as exc:
                self.get_logger().error(f"State publication failed: {exc}")

    return VoicePatrolLifecycleNode


def main(args=None):
    try:
        import rclpy
        from rclpy.executors import MultiThreadedExecutor
        from rclpy.signals import SignalHandlerOptions
        node_type = managed_node_class()
    except ImportError as exc:
        raise SystemExit("请先 source ROS 2 Jazzy 环境；普通 Python 演示使用 python -m robot_voice_patrol。") from exc
    rclpy.init(args=args, signal_handler_options=SignalHandlerOptions.NO)
    node = node_type()
    shutdown = threading.Event()
    prior = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, lambda *_: shutdown.set())
    executor = MultiThreadedExecutor(num_threads=6)
    executor.add_node(node)
    def spin():
        try:
            executor.spin()
        except Exception as exc:
            if not shutdown.is_set():
                node.get_logger().error(f"ROS executor failed: {exc}")
                if node.adapter is not None:
                    node.adapter._executor_failure = str(exc) or type(exc).__name__
                    node.adapter.stop()
                node.control.fail("ROS executor stopped")
                shutdown.set()
    spinner = threading.Thread(target=spin, daemon=True, name="managed-ros-executor")
    spinner.start()
    try:
        if node.get_parameter("autostart").value:
            node.trigger_configure()
            if node.control.snapshot()["state"] == "inactive":
                node.trigger_activate()
        node.get_logger().info("Managed voice patrol node ready; use ros2 lifecycle set /voice_patrol ...")
        while rclpy.ok() and not shutdown.wait(.2):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        # Leave the executor alive while cancellation waits for its action
        # result. Only then shut down DDS and destroy the node.
        node._release_resources()
        executor.shutdown(timeout_sec=2.0, wait_for_threads=False)
        spinner.join(timeout=2)
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        signal.signal(signal.SIGTERM, prior)


if __name__ == "__main__":
    main()
