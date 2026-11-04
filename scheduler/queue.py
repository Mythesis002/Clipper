"""Postgres jobs and slot lease primitives using SKIP LOCKED."""
from datetime import timedelta


def claim_jobs(conn, worker_id: str, limit: int = 1, lease_seconds: int = 120):
    """Claim ready jobs in priority order; expired leases are eligible for reclaim."""
    with conn.cursor() as cur:
        cur.execute(
            """WITH picked AS (
                 SELECT id FROM jobs
                 WHERE (state IN ('pending','retry') OR (state='leased' AND lease_until < now()))
                   AND run_after <= now()
                 ORDER BY priority ASC, run_after ASC, created_at ASC
                 FOR UPDATE SKIP LOCKED LIMIT %s
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
                 FOR UPDATE SKIP LOCKED LIMIT %s
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
                 FOR UPDATE SKIP LOCKED LIMIT %s
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
