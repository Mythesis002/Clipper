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
from scheduler.queue import append_event
from scheduler.materializer import materialize_active_users_if_due

log=logging.getLogger("clipper.planner")


def _enqueue(cur, user_id, kind, payload, priority, dedupe_key, run_after=None):
    max_attempts=100 if kind=="produce" else 5
    cur.execute(
        """INSERT INTO jobs(user_id,kind,payload,priority,run_after,dedupe_key,max_attempts)
           VALUES (%s,%s,%s::jsonb,%s,coalesce(%s,now()),%s,%s)
           ON CONFLICT(user_id,dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING""",
        (user_id,kind,json.dumps(payload),priority,run_after,dedupe_key,max_attempts),
    )


def swap_failed_clip(conn,user_id,slot_id,failed_clip_id,reason):
    """Quarantine a rejected clip and reserve the next eligible one for this slot.

    The slot is moved back to assigned with a fresh preparation job. If no clip
    is available, a produce job is queued; caller must enforce the slot deadline.
    """
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE clips SET status='quarantined',updated_at=now()
               WHERE user_id=%s AND id=%s AND status NOT IN ('posted','quarantined')""",
            (user_id,failed_clip_id))
        cur.execute(
            """SELECT c.id,c.payload FROM clips c
               WHERE c.user_id=%s AND c.id<>%s AND c.status IN ('queued','ready')
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
               LIMIT 1 FOR UPDATE SKIP LOCKED""",(user_id,failed_clip_id))
        candidate=cur.fetchone()
        if candidate:
            clip_id,clip_payload=candidate
            cur.execute(
                """UPDATE slots SET state='assigned',clip_id=%s,container_id=NULL,
                   verification_checked_at=NULL,last_error=%s,lease_owner=NULL,lease_until=NULL,
                   updated_at=now()
                   WHERE id=%s AND user_id=%s
                     AND state IN ('preparing','verified','container_uploading')
                   RETURNING id""",(clip_id,reason[:500],slot_id,user_id))
            if not cur.fetchone():
                return False
            cur.execute("UPDATE clips SET status='assigned',updated_at=now() WHERE user_id=%s AND id=%s",
                        (user_id,clip_id))
            _enqueue(cur,user_id,"prepare_clip",
                     {"slot_id":str(slot_id),"clip_id":int(clip_id),"clip":clip_payload},
                     5,f"slot:{slot_id}:prepare:{int(clip_id)}")
            append_event(conn,user_id,slot_id,"clip_replaced",
                         {"failed_clip_id":int(failed_clip_id),"replacement_clip_id":int(clip_id),
                          "reason":reason[:400]})
            return True

        cur.execute(
            """UPDATE slots SET state='assigned',clip_id=NULL,container_id=NULL,
               verification_checked_at=NULL,last_error=%s,lease_owner=NULL,lease_until=NULL,
               updated_at=now()
               WHERE id=%s AND user_id=%s
                 AND state IN ('preparing','verified','container_uploading')
               RETURNING id""",(reason[:500]+"; no replacement clip ready",slot_id,user_id))
        if not cur.fetchone():
            return False
        _enqueue(cur,user_id,"produce",{"slot_id":str(slot_id)},0,
                 f"slot:{slot_id}:produce",run_after=None)
        append_event(conn,user_id,slot_id,"clip_replacement_unavailable",
                     {"failed_clip_id":int(failed_clip_id),"reason":reason[:400]})
        return False


def assign_due_slots(conn, lead_minutes=360, limit=100):
    """CAS scheduled slots to assigned and reserve the best eligible ready clip."""
    assigned=0
    with conn.cursor() as cur:
        cur.execute(
            """SELECT s.id,s.user_id,s.account_id,s.run_at_utc,r.assignment_lead_minutes
               FROM slots s JOIN users u ON u.id=s.user_id
               JOIN schedule_rules r ON r.user_id=s.user_id AND r.account_id=s.account_id
                 AND r.weekday=(extract(isodow from s.local_date)::int - 1)
                 AND r.local_time=s.local_time AND r.active=true
               WHERE s.state='scheduled' AND u.status='active'
                 AND s.run_at_utc <= now() + (r.assignment_lead_minutes * interval '1 minute')
                 AND s.run_at_utc > now() - (s.max_late_minutes * interval '1 minute')
               ORDER BY s.run_at_utc LIMIT %s FOR UPDATE OF s SKIP LOCKED""",
            (limit,),
        )
        slots=cur.fetchall()
    for slot_id,user_id,account_id,run_at,assignment_lead in slots:
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
                       LIMIT 1 FOR UPDATE SKIP LOCKED""",
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
            """SELECT s.id,s.user_id,s.clip_id FROM slots s
               JOIN schedule_rules r ON r.user_id=s.user_id AND r.account_id=s.account_id
                 AND r.weekday=(extract(isodow from s.local_date)::int - 1)
                 AND r.local_time=s.local_time AND r.active=true
               WHERE s.state='verified' AND s.verification_checked_at IS NOT NULL
                 AND s.run_at_utc <= now() + (r.container_lead_minutes * interval '1 minute')
                 AND s.run_at_utc > now() - (s.max_late_minutes * interval '1 minute')
               ORDER BY s.run_at_utc LIMIT %s FOR UPDATE OF s SKIP LOCKED""",
            (limit,),
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
                 SELECT s.id,s.user_id,s.state FROM slots s
                 WHERE s.state IN ('scheduled','assigned','preparing','verified','container_uploading','container_ready')
                   AND s.run_at_utc + (s.max_late_minutes * interval '1 minute') < now()
                 ORDER BY s.run_at_utc LIMIT %s FOR UPDATE OF s SKIP LOCKED
               )
               UPDATE slots s SET state='missed',last_error='max_late_minutes exceeded',
                 lease_owner=NULL,lease_until=NULL,updated_at=now()
               FROM expired e WHERE s.id=e.id RETURNING s.id,s.user_id,e.state""",(limit,))
        rows=cur.fetchall()
        for slot_id,user_id,previous_state in rows:
            append_event(conn,user_id,slot_id,"slot_missed",
                         {"reason":"max_late_minutes exceeded","previous_state":previous_state})
        return len(rows)


def quarantine_stuck_publishing(conn,limit=200):
    """Convert expired publish leases to uncertain; never retry an external publish."""
    with conn.cursor() as cur:
        cur.execute(
            """WITH stuck AS (
                 SELECT id,user_id FROM slots
                 WHERE state='publishing' AND lease_until IS NOT NULL AND lease_until < now()
                 ORDER BY publish_started_at LIMIT %s FOR UPDATE SKIP LOCKED
               )
               UPDATE slots s SET state='publish_uncertain',
                 last_error='dispatcher lease expired after publish_started; reconcile provider outcome',
                 lease_owner=NULL,lease_until=NULL,updated_at=now()
               FROM stuck x WHERE s.id=x.id RETURNING s.id,s.user_id""",(limit,))
        rows=cur.fetchall()
        for slot_id,user_id in rows:
            append_event(conn,user_id,slot_id,"publish_uncertain",
                         {"reason":"dispatcher lease expired after publish_started"})
        return len(rows)

def enqueue_verification_jobs(conn, lead_minutes=30, limit=200):
    count=0
    with conn.cursor() as cur:
        cur.execute(
            """SELECT s.id,s.user_id,s.clip_id FROM slots s
               JOIN schedule_rules r ON r.user_id=s.user_id AND r.account_id=s.account_id
                 AND r.weekday=(extract(isodow from s.local_date)::int - 1)
                 AND r.local_time=s.local_time AND r.active=true
               WHERE s.state='verified' AND s.verification_checked_at IS NULL
                 AND s.run_at_utc <= now() + (r.verify_lead_minutes * interval '1 minute')
                 AND s.run_at_utc > now() - (s.max_late_minutes * interval '1 minute')
               ORDER BY s.run_at_utc LIMIT %s FOR UPDATE OF s SKIP LOCKED""",
            (limit,))
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
                materialized=materialize_active_users_if_due(conn,interval_minutes=60,horizon_days=7)
                assigned=assign_due_slots(conn)
                verified=enqueue_verification_jobs(conn)
                containers=enqueue_container_uploads(conn)
                missed=mark_expired_slots(conn)
                uncertain=quarantine_stuck_publishing(conn)
            if materialized or assigned or verified or containers or missed or uncertain:
                log.info("planner materialized=%d assigned=%d verify_jobs=%d container_jobs=%d missed=%d publish_uncertain=%d",
                         materialized,assigned,verified,containers,missed,uncertain)
        except Exception:
            log.exception("planner failed this iteration")
        time.sleep(poll_seconds)


if __name__=="__main__":
    logging.basicConfig(level=logging.INFO)
    run_planner()
