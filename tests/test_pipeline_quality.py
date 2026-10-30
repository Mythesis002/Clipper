import io
import unittest
from contextlib import redirect_stdout
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




    def test_face_reframe_can_be_disabled_without_touching_video(self):
        self.assertIsNone(clipper.detect_face_crop_x({"render": {"face_reframe": False}}, "missing.mp4"))

    @patch.object(clipper, "load_state")
    @patch.object(clipper, "save_state")
    @patch.object(clipper, "authorized_channels", return_value=[{"name": "Permitted source"}])
    @patch.object(clipper, "queued")
    @patch.object(clipper, "render_clip", return_value="rendered.mp4")
    @patch.object(clipper, "qa_review", return_value={
        "verdict": "soft_fail", "score": None, "issues": ["AI review unavailable"]
    })
    @patch.object(clipper.shutil, "copy2")
    @patch.object(clipper, "post_reel")
    def test_ai_soft_fail_is_held_for_manual_review_not_posted(
        self, post_mock, _copy, _review, _render, queued_mock,
        _channels, save_mock, state_mock
    ):
        clip = {"id": 7, "channel": "Permitted source", "status": "queued",
                "start": 10, "end": 30, "video_id": "video", "caption": "caption"}
        state = {"clips": [clip], "episodes": {}, "next_id": 2, "journal": []}
        state_mock.return_value = state
        queued_mock.return_value = [clip]
        cfg = {"channels": [{"name": "Permitted source", "permission": True}]}
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(clipper, "REVIEW_DIR", Path(folder)):
                result = clipper.publish_next(cfg, wait=False, force=True)
        self.assertTrue(result)
        self.assertEqual(clip["status"], "needs_review")
        post_mock.assert_not_called()
        save_mock.assert_called()

    @patch.object(clipper, "post_reel", side_effect=clipper.PublishUncertainError("timeout"))
    @patch.object(clipper, "save_state")
    @patch.object(clipper, "jr")
    def test_uncertain_instagram_publish_is_quarantined(self, _jr, _save, _post):
        clip = {"id": 4, "channel": "source", "caption": "caption"}
        state = {"clips": [clip], "journal": []}
        result = clipper._post_clip({}, state, clip, "rendered.mp4")
        self.assertFalse(result)
        self.assertEqual(clip["status"], "publish_uncertain")

    @patch.object(clipper, "post_reel", side_effect=RuntimeError("HTTP 400 rejected"))
    @patch.object(clipper, "save_state")
    @patch.object(clipper, "jr")
    @patch.object(clipper, "_fail")
    def test_definite_instagram_failure_remains_retryable(self, fail_mock, _jr, _save, _post):
        clip = {"id": 5, "channel": "source", "caption": "caption", "attempts": 0}
        state = {"clips": [clip], "journal": []}
        result = clipper._post_clip({}, state, clip, "rendered.mp4")
        self.assertIsNone(result)
        self.assertEqual(clip["status"], "queued")
        fail_mock.assert_called_once()

    def test_manual_approve_changes_review_clip_to_queued(self):
        import json
        import tempfile
        from pathlib import Path
        initial = {"clips": [{"id": 12, "status": "needs_review"}], "episodes": {},
                   "next_id": 13, "journal": []}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            path.write_text(json.dumps(initial), encoding="utf-8")
            with patch.object(clipper, "STATE_FILE", path):
                clipper.review_action(12, approve=True)
                state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(state["clips"][0]["status"], "queued")
        self.assertIn("approved_manually_at", state["clips"][0])



    def test_channel_learning_uses_retention_quality_not_raw_views(self):
        import podcast_clipper as clipper
        clips = []
        for i in range(3):
            clips.append({
                "status": "posted", "channel": "Low-view quality",
                "metrics": {"views": 500 + i}, "performance_score": 0.85,
            })
            clips.append({
                "status": "posted", "channel": "High-view weak",
                "metrics": {"views": 100000 + i}, "performance_score": 0.20,
            })
        cfg = {
            "channels": [
                {"name": "High-view weak", "permission": True},
                {"name": "Low-view quality", "permission": True},
                {"name": "New channel", "permission": True},
            ],
            "learning": {"explore_rate": 0.0},
        }
        state = {"clips": clips, "episodes": {}}
        ranked = clipper.order_channels(cfg, state, rng=type(
            "NoExplore", (), {"random": staticmethod(lambda: 0.9)}
        )())
        self.assertEqual(ranked[0]["name"], "New channel")
        self.assertEqual(ranked[1]["name"], "Low-view quality")
        self.assertEqual(ranked[2]["name"], "High-view weak")



    @patch("podcast_clipper.now_iso", return_value="2026-10-30T13:00:00+00:00")
    def test_episode_retry_after_keeps_episode_deferred(self, _now):
        import podcast_clipper as clipper
        state = {"episodes": {
            "episode-1": {"retry_after": "2026-10-30T14:00:00+00:00"},
            "episode-2": {"retry_after": "2026-10-30T12:00:00+00:00"},
            "episode-3": {"note": "permanent successful queue"},
        }}
        self.assertEqual(clipper.done_ids(state), {"episode-1", "episode-3"})

    @patch("podcast_clipper.now_iso", return_value="2026-10-30T15:00:00+00:00")
    def test_episode_becomes_eligible_after_retry_window(self, _now):
        import podcast_clipper as clipper
        state = {"episodes": {
            "episode-1": {"retry_after": "2026-10-30T14:00:00+00:00"},
        }}
        self.assertEqual(clipper.done_ids(state), set())



    def test_env_value_strips_secret_whitespace(self):
        with patch.dict("os.environ", {"CLIPPER_TEST_SECRET": "  secret-value\r\n"}):
            self.assertEqual(clipper.env_value("CLIPPER_TEST_SECRET"), "secret-value")

    def test_gemini_key_ignores_whitespace_primary_and_uses_trimmed_fallback(self):
        with patch.dict("os.environ", {
            "GEMINI_API_KEY": "  \n",
            "GOOGLE_API_KEY": "  fallback-key\r\n",
        }, clear=True):
            self.assertEqual(clipper.gemini_key(), "fallback-key")

    def test_preflight_fails_when_gemini_is_missing_but_enabled(self):
        cfg = {
            "instagram": {"graph_version": "v25.0"},
            "channels": [{"name": "Raj Shamani", "permission": True}],
            "llm": {"enabled": True},
        }
        with patch.dict("os.environ", {
            "IG_USER_ID": "  ",
            "IG_ACCESS_TOKEN": "  ",
            "GEMINI_API_KEY": "  ",
        }, clear=True), patch.object(clipper.shutil, "which", return_value="/usr/bin/tool"):
            output = io.StringIO()
            with redirect_stdout(output):
                result = clipper.check(cfg, live=False)
        self.assertFalse(result)
        self.assertIn("FAIL env GEMINI_API_KEY", output.getvalue())
        self.assertIn("FAIL env IG_USER_ID", output.getvalue())

    def test_instagram_upload_smoke_uploads_and_waits_without_publishing(self):
        import tempfile
        from pathlib import Path
        cfg = {"instagram": {"graph_version": "v25.0"}}
        create_response = Mock(ok=True, status_code=200,
                               json=lambda: {"id": "container-123", "uri": "https://upload.example/container-123"})
        upload_response = Mock(ok=True, status_code=200)
        status_response = Mock(ok=True, json=lambda: {"status_code": "FINISHED", "status": "ready"})
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "smoke.mp4"
            video.write_bytes(b"synthetic-mp4-payload")
            with patch.dict("os.environ", {"IG_USER_ID": "ig-123", "IG_ACCESS_TOKEN": "token-123"}), \
                 patch.object(clipper.requests, "post",
                              side_effect=[create_response, upload_response]) as post_mock, \
                 patch.object(clipper.requests, "get", return_value=status_response) as get_mock:
                container_id = clipper.upload_reel_container(cfg, video, "smoke test")
        self.assertEqual(container_id, "container-123")
        self.assertEqual(post_mock.call_count, 2)
        self.assertEqual(post_mock.call_args_list[1].args[0], "https://upload.example/container-123")
        self.assertEqual(post_mock.call_args_list[1].kwargs["headers"]["Content-Type"],
                         "application/octet-stream")
        self.assertEqual(get_mock.call_count, 1)
        self.assertNotIn("/media_publish", str(post_mock.call_args_list))

    def test_instagram_upload_smoke_rejects_missing_credentials_before_network(self):
        import tempfile
        from pathlib import Path
        cfg = {"instagram": {"graph_version": "v25.0"}}
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "smoke.mp4"
            video.write_bytes(b"mp4")
            with patch.dict("os.environ", {"IG_USER_ID": "  ", "IG_ACCESS_TOKEN": "\\n"}), \
                 patch.object(clipper.requests, "post") as post_mock:
                with self.assertRaisesRegex(RuntimeError, "IG_USER_ID and IG_ACCESS_TOKEN"):
                    clipper.upload_reel_container(cfg, video, "smoke")
        post_mock.assert_not_called()

    def test_instagram_upload_smoke_rejects_unexpected_status(self):
        import tempfile
        from pathlib import Path
        cfg = {"instagram": {"graph_version": "v25.0"}}
        create_response = Mock(ok=True, status_code=200,
                               json=lambda: {"id": "container-123", "uri": "https://upload.example/container-123"})
        upload_response = Mock(ok=True, status_code=200)
        status_response = Mock(ok=True, json=lambda: {"status_code": "UNKNOWN"})
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "smoke.mp4"
            video.write_bytes(b"synthetic-mp4-payload")
            with patch.dict("os.environ", {"IG_USER_ID": "ig-123", "IG_ACCESS_TOKEN": "token-123"}), \
                 patch.object(clipper.requests, "post",
                              side_effect=[create_response, upload_response]), \
                 patch.object(clipper.requests, "get", return_value=status_response):
                with self.assertRaisesRegex(RuntimeError, "unexpected container status"):
                    clipper.upload_reel_container(cfg, video, "smoke")

    def test_preflight_requires_hugging_face_token_when_diarization_enabled(self):
        cfg = {
            "instagram": {"graph_version": "v25.0"},
            "channels": [{"name": "Raj Shamani", "permission": True}],
            "llm": {"enabled": False},
            "diarization": {"enabled": True},
        }
        with patch.dict("os.environ", {
            "IG_USER_ID": "ig-id", "IG_ACCESS_TOKEN": "ig-token", "HF_TOKEN": "  ",
        }, clear=True), patch.object(clipper.shutil, "which", return_value="/usr/bin/tool"), patch.object(
            clipper.requests, "get", return_value=Mock(ok=True, json=lambda: {"username": "test"})
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                result = clipper.check(cfg, live=False)
        self.assertFalse(result)
        self.assertIn("FAIL env HF_TOKEN", output.getvalue())

    def test_repository_config_enables_diarization_and_only_raj_shamani_source(self):
        import podcast_clipper as clipper
        cfg = clipper.load_config()
        self.assertTrue(cfg["diarization"]["enabled"])
        self.assertEqual(
            [channel["name"] for channel in clipper.authorized_channels(cfg)],
            ["Raj Shamani"],
        )
        self.assertGreaterEqual(cfg["learning"]["min_channel_samples"], 1)
        self.assertGreaterEqual(cfg["learning"]["min_format_samples"], 1)

    def test_only_sources_with_explicit_permission_are_authorized(self):
        cfg = clipper.load_config()
        authorized = clipper.authorized_channels(cfg)
        self.assertEqual([channel["name"] for channel in authorized],
                         ["Raj Shamani"])
        self.assertTrue(authorized[0]["permission"])
        self.assertTrue(all(
            channel["permission"] is False
            for channel in cfg["channels"]
            if channel["name"] != "Raj Shamani"
        ))

if __name__ == "__main__":
    unittest.main()
