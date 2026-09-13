import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

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
        self.assertEqual(shots[1]["boundary_reason"], "scene_change")

    def test_fallback_covers_whole_video(self):
        shots = vision._fallback_shots(180)

        self.assertGreaterEqual(len(shots), 1)
        self.assertEqual(shots[0]["start_time"], 0)
        self.assertEqual(shots[-1]["end_time"], 180)

    def test_semantic_units_combine_transcript_and_real_scene_boundaries(self):
        segments = [
            {"start": 0, "end": 90, "text": "第一部分"},
            {"start": 90, "end": 180, "text": "第二部分"},
        ]
        shots = [
            {"start_time": 0, "timestamp": 10, "boundary_reason": "start"},
            {"start_time": 45, "timestamp": 55, "boundary_reason": "scene_change"},
            {"start_time": 120, "timestamp": 130, "boundary_reason": "max_duration"},
        ]

        with (
            patch.object(vision, "FRAME_SEMANTIC_MIN_SECONDS", 10),
            patch.object(vision, "FRAME_SEMANTIC_MAX_SECONDS", 75),
        ):
            units = vision.build_semantic_units(segments, shots, 180)

        boundaries = [unit["start_time"] for unit in units]
        self.assertIn(45, boundaries)
        self.assertIn(90, boundaries)
        self.assertNotIn(120, boundaries)
        self.assertTrue(all(unit["end_time"] - unit["start_time"] <= 75 for unit in units))

    def test_semantic_selection_covers_units_and_limits_time_gaps(self):
        segments = [
            {"start": 0, "end": 90, "text": "问题一"},
            {"start": 90, "end": 180, "text": "问题二"},
        ]
        shots = []
        for index, timestamp in enumerate((20, 60, 110, 150), start=1):
            shots.append({
                "shot_id": f"shot-{index}",
                "start_time": timestamp - 20,
                "end_time": timestamp + 20,
                "timestamp": timestamp,
                "quality_score": 0.8,
                "stability_score": 0.9,
                "transition_score": 0.01,
                "boundary_reason": "max_duration",
            })

        with (
            patch.object(vision, "FRAME_MAX_COUNT", 10),
            patch.object(vision, "FRAME_TARGET_INTERVAL_SECONDS", 60),
            patch.object(vision, "FRAME_MAX_GAP_SECONDS", 90),
        ):
            units = vision.build_semantic_units(segments, shots, 180)
            selected, stats = vision.select_shots_for_semantic_coverage(
                shots, units, 180
            )

        self.assertEqual(stats["semantic_coverage_ratio"], 1)
        self.assertLessEqual(stats["max_frame_gap_seconds"], 90)
        self.assertTrue(all(frame.get("semantic_unit_id") for frame in selected))

    def test_dedup_keeps_identical_frames_from_different_semantic_units(self):
        with tempfile.TemporaryDirectory() as temporary:
            frames_dir = Path(temporary)
            for name in ("frame_001.jpg", "frame_002.jpg", "frame_003.jpg"):
                Image.new("RGB", (32, 32), "white").save(frames_dir / name)
            frames = [
                {
                    "id": "frame-1", "timestamp": 10,
                    "image_url": "/frames/frame_001.jpg",
                    "semantic_unit_id": "unit-1", "coverage_anchor": True,
                },
                {
                    "id": "frame-2", "timestamp": 20,
                    "image_url": "/frames/frame_002.jpg",
                    "semantic_unit_id": "unit-2", "coverage_anchor": True,
                },
                {
                    "id": "frame-3", "timestamp": 22,
                    "image_url": "/frames/frame_003.jpg",
                    "semantic_unit_id": "unit-2", "coverage_anchor": False,
                },
            ]

            result = vision.deduplicate_frames(frames, frames_dir)

        self.assertEqual([frame["id"] for frame in result], ["frame-1", "frame-2"])

    def test_coverage_repair_targets_uncovered_unit(self):
        units = [
            {"id": "unit-1", "start_time": 0, "end_time": 60},
            {"id": "unit-2", "start_time": 60, "end_time": 120},
        ]
        frames = [{
            "shot_id": "shot-1", "timestamp": 20, "semantic_unit_id": "unit-1"
        }]
        shots = [{
            "shot_id": "shot-2", "timestamp": 80, "quality_score": 0.8,
            "stability_score": 0.9, "transition_score": 0.01,
        }]

        with patch.object(vision, "FRAME_MAX_COUNT", 10):
            repairs = vision.select_coverage_repairs(
                frames, shots, units, 120, {"shot-1"}
            )

        self.assertEqual(repairs[0]["semantic_unit_id"], "unit-2")
        self.assertEqual(repairs[0]["selection_reason"], "uncovered_unit_repair")


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
