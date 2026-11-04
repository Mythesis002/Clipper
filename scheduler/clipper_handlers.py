"""Worker handlers for Clipper's existing producer, renderer, QA and Instagram uploader.

All heavy media work runs here, not in the T-0 dispatcher. Platform tokens are
decrypted from PostgreSQL only for the duration of the worker operation.
"""
import json
import logging
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

import podcast_clipper as pipeline
from db import connect
from clock import DatabaseClock
from scheduler.crypto import decrypt_token
from scheduler.instagram_limits import assert_publish_quota_available
from scheduler.planner import _enqueue,swap_failed_clip
from scheduler.provider_limits import provider_request
from scheduler.queue import append_event
from storage import download_file,upload_file

log=logging.getLogger("clipper.handlers")
BASE=Path(__file__).resolve().parents[1]




@contextmanager
def _tenant_state_lock(user_id):
    """Serialize legacy pipeline state read-modify-write per tenant."""
    conn=connect()
    conn.autocommit=True
    lock_key=f"clipper:legacy-state:{user_id}"
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(hashtextextended(%s,0))",(lock_key,))
        yield
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))",(lock_key,))
        finally:
            conn.close()

def _cfg():
    return pipeline.load_config(os.environ.get("CLIPPER_CONFIG",str(BASE/"config.yaml")))


def _slot_deadline(user_id,slot_id):
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT run_at_utc,max_late_minutes,state FROM slots WHERE id=%s AND user_id=%s",
                        (slot_id,user_id))
            row=cur.fetchone()
    if not row: raise RuntimeError("slot not found for tenant")
    run_at,max_late,state=row
    from datetime import timedelta
    now=DatabaseClock(connect).now()
    expired=now>run_at+timedelta(minutes=max_late)
    return expired,state


