"""Optional offline Chinese microphone client; importing this module records nothing.

Install requirements-voice.txt and supply an already downloaded local Vosk model.
Each utterance uses its own stream. The microphone is CLOSED during confirmation
and HTTP requests, so a delayed reply can never release queued spoken commands.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import queue
import re
import sys
import threading
import time
import uuid
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

_CLIENT_SESSION_ID = "offline-voice-" + uuid.uuid4().hex


class VoiceCaptureError(RuntimeError):
    """Reject an incomplete or stale utterance rather than execute part of it."""


def device_argument(value: str) -> int | str:
    try:
        return int(value)
    except ValueError:
        return value


def normalize_transcript(text: str) -> str:
    """Vosk Chinese models may place spaces between Chinese words."""
    return re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", text).strip()


def server_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("--url 必须为不含账号、查询参数的 HTTP 或 HTTPS 服务地址")
    return value.rstrip("/")


def is_stop_command(text: str) -> bool:
    normalized = re.sub(r"[\s，。！？,.!?]", "", text)
    return normalized in {"停止", "停下", "停一下", "停止任务", "取消任务", "紧急停止"}


def command_from_transcript(text: str, wake_word: str = "", stop_only: bool = False) -> tuple[str, bool] | None:
    """Only a complete stop command bypasses the optional textual wake prefix.

    This is transcript prefix filtering, not an always-on acoustic wake-word model.
    Negated phrases such as '不要停止' never match the priority control path.
    """
    text = normalize_transcript(text)
    if is_stop_command(text):
        return text, True
    if stop_only:
        return None
    wake_word = normalize_transcript(wake_word)
    if wake_word:
        if not text.casefold().startswith(wake_word.casefold()):
            return None
        text = text[len(wake_word):].lstrip(" ，。,:：")
    if not text:
        return None
    return text, is_stop_command(text)


def send_command(url: str, text: str, timeout: float = 8.0, request_id: str | None = None) -> dict[str, Any]:
    """Send once only: a transport timeout must not retry a possibly accepted task."""
    url = server_url(url)
    payload = {"text": text, "request_id": request_id or uuid.uuid4().hex, "session_id": _CLIENT_SESSION_ID}
    endpoint = "/api/command"
    if is_stop_command(text):
        endpoint, payload = "/api/control", {"action": "stop"}
    request = Request(url + endpoint, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                      headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(1_048_577)
        if len(raw) > 1_048_576:
            raise RuntimeError("服务响应过大，未解析；请在控制台检查任务状态")
        result = json.loads(raw.decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("响应不是 JSON 对象")
        if result.get("ok") is not True:
            raise RuntimeError(str(result.get("message") or "服务未确认接收指令"))
        return result
    except HTTPError as exc:
        try:
            result = json.loads(exc.read(65536).decode("utf-8"))
            message = result.get("message", f"HTTP {exc.code}") if isinstance(result, dict) else f"HTTP {exc.code}"
        except (ValueError, UnicodeError):
            message = f"HTTP {exc.code}"
        finally:
            exc.close()
        raise RuntimeError(f"服务拒绝指令：{message}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("服务连接失败或超时。不会自动重试；请先在网页控制台确认任务状态。") from exc
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError("服务返回无效数据。请在网页控制台确认任务状态后再继续。") from exc


class AudioBuffer:
    """Bounded audio queue. Dropped audio invalidates the entire utterance."""

    def __init__(self, capacity: int = 12):
        self.blocks: queue.Queue[tuple[float, bytes]] = queue.Queue(maxsize=capacity)
        self.invalid = threading.Event()

    def callback(self, indata, frames, timing, status) -> None:
        if status:
            self.invalid.set()
            return
        if self.invalid.is_set():
            return
        try:
            self.blocks.put_nowait((time.monotonic(), bytes(indata)))
        except queue.Full:
            self.invalid.set()


def record_phrase(sd, recognizer, *, device=None, samplerate: int = 16000,
                  listen_seconds: float = 20.0, max_audio_age: float = 1.0,
                  on_partial: Callable[[str], Any] | None = None) -> str | None:
    """Record one complete utterance. Stream cleanup also occurs on Ctrl+C."""
    audio = AudioBuffer()
    deadline = time.monotonic() + listen_seconds
    previous_partial = ""
    with sd.RawInputStream(samplerate=samplerate, blocksize=max(800, int(samplerate * .1)),
                           device=device, dtype="int16", channels=1, callback=audio.callback):
        while time.monotonic() < deadline:
            if audio.invalid.is_set():
                raise VoiceCaptureError("音频缓冲溢出或设备丢帧，本段语音已丢弃，请重新说完整指令。")
            try:
                captured_at, data = audio.blocks.get(timeout=.2)
            except queue.Empty:
                continue
            if time.monotonic() - captured_at > max_audio_age:
                raise VoiceCaptureError("音频处理延迟过高，本段语音已丢弃，请重新说完整指令。")
            complete = recognizer.AcceptWaveform(data)
            if audio.invalid.is_set():
                raise VoiceCaptureError("音频不完整，本段语音已丢弃，请重试。")
            if complete:
                result = json.loads(recognizer.Result())
                text = normalize_transcript(str(result.get("text", "")))
                if text:
                    return text
            elif on_partial is not None:
                partial = normalize_transcript(str(json.loads(recognizer.PartialResult()).get("partial", "")))
                if partial and partial != previous_partial:
                    previous_partial = partial
                    on_partial(partial)
    # Do not send FinalResult(): a timeout may have cut off a crucial qualifier.
    return None


def run_voice_loop(sd, recognizer_factory: Callable[[], Any], *, url: str, device=None,
                   samplerate: int = 16000, auto_send: bool = False, wake_word: str = "", stop_only: bool = False,
                   confirm: Callable[[str], str] = input,
                   send: Callable[..., dict] = send_command,
                   output: Callable[[str], Any] = print) -> None:
    output("离线中文语音客户端就绪。按 Ctrl+C 退出；语音识别模型不会访问云端。")
    output("自动发送已启用：完整识别的指令会立即发送。" if auto_send else "任务识别后需输入 y 确认；确认期间麦克风已关闭。完整停止指令会优先发送。")
    if wake_word:
        output(f"启用文字前缀过滤：任务须以“{wake_word}”开头；完整停止指令不受此前缀限制。")
    if stop_only:
        output("当前仅接收完整停止指令，其他语音不会发送。")
    while True:
        output("请说一条完整指令（例如：去会议室然后返回起点）…")
        try:
            text = record_phrase(sd, recognizer_factory(), device=device, samplerate=samplerate,
                                 on_partial=lambda value: output("识别中（未执行）：" + value))
        except VoiceCaptureError as exc:
            output(str(exc))
            continue
        if not text:
            output("本轮未获得完整语音，请重说。")
            continue
        output(f"识别文字：{text}")
        parsed = command_from_transcript(text, wake_word, stop_only)
        if parsed is None:
            output("未匹配当前前缀或命令模式，未发送。")
            continue
        text, priority = parsed
        if not auto_send and not priority and confirm("发送这条指令？输入 y 确认，其他输入取消 [y/N]：").strip().lower() not in {"y", "yes", "是"}:
            output("已取消本条指令。")
            continue
        try:
            result = send(url, text)
        except RuntimeError as exc:
            output(str(exc))
        else:
            output("服务反馈：" + str(result.get("message", "指令已接收")))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="行知：使用本地 Vosk 中文模型识别麦克风语音并发送任务。")
    parser.add_argument("--model", type=Path, help="已下载并解压的本地 Vosk 中文模型目录；不会自动下载")
    parser.add_argument("--url", default="http://127.0.0.1:8773", help="巡逻服务地址（默认：http://127.0.0.1:8773）")
    parser.add_argument("--list-devices", action="store_true", help="列出音频设备后退出，不启动录音")
    parser.add_argument("--device", type=device_argument, help="输入设备编号或名称片段")
    parser.add_argument("--samplerate", type=int, help="采样率，默认使用输入设备采样率")
    parser.add_argument("--auto-send", action="store_true", help="识别后直接发送，跳过终端确认（默认关闭）")
    parser.add_argument("--wav", type=Path, help="识别本地 PCM WAV 文件，不打开麦克风")
    parser.add_argument("--transcribe-only", action="store_true", help="只输出 WAV 识别结果，不发送任务")
    parser.add_argument("--wake-word", default="", help="可选任务文字前缀过滤，例如：行知；完整停止指令优先")
    parser.add_argument("--stop-only", action="store_true", help="仅允许完整停止指令，不执行导航等任务")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.list_devices and (args.model is None or not args.model.is_dir()):
        parser.error("请用 --model 指定已解压的本地 Vosk 中文模型目录；使用 --list-devices 可先检查设备")
    try:
        url = server_url(args.url)
    except ValueError as exc:
        parser.error(str(exc))
    if args.transcribe_only and not args.wav:
        parser.error("--transcribe-only 需要同时指定 --wav")
    if args.wav:
        from .audio import MAX_WAV_BYTES, transcribe_wav_bytes
        try:
            if args.wav.stat().st_size > MAX_WAV_BYTES:
                raise ValueError("WAV 不能超过 10 MiB")
            result = transcribe_wav_bytes(args.wav.read_bytes(), args.model)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if args.transcribe_only or not result.get("text"):
                return 0
            parsed = command_from_transcript(result["text"], args.wake_word, args.stop_only)
            if parsed is None:
                print("未匹配当前前缀或命令模式，未发送。")
                return 0
            text, priority = parsed
            if args.auto_send or input("发送这条指令？输入 y 确认 [y/N]：").strip().lower() in {"y", "yes", "是"}:
                print("服务反馈：" + str(send_command(url, text).get("message", "指令已接收")))
            else:
                print("已取消本条指令。")
            return 0
        except (KeyboardInterrupt, EOFError):
            print("\n已取消，未继续发送。")
            return 0
        except Exception as exc:
            print(f"WAV 识别失败：{exc}", file=sys.stderr)
            return 1
    try:
        import sounddevice as sd
    except (ImportError, OSError) as exc:
        print("音频依赖不可用。请安装 requirements-voice.txt，并检查系统 PortAudio 音频支持。", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 2
    try:
        if args.list_devices:
            print(sd.query_devices())
            return 0
        try:
            from vosk import KaldiRecognizer, Model
        except ImportError:
            print("缺少 Vosk，请安装 requirements-voice.txt。", file=sys.stderr)
            return 2
        samplerate = args.samplerate or int(sd.query_devices(args.device, "input")["default_samplerate"])
        if not 8000 <= samplerate <= 192000:
            parser.error("采样率应在 8000 到 192000 Hz 之间")
        sd.check_input_settings(device=args.device, channels=1, dtype="int16", samplerate=samplerate)
        model = Model(model_path=str(args.model.resolve()))
        run_voice_loop(sd, lambda: KaldiRecognizer(model, samplerate), url=url, device=args.device,
                       samplerate=samplerate, auto_send=args.auto_send, wake_word=args.wake_word, stop_only=args.stop_only)
    except (KeyboardInterrupt, EOFError):
        print("\n语音客户端已退出，麦克风流已关闭。")
        return 0
    except Exception as exc:
        print(f"语音客户端无法继续：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
