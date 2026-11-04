"""Concrete worker adapters using Clipper's existing download/render/QA/IG code.

This adapter is intentionally single-account in Phase 1. It runs heavy work in
workers, never in the dispatcher. Instagram tokens are read from the deployment
secret, not logged or persisted in plaintext.
"""
import json
import logging
import os
import tempfile
from pathlib import Path

import yaml

import podcast_clipper as pipeline
from db import connect
from scheduler.queue import append_event
from scheduler.planner import _enqueue,swap_failed_clip
from storage import upload_file,download_file
from scheduler.instagram_limits import assert_publish_quota_available
from scheduler.crypto import decrypt_token

log=logging.getLogger("clipper.handlers")
BASE=Path(__file__).resolve().parents[1]


def _cfg():
    return pipeline.load_config(os.environ.get("CLIPPER_CONFIG",str(BASE/"config.yaml")))


def _update_slot(slot_id, expected, new_state, user_id, **fields):
    allowed={"last_error","container_id"}
    if set(fields)-allowed: raise ValueError("unsupported slot update")
    assignments=["state=%s","updated_at=now()"]
    vals=[new_state]
    for k,v in fields.items(): assignments.append(f"{k}=%s"); vals.append(v)
    vals.extend([slot_id,expected])
    with connect() as conn,conn.transaction():
        with conn.cursor() as cur:
            cur.execute(f"UPDATE slots SET {','.join(assignments)} WHERE id=%s AND state=%s RETURNING id",vals)
            if cur.fetchone(): append_event(conn,user_id,slot_id,new_state,fields)


def prepare_clip(user_id,payload):
    slot_id=payload["slot_id"]; clip_id=int(payload["clip_id"])
    clip=payload.get("clip") or {}
    if not clip:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT payload FROM clips WHERE user_id=%s AND id=%s",(user_id,clip_id))
                row=cur.fetchone()
                if not row: raise RuntimeError("assigned clip row missing")
                clip=row[0]
    cfg=_cfg()
    with connect() as conn,conn.transaction():
        with conn.cursor() as cur:
            cur.execute("UPDATE slots SET state='preparing',updated_at=now() WHERE id=%s AND state='preparing'",(slot_id,))
    path=None
    try:
        path=pipeline.render_clip(cfg,clip)
        review=pipeline.qa_review(cfg,path,clip)
        if review["verdict"]!="pass":
            # Do not publish QA failures. The slot is failed; planner/operator must replace it.
            with connect() as conn,conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("UPDATE clips SET status='needs_review',qa_report=%s::jsonb,updated_at=now() WHERE user_id=%s AND id=%s",
                                (json.dumps(review),user_id,clip_id))
                    cur.execute("UPDATE slots SET state='failed',last_error=%s,updated_at=now() WHERE id=%s AND state='assigned'",
                                ("QA not passed: "+str(review.get("issues",[]))[:500],slot_id))
                    append_event(conn,user_id,slot_id,"clip_qa_blocked",review)
            return {"qa":"blocked"}
        key=f"users/{user_id}/clips/{clip_id}/slot-{slot_id}.mp4"
        upload_file(path,key)
        with connect() as conn,conn.transaction():
            with conn.cursor() as cur:
                cur.execute("UPDATE clips SET status='ready',storage_key=%s,qa_report=%s::jsonb,updated_at=now() WHERE user_id=%s AND id=%s",
                            (key,json.dumps(review),user_id,clip_id))
                cur.execute("UPDATE slots SET state='verified',updated_at=now() WHERE id=%s AND state='assigned' RETURNING id",
                            (slot_id,))
                if cur.fetchone(): append_event(conn,user_id,slot_id,"clip_verified",{"storage_key":key,"qa":review})
        return {"storage_key":key,"qa":review}
    finally:
        if path:
            Path(path).unlink(missing_ok=True)
        pipeline.shutil.rmtree(pipeline.WORK,ignore_errors=True)


def upload_container(user_id,payload):
    slot_id=payload["slot_id"]; clip_id=int(payload["clip_id"])
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT storage_key,payload FROM clips WHERE user_id=%s AND id=%s",(user_id,clip_id))
            row=cur.fetchone()
    if not row or not row[0]: raise RuntimeError("rendered clip storage key missing")
    storage_key,clip=row
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT a.external_id,a.token_ciphertext,a.status FROM slots s
                   JOIN connected_accounts a ON a.id=s.account_id AND a.user_id=s.user_id
                   WHERE s.id=%s AND s.user_id=%s""",(slot_id,user_id))
            account=cur.fetchone()
    if not account or account[2]!="active" or not account[1]:
        raise RuntimeError("Instagram account inactive or missing encrypted token")
    ig_id,token=account[0],decrypt_token(account[1])
    quota=assert_publish_quota_available(os.environ.get("IG_GRAPH_VERSION","v25.0"),ig_id,token)
    with tempfile.TemporaryDirectory(prefix="clipper-container-") as temp:
        path=download_file(storage_key,Path(temp)/"clip.mp4")
        cfg=_cfg()
        try:
            container_id=pipeline.upload_reel_container(cfg,path,str(clip.get("caption") or ""))
        except Exception as exc:
            # Only a processing/compatibility rejection is a permanent media issue.
            permanent=("InstagramProcessingFailedError" in type(exc).__name__
                       or "compatibility retry failed" in str(exc))
            if permanent:
                with connect() as conn,conn.transaction():
                    swapped=swap_failed_clip(conn,user_id,slot_id,clip_id,
                                              "Instagram container rejected media: "+str(exc)[:300])
                return {"container_rejected":True,"replacement_assigned":swapped}
            raise
    _update_slot(slot_id,"container_uploading","container_ready",user_id,container_id=str(container_id))
    with connect() as conn,conn.transaction():
        with conn.cursor() as cur:
            cur.execute("UPDATE slots SET quota_checked_at=now(),quota_snapshot=%s::jsonb WHERE id=%s AND state='container_ready'",
                        (json.dumps(quota),slot_id))
        append_event(conn,user_id,slot_id,"container_ready",{"container_id":str(container_id),"quota":quota})
    return {"container_id":str(container_id)}


JOB_HANDLERS={"prepare_clip":prepare_clip,"verify_slot":verify_slot,"upload_container":upload_container,"produce":produce_for_slot}
