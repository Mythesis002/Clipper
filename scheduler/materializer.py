"""Idempotent seven-day schedule materializer with IANA/DST-safe conversion."""
from datetime import date,datetime,time,timedelta,timezone
from zoneinfo import ZoneInfo


def local_datetime_to_utc(local_date: date, local_time: time, tz_name: str) -> datetime:
    """Resolve wall time: first occurrence for folds, first valid minute after gaps."""
    zone=ZoneInfo(tz_name)
    naive=datetime.combine(local_date,local_time.replace(tzinfo=None))
    for minute_offset in range(0,181):
        candidate=naive+timedelta(minutes=minute_offset)
        valid=[]
        for fold in (0,1):
            aware=candidate.replace(tzinfo=zone,fold=fold)
            roundtrip=aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
            if roundtrip==candidate:
                valid.append(aware.astimezone(timezone.utc))
        if valid:
            return min(valid)
    raise ValueError(f"Unable to resolve local schedule time {naive} in {tz_name}")


def materialize_user(conn,user_id,horizon_days=7,now_utc=None):
    if now_utc is None:
        with conn.cursor() as cur:
            cur.execute("SELECT now()")
            now_utc=cur.fetchone()[0]
    if now_utc.tzinfo is None:
        raise ValueError("materializer clock must return timezone-aware UTC")
    now_utc=now_utc.astimezone(timezone.utc)
    created=0
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.user_id',%s,true)",(str(user_id),))
            cur.execute("SELECT tz FROM users WHERE id=%s AND status='active'",(user_id,))
            row=cur.fetchone()
            if not row: return 0
            tz_name=row[0]
            ZoneInfo(tz_name)
            cur.execute(
                """SELECT r.account_id,r.weekday,r.local_time,r.max_late_minutes,
                          r.assignment_lead_minutes,r.verify_lead_minutes,r.container_lead_minutes
                   FROM schedule_rules r JOIN connected_accounts a
                     ON a.id=r.account_id AND a.user_id=r.user_id AND a.status='active'
                   WHERE r.user_id=%s AND r.active=true""",(user_id,))
            rules=cur.fetchall()
            local_today=now_utc.astimezone(ZoneInfo(tz_name)).date()
            for day_offset in range(horizon_days):
                local_date=local_today+timedelta(days=day_offset)
                weekday=local_date.weekday()  # Monday=0
                for account_id,rule_weekday,local_time,max_late,assignment_lead,verify_lead,container_lead in rules:
                    if rule_weekday!=weekday:
                        continue
                    run_at=local_datetime_to_utc(local_date,local_time,tz_name)
                    cur.execute(
                        """INSERT INTO slots
                           (user_id,account_id,local_date,local_time,tz_snapshot,run_at_utc,
                            max_late_minutes,assignment_lead_minutes,verify_lead_minutes,
                            container_lead_minutes,state)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'scheduled')
                           ON CONFLICT(account_id,local_date,local_time) DO UPDATE SET
                             tz_snapshot=EXCLUDED.tz_snapshot,run_at_utc=EXCLUDED.run_at_utc,
                             max_late_minutes=EXCLUDED.max_late_minutes,
                             assignment_lead_minutes=EXCLUDED.assignment_lead_minutes,
                             verify_lead_minutes=EXCLUDED.verify_lead_minutes,
                             container_lead_minutes=EXCLUDED.container_lead_minutes,
                             state='scheduled',last_error=NULL,updated_at=now()
                           WHERE slots.state='cancelled' AND slots.last_error='schedule_rule_changed'
                             AND slots.run_at_utc>now() AND slots.lease_owner IS NULL""",
                        (user_id,account_id,local_date,local_time,tz_name,run_at,max_late,
                         assignment_lead,verify_lead,container_lead))
                    created+=max(0,cur.rowcount)
    return created


def materialize_active_users(conn,horizon_days=7):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE status='active' ORDER BY id")
        users=[row[0] for row in cur.fetchall()]
    total=0
    for user_id in users:
        with conn.transaction():
            total+=materialize_user(conn,str(user_id),horizon_days=horizon_days)
    return total


def materialize_active_users_if_due(conn,interval_minutes=60,horizon_days=7):
    """Hourly idempotent materialization with a DB advisory lock across planner replicas."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_xact_lock(hashtextextended('clipper:materializer',0))")
        if not cur.fetchone()[0]:
            return 0
        cur.execute("SELECT last_run_at FROM scheduler_control WHERE key='materializer' FOR UPDATE")
        row=cur.fetchone()
        if row:
            cur.execute("SELECT now() - %s < (%s * interval '1 minute')",(row[0],interval_minutes))
            if cur.fetchone()[0]:
                return 0
        cur.execute(
            """INSERT INTO scheduler_control(key,last_run_at) VALUES ('materializer',now())
               ON CONFLICT(key) DO UPDATE SET last_run_at=now()""")
    return materialize_active_users(conn,horizon_days=horizon_days)
