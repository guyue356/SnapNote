import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import asr


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class WhisperParameterTests(unittest.TestCase):
    def test_model_loader_reuses_configured_huggingface_cache(self):
        original_model = asr._whisper_model
        original_available = asr._whisper_available
        try:
            asr._whisper_model = None
            asr._whisper_available = True
            with patch("faster_whisper.WhisperModel") as model_class:
                loaded = asr._get_whisper()

            self.assertIs(loaded, model_class.return_value)
            model_class.assert_called_once_with(
                asr.WHISPER_MODEL_SIZE,
                device=asr.WHISPER_DEVICE,
                compute_type=asr.WHISPER_COMPUTE_TYPE,
                download_root=str(asr.WHISPER_CACHE_DIR),
            )
        finally:
            asr._whisper_model = original_model
            asr._whisper_available = original_available

    def test_parameters_match_video2knowledge(self):
        options = asr._whisper_transcribe_options("audio.wav", "zh")

        self.assertEqual(asr.WHISPER_MODEL_SIZE, "large-v3")
        self.assertEqual(options["beam_size"], 1)
        self.assertEqual(options["best_of"], 1)
        self.assertTrue(options["vad_filter"])
        self.assertEqual(options["vad_parameters"]["min_silence_duration_ms"], 500)
        self.assertEqual(options["vad_parameters"]["speech_pad_ms"], 200)
        self.assertFalse(options["condition_on_previous_text"])
        self.assertEqual(options["compression_ratio_threshold"], 2.2)
        self.assertEqual(options["log_prob_threshold"], -1.0)
        self.assertEqual(options["no_speech_threshold"], 0.6)
        self.assertEqual(options["repetition_penalty"], 1.1)
        self.assertEqual(options["no_repeat_ngram_size"], 3)
        self.assertTrue(options["word_timestamps"])
        self.assertEqual(options["hallucination_silence_threshold"], 2.0)

    def test_repeated_long_segment_is_filtered(self):
        kept = [{"start": 0, "end": 5, "text": "重复内容"}]
        repeated = {"start": 5, "end": 30, "text": "重复内容"}

        self.assertTrue(asr._is_repeated_whisper_hallucination(repeated, kept))


class MimoParameterTests(unittest.TestCase):
    def test_parameters_match_video2knowledge(self):
        self.assertEqual(asr.MIMO_ASR_MODEL, "mimo-v2.5-asr")
        self.assertEqual(asr.MIMO_ASR_CHUNK_SECONDS, 90)
        self.assertEqual(asr.MIMO_ASR_MP3_BITRATE, "32k")
        self.assertEqual(asr.MIMO_ASR_CONCURRENCY, 3)
        self.assertEqual(asr.MIMO_ASR_TIMEOUT_SECONDS, 90)
        self.assertEqual(asr.MIMO_ASR_MAX_ATTEMPTS, 2)
        self.assertEqual(asr.MIMO_ASR_HEARTBEAT_SECONDS, 10)

    def test_request_uses_audio_payload_model_and_language(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse({"choices": [{"message": {"content": "测试转写"}}]})

        with tempfile.TemporaryDirectory() as directory:
            audio_path = Path(directory) / "chunk.mp3"
            audio_path.write_bytes(b"fake-mp3")
            with (
                patch.object(asr, "MIMO_API_KEY", "test-key"),
                patch.object(asr, "MIMO_BASE_URL", "https://mimo.example/v1"),
                patch("app.asr.urllib.request.urlopen", side_effect=fake_urlopen),
            ):
                result = asr._mimo_asr_transcribe_sync(audio_path, "zh")

        self.assertEqual(result, "测试转写")
        self.assertEqual(captured["timeout"], 90)
        request = captured["request"]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "https://mimo.example/v1/chat/completions")
        self.assertEqual(request.headers["Api-key"], "test-key")
        self.assertEqual(payload["model"], "mimo-v2.5-asr")
        self.assertEqual(payload["asr_options"], {"language": "zh"})
        audio = payload["messages"][0]["content"][0]["input_audio"]
        self.assertEqual(audio["format"], "mp3")
        self.assertTrue(audio["data"].startswith("data:audio/mp3;base64,"))

    def test_list_message_content_is_supported(self):
        content = asr._extract_message_content(
            {"content": [{"text": "第一段"}, {"text": "第二段"}]}
        )
        self.assertEqual(content, "第一段\n第二段")


class ProviderDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatches_to_selected_provider(self):
        expected = ([{"start": 0, "end": 1, "text": "ok"}], "mimo:test")
        with patch("app.asr.transcribe_with_mimo", new=AsyncMock(return_value=expected)) as mocked:
            actual = await asr.transcribe_audio("audio.wav", 1, "mimo")

        self.assertEqual(actual, expected)
        mocked.assert_awaited_once()

    async def test_unknown_provider_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported ASR provider"):
            await asr.transcribe_audio("audio.wav", 1, "unknown")


if __name__ == "__main__":
    unittest.main()
