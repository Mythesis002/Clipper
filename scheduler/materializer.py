"""Idempotent schedule materializer; inserts next seven days of slots."""
from datetime import datetime, timedelta, time, timezone
from zoneinfo import ZoneInfo

from scheduler.timezones import local_datetime_to_utc


def materialize_user(conn, user_id: str, now_utc: datetime | None = None, horizon_days: int = 7) -> int:
    if now_utc is None:
        with conn.cursor() as cur:
            cur.execute("SELECT now()")
            now_utc = cur.fetchone()[0]
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    inserted = 0
    with conn.cursor() as cur:
        cur.execute("SELECT tz FROM users WHERE id=%s AND status='active'", (user_id,))
        row = cur.fetchone()
        if not row:
            return 0
        tz_name = row[0]
        ZoneInfo(tz_name)  # fail early for invalid timezone
        cur.execute(
            """SELECT id, weekday, local_time, max_late_minutes
               FROM schedule_rules WHERE user_id=%s AND active=true""",
            (user_id,),
        )
        rules = cur.fetchall()
        local_today = now_utc.astimezone(ZoneInfo(tz_name)).date()
        for day_offset in range(horizon_days + 1):
            local_date = local_today + timedelta(days=day_offset)
            weekday = local_date.weekday()  # Monday=0
            for account_id, rule_weekday, local_time, max_late in rules:
                if rule_weekday != weekday:
                    continue
                # PostgreSQL time may be returned as time with tzinfo=None.
                run_at = local_datetime_to_utc(local_date, local_time, tz_name)
                cur.execute(
                    """INSERT INTO slots
                       (user_id,account_id,local_date,local_time,tz_snapshot,run_at_utc,
                        state,max_late_minutes)
                       VALUES (%s,%s,%s,%s,%s,%s,'scheduled',%s)
                       ON CONFLICT(account_id,local_date,local_time) DO NOTHING
                       RETURNING id""",
                    (user_id, account_id, local_date, local_time, tz_name, run_at, max_late),
                )
                if cur.fetchone():
                    inserted += 1
    return inserted


def materialize_active_users(conn, horizon_days: int = 7) -> int:
    total = 0
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE status='active' ORDER BY id")
        users = [row[0] for row in cur.fetchall()]
    for user_id in users:
        with conn.transaction():
            total += materialize_user(conn, str(user_id), horizon_days=horizon_days)
    return total
