"""Validated, bounded WAV-to-text using a caller-provided local Vosk model.

No model is downloaded. No audio is sent to an external service. The server may
call this module from worker threads; immutable models are cached under a lock,
while each transcription receives its own recognizer and a bounded worker slot.
"""
from __future__ import annotations

from array import array
from collections import OrderedDict
from dataclasses import dataclass
import importlib
import io
import json
import math
from pathlib import Path
import re
import sys
import threading
import wave

MAX_WAV_BYTES = 10 * 1024 * 1024
MAX_WAV_SECONDS = 60.0
TARGET_RATE = 16000
_MODEL_CACHE: OrderedDict[str, object] = OrderedDict()
_MODEL_LOCK = threading.Lock()
_TRANSCRIPTION_SLOTS = threading.BoundedSemaphore(2)


class AudioValidationError(ValueError):
    """The uploaded file is not a supported bounded PCM WAV."""


class AudioUnavailableError(RuntimeError):
    """Offline recognition is not configured or is currently busy."""


@dataclass(frozen=True)
class PCMData:
    frames: bytes
    sample_rate: int
    sample_width: int
    channels: int
    frame_count: int

    @property
    def duration(self) -> float:
        return self.frame_count / self.sample_rate


def validate_wav(data: bytes) -> PCMData:
    if not isinstance(data, bytes):
        raise AudioValidationError("需要 WAV 文件的二进制内容")
    if len(data) > MAX_WAV_BYTES:
        raise AudioValidationError("WAV 文件不能超过 10 MiB")
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise AudioValidationError("仅支持 RIFF PCM WAV 文件；不支持 MP3、WebM 或压缩音频")
    if int.from_bytes(data[4:8], "little") + 8 > len(data):
        raise AudioValidationError("WAV 文件不完整，声明长度大于实际文件")
    try:
        with wave.open(io.BytesIO(data), "rb") as stream:
            channels, width, rate, count = stream.getnchannels(), stream.getsampwidth(), stream.getframerate(), stream.getnframes()
            if stream.getcomptype() != "NONE":
                raise AudioValidationError("只支持未压缩 PCM WAV")
            if channels not in (1, 2):
                raise AudioValidationError("WAV 必须为单声道或双声道")
            if width not in (1, 2, 3, 4):
                raise AudioValidationError("PCM 位深须为 8、16、24 或 32 位整数")
            if not 8000 <= rate <= 96000:
                raise AudioValidationError("WAV 采样率须在 8000 到 96000 Hz 之间")
            if count <= 0:
                raise AudioValidationError("WAV 没有音频帧")
            if count / rate > MAX_WAV_SECONDS:
                raise AudioValidationError("WAV 时长不能超过 60 秒")
            frames = stream.readframes(count)
            if len(frames) != count * width * channels:
                raise AudioValidationError("WAV 音频帧不完整")
    except (wave.Error, EOFError, OSError) as exc:
        raise AudioValidationError(f"无法解析 PCM WAV：{exc}") from exc
    return PCMData(frames, rate, width, channels, count)


def pcm16_mono(pcm: PCMData, target_rate: int = TARGET_RATE) -> bytes:
    """Integer PCM mixing and linear resampling without third-party DSP packages.

    This is a lightweight speech input converter, not a mastering-quality filter.
    Native 16 kHz / mono / 16-bit WAV avoids all conversion.
    """
    if pcm.sample_width == 2 and pcm.channels == 1 and pcm.sample_rate == target_rate:
        return pcm.frames
    samples = array("h")
    width, channels = pcm.sample_width, pcm.channels
    for offset in range(0, len(pcm.frames), width * channels):
        total = 0
        for channel in range(channels):
            raw = pcm.frames[offset + channel * width:offset + (channel + 1) * width]
            value = (raw[0] - 128) << 8 if width == 1 else int.from_bytes(raw, "little", signed=True) >> (8 * (width - 2))
            total += value
        samples.append(max(-32768, min(32767, round(total / channels))))
    if pcm.sample_rate != target_rate:
        output = array("h")
        output_length = max(1, round(len(samples) * target_rate / pcm.sample_rate))
        ratio = pcm.sample_rate / target_rate
        for index in range(output_length):
            source = index * ratio
            low = min(int(source), len(samples) - 1)
            high = min(low + 1, len(samples) - 1)
            fraction = source - low
            output.append(max(-32768, min(32767, round(samples[low] * (1 - fraction) + samples[high] * fraction))))
        samples = output
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def _get_model(model_path):
    if not model_path:
        raise AudioUnavailableError("未配置本地 Vosk 模型；请设置 VOICE_PATROL_VOSK_MODEL 后重启服务")
    path = Path(model_path).expanduser().resolve()
    if not path.is_dir():
        raise AudioUnavailableError("配置的 Vosk 模型目录不存在")
    try:
        vosk = importlib.import_module("vosk")
    except (ImportError, OSError) as exc:
        raise AudioUnavailableError("Vosk 依赖不可用，请安装 requirements-voice.txt") from exc
    key = str(path)
    with _MODEL_LOCK:
        if key in _MODEL_CACHE:
            _MODEL_CACHE.move_to_end(key)
            return vosk, _MODEL_CACHE[key]
        try:
            model = vosk.Model(model_path=key)
        except Exception as exc:
            raise AudioUnavailableError("无法加载本地 Vosk 模型，请检查解压目录与模型完整性") from exc
        _MODEL_CACHE[key] = model
        while len(_MODEL_CACHE) > 2:
            _MODEL_CACHE.popitem(last=False)
        return vosk, model


def _text(value: str) -> str:
    return re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", value).strip()


def transcribe_wav_bytes(data: bytes, model_path) -> dict:
    """Validate WAV before loading a model; return text only, never execute it."""
    pcm = validate_wav(data)
    if not _TRANSCRIPTION_SLOTS.acquire(blocking=False):
        raise AudioUnavailableError("离线语音服务忙，请稍后重试")
    try:
        vosk, model = _get_model(model_path)
        frames = pcm16_mono(pcm)
        recognizer = vosk.KaldiRecognizer(model, TARGET_RATE)
        recognizer.SetWords(True)
        text_parts, words = [], []

        def collect(raw):
            result = json.loads(raw)
            text = str(result.get("text", "")).strip()
            if text:
                text_parts.append(text)
            for item in result.get("result", []):
                if isinstance(item, dict):
                    words.append({key: value for key, value in item.items()
                                  if key in {"word", "start", "end", "conf"}
                                  and (isinstance(value, str) or isinstance(value, (int, float)) and math.isfinite(value))})

        for offset in range(0, len(frames), 8000):
            if recognizer.AcceptWaveform(frames[offset:offset + 8000]):
                collect(recognizer.Result())
        collect(recognizer.FinalResult())
        return {"ok": True, "text": _text(" ".join(text_parts)), "engine": "vosk", "simulated": False,
                "duration_seconds": round(pcm.duration, 3), "sample_rate": TARGET_RATE,
                "original_sample_rate": pcm.sample_rate, "channels": pcm.channels, "words": words}
    finally:
        _TRANSCRIPTION_SLOTS.release()
