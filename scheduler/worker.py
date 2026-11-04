"""Worker process for pre-slot work; no media_publish call occurs here.

Phase 1 worker handlers are deliberately pluggable: clip production/render/QA
can be invoked by a deployment adapter without coupling the dispatcher to ffmpeg
or Gemini. A missing handler fails visibly and retries according to job policy.
"""
import argparse
import json
import logging
import os
import socket
import time
from pathlib import Path

from db import connect
from scheduler.queue import claim_jobs, finish_job

log = logging.getLogger("clipper.worker")


def _load_handlers():
    # Production integration entry point. Deployments can import a handler module
    # (e.g. package exposing JOB_HANDLERS) to call existing produce/render/QA.
    module_name = os.environ.get("CLIPPER_JOB_HANDLER_MODULE", "").strip()
    if not module_name:
        return {}
    module = __import__(module_name, fromlist=["JOB_HANDLERS"])
    return getattr(module, "JOB_HANDLERS", {})


def run_worker(poll_seconds=2):
    worker_id = os.environ.get("WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}"
    handlers = _load_handlers()
    log.info("worker online id=%s handlers=%s", worker_id, sorted(handlers))
    while True:
        with connect() as conn:
            with conn.transaction():
                jobs = claim_jobs(conn, worker_id, limit=1, lease_seconds=3600)
            if not jobs:
                time.sleep(poll_seconds)
                continue
            job_id, kind, user_id, payload, attempts, max_attempts = jobs[0]
            stop_heartbeat=threading.Event()
            heartbeat_thread=None
            def heartbeat_loop():
                while not stop_heartbeat.wait(30):
                    try:
                        with connect() as heartbeat_conn,heartbeat_conn.transaction():
                            from scheduler.queue import heartbeat_job
                            if not heartbeat_job(heartbeat_conn,job_id,worker_id,lease_seconds=3600):
                                log.error("job lease lost id=%s; worker must stop treating result as authoritative",job_id)
                                return
                    except Exception:
                        log.exception("job heartbeat failed id=%s",job_id)
            heartbeat_thread=threading.Thread(target=heartbeat_loop,daemon=True)
            heartbeat_thread.start()
            try:
                handler = handlers.get(kind)
                if handler is None:
                    raise RuntimeError(
                        f"No handler registered for job kind {kind!r}; set CLIPPER_JOB_HANDLER_MODULE"
                    )
                result = handler(user_id=str(user_id), payload=payload)
                if isinstance(result,dict) and result.get("retry_after_seconds"):
                    delay=max(5,int(result["retry_after_seconds"]))
                    with conn.transaction():
                        with conn.cursor() as cur:
                            cur.execute(
                                """UPDATE jobs SET state='retry',run_after=now()+(%s * interval '1 second'),
                                   lease_owner=NULL,lease_until=NULL,last_error=%s,updated_at=now()
                                   WHERE id=%s AND state='leased' AND lease_owner=%s""",
                                (delay,str(result.get("reason") or "handler requested retry")[:1000],job_id,worker_id))
                    log.info("job deferred id=%s kind=%s in=%ss",job_id,kind,delay)
                else:
                    with conn.transaction():
                        finish_job(conn, job_id, worker_id, True)
                    log.info("job succeeded id=%s kind=%s user_id=%s result=%s",
                             job_id, kind, user_id, str(result)[:160])
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"[:1000]
                state = "failed" if attempts >= max_attempts else "retry"
                delay = min(3600, 15 * (2 ** min(attempts - 1, 8)))
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(
                            """UPDATE jobs SET state=%s, run_after=now() + (%s * interval '1 second'),
                               lease_owner=NULL, lease_until=NULL, last_error=%s, updated_at=now()
                               WHERE id=%s AND lease_owner=%s""",
                            (state, delay, error, job_id, worker_id),
                        )
                if state=="failed" and isinstance(payload,dict) and payload.get("slot_id"):
                    try:
                        with connect() as fail_conn,fail_conn.transaction():
                            with fail_conn.cursor() as cur:
                                cur.execute("UPDATE slots SET state='failed',last_error=%s,updated_at=now() WHERE id=%s AND state IN ('assigned','container_uploading')",
                                            (error,str(payload["slot_id"])))
                                cur.execute("INSERT INTO events(user_id,slot_id,type,detail) VALUES (%s,%s,'job_exhausted',%s::jsonb)",
                                            (user_id,payload["slot_id"],json.dumps({"kind":kind,"error":error})))
                    except Exception:
                        log.exception("could not mark slot failed after job exhaustion")
                log.exception("job failed id=%s kind=%s state=%s", job_id, kind, state)
            finally:
                stop_heartbeat.set()
                if heartbeat_thread:
                    heartbeat_thread.join(timeout=2)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_worker()
