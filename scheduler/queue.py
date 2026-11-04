"""Postgres jobs and slot lease primitives using SKIP LOCKED."""
import json
from datetime import timedelta


def _mark_exhausted_job_slots(conn, rows):
    """Fail slots whose job lease expired after exhausting its retry budget."""
    for job_id,user_id,payload,kind in rows:
        if not isinstance(payload,dict) or not payload.get("slot_id"):
            continue
        slot_id=str(payload["slot_id"])
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE slots SET state='failed',last_error=%s,lease_owner=NULL,lease_until=NULL,updated_at=now()
                   WHERE id=%s AND user_id=%s
                     AND state IN ('scheduled','assigned','preparing','verified','container_uploading')
                   RETURNING id""",
                (f"job {kind} exhausted retries after lease expiry",slot_id,user_id))
            if cur.fetchone():
                cur.execute(
                    """INSERT INTO events(user_id,slot_id,type,detail)
                       VALUES (%s,%s,'job_exhausted',%s::jsonb)""",
                    (user_id,slot_id,json.dumps({"job_id":str(job_id),"kind":kind,
                                               "reason":"worker lease expired at retry limit"})))


def claim_jobs(conn, worker_id: str, limit: int = 1, lease_seconds: int = 120):
    """Claim ready jobs in priority order; expired leases are eligible for reclaim."""
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE jobs SET state='failed',lease_owner=NULL,lease_until=NULL,
                   last_error='worker lease expired after max_attempts',updated_at=now()
               WHERE state='leased' AND lease_until < now() AND attempts >= max_attempts
               RETURNING id,user_id,payload,kind"""
        )
        _mark_exhausted_job_slots(conn,cur.fetchall())
        cur.execute(
            """WITH picked AS (
                 SELECT id FROM jobs
                 WHERE (state IN ('pending','retry') OR (state='leased' AND lease_until < now()))
                   AND attempts < max_attempts
                   AND run_after <= now()
                 ORDER BY priority ASC, run_after ASC, created_at ASC
                 LIMIT %s FOR UPDATE SKIP LOCKED
               )
               UPDATE jobs j SET state='leased', lease_owner=%s,
                   lease_until=now() + (%s * interval '1 second'),
                   attempts=j.attempts+1, updated_at=now()
               FROM picked WHERE j.id=picked.id
               RETURNING j.id,j.kind,j.user_id,j.payload,j.attempts,j.max_attempts""",
            (limit, worker_id, lease_seconds),
        )
        return cur.fetchall()


def heartbeat_job(conn, job_id, worker_id: str, lease_seconds: int = 120) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE jobs SET lease_until=now() + (%s * interval '1 second'), updated_at=now()
               WHERE id=%s AND state='leased' AND lease_owner=%s AND lease_until > now()
               RETURNING id""",
            (lease_seconds, job_id, worker_id),
        )
        return cur.fetchone() is not None


def finish_job(conn, job_id, worker_id: str, success: bool, error: str | None = None):
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE jobs SET state=%s, lease_owner=NULL, lease_until=NULL,
                   last_error=%s, updated_at=now()
               WHERE id=%s AND state='leased' AND lease_owner=%s
               RETURNING id""",
            ("succeeded" if success else "retry", (error or "")[:1000] or None, job_id, worker_id),
        )
        return cur.fetchone() is not None


def claim_due_slots(conn, dispatcher_id: str, limit: int = 100, lease_seconds: int = 30):
    """Claim ready slots in UTC order. The row lock + lease makes dispatch multi-replica safe."""
    with conn.cursor() as cur:
        cur.execute(
            """WITH picked AS (
                 SELECT id FROM slots
                 WHERE state='container_ready' AND run_at_utc <= now()
                   AND (lease_until IS NULL OR lease_until < now())
                 ORDER BY run_at_utc ASC
                 LIMIT %s FOR UPDATE SKIP LOCKED
               )
               UPDATE slots s SET lease_owner=%s,
                   lease_until=now() + (%s * interval '1 second'), updated_at=now()
               FROM picked WHERE s.id=picked.id
               RETURNING s.id,s.user_id,s.account_id,s.clip_id,s.container_id,
                         s.run_at_utc,s.max_late_minutes,s.attempts""",
            (limit, dispatcher_id, lease_seconds),
        )
        return cur.fetchall()


def claim_slots_near_due(conn, dispatcher_id: str, seconds_ahead: int = 8,
                          limit: int = 100, lease_seconds: int = 30):
    """Claim slots due now or soon; dispatchers sleep to run_at after claiming.

    Claims are bounded by an 8s horizon to reduce early lease ownership. The
    lease is refreshed while sleeping by the dispatcher heartbeat loop.
    """
    with conn.cursor() as cur:
        cur.execute(
            """WITH picked AS (
                 SELECT id FROM slots
                 WHERE state='container_ready'
                   AND run_at_utc <= now() + (%s * interval '1 second')
                   AND run_at_utc + (max_late_minutes * interval '1 minute') >= now()
                   AND (lease_until IS NULL OR lease_until < now())
                 ORDER BY run_at_utc ASC
                 LIMIT %s FOR UPDATE SKIP LOCKED
               )
               UPDATE slots s SET lease_owner=%s,
                   lease_until=now() + (%s * interval '1 second'), updated_at=now()
               FROM picked WHERE s.id=picked.id
               RETURNING s.id,s.user_id,s.account_id,s.clip_id,s.container_id,
                         s.run_at_utc,s.max_late_minutes,s.attempts""",
            (seconds_ahead, limit, dispatcher_id, lease_seconds),
        )
        return cur.fetchall()


def heartbeat_slot(conn, slot_id, dispatcher_id: str, lease_seconds: int = 30) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE slots SET lease_until=now() + (%s * interval '1 second'), updated_at=now()
               WHERE id=%s AND lease_owner=%s AND lease_until > now()
                 AND state='container_ready'
               RETURNING id""",
            (lease_seconds, slot_id, dispatcher_id),
        )
        return cur.fetchone() is not None


def transition_slot(conn, slot_id, expected_state: str, new_state: str,
                    dispatcher_id: str | None = None, **fields) -> bool:
    """CAS transition. Only columns explicitly allowlisted can be changed."""
    allowed = {"clip_id", "container_id", "media_id", "publish_started_at",
               "published_at", "attempts", "last_error", "lease_owner", "lease_until"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unsupported slot fields: {sorted(unknown)}")
    assignments = ["state=%s", "updated_at=now()"]
    values = [new_state]
    for name, value in fields.items():
        assignments.append(f"{name}=%s")
        values.append(value)
    where = "id=%s AND state=%s"
    values.extend([slot_id, expected_state])
    if dispatcher_id is not None:
        where += " AND lease_owner=%s AND lease_until > now()"
        values.append(dispatcher_id)
    with conn.cursor() as cur:
        cur.execute(f"UPDATE slots SET {', '.join(assignments)} WHERE {where} RETURNING id", values)
        return cur.fetchone() is not None


def append_event(conn, user_id, slot_id, event_type, detail=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO events(user_id,slot_id,type,detail) VALUES (%s,%s,%s,%s::jsonb)",
            (user_id, slot_id, event_type, __import__("json").dumps(detail or {})),
        )