def _mark_missed(user_id,slot_id,reason):
    with connect() as conn,conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE slots SET state='missed',last_error=%s,lease_owner=NULL,lease_until=NULL,
                   updated_at=now() WHERE id=%s AND user_id=%s
                   AND state NOT IN ('publishing','published','publish_uncertain','missed','failed','cancelled')
                   RETURNING id""",(reason[:500],slot_id,user_id))
            if cur.fetchone(): append_event(conn,user_id,slot_id,"slot_missed",{"reason":reason})


def _load_clip(user_id,clip_id,provided=None):
    if provided:
        return provided
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT payload FROM clips WHERE user_id=%s AND id=%s",(user_id,clip_id))
            row=cur.fetchone()
    if not row: raise RuntimeError("clip row missing")
    return row[0]


def prepare_clip(user_id,payload):
    """T-6h worker: render, run existing QA, store MP4, then mark verified."""
    slot_id=str(payload["slot_id"]); clip_id=int(payload["clip_id"])
    expired,state=_slot_deadline(user_id,slot_id)
    if expired:
        _mark_missed(user_id,slot_id,"deadline exceeded before render")
        return {"missed":True}
    with connect() as conn,conn.transaction():
        with conn.cursor() as cur:
            cur.execute("UPDATE slots SET state='preparing',updated_at=now() WHERE id=%s AND user_id=%s AND state='assigned' RETURNING id",
                        (slot_id,user_id))
            if not cur.fetchone():
                return {"slot_state_changed":True}
    clip=_load_clip(user_id,clip_id,payload.get("clip"))
    cfg=_cfg()
    path=None
    try:
        path=pipeline.render_clip(cfg,clip)
        review=pipeline.qa_review(cfg,path,clip)
        if review.get("verdict")!="pass":
            with connect() as conn,conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("UPDATE clips SET status='needs_review',qa_report=%s::jsonb,updated_at=now() WHERE user_id=%s AND id=%s",
                                (json.dumps(review),user_id,clip_id))
                    append_event(conn,user_id,slot_id,"clip_qa_blocked",review)
                    swapped=swap_failed_clip(conn,user_id,slot_id,clip_id,
                                             "QA not passed: "+str(review.get("issues",[]))[:300])
            return {"qa":"blocked","replacement_assigned":swapped}

        key=f"users/{user_id}/clips/{clip_id}/slot-{slot_id}.mp4"
        upload_file(path,key)
        with connect() as conn,conn.transaction():
            with conn.cursor() as cur:
                cur.execute("UPDATE clips SET status='ready',storage_key=%s,qa_report=%s::jsonb,updated_at=now() WHERE user_id=%s AND id=%s",
                            (key,json.dumps(review),user_id,clip_id))
                cur.execute("UPDATE slots SET state='verified',updated_at=now() WHERE id=%s AND user_id=%s AND state='preparing' RETURNING id",
                            (slot_id,user_id))
                if cur.fetchone(): append_event(conn,user_id,slot_id,"clip_rendered_qa_passed",{"storage_key":key,"qa":review})
        return {"storage_key":key,"qa":review}
    finally:
        if path: Path(path).unlink(missing_ok=True)
        pipeline.shutil.rmtree(pipeline.WORK,ignore_errors=True)


def verify_slot(user_id,payload):
    """T-30m verification: object existence, ffprobe, and Instagram media profile."""
    slot_id=str(payload["slot_id"]); clip_id=int(payload["clip_id"])
    expired,state=_slot_deadline(user_id,slot_id)
    if expired:
        _mark_missed(user_id,slot_id,"deadline exceeded before media verification")
        return {"missed":True}
    key=payload.get("storage_key")
    if not key:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT storage_key FROM clips WHERE user_id=%s AND id=%s",(user_id,clip_id))
                row=cur.fetchone()
                key=row[0] if row else None
    if not key: raise RuntimeError("verification cannot find rendered clip object")
    with tempfile.TemporaryDirectory(prefix="clipper-verify-") as temp:
        path=download_file(key,Path(temp)/"verify.mp4")
        media=pipeline.probe_media(path)
        issues=pipeline.instagram_media_issues(media)
        if (media.get("w"),media.get("h"))!=(1080,1920):
            issues.append("expected 1080x1920 output")
        if not media.get("has_video") or not media.get("has_audio"):
            issues.append("missing audio/video stream")
    with connect() as conn,conn.transaction():
        with conn.cursor() as cur:
            if issues:
                cur.execute("UPDATE clips SET status='needs_review',updated_at=now() WHERE user_id=%s AND id=%s",(user_id,clip_id))
                append_event(conn,user_id,slot_id,"slot_verification_failed",{"issues":issues,"media":media})
                swapped=swap_failed_clip(conn,user_id,slot_id,clip_id,
                                         "T-30 media verification failed: "+ "; ".join(issues)[:300])
            else:
                cur.execute("UPDATE slots SET verification_checked_at=now(),updated_at=now() WHERE id=%s AND user_id=%s AND state='verified' RETURNING id",
                            (slot_id,user_id))
                if cur.fetchone(): append_event(conn,user_id,slot_id,"slot_media_verified",{"media":media})
    if issues: return {"verification":"failed","issues":issues,"replacement_assigned":swapped}
    return {"verification":"passed","media":media}


def upload_container(user_id,payload):
    """T-10m worker: check quota, upload resumable media, poll FINISHED, persist container."""
    slot_id=str(payload["slot_id"]); clip_id=int(payload["clip_id"])
    expired,state=_slot_deadline(user_id,slot_id)
    if expired:
        _mark_missed(user_id,slot_id,"deadline exceeded before container upload")
        return {"missed":True}
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT storage_key,payload FROM clips WHERE user_id=%s AND id=%s",(user_id,clip_id))
            clip_row=cur.fetchone()
            cur.execute(
                """SELECT a.external_id,a.token_ciphertext,a.status FROM slots s
                   JOIN connected_accounts a ON a.id=s.account_id AND a.user_id=s.user_id
                   WHERE s.id=%s AND s.user_id=%s""",(slot_id,user_id))
            account=cur.fetchone()
    if not clip_row or not clip_row[0]: raise RuntimeError("rendered clip storage key missing")
    if not account or account[2]!="active" or not account[1]:
        raise RuntimeError("Instagram account inactive or missing encrypted token")
    storage_key,clip=clip_row
    ig_id,token=account[0],decrypt_token(account[1])
    with provider_request("meta"):
        quota=assert_publish_quota_available(os.environ.get("IG_GRAPH_VERSION","v25.0"),ig_id,token)
    with tempfile.TemporaryDirectory(prefix="clipper-container-") as temp:
        path=download_file(storage_key,Path(temp)/"clip.mp4")
        cfg=_cfg()
        try:
            with provider_request("meta"):
                container_id=pipeline.upload_reel_container(
                    cfg,path,str(clip.get("caption") or ""),access_token=token,ig_user_id=ig_id)
        except Exception as exc:
            permanent=("InstagramProcessingFailedError" in type(exc).__name__
                       or "compatibility retry failed" in str(exc))
            if permanent:
                with connect() as conn,conn.transaction():
                    swapped=swap_failed_clip(conn,user_id,slot_id,clip_id,
                                              "Instagram container rejected media: "+str(exc)[:300])
                return {"container_rejected":True,"replacement_assigned":swapped}
            raise
    with connect() as conn,conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE slots SET state='container_ready',container_id=%s,
                   quota_checked_at=now(),quota_snapshot=%s::jsonb,updated_at=now()
                   WHERE id=%s AND user_id=%s AND state='container_uploading' RETURNING id""",
                (str(container_id),json.dumps(quota),slot_id,user_id))
            if cur.fetchone():
                append_event(conn,user_id,slot_id,"container_ready",
                             {"container_id":str(container_id),"quota":quota})
    return {"container_id":str(container_id)}


