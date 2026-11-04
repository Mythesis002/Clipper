"""Always-on PostgreSQL dispatcher.

T-0 performs only the Instagram media_publish call. Containers must be prepared
and processed before a slot enters container_ready. Ambiguous publish results
are persisted as publish_uncertain and never retried automatically.
"""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import logging
import os
import socket
import time
from datetime import datetime, timezone

import requests
from contextlib import contextmanager

from db import connect
from scheduler.instagram_limits import assert_publish_quota_available
from scheduler.queue import (
    append_event, claim_slots_near_due, heartbeat_slot, transition_slot,
)

log = logging.getLogger("clipper.dispatcher")


def _publish_url(graph_version: str, account_external_id: str) -> str:
    return f"https://graph.facebook.com/{graph_version}/{account_external_id}/media_publish"


def _publish_one(slot, dispatcher_id: str, account: dict, cfg: dict) -> None:
    slot_id, user_id, account_id, clip_id, container_id, run_at, max_late, attempts = slot
    now = datetime.now(timezone.utc)
    late_limit = run_at + __import__("datetime").timedelta(minutes=max_late)
    if now > late_limit:
        with connect() as conn, conn.transaction():
            transition_slot(conn, slot_id, "container_ready", "missed",
                            dispatcher_id, last_error="slot exceeded max_late_minutes",
                            lease_owner=None, lease_until=None)
            append_event(conn, user_id, slot_id, "slot_missed",
                         {"now": now.isoformat(), "run_at": run_at.isoformat()})
        return

    # Verify that quota was checked during pre-slot container preparation.
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT quota_checked_at,quota_snapshot FROM slots WHERE id=%s",(slot_id,))
            quota_row=cur.fetchone()
    if not quota_row or not quota_row[0] or (now-quota_row[0]).total_seconds()>900:
        with connect() as conn, conn.transaction():
            transition_slot(conn,slot_id,"container_ready","failed",dispatcher_id,
                            last_error="pre-publish quota snapshot missing or stale",
                            lease_owner=None,lease_until=None)
            append_event(conn,user_id,slot_id,"publish_blocked_stale_quota",{})
        log.error("slot=%s blocked: stale quota snapshot; no media_publish call",slot_id)
        return

    # If process dies after persisting publishing but before/after HTTP call, the
    # reconciler must inspect recent media. Never auto-retry this state.
    with connect() as conn, conn.transaction():
        claimed = transition_slot(
            conn, slot_id, "container_ready", "publishing", dispatcher_id,
            publish_started_at=now, attempts=attempts + 1,
        )
        if not claimed:
            log.warning("slot lost lease or state changed; skipping publish slot=%s", slot_id)
            return
        append_event(conn, user_id, slot_id, "publish_started",
                     {"container_id": container_id, "run_at": run_at.isoformat()})

    try:
        response = requests.post(
            _publish_url(cfg.get("graph_version", "v25.0"), account["external_id"]),
            data={"creation_id": container_id, "access_token": account["access_token"]},
            timeout=(5, 30),
        )
    except requests.RequestException as exc:
        with connect() as conn, conn.transaction():
            transition_slot(conn, slot_id, "publishing", "publish_uncertain",
                            last_error=f"publish outcome unknown: {type(exc).__name__}",
                            lease_owner=None, lease_until=None)
            append_event(conn, user_id, slot_id, "publish_uncertain",
                         {"error_type": type(exc).__name__})
        log.error("publish uncertain slot=%s; not retrying", slot_id)
        return

    if response.status_code >= 500:
        with connect() as conn, conn.transaction():
            transition_slot(conn, slot_id, "publishing", "publish_uncertain",
                            last_error=f"Instagram HTTP {response.status_code}",
                            lease_owner=None, lease_until=None)
            append_event(conn, user_id, slot_id, "publish_uncertain",
                         {"http_status": response.status_code})
        return
    try:
        body = response.json()
    except ValueError:
        body = {}
    media_id = body.get("id") if response.ok and isinstance(body, dict) else None
    if not media_id:
        # A 4xx is normally a definite rejection; a successful response without
        # the expected media ID is ambiguous and needs reconciliation.
        target = "failed" if response.status_code < 500 and not response.ok else "publish_uncertain"
        with connect() as conn, conn.transaction():
            transition_slot(conn, slot_id, "publishing", target,
                            last_error=f"Instagram response HTTP {response.status_code}: {response.text[:250]}",
                            lease_owner=None, lease_until=None)
            append_event(conn, user_id, slot_id, target,
                         {"http_status": response.status_code})
        return

    with connect() as conn, conn.transaction():
        transition_slot(conn, slot_id, "publishing", "published",
                        media_id=str(media_id), published_at=datetime.now(timezone.utc),
                        lease_owner=None, lease_until=None, last_error=None)
        append_event(conn, user_id, slot_id, "published", {"media_id": str(media_id)})


