"""Postgres-backed worker pool for pre-slot production, rendering, QA and upload jobs."""
import argparse
import json
import logging
import os
import random
import socket
import threading
import time

from db import connect
from scheduler.queue import claim_jobs,finish_job

log=logging.getLogger("clipper.worker")


def _load_handlers():
    module_name=os.environ.get("CLIPPER_JOB_HANDLER_MODULE","").strip()
    if not module_name: return {}
    module=__import__(module_name,fromlist=["JOB_HANDLERS"])
    return getattr(module,"JOB_HANDLERS",{})


def _heartbeat(job_id,worker_id,stop_event):
    while not stop_event.wait(30):
        try:
            with connect() as conn,conn.transaction():
                from scheduler.queue import heartbeat_job
                if not heartbeat_job(conn,job_id,worker_id,lease_seconds=3600):
                    log.error("job lease lost id=%s; handler result will be CAS-guarded",job_id)
                    return
        except Exception:
            log.exception("job heartbeat failed id=%s",job_id)


def _record_exhausted(user_id,job_id,kind,payload,error):
    if not isinstance(payload,dict) or not payload.get("slot_id"): return
    slot_id=str(payload["slot_id"])
    try:
        with connect() as conn,conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE slots SET state='failed',last_error=%s,lease_owner=NULL,lease_until=NULL,
                       updated_at=now() WHERE id=%s AND user_id=%s
                       AND state IN ('assigned','preparing','verified','container_uploading')
                       RETURNING clip_id""",
                    (error,slot_id,user_id))
                row=cur.fetchone()
                if row:
                    if row[0] is not None:
                        cur.execute("UPDATE clips SET status='needs_review',updated_at=now() WHERE user_id=%s AND id=%s AND status='assigned'",
                                    (user_id,row[0]))
                    cur.execute(
                        """INSERT INTO events(user_id,slot_id,type,detail)
                           VALUES (%s,%s,'job_exhausted',%s::jsonb)""",
                        (user_id,slot_id,json.dumps({"job_id":str(job_id),"kind":kind,"error":error})))
    except Exception:
        log.exception("could not mark slot failed after job exhaustion")


def run_worker(poll_seconds=2):
    worker_id=os.environ.get("WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}"
    handlers=_load_handlers()
    log.info("worker online id=%s handlers=%s",worker_id,sorted(handlers))
    while True:
        try:
            with connect() as conn,conn.transaction():
                jobs=claim_jobs(conn,worker_id,limit=1,lease_seconds=3600)
        except Exception:
            log.exception("worker DB claim failed")
            time.sleep(poll_seconds)
            continue
        if not jobs:
            time.sleep(poll_seconds)
            continue

        job_id,kind,user_id,payload,attempts,max_attempts=jobs[0]
        stop_heartbeat=threading.Event()
        heartbeat_thread=threading.Thread(
            target=_heartbeat,args=(job_id,worker_id,stop_heartbeat),daemon=True)
        heartbeat_thread.start()
        try:
            handler=handlers.get(kind)
            if handler is None:
                raise RuntimeError(f"No handler registered for job kind {kind!r}")
            result=handler(user_id=str(user_id),payload=payload)
            if isinstance(result,dict) and result.get("retry_after_seconds"):
                delay=max(5,int(result["retry_after_seconds"]))
                exhausted=attempts>=max_attempts
                with connect() as conn,conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(
                            """UPDATE jobs SET state=%s,run_after=now()+(%s * interval '1 second'),
                               lease_owner=NULL,lease_until=NULL,last_error=%s,updated_at=now()
                               WHERE id=%s AND state='leased' AND lease_owner=%s AND lease_until > now()
                               RETURNING id""",
                            ("failed" if exhausted else "retry",delay,
                             str(result.get("reason") or "handler requested retry")[:1000],
                             job_id,worker_id))
                        updated=cur.fetchone() is not None
                if updated and exhausted:
                    _record_exhausted(user_id,job_id,kind,payload,
                                      str(result.get("reason") or "retry limit reached")[:1000])
                if exhausted:
                    log.error("job exhausted retries id=%s kind=%s",job_id,kind)
                else:
                    log.info("job deferred id=%s kind=%s in=%ss",job_id,kind,delay)
            else:
                with connect() as conn,conn.transaction():
                    finish_job(conn,job_id,worker_id,True)
                log.info("job succeeded id=%s kind=%s user_id=%s result=%s",
                         job_id,kind,user_id,str(result)[:160])
        except Exception as exc:
            error=f"{type(exc).__name__}: {exc}"[:1000]
            state="failed" if attempts>=max_attempts else "retry"
            delay=min(3600,15*(2**min(attempts-1,8)))*random.uniform(0.75,1.25)
            try:
                with connect() as conn,conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(
                            """UPDATE jobs SET state=%s,run_after=now()+(%s * interval '1 second'),
                               lease_owner=NULL,lease_until=NULL,last_error=%s,updated_at=now()
                               WHERE id=%s AND state='leased' AND lease_owner=%s AND lease_until > now()
                               RETURNING id""",
                            (state,int(delay),error,job_id,worker_id))
                        updated=cur.fetchone() is not None
                if updated and state=="failed":
                    _record_exhausted(user_id,job_id,kind,payload,error)
            except Exception:
                log.exception("could not update failed job id=%s",job_id)
            log.exception("job failed id=%s kind=%s state=%s",job_id,kind,state)
        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=2)


if __name__=="__main__":
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    run_worker()
