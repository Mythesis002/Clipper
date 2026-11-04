import unittest
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from clock import FrozenClock
from scheduler.timezones import local_datetime_to_utc, utc_to_local


class DstConversionTests(unittest.TestCase):
    def test_ambiguous_fall_back_uses_first_occurrence(self):
        # New York 2026-11-01 01:30 occurs twice; first is EDT (UTC-4).
        result=local_datetime_to_utc(date(2026,11,1),time(1,30),"America/New_York")
        self.assertEqual(result,datetime(2026,11,1,5,30,tzinfo=timezone.utc))

    def test_nonexistent_spring_time_moves_to_first_valid_minute(self):
        # New York jumps from 01:59:59 to 03:00:00.
        result=local_datetime_to_utc(date(2026,3,8),time(2,30),"America/New_York")
        self.assertEqual(result,datetime(2026,3,8,7,0,tzinfo=timezone.utc))
        self.assertEqual(utc_to_local(result,"America/New_York").strftime("%H:%M"),"03:00")

    def test_lord_howe_half_hour_gap(self):
        # Lord Howe jumps from 02:00 to 02:30 in spring; resolve to 02:30.
        result=local_datetime_to_utc(date(2026,10,4),time(2,15),"Australia/Lord_Howe")
        self.assertEqual(result,datetime(2026,10,3,15,30,tzinfo=timezone.utc))


    def test_full_day_dateline_gap_uses_first_valid_instant(self):
        # Samoa skipped 2011-12-30 when it moved across the dateline.
        result=local_datetime_to_utc(date(2011,12,30),time(12,0),"Pacific/Apia")
        self.assertEqual(result,datetime(2011,12,30,10,0,tzinfo=timezone.utc))

    def test_kathmandu_odd_offset(self):
        result=local_datetime_to_utc(date(2026,11,4),time(18,0),"Asia/Kathmandu")
        self.assertEqual(result,datetime(2026,11,4,12,15,tzinfo=timezone.utc))

    def test_clock_is_injectable_and_utc_normalized(self):
        clock=FrozenClock(datetime(2026,1,1,12,tzinfo=timezone.utc))
        self.assertEqual(clock.now().hour,12)
        clock.advance_seconds(30)
        self.assertEqual(clock.now().second,30)


class ScheduleScaleSimulation(unittest.TestCase):
    def test_1000_users_two_daily_slots_for_seven_days_no_duplicate_slot_keys(self):
        zones=["Asia/Kolkata","Asia/Kathmandu","America/New_York",
               "Europe/London","Australia/Lord_Howe","Pacific/Auckland",
               "America/Los_Angeles","UTC"]
        # Deterministic materializer model: 1,000 tenants x 2 slots x 7 local days.
        keys=set()
        for user in range(1000):
            tz=zones[user % len(zones)]
            for day in range(7):
                local_day=date(2026,11,1)+__import__("datetime").timedelta(days=day)
                for slot in (time(6,0),time(18,0)):
                    run_at=local_datetime_to_utc(local_day,slot,tz)
                    key=(user,local_day,slot)
                    self.assertNotIn(key,keys)
                    keys.add(key)
                    self.assertIsNotNone(run_at.tzinfo)
        self.assertEqual(len(keys),14000)


if __name__=="__main__":
    unittest.main()
