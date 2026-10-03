"""Bounded local streaming ASR sessions. Transcripts never execute robot tasks."""
from __future__ import annotations

from dataclasses import dataclass, field
import importlib.util
import json
from pathlib import Path
import re
import threading
import time
from typing import Protocol
import uuid

from .audio import AudioUnavailableError, _get_model


class VoiceSessionError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class StreamingRecognizer(Protocol):
    def accept(self, pcm: bytes) -> dict: ...
    def finish(self) -> dict: ...
    def close(self) -> None: ...


class ASRProvider(Protocol):
    name: str
    def available(self) -> bool: ...
    def create_recognizer(self, sample_rate: int) -> StreamingRecognizer: ...


class TTSProvider(Protocol):
    name: str
    def synthesize(self, text: str, language: str = "zh-CN") -> dict: ...


def normalize(text) -> str:
    return re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", str(text)).strip()


class VoskStream:
    def __init__(self, recognizer):
        self.recognizer = recognizer

    def accept(self, pcm: bytes) -> dict:
        if self.recognizer.AcceptWaveform(pcm):
            data = json.loads(self.recognizer.Result())
            return {"is_final": True, "text": normalize(data.get("text", "")), "words": data.get("result", [])}
        return {"is_final": False, "partial": normalize(json.loads(self.recognizer.PartialResult()).get("partial", ""))}

    def finish(self) -> dict:
        data = json.loads(self.recognizer.FinalResult())
        return {"is_final": True, "text": normalize(data.get("text", "")), "words": data.get("result", [])}

    def close(self):
        self.recognizer = None


class VoskASRProvider:
    name = "vosk"
    local = True
    simulated = False

    def __init__(self, model_path=None):
        self.model_path = model_path

    def available(self) -> bool:
        return bool(self.model_path and Path(self.model_path).is_dir() and importlib.util.find_spec("vosk"))

    def create_recognizer(self, sample_rate: int):
        vosk, model = _get_model(self.model_path)
        recognizer = vosk.KaldiRecognizer(model, sample_rate)
        recognizer.SetWords(True)
        return VoskStream(recognizer)


class BrowserTTSProvider:
    """Returns a bounded playback instruction, executed by browser speechSynthesis."""
    name = "browser"

    def synthesize(self, text: str, language: str = "zh-CN") -> dict:
        return {"ok": True, "provider": self.name, "text": text, "language": language,
                "playback": "browser_speech_synthesis", "audio_generated": False}


@dataclass
class _Session:
    recognizer: StreamingRecognizer
    provider: str
    created: float
    touched: float
    simulated: bool = False
    local: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)
    next_sequence: int = 0
    byte_count: int = 0
    state: str = "listening"
    partial: str = ""
    segments: list = field(default_factory=list)
    corrections: list = field(default_factory=list)


