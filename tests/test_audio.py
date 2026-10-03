"""Bounded WAV parsing and recognizer lifecycle tests; no external model needed."""
from array import array
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

from robot_voice_patrol import audio


def wav_bytes(frames=None, *, channels=1, width=2, rate=16000, count=160):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(width)
        stream.setframerate(rate)
        stream.writeframes(frames if frames is not None else b"\0" * count * width * channels)
    return buffer.getvalue()


class AudioTests(unittest.TestCase):
    def test_native_pcm_roundtrip_does_not_alter_samples(self):
        frames = struct.pack("<4h", -32768, -1, 0, 32767)
        parsed = audio.validate_wav(wav_bytes(frames))
        self.assertEqual(audio.pcm16_mono(parsed), frames)
        self.assertEqual(parsed.frame_count, 4)

    def test_stereo_8bit_is_centered_and_mixed(self):
        parsed = audio.validate_wav(wav_bytes(bytes([0, 255, 128, 128]), channels=2, width=1))
        self.assertEqual(struct.unpack("<2h", audio.pcm16_mono(parsed)), (-128, 0))

    def test_24bit_signed_extremes_are_preserved(self):
        frames = (-8388608).to_bytes(3, "little", signed=True) + (8388607).to_bytes(3, "little", signed=True)
        parsed = audio.validate_wav(wav_bytes(frames, width=3))
        self.assertEqual(struct.unpack("<2h", audio.pcm16_mono(parsed)), (-32768, 32767))

    def test_32bit_signed_conversion(self):
        parsed = audio.validate_wav(wav_bytes(struct.pack("<2i", -2147483648, 2147483647), width=4))
        self.assertEqual(struct.unpack("<2h", audio.pcm16_mono(parsed)), (-32768, 32767))

    def test_resampling_preserves_duration(self):
        parsed = audio.validate_wav(wav_bytes(rate=22050, count=22050))
        self.assertEqual(len(audio.pcm16_mono(parsed)), 32000)

    def test_rejects_non_wav_before_model_import(self):
        with patch.object(audio.importlib, "import_module") as importer:
            with self.assertRaises(audio.AudioValidationError):
                audio.transcribe_wav_bytes(b"not a wav", "unused")
        importer.assert_not_called()

    def test_rejects_oversized_upload_before_parsing(self):
        with self.assertRaisesRegex(audio.AudioValidationError, "10 MiB"):
            audio.validate_wav(b"0" * (audio.MAX_WAV_BYTES + 1))

    def test_rejects_audio_longer_than_limit(self):
        with self.assertRaisesRegex(audio.AudioValidationError, "60 秒"):
            audio.validate_wav(wav_bytes(rate=8000, count=8000 * 61))

    def test_allows_exact_duration_limit(self):
        self.assertEqual(audio.validate_wav(wav_bytes(rate=8000, count=8000 * 60)).duration, 60)

    def test_rejects_truncated_riff(self):
        with self.assertRaisesRegex(audio.AudioValidationError, "不完整"):
            audio.validate_wav(wav_bytes()[:-2])

    def test_rejects_truncated_data_even_if_riff_length_corrected(self):
        raw = bytearray(wav_bytes()[:-2])
        raw[4:8] = (len(raw) - 8).to_bytes(4, "little")
        with self.assertRaisesRegex(audio.AudioValidationError, "不完整"):
            audio.validate_wav(bytes(raw))

    def test_rejects_empty_audio(self):
        with self.assertRaisesRegex(audio.AudioValidationError, "没有音频帧"):
            audio.validate_wav(wav_bytes(b""))

    def test_rejects_multichannel_and_unsupported_rate(self):
        for data in [wav_bytes(channels=3), wav_bytes(rate=4000), wav_bytes(rate=192000)]:
            with self.subTest(size=len(data)), self.assertRaises(audio.AudioValidationError):
                audio.validate_wav(data)

    def test_missing_model_is_reported_without_download(self):
        with patch.object(audio.importlib, "import_module") as importer:
            with self.assertRaises(audio.AudioUnavailableError):
                audio.transcribe_wav_bytes(wav_bytes(), None)
        importer.assert_not_called()

    def test_bounded_workers_reject_without_waiting(self):
        slots = threading.BoundedSemaphore(1)
        slots.acquire()
        with patch.object(audio, "_TRANSCRIPTION_SLOTS", slots):
            with self.assertRaisesRegex(audio.AudioUnavailableError, "忙"):
                audio.transcribe_wav_bytes(wav_bytes(), "unused")
        slots.release()

    def test_actual_recognizer_result_assembled_and_normalized(self):
        class Recognizer:
            def __init__(self, model, rate):
                self.rate = rate
            def SetWords(self, enabled):
                pass
            def AcceptWaveform(self, data):
                return False
            def FinalResult(self):
                return json.dumps({"text": "去 会议室 然后 返回 起点", "result": [{"word": "会议室", "conf": .9, "start": 0.2, "end": 1}]})

        class Vosk:
            KaldiRecognizer = Recognizer

        with patch.object(audio, "_get_model", return_value=(Vosk, object())):
            result = audio.transcribe_wav_bytes(wav_bytes(), "unused")
        self.assertEqual(result["text"], "去会议室然后返回起点")
        self.assertFalse(result["simulated"])
        self.assertEqual(result["words"][0]["word"], "会议室")

    def test_model_cache_initializes_once_under_concurrent_access(self):
        created = []
        class Vosk:
            @staticmethod
            def Model(model_path):
                model = object()
                created.append(model)
                return model

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(audio.importlib, "import_module", return_value=Vosk), patch.object(audio, "_MODEL_CACHE", audio.OrderedDict()):
                with ThreadPoolExecutor(max_workers=4) as executor:
                    results = list(executor.map(lambda _: audio._get_model(directory)[1], range(8)))
        self.assertEqual(len(created), 1)
        self.assertTrue(all(model is results[0] for model in results))


if __name__ == "__main__":
    unittest.main()
