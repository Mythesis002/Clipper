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


class DailyScheduleTests(unittest.TestCase):
    def test_due_slot_uses_one_daily_2pm_slot_on_weekdays_and_weekends(self):
        cfg = {
            "timezone": "Asia/Kolkata",
            "post_times": ["14:00"],
            "schedule": {"max_wait_min": 25, "catchup_hours": 7},
        }
        for now, expected in (
            (datetime(2026, 11, 2, 13, 55, tzinfo=ZoneInfo("Asia/Kolkata")), "Monday 14:00"),
            (datetime(2026, 11, 7, 13, 55, tzinfo=ZoneInfo("Asia/Kolkata")), "Saturday 14:00"),
            (datetime(2026, 11, 8, 13, 55, tzinfo=ZoneInfo("Asia/Kolkata")), "Sunday 14:00"),
        ):
            with self.subTest(now=now):
                result = clipper.due_slot(cfg, {"clips": []}, now=now)
                self.assertIsNotNone(result)
                self.assertEqual(result[0].strftime("%A %H:%M"), expected)

    def test_daily_slot_diagnostic_shows_one_2pm_slot(self):
        cfg = {
            "timezone": "Asia/Kolkata",
            "post_times": ["14:00"],
            "schedule": {"max_wait_min": 25, "catchup_hours": 7},
        }
        now = datetime(2026, 11, 7, 13, 40, tzinfo=ZoneInfo("Asia/Kolkata"))
        diagnostic = clipper.posting_slot_diagnostic(cfg, {"clips": []}, now=now)
        self.assertIn("configured_slots=14:00 IST", diagnostic)
        self.assertNotIn("weekly_slots", diagnostic)
        self.assertNotIn("Saturday", diagnostic)

    def test_catchup_allows_4_to_7_hours_late_but_expires_after_7_hours(self):
        cfg = {"timezone": "Asia/Kolkata", "post_times": ["14:00"],
               "schedule": {"max_wait_min": 25, "catchup_hours": 7}}
        tz = ZoneInfo("Asia/Kolkata")
        for now in (datetime(2026, 11, 6, 18, 0, tzinfo=tz),
                    datetime(2026, 11, 6, 20, 59, tzinfo=tz)):
            with self.subTest(now=now):
                self.assertIsNotNone(clipper.due_slot(cfg, {"clips": []}, now=now))
        expired = datetime(2026, 11, 6, 21, 1, tzinfo=tz)
        self.assertIsNone(clipper.due_slot(cfg, {"clips": []}, now=expired))

class CaptionRenderTests(unittest.TestCase):
    def test_title_is_short_lived_and_kinetic_word_has_ass_animation(self):
        words = [(0.2, "This"), (0.5, "is"), (0.8, "important."), (1.2, "Next")]
        plan = build_edit_plan({"content_type": "insight"})
        with TemporaryDirectory() as temp:
            path = Path(temp) / "captions.ass"
            clipper.build_ass(words, 0, 20, "A useful insight", path, plan)
            rendered = path.read_text(encoding="utf-8")
        self.assertIn("Dialogue: 1,0:00:00.00,0:00:02.80", rendered)
        self.assertIn(r"\fscx112\fscy112\t(0,120", rendered)


if __name__ == "__main__":
    unittest.main()
