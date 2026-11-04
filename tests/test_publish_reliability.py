"""Regression tests for Gemini editorial screening and publish queue resilience."""

import json
import os
import tempfile
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from unittest.mock import patch

import podcast_clipper as clipper


class PromotionScreenTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "llm": {"enabled": True},
            "intelligence": {
                "promotion_policy": "manual_review",
                "promotion_threshold": 0.72,
            },
        }
        self.clip = {
            "id": 101,
            "title": "A factual discussion",
            "words": [[0.0, "Here"], [0.4, "is"], [0.8, "the"], [1.2, "evidence."]],
        }
        self.clean_response = {
            "content_type": "insight",
            "promotion_type": "none",
            "promotion_confidence": 0.0,
            "classification_confidence": 0.99,
            "evidence": "",
        }

    def test_parse_json_accepts_fences_and_prose_wrappers(self):
        payload = {"promotion_type": "none", "confidence": 0.99}
        self.assertEqual(clipper.parse_json(" ```json\n" + json.dumps(payload) + "\n``` "), payload)
        self.assertEqual(clipper.parse_json("Result:\n" + json.dumps(payload) + "\nThanks."), payload)

    @patch.object(clipper, "gemini_key", return_value="test-key")
    @patch.object(clipper, "gemini_text")
    def test_invalid_json_retries_once_then_accepts_valid_result(self, gemini_text, _key):
        gemini_text.side_effect = ["not-json", json.dumps(self.clean_response)]
        result = clipper.screen_queued_clip_for_promotion(self.cfg, self.clip)
        self.assertFalse(result["review_required"])
        self.assertFalse(result["excluded"])
        self.assertEqual(gemini_text.call_count, 2)
        self.assertEqual(self.clip["promotion_screen_attempts"], 2)
        self.assertEqual(self.clip["promotion_type"], "none")
        self.assertIsNone(self.clip["promotion_screen_error"])
        self.assertIn("response_schema", gemini_text.call_args.kwargs)

    @patch.object(clipper, "gemini_key", return_value="test-key")
    @patch.object(clipper, "gemini_text", return_value="still-not-json")
    def test_two_invalid_responses_fail_closed_for_manual_review(self, gemini_text, _key):
        result = clipper.screen_queued_clip_for_promotion(self.cfg, self.clip)
        self.assertTrue(result["review_required"])
        self.assertFalse(result["excluded"])
        self.assertEqual(gemini_text.call_count, 2)
        self.assertEqual(self.clip["promotion_screen_attempts"], 2)
        self.assertIn("GeminiError", self.clip["promotion_screen_error"])

    @patch.object(clipper, "gemini_key", return_value="test-key")
    @patch.object(clipper, "gemini_text")
    def test_sponsor_read_is_held_for_human_review(self, gemini_text, _key):
        response = dict(self.clean_response, content_type="promotion",
                        promotion_type="sponsor_read", promotion_confidence=0.99,
                        evidence="This episode is brought to you by Acme.")
        gemini_text.return_value = json.dumps(response)
        result = clipper.screen_queued_clip_for_promotion(self.cfg, self.clip)
        self.assertTrue(result["review_required"])
        self.assertFalse(result["excluded"])
        self.assertEqual(self.clip["promotion_type"], "sponsor_read")


class PostingSlotDiagnosticTests(unittest.TestCase):
    def test_diagnostic_reports_local_time_slots_and_posted_slot_accounting(self):
        cfg = {
            "timezone": "Asia/Kolkata",
            "post_times": ["06:00", "18:00"],
            "schedule": {"max_wait_min": 25, "catchup_hours": 6},
        }
        now = datetime(2026, 11, 2, 3, 40, tzinfo=ZoneInfo("Asia/Kolkata"))
        state = {
            "clips": [{
                "id": 9,
                "status": "posted",
                "posted_at": "2026-11-01T18:02:00+05:30",
            }]
        }

        diagnostic = clipper.posting_slot_diagnostic(cfg, state, now=now)

        self.assertIn("local_now=2026-11-02 03:40:00 IST", diagnostic)
        self.assertIn("timezone=Asia/Kolkata", diagnostic)
        self.assertIn("configured_slots=06:00,18:00 IST", diagnostic)
        self.assertIn("eligible_window=-6h/+25m", diagnostic)
        self.assertIn("already_accounted=", diagnostic)
        self.assertIn("clip 9 (posted)", diagnostic)


