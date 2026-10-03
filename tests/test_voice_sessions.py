import unittest
from robot_voice_patrol.voice_sessions import VoiceSessionError, VoiceSessionService


class Recognizer:
    def __init__(self):
        self.calls = 0
        self.closed = False
    def accept(self, pcm):
        self.calls += 1
        return {"is_final": self.calls == 2, "partial": "去 会", "text": "去 会 议 室"}
    def finish(self):
        return {"is_final": True, "text": "然 后 返 回 起 点"}
    def close(self):
        self.closed = True


class Provider:
    name = "fixture"
    simulated = True
    local = True
    def available(self):
        return True
    def create_recognizer(self, rate):
        self.recognizer = Recognizer()
        return self.recognizer


class VoiceSessionTests(unittest.TestCase):
    def setUp(self):
        self.provider = Provider()
        self.now = 0
        self.service = VoiceSessionService(asr_provider=self.provider, clock=lambda: self.now)
        self.addCleanup(self.service.close)
    def create(self):
        return self.service.create_session(provider="fixture")["session_id"]
    def test_ordered_chunks_and_finalization_keep_evidence_and_never_execute(self):
        identifier = self.create()
        first = self.service.append_chunk(identifier, b"\0\0" * 160, 0)
        self.assertEqual("去会", first["partial"])
        self.assertTrue(first["simulated"])
        self.assertTrue(first["local"])
        second = self.service.append_chunk(identifier, b"\0\0" * 160, 1)
        self.assertEqual("去会议室", second["text"])
        final = self.service.finish_session(identifier)
        self.assertEqual("去会议室然后返回起点", final["text"])
        self.assertEqual(final, self.service.finish_session(identifier))
        self.assertTrue(final["requires_confirmation"])
        self.assertFalse(self.service.capabilities()["task_execution"])
        self.assertTrue(self.provider.recognizer.closed)
    def test_out_of_order_or_duplicate_chunk_does_not_feed_recognizer(self):
        identifier = self.create()
        self.service.append_chunk(identifier, b"\0\0", 0)
        for sequence in (0, 2, True, -1):
            with self.assertRaises(VoiceSessionError):
                self.service.append_chunk(identifier, b"\0\0", sequence)
        self.assertEqual(1, self.provider.recognizer.calls)
    def test_cancel_removes_transcript_and_correction_history(self):
        identifier = self.create()
        self.service.append_chunk(identifier, b"\0\0", 0)
        self.service.append_chunk(identifier, b"\0\0", 1)
        fixed = self.service.correct_segment(identifier, "seg001", "去仓库")
        self.assertEqual("去仓库", fixed["text"])
        self.assertEqual("去会议室", fixed["corrections"][0]["before"])
        self.assertEqual(2, fixed["segments"][0]["revision"])
        self.assertEqual("", self.service.cancel_session(identifier)["text"])
        for method in (lambda: self.service.finish_session(identifier), lambda: self.service.correct_segment(identifier, "seg001", "文字"), lambda: self.service.append_chunk(identifier, b"\0\0", 2)):
            with self.assertRaises(VoiceSessionError):
                method()
    def test_rate_channels_chunk_bounds_and_duration_are_enforced(self):
        for args in ((48000, 1), (16000, 2), (True, 1)):
            with self.assertRaises(VoiceSessionError):
                self.service.create_session(*args)
        identifier = self.create()
        for pcm in (b"", b"\0", bytes(32002)):
            with self.assertRaises(VoiceSessionError):
                self.service.append_chunk(identifier, pcm, 0)
        for sequence in range(60):
            self.service.append_chunk(identifier, bytes(32000), sequence)
        with self.assertRaises(VoiceSessionError):
            self.service.append_chunk(identifier, b"\0\0", 60)
        self.assertTrue(self.provider.recognizer.closed)
    def test_idle_expiry_and_concurrency_release_resources(self):
        ids = [self.create() for _ in range(4)]
        with self.assertRaises(VoiceSessionError):
            self.create()
        self.service.cancel_session(ids[0])
        self.create()
        self.now = 121
        with self.assertRaises(VoiceSessionError):
            self.service.finish_session(ids[1])
        self.assertTrue(self.provider.recognizer.closed)
        self.create()
    def test_provider_location_metadata_and_tts_validation(self):
        self.assertTrue(self.service.capabilities()["asr"][0]["simulated"])
        self.provider.local = False
        self.assertFalse(self.service.capabilities()["asr"][0]["local"])
        self.assertFalse(self.service.synthesize("您好")["audio_generated"])
        for text, language in (("", "zh-CN"), ("a"*1001, "zh-CN"), ("hello", "bad")):
            with self.assertRaises(VoiceSessionError):
                self.service.synthesize(text, language)


if __name__ == "__main__":
    unittest.main()
