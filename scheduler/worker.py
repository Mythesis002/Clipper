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
                jobs = claim_jobs(conn, worker_id, limit=1)
            if not jobs:
                time.sleep(poll_seconds)
                continue
            job_id, kind, user_id, payload, attempts, max_attempts = jobs[0]
            try:
                handler = handlers.get(kind)
                if handler is None:
                    raise RuntimeError(
                        f"No handler registered for job kind {kind!r}; set CLIPPER_JOB_HANDLER_MODULE"
                    )
                result = handler(user_id=str(user_id), payload=payload)
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
                log.exception("job failed id=%s kind=%s state=%s", job_id, kind, state)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_worker()
