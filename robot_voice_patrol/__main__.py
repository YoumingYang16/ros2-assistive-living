"""python -m robot_voice_patrol starts a hardware-independent voice console."""
from __future__ import annotations

import argparse
import json
import sqlite3
import time

from .config import load_config
from .engine import MissionEngine
from .mock_adapter import MockAdapter
from .server import create_server


def main(argv=None):
    parser = argparse.ArgumentParser(description="ROS 2 中文语音导航与巡检任务平台")
    parser.add_argument("--mode", choices=["mock", "ros2"], default="mock")
    parser.add_argument("--home", action="store_true", help="加载内置居家地图和生活辅助入口")
    parser.add_argument("--config", help="地点和任务配置 JSON 路径")
    parser.add_argument("--port", type=int, default=8773)
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "localhost", "0.0.0.0"])
    parser.add_argument("--db", help="SQLite 数据库；居家默认 .runtime/home.sqlite3，传统控制台默认 .runtime/missions.sqlite3；:memory: 不持久化")
    parser.add_argument("--journal", default=".runtime/missions.jsonl", help="已完成任务日志路径")
    parser.add_argument("--command", help="无界面执行一条文字任务并输出 JSON 报告")
    parser.add_argument("--session-id", default="cli", help="持久化对话会话标识")
    parser.add_argument("--request-id", help="可选请求去重标识，同 ID 不重复执行")
    parser.add_argument("--preview", action="store_true", help="只预览 --command 的计划")
    parser.add_argument("--history", action="store_true", help="输出最近任务历史后退出")
    parser.add_argument("--doctor", action="store_true", help="打印依赖诊断，不录音、不调用模型")
    parser.add_argument("--probe-ros", action="store_true", help="诊断时尝试加载 ROS 原生库")
    parser.add_argument("--fixture-skills", action="store_true", help="明确启用额外技能的软件测试端点（仍非硬件）")
    parser.add_argument("--restore-backup", metavar="BACKUP_ID", help="关闭其他服务后，校验并离线恢复备份；先保存恢复前备份")
    parser.add_argument("--workflow", help="读取工作流 JSON 文件，配合 --preview 可只编译")
    parser.add_argument("--scenario", choices=["all", "configured", "empty", "inconclusive", "sensor_failure", "navigation_timeout", "transient_navigation"],
                        help="与 --workflow 一起在隔离软件环境验证，不打开运行数据库；all 比较全部六种场景")
    parser.add_argument("--queue", action="store_true", help="将 --command 或 --workflow 加入持久化队列后退出")
    parser.add_argument("--command-timeout", type=float, default=120.0)
    args = parser.parse_args(argv)
    args.db = args.db or (".runtime/home.sqlite3" if args.home else ".runtime/missions.sqlite3")
    if not 0 < args.command_timeout <= 7200:
        parser.error("command-timeout 必须在 0 到 7200 秒之间")
    try:
        if args.home and not args.config:
            from importlib.resources import files
            config = load_config(str(files("robot_voice_patrol").joinpath("config", "home.json")))
        else:
            config = load_config(args.config)
        if args.scenario:
            if not args.workflow or any((args.command, args.queue, args.restore_backup, args.history, args.preview, args.doctor)):
                parser.error("--scenario 必须与 --workflow 一起使用，不能混合执行、恢复或其他单次模式")
            from pathlib import Path
            from .scenarios import ScenarioService
            service = ScenarioService()
            identifiers = [item["id"] for item in service.catalog()["scenarios"]] if args.scenario == "all" else [args.scenario]
            result = service.run({"workflow": json.loads(Path(args.workflow).read_text(encoding="utf-8-sig")), "scenario_ids": identifiers}, config)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.restore_backup:
            from .data_management import restore_offline
            print(json.dumps(restore_offline(args.db, args.restore_backup), ensure_ascii=False, indent=2))
            return 0
        if args.doctor:
            from .diagnostics import collect_diagnostics
            print(json.dumps(collect_diagnostics(config, probe_ros=args.probe_ros), ensure_ascii=False, indent=2))
            return 0
        if args.mode == "mock":
            adapter = MockAdapter(config, fixture_skills=args.fixture_skills)
        else:
            try:
                import rclpy
                from .ros_adapter import Ros2Adapter
            except ImportError as exc:
                parser.error("ROS 2 运行时不可用。请在已安装 ROS 2 Jazzy 的环境 source setup.bash 后运行，或使用 --mode mock")
            from rclpy.signals import SignalHandlerOptions
            rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
            adapter = Ros2Adapter(config)
        engine = MissionEngine(config, adapter, args.journal, db_path=args.db,
                               start_scheduler=not (args.command or args.workflow or args.history or args.preview or args.queue))
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        parser.error(str(exc))
    server = None
    try:
        if args.history:
            print(json.dumps(engine.history(), ensure_ascii=False, indent=2))
            return 0
        if args.workflow:
            from pathlib import Path
            from .workflow import compile_workflow
            plan = compile_workflow(json.loads(Path(args.workflow).read_text(encoding="utf-8-sig")), config)
            if args.preview:
                print(json.dumps({"ok": True, "plan": plan.to_dict()}, ensure_ascii=False, indent=2))
                return 0
            if args.queue:
                engine.scheduler.control("pause")
                response = engine.scheduler.add(plan, request_id=args.request_id, session_id=args.session_id)
            else:
                response = engine.submit_structured(plan, request_id=args.request_id, session_id=args.session_id)
            if args.queue:
                print(json.dumps(response, ensure_ascii=False, indent=2))
                return 0
            deadline = time.monotonic() + args.command_timeout
            while engine.snapshot()["state"] in {"running", "pausing", "paused", "cancelling"}:
                if time.monotonic() >= deadline:
                    engine.control("stop")
                    raise TimeoutError("工作流超过等待期限，已请求停止")
                time.sleep(.05)
            value = engine.snapshot()
            print(json.dumps(value, ensure_ascii=False, indent=2))
            return 1 if value["state"] == "failed" else 0
        if args.preview:
            if not args.command:
                parser.error("--preview 需要 --command")
            print(json.dumps(engine.preview(args.command, args.session_id), ensure_ascii=False, indent=2))
            return 0
        if args.command:
            if args.queue:
                engine.scheduler.control("pause")
                response = engine.enqueue({"text": args.command, "request_id": args.request_id, "session_id": args.session_id})
                print(json.dumps(response, ensure_ascii=False, indent=2))
                return 0
            response = engine.submit(args.command, args.request_id, args.session_id)
            if not response.get("ok", True):
                print(json.dumps(response, ensure_ascii=False, indent=2))
                return 1
            if response.get("needs_clarification") or response.get("needs_confirmation"):
                print(json.dumps(response, ensure_ascii=False, indent=2))
                return 2
            if response.get("job") or response.get("assistive"):
                print(json.dumps(response, ensure_ascii=False, indent=2))
                return 0
            if response.get("duplicate"):
                if response.get("mission_id"):
                    response["original_mission"] = engine.mission_detail(response["mission_id"])["mission"]
                print(json.dumps(response, ensure_ascii=False, indent=2))
                return 1 if response.get("original_mission", {}).get("state") in {"failed", "interrupted"} else 0
            deadline = time.monotonic() + args.command_timeout
            while engine.snapshot()["state"] in {"running", "pausing", "paused", "cancelling"}:
                if time.monotonic() >= deadline:
                    engine.control("stop")
                    raise TimeoutError("命令运行超过等待期限，已请求停止")
                time.sleep(.05)
            state = engine.snapshot()
            print(json.dumps(state, ensure_ascii=False, indent=2))
            return 1 if state["state"] == "failed" else 0
        server = create_server(engine, args.host, args.port)
        print(f"Voice Patrol | {'软件模拟（预设数据）' if args.mode == 'mock' else 'ROS 2 接口'}", flush=True)
        print(f"控制台：http://{args.host}:{server.server_port}  Ctrl+C 退出", flush=True)
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        return 0
    except (ValueError, OSError, TimeoutError) as exc:
        print(str(exc))
        return 1
    finally:
        if server:
            server.server_close()
        engine.close()
        if args.mode == "ros2":
            import rclpy
            if rclpy.ok():
                rclpy.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
