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
import threading
from datetime import datetime, timezone

import requests
from contextlib import contextmanager

from db import connect
from scheduler.crypto import decrypt_token
from scheduler.provider_limits import provider_request
from scheduler.instagram_limits import assert_publish_quota_available
from scheduler.queue import (
    append_event, claim_slots_near_due, heartbeat_slot, transition_slot,
)

log = logging.getLogger("clipper.dispatcher")


def _publish_url(graph_version: str, account_external_id: str) -> str:
    return f"https://graph.facebook.com/{graph_version}/{account_external_id}/media_publish"


def _publish_one(slot, dispatcher_id: str, account: dict, cfg: dict, db_conn) -> None:
    slot_id,user_id,account_id,clip_id,container_id,run_at,max_late,attempts=slot
    with db_conn.cursor() as cur:
        cur.execute("SELECT now()")
        now=cur.fetchone()[0]
    late_limit=run_at+__import__("datetime").timedelta(minutes=max_late)
    if now>late_limit:
        with db_conn.transaction():
            transition_slot(db_conn,slot_id,"container_ready","missed",dispatcher_id,
                            last_error="slot exceeded max_late_minutes",
                            lease_owner=None,lease_until=None)
            append_event(db_conn,user_id,slot_id,"slot_missed",
                         {"now":now.isoformat(),"run_at":run_at.isoformat()})
        return

    # Quota was checked during T-10 container preparation. T-0 must only call
    # media_publish; stale snapshots fail closed without an extra Graph API call.
    with db_conn.cursor() as cur:
        cur.execute("SELECT quota_checked_at,quota_snapshot FROM slots WHERE id=%s",(slot_id,))
        quota_row=cur.fetchone()
    if not quota_row or not quota_row[0] or (now-quota_row[0]).total_seconds()>900:
        with db_conn.transaction():
            transition_slot(db_conn,slot_id,"container_ready","failed",dispatcher_id,
                            last_error="pre-publish quota snapshot missing or stale",
                            lease_owner=None,lease_until=None)
            append_event(db_conn,user_id,slot_id,"publish_blocked_stale_quota",{})
        log.error("slot=%s blocked: stale quota snapshot; no media_publish call",slot_id)
        return

    # Persist publish intent before making the irreversible external request.
    with db_conn.transaction():
        claimed=transition_slot(
            db_conn,slot_id,"container_ready","publishing",dispatcher_id,
            publish_started_at=now,attempts=attempts+1,
        )
        if not claimed:
            log.warning("slot lost lease or state changed; skip slot=%s",slot_id)
            return
        append_event(db_conn,user_id,slot_id,"publish_started",
                     {"container_id":container_id,"run_at":run_at.isoformat()})

    try:
        with provider_request("meta"):
            response=requests.post(
                _publish_url(cfg.get("graph_version","v25.0"),account["external_id"]),
                data={"creation_id":container_id,"access_token":account["access_token"]},
                timeout=(5,30),
            )
    except requests.RequestException as exc:
        with db_conn.transaction():
            transition_slot(db_conn,slot_id,"publishing","publish_uncertain",
                            last_error=f"publish outcome unknown: {type(exc).__name__}",
                            lease_owner=None,lease_until=None)
            append_event(db_conn,user_id,slot_id,"publish_uncertain",
                         {"error_type":type(exc).__name__})
        log.error("publish uncertain slot=%s; not retrying",slot_id)
        return

    if response.status_code>=500:
        with db_conn.transaction():
            transition_slot(db_conn,slot_id,"publishing","publish_uncertain",
                            last_error=f"Instagram HTTP {response.status_code}",
                            lease_owner=None,lease_until=None)
            append_event(db_conn,user_id,slot_id,"publish_uncertain",
                         {"http_status":response.status_code})
        return
    try: body=response.json()
    except ValueError: body={}
    media_id=body.get("id") if response.ok and isinstance(body,dict) else None
    if not media_id:
        target="failed" if response.status_code<500 and not response.ok else "publish_uncertain"
        with db_conn.transaction():
            transition_slot(db_conn,slot_id,"publishing",target,
                            last_error=f"Instagram response HTTP {response.status_code}: {response.text[:250]}",
                            lease_owner=None,lease_until=None)
            append_event(db_conn,user_id,slot_id,target,{"http_status":response.status_code})
        return

    with db_conn.transaction():
        with db_conn.cursor() as cur:
            cur.execute("SELECT now()")
            published_at=cur.fetchone()[0]
        transition_slot(db_conn,slot_id,"publishing","published",
                        media_id=str(media_id),published_at=published_at,
                        lease_owner=None,lease_until=None,last_error=None)
        append_event(db_conn,user_id,slot_id,"published",{"media_id":str(media_id)})
        if clip_id is not None:
            with db_conn.cursor() as cur:
                cur.execute(
                    """UPDATE clips SET status='posted',
                       payload=jsonb_set(
                         jsonb_set(
                           jsonb_set(
                             jsonb_set(payload,'{status}','"posted"'::jsonb,true),
                             '{media_id}',to_jsonb(%s::text),true),
                           '{posted_at}',to_jsonb(%s::text),true),
                         '{platforms,instagram}',%s::jsonb,true),
                       updated_at=now()
                       WHERE user_id=%s AND id=%s""",
                    (str(media_id),published_at.isoformat(),
                     json.dumps({"status":"posted","media_id":str(media_id),
                                 "url":f"https://www.instagram.com/reel/{media_id}/",
                                 "posted_at":published_at.isoformat()}),user_id,clip_id),
                )
                if os.environ.get("YOUTUBE_ENABLED","true").strip().lower()=="true":
                    cur.execute(
                        """SELECT id FROM connected_accounts WHERE user_id=%s AND platform='youtube'
                           AND status='active' AND token_ciphertext IS NOT NULL ORDER BY created_at DESC LIMIT 1""",
                        (user_id,))
                    youtube_account=cur.fetchone()
                    if youtube_account:
                        cur.execute(
                            """INSERT INTO jobs(user_id,kind,payload,priority,run_after,dedupe_key)
                               VALUES (%s,'youtube_upload',%s::jsonb,50,now(),%s)
                               ON CONFLICT(user_id,dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING""",
                            (user_id,json.dumps({"clip_id":int(clip_id)}),f"clip:{clip_id}:youtube"),
                        )
                        append_event(db_conn,user_id,slot_id,"youtube_upload_enqueued",{"clip_id":int(clip_id)})
                    else:
                        cur.execute(
                            """UPDATE clips SET payload=jsonb_set(payload,'{platforms,youtube}',
                               '{"status":"not_configured"}'::jsonb,true) WHERE user_id=%s AND id=%s""",
                            (user_id,clip_id),
                        )


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
        yield conn
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (str(account_id),))
        finally:
            conn.close()


