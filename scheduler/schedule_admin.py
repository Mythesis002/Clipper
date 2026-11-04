"""Schedule edits affect only future unclaimed slots."""
from datetime import datetime,timezone
from scheduler.materializer import materialize_user


def update_schedule_rule(conn,user_id,account_id,weekday,local_time,active=True,
                         max_late_minutes=20,assignment_lead_minutes=360,
                         verify_lead_minutes=30,container_lead_minutes=10):
    """Replace a weekday/time rule and regenerate only future unclaimed slots."""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.user_id',%s,true)",(str(user_id),))
            cur.execute(
                """UPDATE schedule_rules SET active=false,updated_at=now()
                   WHERE user_id=%s AND account_id=%s AND weekday=%s AND active=true
                     AND local_time<>%s""",(user_id,account_id,weekday,local_time))
            cur.execute(
                """INSERT INTO schedule_rules(user_id,account_id,weekday,local_time,active,
                   max_late_minutes,assignment_lead_minutes,verify_lead_minutes,container_lead_minutes)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(account_id,weekday,local_time) DO UPDATE SET
                     active=EXCLUDED.active,max_late_minutes=EXCLUDED.max_late_minutes,
                     assignment_lead_minutes=EXCLUDED.assignment_lead_minutes,
                     verify_lead_minutes=EXCLUDED.verify_lead_minutes,
                     container_lead_minutes=EXCLUDED.container_lead_minutes,updated_at=now()""",
                (user_id,account_id,weekday,local_time,active,max_late_minutes,
                 assignment_lead_minutes,verify_lead_minutes,container_lead_minutes))
            cur.execute(
                """UPDATE slots SET state='cancelled',last_error='schedule_rule_changed',
                   lease_owner=NULL,lease_until=NULL,updated_at=now()
                   WHERE user_id=%s AND account_id=%s AND state='scheduled'
                     AND run_at_utc>now()""",(user_id,account_id))
        return materialize_user(conn,str(user_id),horizon_days=7)


def update_user_timezone(conn,user_id,tz_name):
    """Change IANA timezone; regenerate only future scheduled/unclaimed slots."""
    from zoneinfo import ZoneInfo
    ZoneInfo(tz_name)  # validate before touching the DB
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.user_id',%s,true)",(str(user_id),))
            cur.execute("UPDATE users SET tz=%s,updated_at=now() WHERE id=%s",(tz_name,user_id))
            cur.execute(
                """UPDATE slots SET state='cancelled',last_error='schedule_rule_changed',
                   lease_owner=NULL,lease_until=NULL,updated_at=now()
                   WHERE user_id=%s AND state='scheduled' AND run_at_utc>now()""",(user_id,))
        return materialize_user(conn,str(user_id),horizon_days=7)
