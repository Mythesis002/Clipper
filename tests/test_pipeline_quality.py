import unittest
from unittest.mock import Mock, patch

import numpy as np

import podcast_clipper as clipper


class PipelineQualityTests(unittest.TestCase):
    def test_missing_heatmap_returns_neutral_signal(self):
        heat = clipper.heat_per_second([], 30)
        self.assertEqual(len(heat), 30)
        self.assertTrue(np.allclose(heat, 0.5))

    def test_malformed_heatmap_returns_neutral_signal(self):
        heat = clipper.heat_per_second([{"bad": "row"}], 12)
        self.assertEqual(len(heat), 12)
        self.assertTrue(np.allclose(heat, 0.5))

    def test_constant_heatmap_does_not_create_nan(self):
        rows = [
            {"start_time": 0, "end_time": 5, "value": 0.7},
            {"start_time": 5, "end_time": 10, "value": 0.7},
        ]
        heat = clipper.heat_per_second(rows, 10)
        self.assertTrue(np.isfinite(heat).all())
        self.assertTrue(np.allclose(heat, 0.5))

    def test_no_transcript_does_not_create_arbitrary_clips(self):
        cfg = {"clip": {"min_s": 15, "max_s": 45, "min_gap_s": 90}}
        heat = np.full(120, 0.5)
        self.assertEqual(clipper.score_candidates(cfg, heat, [], 120), [])

    def test_caption_retime_preserves_original_word_timestamps(self):
        original = [(1.0, "helo"), (1.5, "world")]
        corrected = clipper.retime(original, "hello world")
        self.assertEqual(corrected, [(1.0, "hello"), (1.5, "world")])

    def test_caption_retime_rejects_token_count_changes(self):
        original = [(1.0, "hello"), (1.5, "world")]
        self.assertIsNone(clipper.retime(original, "hello wonderful world"))

    def test_caption_guard_requires_same_token_count(self):
        original = [(1.0, "hello"), (1.5, "world")]
        changed = [(1.0, "hello"), (1.5, "there")]
        self.assertFalse(clipper.caption_change_ok(original, changed))

    @patch.object(clipper, "probe_media", return_value={
        "duration": 10.0, "has_video": True, "has_audio": True
    })
    @patch.object(clipper.subprocess, "run")
    def test_technical_qa_fails_when_ffmpeg_fails(self, run_mock, _probe):
        run_mock.return_value = Mock(returncode=1, stderr="decoder error")
        with self.assertRaisesRegex(RuntimeError, "ffmpeg technical QA failed"):
            clipper.qa_checks({}, "fake.mp4")

    @patch.object(clipper, "probe_media", return_value={
        "duration": 10.0, "has_video": True, "has_audio": True
    })
    @patch.object(clipper.subprocess, "run")
    def test_technical_qa_detects_long_silence(self, run_mock, _probe):
        run_mock.return_value = Mock(
            returncode=0,
            stderr=(
                "black_duration:0.2 "
                "silence_duration: 3.0 "
            ),
        )
        result = clipper.qa_checks({}, "fake.mp4")
        self.assertTrue(any("continuous silence" in item for item in result["issues"]))

    @patch.object(clipper, "qa_checks", return_value={"issues": []})
    @patch.object(clipper, "gemini_qa", return_value=None)
    def test_missing_final_ai_review_requires_manual_review(self, _gemini, _technical):
        result = clipper.qa_review({"qa": {"ai_unavailable": "manual_review"}}, "fake.mp4")
        self.assertEqual(result["verdict"], "soft_fail")
        self.assertIn("manual review required", result["issues"][0].lower())

    def test_apply_rerank_ignores_invalid_candidate_ids(self):
        candidates = [{"pre": 0.5}]
        applied = clipper.apply_rerank(candidates, [
            {"id": 99, "hook": 9, "standalone": 9, "emotion": 9}
        ])
        self.assertEqual(applied, 0)
        self.assertNotIn("llm", candidates[0])


if __name__ == "__main__":
    unittest.main()
