"""Slot planner: assigns clips early and enqueues preparation/container jobs.

This process does no rendering, Gemini work, downloads, or publishing. It only
uses PostgreSQL transactions to advance slots and enqueue idempotent jobs.
"""
import json
import logging
import os
import time
from datetime import timedelta

from db import connect
from scheduler.queue import append_event\nfrom scheduler.materializer import materialize_active_users

log=logging.getLogger("clipper.planner")


def _enqueue(cur, user_id, kind, payload, priority, dedupe_key, run_after=None):
    cur.execute(
        """INSERT INTO jobs(user_id,kind,payload,priority,run_after,dedupe_key)
           VALUES (%s,%s,%s::jsonb,%s,coalesce(%s,now()),%s)
           ON CONFLICT(user_id,dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING""",
        (user_id,kind,json.dumps(payload),priority,run_after,dedupe_key),
    )


def assign_due_slots(conn, lead_minutes=360, limit=100):
    """CAS scheduled slots to assigned and reserve the best eligible ready clip."""
    assigned=0
    with conn.cursor() as cur:
        cur.execute(
            """SELECT s.id,s.user_id,s.account_id,s.run_at_utc
               FROM slots s JOIN users u ON u.id=s.user_id
               WHERE s.state='scheduled' AND u.status='active'
                 AND s.run_at_utc <= now() + (%s * interval '1 minute')
                 AND s.run_at_utc > now() - (s.max_late_minutes * interval '1 minute')
               ORDER BY s.run_at_utc FOR UPDATE OF s SKIP LOCKED LIMIT %s""",
            (lead_minutes,limit),
        )
        slots=cur.fetchall()
    for slot_id,user_id,account_id,run_at in slots:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT c.id,c.payload FROM clips c
                       WHERE c.user_id=%s AND c.status IN ('queued','ready')
                         AND EXISTS (
                           SELECT 1 FROM sources src
                           WHERE src.user_id=c.user_id
                             AND src.youtube_handle=c.payload->>'source_handle'
                             AND src.permission_attested=true AND src.status='active'
                         )
                         AND NOT EXISTS (
                           SELECT 1 FROM slots x WHERE x.user_id=c.user_id
                             AND x.clip_id=c.id AND x.state NOT IN ('published','missed','failed','cancelled')
                         )
                       ORDER BY c.score DESC NULLS LAST,c.created_at,c.id
                       FOR UPDATE SKIP LOCKED LIMIT 1""",
                    (user_id,),
                )
                clip=cur.fetchone()
                if not clip:
                    cur.execute(
                        """UPDATE slots SET state='assigned',last_error='no ready clip; production requested',
                           updated_at=now() WHERE id=%s AND state='scheduled' RETURNING id""",
                        (slot_id,),
                    )
                    if cur.fetchone():
                        _enqueue(cur,user_id,"produce",{"slot_id":str(slot_id)},0,
                                 f"slot:{slot_id}:produce",run_after=None)
                        append_event(conn,user_id,slot_id,"buffer_empty_produce_enqueued",{})
                    continue
                clip_id,payload=clip
                cur.execute(
                    """UPDATE slots SET state='assigned',clip_id=%s,updated_at=now()
                       WHERE id=%s AND state='scheduled' RETURNING id""",
                    (clip_id,slot_id),
                )
                if not cur.fetchone():
                    continue
                cur.execute("UPDATE clips SET status='assigned',updated_at=now() WHERE user_id=%s AND id=%s",
                            (user_id,clip_id))
                _enqueue(cur,user_id,"prepare_clip",
                         {"slot_id":str(slot_id),"clip_id":int(clip_id),"clip":payload},
                         10,f"slot:{slot_id}:prepare:{int(clip_id)}")
                append_event(conn,user_id,slot_id,"slot_assigned",{"clip_id":int(clip_id)})
                assigned+=1
    return assigned


def enqueue_container_uploads(conn, lead_minutes=10, limit=200):
    count=0
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id,user_id,clip_id FROM slots
               WHERE state='verified' AND verification_checked_at IS NOT NULL
                 AND run_at_utc <= now() + (%s * interval '1 minute')
                 AND run_at_utc > now() - (max_late_minutes * interval '1 minute')
               ORDER BY run_at_utc FOR UPDATE SKIP LOCKED LIMIT %s""",
            (lead_minutes,limit),
        )
        slots=cur.fetchall()
    for slot_id,user_id,clip_id in slots:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT payload FROM clips WHERE user_id=%s AND id=%s",
                            (user_id,clip_id))
                row=cur.fetchone()
                if not row:
                    cur.execute("UPDATE slots SET state='failed',last_error='clip row missing' WHERE id=%s AND state='verified'",(slot_id,))
                    continue
                cur.execute(
                    """UPDATE slots SET state='container_uploading',updated_at=now()
                       WHERE id=%s AND state='verified' RETURNING id""",(slot_id,))
                if not cur.fetchone():
                    continue
                _enqueue(cur,user_id,"upload_container",
                         {"slot_id":str(slot_id),"clip_id":int(clip_id),"clip":row[0]},
                         5,f"slot:{slot_id}:container")
                append_event(conn,user_id,slot_id,"container_upload_enqueued",{})
                count+=1
    return count


