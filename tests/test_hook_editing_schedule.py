import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import podcast_clipper as clipper
from editing_engine import build_edit_plan, validate_edit_plan


class HookSelectionTests(unittest.TestCase):
    def test_contextless_opening_beats_dangling_pronoun(self):
        strong = clipper.hook_score("Why do successful founders keep making this mistake?")
        weak = clipper.hook_score("And this is why they did it")
        self.assertGreater(strong, weak)

    def test_hook_score_is_bounded_and_empty_is_zero(self):
        self.assertEqual(clipper.hook_score(""), 0.0)
        self.assertGreaterEqual(clipper.hook_score("The truth about pricing is surprising"), 0.0)
        self.assertLessEqual(clipper.hook_score("The truth about pricing is surprising"), 1.0)


class PremiumCaptionPlanTests(unittest.TestCase):
    def test_educational_plan_uses_kinetic_caption_treatment(self):
        plan = build_edit_plan({"content_type": "insight"})
        self.assertEqual(plan["caption_style"], "kinetic")
        self.assertEqual(plan["caption_animation"], "pop")
        self.assertEqual(plan["title_duration_s"], 2.8)

    def test_invalid_animation_is_safely_normalized(self):
        plan = validate_edit_plan({"caption_animation": "run-arbitrary-filter", "title_duration_s": 999})
        self.assertEqual(plan["caption_animation"], "pop")
        self.assertEqual(plan["title_duration_s"], 5.0)


class WeekdayScheduleTests(unittest.TestCase):
    def test_due_slot_uses_weekday_config(self):
        cfg = {
            "timezone": "Asia/Kolkata",
            "post_times": ["19:00"],
            "weekly_post_times": {"Monday": ["19:00"], "Saturday": ["12:00"]},
            "schedule": {"max_wait_min": 25, "catchup_hours": 6},
        }
        now = datetime(2026, 11, 2, 18, 55, tzinfo=ZoneInfo("Asia/Kolkata"))
        result = clipper.due_slot(cfg, {"clips": []}, now=now)
        self.assertIsNotNone(result)
        self.assertEqual(result[0].strftime("%A %H:%M"), "Monday 19:00")

    def test_diagnostic_mentions_weekday_slots(self):
        cfg = {
            "timezone": "Asia/Kolkata",
            "post_times": ["19:00"],
            "weekly_post_times": {"Saturday": ["12:00"]},
            "schedule": {"max_wait_min": 25, "catchup_hours": 6},
        }
        now = datetime(2026, 11, 7, 11, 40, tzinfo=ZoneInfo("Asia/Kolkata"))
        diagnostic = clipper.posting_slot_diagnostic(cfg, {"clips": []}, now=now)
        self.assertIn("Saturday", diagnostic)
        self.assertIn("12:00", diagnostic)


class CaptionRenderTests(unittest.TestCase):
    def test_title_is_short_lived_and_kinetic_word_has_ass_animation(self):
        words = [(0.2, "This"), (0.5, "is"), (0.8, "important."), (1.2, "Next")]
        plan = build_edit_plan({"content_type": "insight"})
        with TemporaryDirectory() as temp:
            path = Path(temp) / "captions.ass"
            clipper.build_ass(words, 0, 20, "A useful insight", path, plan)
            rendered = path.read_text(encoding="utf-8")
        self.assertIn("Dialogue: 1,0:00:00.00,0:00:02.80", rendered)
        self.assertIn("\\fscx112\\fscy112\\t(0,120", rendered)


if __name__ == "__main__":
    unittest.main()
