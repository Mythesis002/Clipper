import unittest

import editing_engine
import intelligence_engine
import learning_engine
import podcast_clipper as clipper


class IntelligentSystemTests(unittest.TestCase):
    def test_timed_transcript_keeps_timestamped_blocks(self):
        text = intelligence_engine.timed_transcript(
            [(1.0, "Hello"), (2.0, "world"), (22.0, "Next"), (23.0, "topic")],
            bucket_seconds=20,
        )
        self.assertIn("[00:00] Hello world", text)
        self.assertIn("[00:20] Next topic", text)

    def test_episode_map_drops_invalid_and_out_of_range_moments(self):
        result = intelligence_engine.validate_episode_map({
            "summary": "A business conversation",
            "topics": [
                {"name": "valid", "start": 0, "end": 20},
                {"name": "invalid", "start": 30, "end": 10},
            ],
            "moments": [
                {"start": 5, "end": 25, "kind": "story", "confidence": 0.9},
                {"start": float("nan"), "end": 40, "kind": "promotion", "confidence": 0.9},
                {"start": 80, "end": 90, "kind": "insight", "confidence": 0.8},
            ],
        }, duration=60)
        self.assertEqual(len(result["topics"]), 1)
        self.assertEqual(len(result["moments"]), 1)
        self.assertEqual(result["moments"][0]["kind"], "story")

    def test_semantic_window_aligns_to_transcript_words(self):
        words = [(float(t), f"word{t}") for t in range(0, 60)]
        episode_map = {"moments": [{
            "start": 10, "end": 35, "kind": "story", "topic": "failure",
            "summary": "A complete story", "confidence": 0.9,
            "promotion_type": "none",
        }]}
        candidates = intelligence_engine.semantic_candidate_windows(
            episode_map, words, min_seconds=15, max_seconds=45, duration=60)
        self.assertEqual(len(candidates), 1)
        self.assertGreaterEqual(candidates[0]["end"] - candidates[0]["start"], 15)
        self.assertLessEqual(candidates[0]["end"] - candidates[0]["start"], 45)
        self.assertTrue(candidates[0]["text"].startswith("word"))

    def test_high_confidence_sponsor_read_requires_review_by_default(self):
        decision = intelligence_engine.editorial_gate({
            "content_type": "promotion", "promotion_type": "sponsor_read",
            "promotion_confidence": 0.91, "semantic_confidence": 0.91,
        })
        self.assertTrue(decision["eligible"])
        self.assertTrue(decision["review_required"])
        self.assertEqual(decision["reason"], "promotional_content")

    def test_uncertain_promotion_with_high_confidence_requires_review(self):
        decision = intelligence_engine.editorial_gate({
            "content_type": "story", "promotion_type": "unclear",
            "promotion_confidence": 0.86, "classification_confidence": 0.86,
        })
        self.assertTrue(decision["review_required"])

    def test_later_non_promo_rerank_cannot_erase_stronger_episode_promo_signal(self):
        candidates = [{
            "start": 10, "end": 30, "pre": 0.8,
            "promotion_type": "sponsor_read", "promotion_confidence": 0.92,
            "semantic_kind": "story", "semantic_confidence": 0.90,
        }]
        clipper.apply_rerank(candidates, [{
            "id": 0, "hook": 8, "standalone": 8, "emotion": 8,
            "content_type": "story", "promotion_type": "none",
            "promotion_confidence": 0.0, "classification_confidence": 0.9,
        }])
        self.assertEqual(candidates[0]["promotion_type"], "sponsor_read")
        decision = intelligence_engine.editorial_gate(candidates[0])
        self.assertTrue(decision["review_required"])

    def test_promotional_exclusion_policy_is_explicit(self):
        decision = intelligence_engine.editorial_gate({
            "content_type": "promotion", "promotion_type": "affiliate",
            "promotion_confidence": 0.95,
        }, promotion_policy="exclude")
        self.assertFalse(decision["eligible"])
        self.assertFalse(decision["review_required"])

    def test_casual_product_mention_is_not_automatically_promotion(self):
        decision = intelligence_engine.editorial_gate({
            "content_type": "insight", "promotion_type": "none",
            "promotion_confidence": 0.0, "classification_confidence": 0.9,
        })
        self.assertTrue(decision["eligible"])
        self.assertFalse(decision["review_required"])

    def test_edit_plan_varies_by_content_type_and_is_bounded(self):
        story = editing_engine.build_edit_plan({"content_type": "story"})
        lesson = editing_engine.build_edit_plan({"content_type": "explanation"})
        self.assertEqual(story["format"], "story")
        self.assertEqual(lesson["format"], "educational")
        self.assertEqual(story["motion"], "subtle")
        self.assertEqual(lesson["caption_group_words"], 3)
        unsafe = editing_engine.validate_edit_plan({
            "format": "arbitrary", "caption_group_words": 999,
            "caption_font_size": 999, "crop_strategy": "execute-filter",
        })
        self.assertEqual(unsafe["format"], "general")
        self.assertEqual(unsafe["caption_group_words"], 6)
        self.assertEqual(unsafe["caption_font_size"], 84)
        self.assertEqual(unsafe["crop_strategy"], "face_track")
        self.assertFalse(unsafe["broll"])
        self.assertFalse(unsafe["camera_cutting"])

    def test_candidate_decisions_include_selected_and_unselected_candidates(self):
        state = {"candidate_decisions": []}
        candidates = [
            {"start": 10, "end": 30, "final": 0.9,
             "learning_features": {"content_type": "story"}},
            {"start": 50, "end": 70, "final": 0.7,
             "learning_features": {"content_type": "insight"}},
        ]
        count = learning_engine.record_candidate_decisions(
            state, "episode-1", "channel", candidates, [(10, 30)],
            recorded_at="2026-01-01T00:00:00+00:00",
        )
        self.assertEqual(count, 2)
        self.assertTrue(state["candidate_decisions"][0]["selected"])
        self.assertFalse(state["candidate_decisions"][1]["selected"])

    def test_feedback_is_persisted_with_reason_and_timestamp(self):
        state = {"clips": [{"id": 4, "status": "posted"}]}
        clip = learning_engine.add_feedback(
            state, 4, "promotional", reasons=["sales_pitch"],
            note="This is an integration read.",
            recorded_at="2026-01-01T00:00:00+00:00",
        )
        self.assertEqual(clip["human_label"], "promotional")
        self.assertEqual(clip["human_feedback"][0]["reasons"], ["sales_pitch"])

    def test_feedback_ranker_waits_for_both_classes_and_learns_direction(self):
        state = {"clips": []}
        self.assertIsNone(learning_engine.train_editorial_ranker(state))
        for i in range(24):
            good = i < 12
            state["clips"].append({
                "status": "posted",
                "human_label": "good" if good else "weak",
                "features": {
                    "hook": 0.9 if good else 0.2,
                    "editorial_score": 0.9 if good else 0.25,
                    "standalone_heuristic": 0.9 if good else 0.2,
                    "payoff_heuristic": 0.85 if good else 0.2,
                    "context_dependency": 0.05 if good else 0.8,
                    "duration_bucket": "20-29s",
                    "hook_type": "question" if good else "strong_statement",
                },
            })
        model = learning_engine.train_editorial_ranker(state, min_samples=20, min_per_class=6)
        self.assertIsNotNone(model)
        good_candidate = {
            "hook": 0.9, "editorial_score": 0.9, "standalone_heuristic": 0.9,
            "payoff_heuristic": 0.85, "context_dependency": 0.05,
            "duration_bucket": "20-29s", "hook_type": "question",
        }
        weak_candidate = {
            "hook": 0.2, "editorial_score": 0.25, "standalone_heuristic": 0.2,
            "payoff_heuristic": 0.2, "context_dependency": 0.8,
            "duration_bucket": "20-29s", "hook_type": "strong_statement",
        }
        self.assertGreater(
            learning_engine.predict_editorial_score(good_candidate, model),
            learning_engine.predict_editorial_score(weak_candidate, model),
        )

    def test_metrics_are_snapshotted_once_at_maturity_bands(self):
        clip = {"posted_at": "2026-01-01T00:00:00+00:00"}
        metrics = {"reach": 1000, "shares": 10, "average_watch_time": 12.5}
        first = learning_engine.add_metric_snapshot(
            clip, metrics, clip["posted_at"], "2026-01-02T01:00:00+00:00")
        second = learning_engine.add_metric_snapshot(
            clip, metrics, clip["posted_at"], "2026-01-04T01:00:00+00:00")
        third = learning_engine.add_metric_snapshot(
            clip, metrics, clip["posted_at"], "2026-01-08T01:00:00+00:00")
        self.assertEqual([x["band"] for x in first], ["24h"])
        self.assertEqual([x["band"] for x in second], ["72h"])
        self.assertEqual([x["band"] for x in third], ["7d"])
        self.assertEqual(len(clip["metric_snapshots"]), 3)


if __name__ == "__main__":
    unittest.main()