def mark_expired_slots(conn, limit=500):
    """Mark unpublishable slots missed after the user's late deadline; never touch publishing."""
    with conn.cursor() as cur:
        cur.execute(
            """WITH expired AS (
                 SELECT id,user_id FROM slots
                 WHERE state IN ('scheduled','assigned','verified','container_uploading','container_ready')
                   AND run_at_utc + (max_late_minutes * interval '1 minute') < now()
                 ORDER BY run_at_utc FOR UPDATE SKIP LOCKED LIMIT %s
               )
               UPDATE slots s SET state='missed',last_error='max_late_minutes exceeded',
                 lease_owner=NULL,lease_until=NULL,updated_at=now()
               FROM expired e WHERE s.id=e.id RETURNING s.id,s.user_id""",(limit,))
        rows=cur.fetchall()
        for slot_id,user_id in rows:
            append_event(conn,user_id,slot_id,"slot_missed",{"reason":"max_late_minutes exceeded"})
        return len(rows)


def enqueue_verification_jobs(conn, lead_minutes=30, limit=200):
    count=0
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id,user_id,clip_id FROM slots
               WHERE state='verified' AND verification_checked_at IS NULL
                 AND run_at_utc <= now() + (%s * interval '1 minute')
                 AND run_at_utc > now() - (max_late_minutes * interval '1 minute')
               ORDER BY run_at_utc FOR UPDATE SKIP LOCKED LIMIT %s""",
            (lead_minutes,limit))
        slots=cur.fetchall()
    for slot_id,user_id,clip_id in slots:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT storage_key FROM clips WHERE user_id=%s AND id=%s",(user_id,clip_id))
                row=cur.fetchone()
                if not row or not row[0]:
                    cur.execute("UPDATE slots SET state='failed',last_error='rendered clip storage key missing' WHERE id=%s AND state='verified'",(slot_id,))
                    append_event(conn,user_id,slot_id,"slot_verification_failed",{"reason":"storage key missing"})
                    continue
                _enqueue(cur,user_id,"verify_slot",{"slot_id":str(slot_id),"clip_id":int(clip_id),"storage_key":row[0]},
                         8,f"slot:{slot_id}:verify")
                count+=1
    return count


def run_planner(poll_seconds=5):
    while True:
        try:
            with connect() as conn,conn.transaction():
                materialized=materialize_active_users(conn,horizon_days=7)
                assigned=assign_due_slots(conn)
                verified=enqueue_verification_jobs(conn)
                containers=enqueue_container_uploads(conn)
                missed=mark_expired_slots(conn)
            if materialized or assigned or verified or containers or missed:
                log.info("planner materialized=%d assigned=%d verify_jobs=%d container_jobs=%d missed=%d",
                         materialized,assigned,verified,containers,missed)
        except Exception:
            log.exception("planner failed this iteration")
        time.sleep(poll_seconds)


if __name__=="__main__":
    logging.basicConfig(level=logging.INFO)
    run_planner()
