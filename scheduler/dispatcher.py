"""Always-on PostgreSQL dispatcher.

T-0 performs only the Instagram media_publish call. Containers must be prepared
and processed before a slot enters container_ready. Ambiguous publish results
are persisted as publish_uncertain and never retried automatically.
"""
import argparse
import json
import logging
import os
import socket
import time
from datetime import datetime, timezone

import requests

from db import connect
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


def run_dispatcher(poll_seconds=1.0, claim_limit=50):
    dispatcher_id = os.environ.get("DISPATCHER_ID") or f"{socket.gethostname()}-{os.getpid()}"
    log.info("dispatcher online id=%s", dispatcher_id)
    while True:
        try:
            with connect() as conn, conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO dispatcher_heartbeats(dispatcher_id,ts,detail) VALUES (%s,now(),'{}'::jsonb) "
                        "ON CONFLICT(dispatcher_id) DO UPDATE SET ts=now(),detail='{}'::jsonb",
                        (dispatcher_id,),
                    )
                slots = claim_slots_near_due(conn, dispatcher_id, seconds_ahead=3,
                                             limit=claim_limit, lease_seconds=30)
            for slot in slots:
                slot_id, user_id, account_id, clip_id, container_id, run_at, max_late, attempts = slot
                delay = (run_at - datetime.now(timezone.utc)).total_seconds()
                if delay > 0:
                    # Never sleep beyond lease. If the slot is >20s away, release it
                    # instead of holding a lease while waiting.
                    if delay > 20:
                        with connect() as conn, conn.transaction():
                            transition_slot(conn, slot_id, "container_ready", "container_ready",
                                            dispatcher_id, lease_owner=None, lease_until=None)
                        continue
                    deadline = time.monotonic() + delay
                    while time.monotonic() < deadline:
                        with connect() as conn, conn.transaction():
                            if not heartbeat_slot(conn, slot_id, dispatcher_id, lease_seconds=30):
                                break
                        time.sleep(min(0.5, max(0.01, deadline - time.monotonic())))
                # Re-read account and use KMS decrypt adapter in deployment.
                with connect() as conn:
                    account_row = _load_slot_account(conn, slot_id, user_id)
                if account_row["status"] != "active":
                    with connect() as conn, conn.transaction():
                        transition_slot(conn, slot_id, "container_ready", "failed", dispatcher_id,
                                       last_error="connected account is not active",
                                       lease_owner=None, lease_until=None)
                    continue
                # Phase 1 local/dev secret adapter: account token may be stored as bytea.
                # TODO(verify): replace with KMS envelope decrypt in Phase 2 before production.
                token = os.environ.get("IG_ACCESS_TOKEN", "").strip()
                if not token:
                    raise RuntimeError("IG_ACCESS_TOKEN is not configured for Phase 1 dispatcher")
                _publish_one(slot, dispatcher_id,
                             {"external_id": account_row["external_id"], "access_token": token},
                             {"graph_version": os.environ.get("IG_GRAPH_VERSION", "v25.0")})
        except Exception:
            log.exception("dispatcher loop error; retrying next poll")
        time.sleep(poll_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_dispatcher()