class PublishQueueTests(unittest.TestCase):
    def test_disabled_creator_queue_does_not_block_active_queue_refill(self):
        state = {
            "clips": [
                {"id": 1, "status": "queued", "channel": "Removed creator"},
                {"id": 2, "status": "queued", "channel": "Allowed creator"},
                {"id": 3, "status": "posted", "channel": "Removed creator"},
            ],
        }
        cfg = {"channels": [
            {"name": "Allowed creator", "permission": True},
            {"name": "Removed creator", "permission": False},
        ]}
        self.assertEqual([c["id"] for c in clipper.queued(state)], [1, 2])
        self.assertEqual(
            [c["id"] for c in clipper.queued_for_authorized_channels(state, cfg)],
            [2],
        )

    def _state(self, count=4):
        return {
            "episodes": {},
            "clips": [
                {
                    "id": i + 1, "status": "queued", "channel": "Allowed",
                    "start": 0, "end": 20, "title": f"Clip {i + 1}",
                    "words": [[0.0, "test"]],
                }
                for i in range(count)
            ],
            "journal": [],
        }

    def _run_with_screens(self, screens):
        state = self._state()
        cfg = {
            "queue": {"publish_candidate_limit": 10},
            "intelligence": {"promotion_policy": "manual_review"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            rendered = Path(temp_dir) / "rendered.mp4"
            rendered.write_bytes(b"test media")
            review_dir = Path(temp_dir) / "review"
            with (
                patch.object(clipper, "load_state", return_value=state),
                patch.object(clipper, "save_state"),
                patch.object(clipper, "authorized_channels", return_value=[{"name": "Allowed"}]),
                patch.object(clipper, "render_clip", return_value=rendered),
                patch.object(clipper, "screen_queued_clip_for_promotion", side_effect=screens) as screen,
                patch.object(clipper, "qa_review", return_value={
                    "verdict": "pass", "score": 9.0, "issues": [],
                }),
                patch.object(clipper, "_post_clip", return_value=True) as post,
                patch.object(clipper, "jr"),
                patch.object(clipper, "gha"),
                patch.object(clipper, "REVIEW_DIR", review_dir),
            ):
                result = clipper.publish_next(cfg, wait=False, force=True)
        return result, state, screen, post

    def test_queue_continues_after_three_review_holds_and_posts_fourth(self):
        held = {"review_required": True, "excluded": False,
                "issues": ["human review"], "already_screened": False}
        clean = {"review_required": False, "excluded": False,
                 "issues": [], "already_screened": False}
        result, state, screen, post = self._run_with_screens([held, held, held, clean])
        self.assertTrue(result)
        self.assertEqual(screen.call_count, 4)
        self.assertEqual([c["status"] for c in state["clips"][:3]], ["needs_review"] * 3)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args[2]["id"], 4)

    def test_review_only_slot_returns_failure_instead_of_green_success(self):
        held = {"review_required": True, "excluded": False,
                "issues": ["human review"], "already_screened": False}
        result, state, screen, post = self._run_with_screens([held] * 4)
        self.assertFalse(result)
        self.assertEqual(screen.call_count, 4)
        self.assertTrue(all(c["status"] == "needs_review" for c in state["clips"]))
        post.assert_not_called()

    def test_transient_promotion_screen_failure_requeues_after_cooldown_only(self):
        from datetime import timedelta, timezone
        now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
        state = {"clips": [
            {"id": 1, "status": "needs_review", "promotion_screen_error": "timeout",
             "promotion_retry_after": (now - timedelta(minutes=1)).isoformat(),
             "promotion_retry_count": 0},
            {"id": 2, "status": "needs_review", "promotion_screen_error": "timeout",
             "promotion_retry_after": (now - timedelta(minutes=1)).isoformat(),
             "promotion_retry_count": 0, "editorial_review_required": True},
            {"id": 3, "status": "needs_review", "promotion_screen_error": "timeout",
             "promotion_retry_after": (now + timedelta(hours=1)).isoformat(),
             "promotion_retry_count": 0},
        ]}
        changed = clipper.retry_transient_promotion_reviews(state, now=now)
        self.assertEqual(changed, 1)
        self.assertEqual(state["clips"][0]["status"], "queued")
        self.assertEqual(state["clips"][0]["promotion_retry_count"], 1)
        self.assertEqual(state["clips"][1]["status"], "needs_review")
        self.assertEqual(state["clips"][2]["status"], "needs_review")

    def test_qa_approved_clip_below_preferred_score_can_pass_fallback_floor(self):
        cfg = {"qa": {"min_ai_score": 6.5, "fallback_min_ai_score": 5.5}}
        with patch.object(clipper, "qa_checks", return_value={"issues": []}), \
             patch.object(clipper, "gemini_qa", return_value={
                 "approve": True, "unsafe": False, "score": 5.8,
                 "issues": ["minor pacing issue"],
             }):
            result = clipper.qa_review(cfg, "dummy.mp4", {})
        self.assertEqual(result["verdict"], "pass")
        self.assertTrue(result["fallback"])
        self.assertEqual(result["score"], 5.8)

    def test_gemini_rejection_does_not_use_fallback(self):
        cfg = {"qa": {"min_ai_score": 6.5, "fallback_min_ai_score": 5.5}}
        with patch.object(clipper, "qa_checks", return_value={"issues": []}), \
             patch.object(clipper, "gemini_qa", return_value={
                 "approve": False, "unsafe": False, "score": 6.2,
                 "issues": ["misleading opening"],
             }):
            result = clipper.qa_review(cfg, "dummy.mp4", {})
        self.assertEqual(result["verdict"], "soft_fail")
        self.assertFalse(result.get("fallback", False))

class YouTubeUploaderDependencyTests(unittest.TestCase):
    def test_google_oauth_and_youtube_client_dependencies_are_installed(self):
        # The uploader imports these lazily. Test them explicitly so CI catches
        # a missing dependency before a real Reel is published.
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        self.assertTrue(all((RefreshError, Request, Credentials, build, HttpError, MediaFileUpload)))

    def test_missing_youtube_oauth_secret_has_actionable_error(self):
        import youtube_uploader

        with patch.dict(os.environ, {"YT_TOKEN_JSON": ""}):
            with self.assertRaisesRegex(RuntimeError, "YT_TOKEN_JSON is not configured"):
                youtube_uploader._credentials_from_environment()


if __name__ == "__main__":
    unittest.main()