class VoiceSessionService:
    MAX_SESSIONS = 4
    MAX_SECONDS = 60
    MAX_CHUNK_BYTES = 32000
    IDLE_TTL = 120
    MAX_RETAINED = 64

    def __init__(self, model_path=None, asr_provider=None, tts_provider=None, *, clock=time.monotonic):
        self.asr = asr_provider or VoskASRProvider(model_path)
        self.tts = tts_provider or BrowserTTSProvider()
        self._clock = clock
        self._lock = threading.RLock()
        self._sessions: dict[str, _Session] = {}

    def capabilities(self) -> dict:
        return {"ok": True, "asr": [{"id": self.asr.name, "available": self.asr.available(),
                                      "local": getattr(self.asr, "local", False) is True,
                                      "simulated": getattr(self.asr, "simulated", False) is True, "streaming": True},
                                     {"id": "browser", "available": "browser_dependent", "local": False, "streaming": True}],
                "tts": [{"id": self.tts.name, "playback": "browser" if self.tts.name == "browser" else "provider"}],
                "sample_rate": 16000, "channels": 1, "encoding": "PCM16LE", "max_seconds": self.MAX_SECONDS,
                "max_chunk_bytes": self.MAX_CHUNK_BYTES, "max_sessions": self.MAX_SESSIONS,
                "segmentation": "recognizer_endpoint", "corrections": True, "task_execution": False}

    def _prune(self):
        now = self._clock()
        for identifier, session in list(self._sessions.items()):
            if now - session.touched > self.IDLE_TTL:
                with session.lock:
                    session.recognizer.close()
                    del self._sessions[identifier]
        terminal = [(key, value) for key, value in self._sessions.items() if value.state != "listening"]
        for key, _ in sorted(terminal, key=lambda pair: pair[1].touched)[:max(0, len(self._sessions) - self.MAX_RETAINED)]:
            del self._sessions[key]

    def create_session(self, sample_rate=16000, channels=1, provider=None) -> dict:
        if isinstance(sample_rate, bool) or sample_rate != 16000 or isinstance(channels, bool) or channels != 1:
            raise VoiceSessionError("流式识别仅支持 16000 Hz、单声道 PCM16LE")
        if provider and provider != self.asr.name:
            raise VoiceSessionError("该本地 ASR 提供器未注册")
        with self._lock:
            self._prune()
            if sum(session.state == "listening" for session in self._sessions.values()) >= self.MAX_SESSIONS:
                raise VoiceSessionError("同时进行的语音会话过多，请先结束已有会话", 429)
            recognizer = self.asr.create_recognizer(sample_rate)
            now = self._clock()
            identifier = uuid.uuid4().hex
            self._sessions[identifier] = _Session(recognizer, self.asr.name, now, now,
                simulated=getattr(self.asr, "simulated", False) is True, local=getattr(self.asr, "local", False) is True)
        return {"ok": True, "session_id": identifier, "state": "listening", "next_sequence": 0,
                "sample_rate": 16000, "channels": 1, "max_seconds": self.MAX_SECONDS, "provider": self.asr.name}

    def _lookup(self, identifier) -> _Session:
        with self._lock:
            self._prune()
            session = self._sessions.get(identifier)
            if session is None:
                raise VoiceSessionError("语音会话不存在或已过期", 404)
            return session

    def _result(self, identifier, session, *, is_final=False) -> dict:
        return {"ok": True, "session_id": identifier, "state": session.state, "provider": session.provider,
                "partial": session.partial, "text": "".join(segment["text"] for segment in session.segments),
                "is_final": is_final, "segments": [dict(value) for value in session.segments],
                "corrections": [dict(value) for value in session.corrections], "next_sequence": session.next_sequence,
                "duration_seconds": round(session.byte_count / 32000, 3), "simulated": session.simulated, "local": session.local,
                "requires_confirmation": True}

    def _collect(self, session, result):
        if result.get("is_final"):
            text = normalize(result.get("text", ""))
            if text:
                session.segments.append({"id": f"seg{len(session.segments) + 1:03d}", "text": text,
                                         "revision": 1, "source": session.provider, "corrected": False})
            session.partial = ""
        else:
            session.partial = normalize(result.get("partial", ""))[:2000]

    def append_chunk(self, session_id: str, data: bytes, sequence: int) -> dict:
        if not isinstance(data, bytes) or not data or len(data) % 2 or len(data) > self.MAX_CHUNK_BYTES:
            raise VoiceSessionError("音频块必须为非空 PCM16LE，长度为偶数且不超过 32000 字节")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise VoiceSessionError("X-Audio-Sequence 必须为从 0 开始的整数")
        session = self._lookup(session_id)
        with session.lock:
            if session.state != "listening":
                raise VoiceSessionError("语音会话已结束或取消，不能再上传音频", 409)
            if sequence != session.next_sequence:
                raise VoiceSessionError(f"音频序号不匹配，期望 {session.next_sequence}；重复块不会重新处理", 409)
            if session.byte_count + len(data) > 32000 * self.MAX_SECONDS:
                session.state = "cancelled"; session.segments.clear(); session.partial = ""; session.recognizer.close()
                raise VoiceSessionError("语音超过 60 秒，已取消并丢弃本次会话", 413)
            result = session.recognizer.accept(data)
            session.byte_count += len(data); session.next_sequence += 1; session.touched = self._clock()
            self._collect(session, result)
            return self._result(session_id, session, is_final=bool(result.get("is_final")))

    def finish_session(self, session_id: str) -> dict:
        session = self._lookup(session_id)
        with session.lock:
            if session.state == "cancelled":
                raise VoiceSessionError("已取消的语音会话不会恢复或执行", 409)
            if session.state == "listening":
                self._collect(session, session.recognizer.finish())
                session.state = "finished"; session.recognizer.close(); session.touched = self._clock()
            return self._result(session_id, session, is_final=True)

    def cancel_session(self, session_id: str) -> dict:
        session = self._lookup(session_id)
        with session.lock:
            session.state = "cancelled"; session.segments.clear(); session.corrections.clear(); session.partial = ""
            session.recognizer.close(); session.touched = self._clock()
            return {"ok": True, "session_id": session_id, "state": "cancelled", "text": ""}

    def correct_segment(self, session_id: str, segment_id: str, text: str) -> dict:
        if not isinstance(text, str) or not text.strip() or len(text) > 500:
            raise VoiceSessionError("纠正文字须为 1 到 500 个字符")
        session = self._lookup(session_id)
        with session.lock:
            if session.state == "cancelled":
                raise VoiceSessionError("会话已取消", 409)
            segment = next((item for item in session.segments if item["id"] == segment_id), None)
            if segment is None:
                raise VoiceSessionError("语音片段不存在", 404)
            session.corrections.append({"segment_id": segment_id, "before": segment["text"], "after": text.strip(), "revision": segment["revision"] + 1})
            segment.update(text=text.strip(), revision=segment["revision"] + 1, corrected=True)
            session.touched = self._clock()
            return self._result(session_id, session, is_final=session.state == "finished")

    def synthesize(self, text: str, language="zh-CN") -> dict:
        if not isinstance(text, str) or not text.strip() or len(text) > 1000:
            raise VoiceSessionError("播报文字须为 1 到 1000 个字符")
        if language not in {"zh-CN", "en-US"}:
            raise VoiceSessionError("当前支持 zh-CN 或 en-US 播报")
        return self.tts.synthesize(text.strip(), language)

    def close(self):
        with self._lock:
            for session in self._sessions.values():
                with session.lock:
                    session.state = "cancelled"; session.recognizer.close()
            self._sessions.clear()