def _dispatch_claimed_slot(slot, dispatcher_id, stop_event=None):
    slot_id,user_id,account_id,clip_id,container_id,run_at,max_late,attempts=slot
    with connect() as clock_conn:
        with clock_conn.cursor() as cur:
            cur.execute("SELECT now()")
            db_now=cur.fetchone()[0]
    delay=(run_at-db_now).total_seconds()
    if delay>0:
        deadline=time.monotonic()+delay
        while time.monotonic()<deadline:
            if stop_event is not None and stop_event.is_set():
                log.warning("shutdown requested while waiting; release slot lease id=%s",slot_id)
                with connect() as conn,conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute("UPDATE slots SET lease_owner=NULL,lease_until=NULL,updated_at=now() WHERE id=%s AND state='container_ready' AND lease_owner=%s",
                                    (slot_id,dispatcher_id))
                return
            with connect() as conn,conn.transaction():
                lease_ok=heartbeat_slot(conn,slot_id,dispatcher_id,lease_seconds=45)
            if not lease_ok:
                log.warning("slot lease lost during wait; skip slot=%s",slot_id)
                return
            time.sleep(min(0.25,max(0.01,deadline-time.monotonic())))
    if stop_event is not None and stop_event.is_set():
        with connect() as conn,conn.transaction():
            with conn.cursor() as cur:
                cur.execute("UPDATE slots SET lease_owner=NULL,lease_until=NULL,updated_at=now() WHERE id=%s AND state='container_ready' AND lease_owner=%s",
                            (slot_id,dispatcher_id))
        return
    # Account lock may block. Heartbeat the slot lease while waiting.
    lock_stop=threading.Event()
    def lock_heartbeat():
        while not lock_stop.wait(10):
            try:
                with connect() as heartbeat_conn,heartbeat_conn.transaction():
                    if not heartbeat_slot(heartbeat_conn,slot_id,dispatcher_id,lease_seconds=45):
                        log.error("slot lease lost waiting account lock id=%s",slot_id)
                        return
            except Exception:
                log.exception("account-lock lease heartbeat failed slot=%s",slot_id)
    heartbeat=threading.Thread(target=lock_heartbeat,daemon=True)
    heartbeat.start()
    try:
        with _account_publish_lock(account_id) as lock_conn:
            with lock_conn.cursor() as cur:
                cur.execute("SELECT now()")
                account_lock_now=cur.fetchone()[0]
            if account_lock_now>run_at+__import__("datetime").timedelta(minutes=max_late):
                with lock_conn.transaction():
                    transition_slot(lock_conn,slot_id,"container_ready","missed",dispatcher_id,
                                    last_error="slot deadline passed while waiting for account serialization",
                                    lease_owner=None,lease_until=None)
                    append_event(lock_conn,user_id,slot_id,"slot_missed",
                                 {"reason":"account serialization exceeded max_late_minutes"})
                return
            account_row=_load_slot_account(lock_conn,slot_id,user_id)
            if account_row["status"]!="active":
                with lock_conn.transaction():
                    transition_slot(lock_conn,slot_id,"container_ready","failed",dispatcher_id,
                                    last_error="connected account is not active",
                                    lease_owner=None,lease_until=None)
                return
            if not account_row.get("token_ciphertext"):
                raise RuntimeError("Instagram account has no encrypted access token; run bootstrap_user.py")
            token=decrypt_token(account_row["token_ciphertext"])
            _publish_one(slot,dispatcher_id,
                         {"external_id":account_row["external_id"],"access_token":token},
                         {"graph_version":os.environ.get("IG_GRAPH_VERSION","v25.0")},
                         db_conn=lock_conn)
    finally:
        lock_stop.set()
        heartbeat.join(timeout=2)
        if heartbeat.is_alive():
            log.error("account-lock heartbeat thread did not stop slot=%s",slot_id)