def _load_slot_account(conn, slot_id, user_id):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT a.external_id,a.token_ciphertext,a.status,a.id
               FROM slots s JOIN connected_accounts a
                 ON a.id=s.account_id AND a.user_id=s.user_id
               WHERE s.id=%s AND s.user_id=%s""",
            (slot_id, user_id),
        )
        row = cur.fetchone()
        if not row:
            raise RuntimeError("slot account lookup failed")
        return {"external_id": row[0], "token_ciphertext": row[1],
                "status": row[2], "account_id": row[3]}


@contextmanager
def _account_publish_lock(account_id):
    """Session-level PostgreSQL advisory lock serializes posts for one IG account."""
    conn=connect()
    conn.autocommit=True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (str(account_id),))
        yield
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (str(account_id),))
        finally:
            conn.close()


def _dispatch_claimed_slot(slot, dispatcher_id):
    slot_id, user_id, account_id, clip_id, container_id, run_at, max_late, attempts = slot
    delay=(run_at-datetime.now(timezone.utc)).total_seconds()
    if delay>0:
        deadline=time.monotonic()+delay
        while time.monotonic()<deadline:
            with connect() as conn,conn.transaction():
                lease_ok=heartbeat_slot(conn,slot_id,dispatcher_id,lease_seconds=45)
            if not lease_ok:
                log.warning("slot lease lost during wait; skip slot=%s",slot_id)
                return
            time.sleep(min(0.25,max(0.01,deadline-time.monotonic())))
    with connect() as conn:
        account_row=_load_slot_account(conn,slot_id,user_id)
    if account_row["status"]!="active":
        with connect() as conn,conn.transaction():
            transition_slot(conn,slot_id,"container_ready","failed",dispatcher_id,
                            last_error="connected account is not active",
                            lease_owner=None,lease_until=None)
        return
    token=os.environ.get("IG_ACCESS_TOKEN","").strip()
    if not token:
        raise RuntimeError("IG_ACCESS_TOKEN is not configured for Phase 1 dispatcher")
    with _account_publish_lock(account_id):
        _publish_one(slot,dispatcher_id,
                     {"external_id":account_row["external_id"],"access_token":token},
                     {"graph_version":os.environ.get("IG_GRAPH_VERSION","v25.0")})


def run_dispatcher(poll_seconds=0.5, claim_limit=None):
    dispatcher_id=os.environ.get("DISPATCHER_ID") or f"{socket.gethostname()}-{os.getpid()}"
    max_workers=max(1,int(os.environ.get("PUBLISH_CONCURRENCY","300")))
    claim_limit=int(claim_limit or max_workers)
    log.info("dispatcher online id=%s concurrency=%d",dispatcher_id,max_workers)
    with ThreadPoolExecutor(max_workers=max_workers,thread_name_prefix="ig-publish") as pool:
        inflight=set()
        while True:
            try:
                completed={f for f in inflight if f.done()}
                for future in completed:
                    inflight.remove(future)
                    try: future.result()
                    except Exception: log.exception("slot dispatcher task failed")
                free=max(0,max_workers-len(inflight))
                if free:
                    with connect() as conn,conn.transaction():
                        with conn.cursor() as cur:
                            cur.execute(
                                "INSERT INTO dispatcher_heartbeats(dispatcher_id,ts,detail) VALUES (%s,now(),%s::jsonb) "
                                "ON CONFLICT(dispatcher_id) DO UPDATE SET ts=now(),detail=EXCLUDED.detail",
                                (dispatcher_id,json.dumps({"inflight":len(inflight),"capacity":max_workers})),
                            )
                        slots=claim_slots_near_due(conn,dispatcher_id,seconds_ahead=3,
                                                   limit=min(free,claim_limit),lease_seconds=45)
                    for slot in slots:
                        inflight.add(pool.submit(_dispatch_claimed_slot,slot,dispatcher_id))
                if inflight:
                    wait(inflight,timeout=poll_seconds,return_when=FIRST_COMPLETED)
                else:
                    time.sleep(poll_seconds)
            except Exception:
                log.exception("dispatcher loop error; retrying next poll")
                time.sleep(poll_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_dispatcher()
