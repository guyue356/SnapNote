import json
import unittest
from unittest.mock import patch

import numpy as np

from app import mimo_vision, vision


class FrameMetricTests(unittest.TestCase):
    def test_sharp_frame_scores_higher_than_flat_frame(self):
        flat = np.full((64, 64), 128, dtype=np.uint8)
        checker = (np.indices((64, 64)).sum(axis=0) % 2 * 255).astype(np.uint8)

        flat_metrics = vision._frame_metrics(flat, None)
        sharp_metrics = vision._frame_metrics(checker, None)

        self.assertGreater(sharp_metrics["sharpness_score"], flat_metrics["sharpness_score"])
        self.assertGreater(sharp_metrics["contrast_score"], flat_metrics["contrast_score"])

    def test_motion_and_transition_detect_visible_change(self):
        dark = np.zeros((64, 64), dtype=np.uint8)
        bright = np.full((64, 64), 255, dtype=np.uint8)

        metrics = vision._frame_metrics(bright, dark)

        self.assertGreater(metrics["motion_score"], 0.9)
        self.assertGreater(metrics["transition_score"], 0.9)
        self.assertLess(metrics["stability_score"], 0.1)


class ShotSelectionTests(unittest.TestCase):
    def test_scene_change_builds_separate_shots(self):
        samples = []
        for index in range(10):
            samples.append({
                "timestamp": float(index),
                "transition_score": 0.8 if index == 5 else 0.01,
                "motion_score": 0.04,
                "quality_score": 0.8,
                "sharpness_score": 0.8,
                "brightness_score": 0.8,
                "contrast_score": 0.8,
                "stability_score": 0.8,
            })

        shots = vision._shots_from_samples(samples, 10)

        self.assertEqual(len(shots), 2)
        self.assertEqual(shots[0]["start_time"], 0)
        self.assertEqual(shots[1]["start_time"], 5)

    def test_fallback_covers_whole_video(self):
        shots = vision._fallback_shots(180)

        self.assertGreaterEqual(len(shots), 1)
        self.assertEqual(shots[0]["start_time"], 0)
        self.assertEqual(shots[-1]["end_time"], 180)


class MimoVisionValidationTests(unittest.TestCase):
    def test_request_uses_mimo_model_api_key_and_json_mode(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return json.dumps({
                    "choices": [{"message": {"content": '{"ok": true}'}}],
                    "usage": {"total_tokens": 12},
                }).encode()

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse()

        with (
            patch.object(mimo_vision, "MIMO_API_KEY", "test-key"),
            patch.object(mimo_vision, "MIMO_BASE_URL", "https://mimo.example/v1"),
            patch("app.mimo_vision.urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            parsed, usage = mimo_vision._request_json_sync(
                [{"role": "user", "content": "Return JSON"}], "test"
            )

        payload = json.loads(captured["request"].data.decode())
        self.assertEqual(captured["request"].headers["Api-key"], "test-key")
        self.assertEqual(payload["model"], "mimo-v2.5")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(parsed, {"ok": True})
        self.assertEqual(usage["total_tokens"], 12)

    def test_json_fence_is_parsed(self):
        parsed = mimo_vision._parse_json_content('```json\n{"frames": []}\n```')
        self.assertEqual(parsed, {"frames": []})

    def test_frame_analysis_is_bounded_and_normalized(self):
        normalized = mimo_vision._normalize_frame_analysis({
            "title": "镜头",
            "style_tags": ["纪实", "快节奏"],
            "needs_motion_context": 1,
            "confidence": 4,
        })

        self.assertEqual(normalized["title"], "镜头")
        self.assertEqual(normalized["style_tags"], ["纪实", "快节奏"])
        self.assertTrue(normalized["needs_motion_context"])
        self.assertEqual(normalized["confidence"], 1)

    def test_local_summary_includes_storyboard_and_dynamic_ratio(self):
        frames = [{
            "id": "frame-1",
            "timestamp": 1,
            "start_time": 0,
            "end_time": 4,
            "is_dynamic": True,
            "visual_analysis": {
                "description": "人物近景",
                "summary": "开场钩子",
                "style_tags": ["高对比"],
                "hook_elements": ["悬念"],
            },
        }]

        summary = mimo_vision._local_style_summary(frames, 4)

        self.assertEqual(summary["pacing"]["dynamic_ratio"], 1)
        self.assertEqual(summary["storyboard"][0]["shot"], "人物近景")
        self.assertIn("高对比", summary["visual_style"])


if __name__ == "__main__":
    unittest.main()
