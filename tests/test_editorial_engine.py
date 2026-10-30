import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import editorial_engine as engine
from speaker_diarization import diarize_media


class EditorialEngineTests(unittest.TestCase):
    def test_transcript_context_keeps_external_context_separate(self):
        words = [
            (8.0, "The"), (8.2, "problem"), (8.4, "started"),
            (10.0, "This"), (10.2, "changed"), (10.4, "everything."),
            (12.0, "And"), (12.2, "that"), (12.4, "was"), (12.6, "the"), (12.8, "lesson."),
        ]
        context = engine.transcript_context(words, 10.0, 10.8)
        self.assertEqual(context["context_before"], "The problem started")
        self.assertEqual(context["segment_text"], "This changed everything.")
        self.assertIn("And that", context["context_after"])
        self.assertNotIn("The problem", context["segment_text"])

    def test_dangling_pronoun_opening_has_higher_context_dependency(self):
        weak = engine.editorial_signals("This is why it happened and we learned a lesson.")
        strong = engine.editorial_signals("The biggest mistake founders make is hiring too early.")
        self.assertGreater(weak["context_dependency"], strong["context_dependency"])
        self.assertGreater(strong["standalone_heuristic"], weak["standalone_heuristic"])

    def test_hook_type_classification(self):
        self.assertEqual(engine.hook_type("Why do most companies fail?"), "question")
        self.assertEqual(engine.hook_type("I lost 80% of my savings"), "number_or_stat")
        self.assertEqual(engine.hook_type("One day I decided to leave"), "personal_story")

    def test_context_enrichment_adds_hook_and_payoff_fields(self):
        words = [
            (1.0, "Why"), (1.2, "do"), (1.4, "businesses"), (1.6, "fail?"),
            (1.8, "Because"), (2.0, "they"), (2.2, "ignore"), (2.4, "customers."),
            (2.6, "That's"), (2.8, "why"), (3.0, "they"), (3.2, "lose."),
        ]
        candidates = [{"start": 1.0, "end": 3.3, "text": "placeholder"}]
        enriched = engine.enrich_candidates_with_context(candidates, words)[0]
        self.assertEqual(enriched["hook_type"], "question")
        self.assertIn("context_before", enriched)
        self.assertIn("payoff_heuristic", enriched)
        self.assertIn("Why do businesses fail", enriched["text"])

    def test_speaker_summary_counts_turns_switches_and_overlap(self):
        turns = [
            {"start": 0.0, "end": 3.0, "speaker": "A"},
            {"start": 3.2, "end": 5.0, "speaker": "B"},
            {"start": 4.5, "end": 6.0, "speaker": "A"},
        ]
        result = engine.speaker_summary(turns, 0.0, 8.0)
        self.assertEqual(result["speaker_count"], 2)
        self.assertEqual(result["speaker_turns"], 3)
        self.assertGreaterEqual(result["speaker_switches"], 1)
        self.assertGreater(result["speaker_overlap_s"], 0.0)
        self.assertIsNotNone(result["speaker_score"])

    def test_missing_speaker_turns_are_neutral(self):
        result = engine.speaker_summary([], 0.0, 20.0)
        self.assertIsNone(result["speaker_count"])
        self.assertIsNone(result["speaker_score"])

    def test_performance_score_uses_retention_and_rates(self):
        strong = engine.performance_quality({
            "reach": 1000, "shares": 40, "saved": 30, "likes": 100,
            "ig_reels_avg_watch_time": 24000, "reels_skip_rate": 10,
        }, 30)
        weak = engine.performance_quality({
            "reach": 1000, "shares": 2, "saved": 2, "likes": 10,
            "ig_reels_avg_watch_time": 3000, "reels_skip_rate": 75,
        }, 30)
        self.assertGreater(strong["score"], weak["score"])
        self.assertAlmostEqual(strong["avg_watch_s"], 24.0)
        self.assertAlmostEqual(strong["skip_rate"], 0.10)

    def test_missing_performance_metrics_do_not_fake_a_high_score(self):
        result = engine.performance_quality({}, 30)
        self.assertIsNone(result["score"])
        self.assertEqual(result["evidence_count"], 0)

    def test_format_priors_require_minimum_sample(self):
        clips = [
            {"status": "posted", "performance_score": 0.9,
             "features": {"hook_type": "question"}}
            for _ in range(3)
        ]
        self.assertEqual(engine.format_priors(clips, min_samples=4), {})
        clips.append({"status": "posted", "performance_score": 0.8,
                      "features": {"hook_type": "question"}})
        priors = engine.format_priors(clips, min_samples=4)
        self.assertIn("hook_type:question", priors)
        self.assertGreater(priors["hook_type:question"]["prior"], 0.5)

    def test_format_prior_only_adds_small_adjustment(self):
        candidates = [{"hook_type": "question", "start": 0, "end": 20}]
        engine.apply_format_priors(candidates, {"hook_type:question": {"prior": 0.9}})
        self.assertAlmostEqual(candidates[0]["performance_prior"], 0.9)

    def test_face_crop_expression_is_escaped_and_piecewise(self):
        from podcast_clipper import face_crop_expression
        expr = face_crop_expression({
            "points": [(0.0, 200), (1.0, 500), (2.0, 350)],
            "max_x": 2200,
        })
        self.assertIsNotNone(expr)
        self.assertIn(r"lt(t\,", expr)
        self.assertIn("round(", expr)
        self.assertIn("t-0.000", expr)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
    def test_dynamic_face_crop_expression_runs_through_ffmpeg(self):
        from podcast_clipper import face_crop_expression
        expr = face_crop_expression({
            "points": [(0.0, 200), (0.7, 600), (1.4, 320)],
            "max_x": 2200,
        })
        vf = f"scale=-2:1920,crop=1080:1920:'{expr}':0"
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "crop-test.mp4"
            result = subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=24:duration=1.8",
                 "-vf", vf, "-frames:v", "24", "-an", str(output)],
                capture_output=True, text=True, timeout=45,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(output.exists())
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(output)],
                capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)
            self.assertEqual(probe.stdout.strip(), "1080x1920")

    def test_diarization_is_optional_without_hugging_face_token(self):
        with patch.dict(os.environ, {"HF_TOKEN": ""}):
            self.assertEqual(diarize_media("does-not-exist.mp4"), [])


if __name__ == "__main__":
    unittest.main()
