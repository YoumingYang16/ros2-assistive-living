"""Local product API with explicit route verbs and bounded audio sessions."""
from __future__ import annotations
import copy
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import urlsplit, parse_qs
from .contracts import CommandError
from .config import _unique_json_pairs
from .voice_sessions import VoiceSessionService, VoiceSessionError
from .audio import AudioUnavailableError
from .scenarios import ScenarioService, ScenarioBusy
from .preflight import structured_plan, inspect_plan

JSON_LIMIT = 256 * 1024
AUDIO_LIMIT = 10 * 1024 * 1024
STATIC = {"index.html", "style.css", "app.js", "voice.js", "streaming-voice.js", "pcm-worklet.js", "workspace.js", "workspace.css", "scenarios.js", "scenarios.css", "assistive.js", "assistive.css", "living.js", "daily-management.js"}


def create_server(engine, host="127.0.0.1", port=8773, *, voice_service=None):
    container = host == "0.0.0.0" and os.environ.get("VOICE_PATROL_CONTAINER_BIND") == "1"
    if host not in {"127.0.0.1", "localhost"} and not container:
        raise ValueError("控制台仅绑定本机；容器映射需显式设置 VOICE_PATROL_CONTAINER_BIND=1")
    voice = voice_service or VoiceSessionService(os.environ.get("VOICE_PATROL_VOSK_MODEL", ""))
    scenarios = ScenarioService()

    class Handler(BaseHTTPRequestHandler):
        server_version = "VoicePatrol/7.0"

        def log_message(self, format, *args):
            pass

        def send_data(self, status, data, content_type="application/json; charset=utf-8", filename=None):
            if isinstance(data, (dict, list)):
                data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(data)

        def local_request(self):
            authority = self.headers.get("Host", "")
            allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if container:
                allowed.update(x.strip() for x in os.environ.get("VOICE_PATROL_ALLOWED_HOSTS", "").split(",") if x.strip())
            origin = self.headers.get("Origin")
            if authority not in allowed or (origin and origin != f"http://{authority}") or self.headers.get("Sec-Fetch-Site") == "cross-site":
                self.send_data(403, {"ok": False, "message": "拒绝非授权 Host 或跨站控制请求"})
                return False
            return True

        def guarded(self, callback):
            if not self.local_request():
                return
            try:
                callback()
            except ScenarioBusy as exc:
                self.send_data(409, {"ok": False, "message": str(exc)})
            except VoiceSessionError as exc:
                self.send_data(exc.status_code, {"ok": False, "message": str(exc)})
            except AudioUnavailableError as exc:
                self.send_data(503, {"ok": False, "message": str(exc)})
            except (ValueError, UnicodeError, CommandError) as exc:
                self.send_data(400, {"ok": False, "message": str(exc)})
            except FileNotFoundError:
                self.send_data(404, {"ok": False, "message": "资源不存在"})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except TimeoutError:
                self.send_data(408, {"ok": False, "message": "请求处理超时；请先查看任务状态"})
            except Exception:
                self.send_data(500, {"ok": False, "message": "服务内部错误，请查看任务状态"})

        def do_GET(self):
            self.guarded(self.get_route)

        def history_integer(self, value, name):
            # HTTP query values are text; accept only ordinary decimal digits,
            # not Python int() extras such as whitespace, + signs or underscores.
            if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,10}", value):
                raise CommandError(f"{name} 必须为十进制非负整数")
            return int(value)

        def get_route(self):
            uri = urlsplit(self.path)
            path, query = uri.path, {k: v[-1] for k, v in parse_qs(uri.query).items()}
            if path == "/api/state":
                value = engine.snapshot()
            elif path == "/api/assistive":
                value = {"ok": True, **engine.assistive.snapshot()}
            elif path == "/api/assistive/catalog":
                value = {"ok": True, **engine.assistive.catalog()}
            elif path == "/api/assistive/history/meta":
                from .assistive_history import history_metadata
                if uri.query:
                    raise CommandError("历史目录不接受查询参数")
                value = {"ok": True, **history_metadata()}
            elif path == "/api/assistive/history":
                from .assistive_history import query_history
                allowed = {"kind", "state", "query", "since", "until", "limit", "offset"}
                raw = parse_qs(uri.query, keep_blank_values=True)
                if set(raw) - allowed or any(len(v) != 1 or (not v[0] and k != "query") for k, v in raw.items()):
                    raise CommandError("历史查询参数无效或重复")
                filters = {k: v for k, v in query.items() if k not in {"limit", "offset"}}
                value = {"ok": True, **query_history(engine.assistive, **filters,
                    limit=self.history_integer(query.get("limit", "25"), "limit"),
                    offset=self.history_integer(query.get("offset", "0"), "offset"))}
            elif match := re.fullmatch(r"/api/assistive/history/([a-zA-Z0-9]{1,64})", path):
                from .assistive_history import record_detail
                raw = parse_qs(uri.query, keep_blank_values=True)
                if set(raw) - {"event_limit", "event_offset"} or any(len(v) != 1 or not v[0] for v in raw.values()):
                    raise CommandError("详情查询参数无效或重复")
                value = {"ok": True, **record_detail(engine.assistive, match[1],
                    event_limit=self.history_integer(query.get("event_limit", "50"), "event_limit"),
                    event_offset=self.history_integer(query.get("event_offset", "0"), "event_offset"))}
            elif path == "/api/config":
                value = {"ok": True, "config": copy.deepcopy(engine.config), "locations": engine.config["locations"],
                         "patrol_routes": engine.config["patrol_routes"], "mode": engine.adapter.mode}
            elif path == "/api/history":
                value = engine.history(int(query.get("limit", 20)), int(query.get("offset", 0)),
                    **{k: query.get(k, "") for k in ("query", "state", "since", "until")}, archived=query.get("archived") == "true")
            elif path == "/api/events":
                events = engine.store.events_page(mission_id=query.get("mission_id"), after=query.get("after", 0), limit=query.get("limit", 100))
                value = {"ok": True, "events": events, "next_cursor": events[-1]["id"] if events else int(query.get("after", 0))}
            elif path == "/api/health":
                value = engine.health()
            elif path == "/api/metrics":
                value = engine.metrics()
            elif path == "/api/skills":
                value = {"ok": True, "skills": engine.registry.catalog(engine.adapter)}
            elif path == "/api/scenarios":
                value = scenarios.catalog()
            elif path == "/api/workflow/schema":
                from .workflow import workflow_schema
                value = {"ok": True, "schema": workflow_schema()}
            elif path == "/api/queue":
                value = engine.scheduler.snapshot()
            elif path == "/api/templates":
                value = {"ok": True, "templates": engine.store.templates()}
            elif path == "/api/memory":
                value = engine.memory_service.query(**{k: v for k, v in query.items() if k in {"object_name", "target", "outcome", "since", "until", "limit"}})
            elif path == "/api/lifecycle":
                value = {"ok": True, **engine.lifecycle.snapshot()}
            elif path == "/api/data/backups":
                value = engine.data.list_backups()
            elif path == "/api/data/policy":
                value = engine.data.policy()
            elif path == "/api/voice/capabilities":
                value = voice.capabilities()
            elif match := re.fullmatch(r"/api/data/backups/(backup-[0-9a-f]{32})/manifest", path):
                engine.data.verify(match[1])
                self.send_data(200, engine.data._path(match[1]).with_suffix(".json").read_bytes(),
                               "application/json; charset=utf-8", match[1] + ".json")
                return
            elif match := re.fullmatch(r"/api/data/backups/(backup-[0-9a-f]{32})/download", path):
                engine.data.verify(match[1])
                self.send_data(200, engine.data._path(match[1]).read_bytes(), "application/vnd.sqlite3", match[1] + ".sqlite3")
                return
            elif match := re.fullmatch(r"/api/missions/([a-zA-Z0-9]{1,64})(/export)?", path):
                self.send_data(200, engine.mission_detail(match[1]), filename=f"mission-{match[1]}.json" if match[2] else None)
                return
            elif path == "/" or path[1:] in STATIC:
                name = "index.html" if path == "/" else path[1:]
                mime = {"html": "text/html", "css": "text/css", "js": "text/javascript"}[name.rsplit(".", 1)[1]]
                self.send_data(200, files("robot_voice_patrol").joinpath("web", name).read_bytes(), mime + "; charset=utf-8")
                return
            else:
                self.send_data(404, {"ok": False, "message": "路径不存在"})
                return
            self.send_data(200, value)

        def read_body(self, limit, *, empty=False):
            self.connection.settimeout(10)
            if self.headers.get("Transfer-Encoding"):
                raise CommandError("不支持分块请求")
            length = int(self.headers.get("Content-Length", "0"))
            if empty and length == 0:
                return b"{}"
            if not 0 < length <= limit:
                raise CommandError(f"请求大小应在 1 到 {limit} 字节之间")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise CommandError("请求不完整")
            return raw

        def do_POST(self):
            self.guarded(self.write_route)

        do_PUT = do_POST
        do_DELETE = do_POST

        def write_route(self):
            path = urlsplit(self.path).path
            if path == "/api/voice/transcribe" and self.command == "POST":
                if self.headers.get_content_type() not in {"audio/wav", "audio/x-wav", "audio/wave"}:
                    self.send_data(415, {"ok": False, "message": "请上传 PCM WAV 音频"})
                    return
                from .audio import transcribe_wav_bytes
                self.send_data(200, transcribe_wav_bytes(self.read_body(AUDIO_LIMIT), os.environ.get("VOICE_PATROL_VOSK_MODEL", "")))
                return
            if (match := re.fullmatch(r"/api/voice/sessions/([a-zA-Z0-9_-]{1,128})/chunk", path)) and self.command == "POST":
                if self.headers.get_content_type() not in {"audio/pcm", "application/octet-stream"}:
                    self.send_data(415, {"ok": False, "message": "流式输入必须为原始 PCM16LE"})
                    return
                value = voice.append_chunk(match[1], self.read_body(32000), int(self.headers.get("X-Audio-Sequence", "-1")))
                self.send_data(200, value)
                return
            if self.command != "DELETE" and self.headers.get_content_type() != "application/json":
                self.send_data(415, {"ok": False, "message": "请使用 application/json"})
                return
            body = json.loads(self.read_body(JSON_LIMIT, empty=self.command == "DELETE"), object_pairs_hook=_unique_json_pairs,
                              parse_constant=lambda value: (_ for _ in ()).throw(ValueError("不允许非有限 JSON 数值")))
            if not isinstance(body, dict):
                raise CommandError("请求必须是 JSON 对象")
            method = self.command
            if path == "/api/config" and method == "PUT":
                value = engine.update_config(body.get("config", body))
            elif (match := re.fullmatch(r"/api/queue/([a-zA-Z0-9]{1,64})", path)) and method == "PUT":
                value = engine.scheduler.update(match[1], body)
            elif path == "/api/data/policy" and method == "PUT":
                value = engine.data.set_policy(body)
            elif match := re.fullmatch(r"/api/templates/([a-zA-Z0-9]{1,64})", path):
                if method == "PUT":
                    value = engine.save_template(body, match[1])
                elif method == "DELETE":
                    engine.store.delete_template(match[1])
                    value = {"ok": True}
                else:
                    return self.send_data(405, {"ok": False, "message": "该路径仅支持 PUT / DELETE"})
            elif (match := re.fullmatch(r"/api/voice/sessions/([a-zA-Z0-9_-]{1,128})", path)) and method == "DELETE":
                value = voice.cancel_session(match[1])
            elif method != "POST":
                return self.send_data(405, {"ok": False, "message": "该路径不支持该方法"})
            elif path == "/api/assistive/action":
                value = engine.assistive.action(body)
            elif path == "/api/command":
                value = engine.submit(body.get("text", ""), body.get("request_id"), body.get("session_id", "default"))
            elif path == "/api/plan":
                value = engine.preview(body.get("text", ""), body.get("session_id", "default"))
            elif path == "/api/preflight":
                if set(body) - {"workflow", "plan", "parameters"}:
                    raise CommandError("预检只接受 workflow/plan 和 parameters")
                with engine._condition:
                    config = copy.deepcopy(engine.config)
                    busy = engine._worker is not None and engine._worker.is_alive()
                value = inspect_plan(structured_plan(body, config), config, engine.adapter, engine.lifecycle.snapshot(), busy=busy)
            elif path == "/api/scenarios/run":
                with engine._condition:
                    config = copy.deepcopy(engine.config)
                value = scenarios.run(body, config)
            elif path == "/api/control":
                if not isinstance(body.get("action"), str):
                    raise CommandError("action 必须是字符串")
                value = engine.control(body["action"])
            elif path in {"/api/workflow/preview", "/api/workflow/submit"}:
                plan = engine.plan_input({"workflow": body.get("workflow"), "parameters": body.get("parameters")})
                value = {"ok": True, "plan": plan.to_dict()} if path.endswith("preview") else engine.submit_structured(plan, request_id=body.get("request_id"), session_id=body.get("session_id", "workflow"))
            elif path == "/api/queue":
                value = engine.enqueue(body)
            elif path == "/api/queue/control":
                value = engine.scheduler.control(body.get("action"))
            elif match := re.fullmatch(r"/api/queue/([a-zA-Z0-9]{1,64})/cancel", path):
                value = engine.scheduler.cancel(match[1])
            elif path == "/api/templates":
                value = engine.save_template(body)
            elif match := re.fullmatch(r"/api/templates/([a-zA-Z0-9]{1,64})/run", path):
                value = engine.run_template(match[1], body)
            elif path == "/api/recovery":
                if body.get("action") != "dismiss" or not isinstance(body.get("mission_id"), str):
                    raise CommandError("请提供 mission_id 和 action:dismiss")
                value = engine.dismiss_recovery(body["mission_id"])
            elif path == "/api/recovery/preview":
                value = engine.preview_recovery(body.get("mission_id"))
            elif path == "/api/recovery/resume":
                value = engine.resume_recovery(body.get("mission_id"), confirmed=body.get("confirmed"), request_id=body.get("request_id"))
            elif path == "/api/lifecycle":
                value = engine.lifecycle_transition(body.get("action"))
            elif path == "/api/data/backup":
                value = engine.data.backup()
            elif path == "/api/data/verify":
                value = engine.data.verify(body.get("backup_id"))
            elif path == "/api/data/restore":
                value = engine.data.stage_restore(body.get("backup_id"), body.get("confirmed"))
            elif path == "/api/data/archive":
                value = engine.data.archive(body.get("before"), body.get("states"), body.get("dry_run", True))
            elif path == "/api/data/unarchive":
                value = {"ok": True, "count": engine.store.unarchive(body.get("mission_ids"))}
            elif path == "/api/voice/sessions":
                value = voice.create_session(body.get("sample_rate", 16000), body.get("channels", 1), body.get("provider"))
            elif match := re.fullmatch(r"/api/voice/sessions/([a-zA-Z0-9_-]{1,128})/(finish|correct)", path):
                value = voice.finish_session(match[1]) if match[2] == "finish" else voice.correct_segment(match[1], body.get("segment_id"), body.get("text"))
            elif path == "/api/voice/synthesize":
                value = voice.synthesize(body.get("text"), body.get("language", "zh-CN"))
            else:
                return self.send_data(404, {"ok": False, "message": "路径不存在"})
            self.send_data(200 if value.get("ok", True) else 400, value)

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def server_close(self):
            voice.close()
            super().server_close()

    return Server((host, port), Handler)


def serve(engine, host="127.0.0.1", port=8773):
    server = create_server(engine, host, port)
    try:
        server.serve_forever(poll_interval=.2)
    finally:
        server.server_close()
