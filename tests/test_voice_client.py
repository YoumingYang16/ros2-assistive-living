"""Microphone boundary tests use fakes; no device, model or network is opened."""
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from robot_voice_patrol import voice_client as voice


class FakeRecognizer:
    def AcceptWaveform(self, data):
        return True

    def Result(self):
        return json.dumps({"text": "去 会议室 然后 返回 起点"}, ensure_ascii=False)


class FakeSoundDevice:
    active = False
    opened = 0
    closed = 0

    def RawInputStream(self, **kwargs):
        device = self

        class Stream:
            def __enter__(self):
                device.active = True
                device.opened += 1
                kwargs["callback"](b"audio", 1600, None, False)
                return self

            def __exit__(self, *exc):
                device.active = False
                device.closed += 1

        return Stream()


class VoiceClientTests(unittest.TestCase):
    def test_recognized_phrase_closes_stream_before_confirmation_and_http(self):
        sd = FakeSoundDevice()
        sent = []

        def confirm(prompt):
            self.assertFalse(sd.active)
            self.assertEqual(sd.closed, 1)
            return "y"

        def send(url, text):
            self.assertFalse(sd.active)
            sent.append(text)
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            voice.run_voice_loop(sd, FakeRecognizer, url="http://127.0.0.1:8765",
                                 confirm=confirm, send=send, output=lambda _: None)
        self.assertEqual(sent, ["去会议室然后返回起点"])

    def test_cancelled_confirmation_does_not_send(self):
        sd = FakeSoundDevice()
        confirmations = iter(["", KeyboardInterrupt()])

        def confirm(prompt):
            self.assertFalse(sd.active)
            item = next(confirmations)
            if isinstance(item, BaseException):
                raise item
            return item

        def send(*args):
            self.fail("Cancelled transcript must never be sent")

        with self.assertRaises(KeyboardInterrupt):
            voice.run_voice_loop(sd, FakeRecognizer, url="http://127.0.0.1:8765",
                                 confirm=confirm, send=send, output=lambda _: None)
        self.assertEqual(sd.opened, 2)
        self.assertEqual(sd.closed, 2)

    def test_audio_overflow_invalidates_phrase_instead_of_dropping_prefix(self):
        audio = voice.AudioBuffer(capacity=1)
        audio.callback(b"first", 1, None, False)
        audio.callback(b"second", 1, None, False)
        self.assertTrue(audio.invalid.is_set())
        self.assertEqual(audio.blocks.qsize(), 1)

    def test_stream_is_closed_on_stale_audio(self):
        sd = FakeSoundDevice()
        # deadline, callback timestamp, while condition, consumed-block age
        with patch.object(voice.time, "monotonic", side_effect=[0, 0, 0, 2]):
            with self.assertRaises(voice.VoiceCaptureError):
                voice.record_phrase(sd, FakeRecognizer(), max_audio_age=1)
        self.assertFalse(sd.active)
        self.assertEqual(sd.closed, 1)

    def test_stream_is_closed_on_interrupted_recognizer(self):
        class Interrupted(FakeRecognizer):
            def AcceptWaveform(self, data):
                raise KeyboardInterrupt

        sd = FakeSoundDevice()
        with self.assertRaises(KeyboardInterrupt):
            voice.record_phrase(sd, Interrupted())
        self.assertFalse(sd.active)
        self.assertEqual(sd.closed, 1)

    def test_transport_timeout_is_never_retried(self):
        with patch.object(voice, "urlopen", side_effect=TimeoutError) as opener:
            with self.assertRaisesRegex(RuntimeError, "不会自动重试"):
                voice.send_command("http://127.0.0.1:8765", "开始巡逻两圈")
        opener.assert_called_once()

    def test_http_payload_preserves_chinese(self):
        class Response(io.BytesIO):
            pass

        with patch.object(voice, "urlopen", return_value=Response(b'{"ok":true,"message":"accepted"}')) as opener:
            result = voice.send_command("http://127.0.0.1:8765/", "去会议室")
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8765/api/command")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["text"], "去会议室")
        self.assertTrue(payload["request_id"])
        self.assertEqual(uuid.UUID(payload["request_id"]).hex, payload["request_id"])
        self.assertTrue(payload["session_id"].startswith("offline-voice-"))
        self.assertTrue(result["ok"])

    def test_server_url_rejects_credentials_and_query(self):
        for url in ["file:///tmp/data", "http://u:p@localhost", "http://localhost?secret=1"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                voice.server_url(url)

    def test_wake_prefix_only_filters_tasks_and_preserves_stop_priority(self):
        self.assertEqual(voice.command_from_transcript("行知，去会议室", "行知"), ("去会议室", False))
        self.assertIsNone(voice.command_from_transcript("去会议室", "行知"))
        self.assertEqual(voice.command_from_transcript("停止", "行知"), ("停止", True))
        self.assertIsNone(voice.command_from_transcript("不要停止", "行知"))

    def test_stop_only_rejects_mixed_and_negated_commands(self):
        for text in ["不要停止", "去会议室然后停止", "如果到了就停止", "开始巡逻"]:
            with self.subTest(text=text):
                self.assertIsNone(voice.command_from_transcript(text, stop_only=True))
        self.assertEqual(voice.command_from_transcript("停止任务！", stop_only=True), ("停止任务！", True))

    def test_stop_uses_control_endpoint(self):
        with patch.object(voice, "urlopen", return_value=io.BytesIO(b'{"ok":true}')) as opener:
            voice.send_command("http://127.0.0.1:8765", "停止任务")
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8765/api/control")
        self.assertEqual(json.loads(request.data), {"action": "stop"})

    def test_stop_is_sent_before_manual_confirmation(self):
        class StopRecognizer(FakeRecognizer):
            def Result(self):
                return json.dumps({"text": "停止"})

        def forbidden_confirm(_):
            self.fail("Complete stop commands must not wait for manual confirmation")

        def stop_after_send(url, text):
            self.assertEqual(text, "停止")
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            voice.run_voice_loop(FakeSoundDevice(), StopRecognizer, url="http://127.0.0.1:8765",
                                 confirm=forbidden_confirm, send=stop_after_send, output=lambda _: None)

    def test_wav_transcribe_only_never_sends_even_recognized_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            wav = Path(directory) / "command.wav"
            wav.write_bytes(b"test fixture decoded by fake")
            with patch("robot_voice_patrol.audio.transcribe_wav_bytes", return_value={"ok": True, "text": "停止", "simulated": False}), patch.object(voice, "send_command") as sender, redirect_stdout(io.StringIO()):
                result = voice.main(["--model", directory, "--wav", str(wav), "--transcribe-only"])
            self.assertEqual(result, 0)
            sender.assert_not_called()


if __name__ == "__main__":
    unittest.main()