def run_dispatcher(poll_seconds=0.5, claim_limit=None, stop_event=None):
    dispatcher_id=os.environ.get("DISPATCHER_ID") or f"{socket.gethostname()}-{os.getpid()}"
    max_workers=max(1,int(os.environ.get("PUBLISH_CONCURRENCY","300")))
    claim_limit=int(claim_limit or max_workers)
    stop_event=stop_event or threading.Event()
    log.info("dispatcher online id=%s concurrency=%d",dispatcher_id,max_workers)
    with ThreadPoolExecutor(max_workers=max_workers,thread_name_prefix="ig-publish") as pool:
        inflight=set()
        while not stop_event.is_set():
            try:
                completed={f for f in inflight if f.done()}
                for future in completed:
                    inflight.remove(future)
                    try: future.result()
                    except Exception: log.exception("slot dispatcher task failed")
                free=max(0,max_workers-len(inflight))
                slots=[]
                with connect() as conn,conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO dispatcher_heartbeats(dispatcher_id,ts,detail) VALUES (%s,now(),%s::jsonb) "
                            "ON CONFLICT(dispatcher_id) DO UPDATE SET ts=now(),detail=EXCLUDED.detail",
                            (dispatcher_id,json.dumps({"inflight":len(inflight),"capacity":max_workers})),
                        )
                    if free:
                        slots=claim_slots_near_due(conn,dispatcher_id,seconds_ahead=3,
                                                   limit=min(free,claim_limit),lease_seconds=45)
                for slot in slots:
                    inflight.add(pool.submit(_dispatch_claimed_slot,slot,dispatcher_id,stop_event))
                if inflight:
                    wait(inflight,timeout=poll_seconds,return_when=FIRST_COMPLETED)
                else:
                    stop_event.wait(poll_seconds)
            except Exception:
                log.exception("dispatcher loop error; retrying next poll")
                stop_event.wait(poll_seconds)





if __name__ == "__main__":
    import signal
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Graceful shutdown stops new claims; in-flight publish requests finish or
    # become uncertain, while future slots release their leases for takeover.
    shutdown=threading.Event()
    def _signal_handler(_signum,_frame):
        shutdown.set()
    signal.signal(signal.SIGTERM,_signal_handler)
    signal.signal(signal.SIGINT,_signal_handler)
    run_dispatcher(stop_event=shutdown)