def produce_for_slot(user_id,payload):
    """Run existing discovery/analysis when the ready clip buffer is empty."""
    slot_id=str(payload["slot_id"])
    expired,state=_slot_deadline(user_id,slot_id)
    if expired:
        _mark_missed(user_id,slot_id,"deadline exceeded while waiting for clip buffer")
        return {"missed":True}
    with _tenant_state_lock(user_id):
        pipeline.produce(_cfg())
    with connect() as conn,conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """SELECT c.id,c.payload FROM clips c
                   WHERE c.user_id=%s AND c.status='queued'
                     AND EXISTS (SELECT 1 FROM sources src
                       WHERE src.user_id=c.user_id AND src.youtube_handle=c.payload->>'source_handle'
                         AND src.permission_attested=true AND src.status='active')
                     AND NOT EXISTS (SELECT 1 FROM slots x WHERE x.user_id=c.user_id
                       AND x.clip_id=c.id AND x.state NOT IN ('published','missed','failed','cancelled'))
                   ORDER BY c.score DESC NULLS LAST,c.created_at,c.id
                   FOR UPDATE SKIP LOCKED LIMIT 1""",(user_id,))
            row=cur.fetchone()
            if not row:
                return {"retry_after_seconds":600,"reason":"no eligible clip in buffer yet"}
            clip_id,clip=row
            cur.execute("UPDATE slots SET clip_id=%s,last_error=NULL,updated_at=now() WHERE id=%s AND user_id=%s AND state='assigned' AND clip_id IS NULL RETURNING id",
                        (clip_id,slot_id,user_id))
            if not cur.fetchone(): return {"slot_state_changed":True}
            cur.execute("UPDATE clips SET status='assigned',updated_at=now() WHERE user_id=%s AND id=%s",
                        (user_id,clip_id))
            _enqueue(cur,user_id,"prepare_clip",
                     {"slot_id":slot_id,"clip_id":int(clip_id),"clip":clip},
                     10,f"slot:{slot_id}:prepare:{int(clip_id)}")
            append_event(conn,user_id,slot_id,"slot_clip_assigned_after_produce",{"clip_id":int(clip_id)})
    return {"clip_id":int(clip_id)}


JOB_HANDLERS={"prepare_clip":prepare_clip,"verify_slot":verify_slot,
              "upload_container":upload_container,"produce":produce_for_slot}
