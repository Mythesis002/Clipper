"""Legacy pipeline adapter: persist old state shape in Postgres and mirror clip records.

This is a transition adapter for the existing produce()/editorial code. It does
not make JSONB the scheduler queue: slot claims, leases, and publishes remain
normalized PostgreSQL rows.
"""
import json
import os
from pathlib import Path

from db import connect
from scheduler.store import JsonFileStateStore, PostgresStateStore

BASE = Path(__file__).resolve().parent
JSON_STORE = JsonFileStateStore(BASE / "state" / "state.json")


def _user_id():
    value = os.environ.get("CLIPPER_USER_ID", "").strip()
    if not value:
        raise RuntimeError("CLIPPER_USER_ID is required when CLIPPER_STATE_BACKEND=postgres")
    return value


def load_state():
    if os.environ.get("CLIPPER_STATE_BACKEND", "json").strip().lower() != "postgres":
        return JSON_STORE.load()
    user_id = _user_id()
    store = PostgresStateStore(connect, user_id)
    state = store.load()
    return state


def save_state(state):
    backend = os.environ.get("CLIPPER_STATE_BACKEND", "json").strip().lower()
    if backend != "postgres":
        JSON_STORE.save(state)
        return
    user_id = _user_id()
    store = PostgresStateStore(connect, user_id)
    store.save(state)
    # Keep normalized clip metadata synchronized for Phase 1 slot assignment.
    with connect() as conn, conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.user_id', %s, true)", (user_id,))
            for clip in state.get("clips", []):
                clip_id = int(clip["id"])
                payload = dict(clip)
                qa = payload.get("qa") or {}
                cur.execute(
                    """INSERT INTO clips(user_id,id,source_video_id,start_seconds,end_seconds,
                       score,status,storage_key,qa_report,payload)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)
                       ON CONFLICT(user_id,id) DO UPDATE SET
                         source_video_id=EXCLUDED.source_video_id,
                         start_seconds=EXCLUDED.start_seconds,end_seconds=EXCLUDED.end_seconds,
                         score=EXCLUDED.score,status=EXCLUDED.status,
                         storage_key=EXCLUDED.storage_key,qa_report=EXCLUDED.qa_report,
                         payload=EXCLUDED.payload,updated_at=now()""",
                    (user_id, clip_id, str(clip.get("video_id") or ""),
                     float(clip.get("start") or 0), float(clip.get("end") or 0),
                     clip.get("score"), str(clip.get("status") or "queued"),
                     clip.get("storage_key"), json.dumps(qa), json.dumps(payload, ensure_ascii=False)),
                )
