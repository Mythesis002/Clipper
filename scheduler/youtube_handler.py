"""Independent YouTube Shorts job, scheduled only after Instagram success."""
import json
import logging
import os
import tempfile
from pathlib import Path

from db import connect
from storage import download_file

log=logging.getLogger("clipper.youtube_job")


def upload_youtube(user_id,payload):
    clip_id=int(payload["clip_id"])
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT storage_key,payload FROM clips WHERE user_id=%s AND id=%s",(user_id,clip_id))
            row=cur.fetchone()
    if not row or not row[0]: raise RuntimeError("YouTube job cannot find stored clip media")
    storage_key,clip=row
    platforms=clip.get("platforms") or {}
    yt=platforms.get("youtube") or {}
    if yt.get("status")=="uploaded":
        return {"already_uploaded":True,"video_id":yt.get("video_id")}
    if yt.get("status") in ("uploading","upload_uncertain"):
        # Crash after videos.insert began is ambiguous. Never create a duplicate video.
        with connect() as conn,conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE clips SET payload=jsonb_set(payload,'{platforms,youtube}',
                       '{"status":"upload_uncertain","error":"worker recovered an in-progress upload; reconcile manually"}'::jsonb,true),
                       updated_at=now() WHERE user_id=%s AND id=%s""",(user_id,clip_id))
        return {"uncertain":True}

    # Persist intent before the external call. A process crash here is deliberately
    # treated as uncertain on recovery rather than risking a duplicate upload.
    with connect() as conn,conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE clips SET payload=jsonb_set(payload,'{platforms,youtube}',
                   jsonb_build_object('status','uploading','started_at',now()),true),
                   updated_at=now() WHERE user_id=%s AND id=%s""",(user_id,clip_id))
    with tempfile.TemporaryDirectory(prefix="clipper-youtube-") as temp:
        video=download_file(storage_key,Path(temp)/"clip.mp4")
        try:
            from youtube_uploader import upload_to_youtube,YouTubeUploadUncertainError
            original=f"https://www.youtube.com/watch?v={clip.get('video_id')}" if clip.get("video_id") else ""
            source_line=f"Original podcast: {original}" if original else f"Source: {clip.get('channel','podcast')}"
            description="\n\n".join(x for x in [str(clip.get("caption") or "").strip(),"#Shorts",source_line] if x)
            result=upload_to_youtube(
                video=video,title=clip.get("title") or "Podcast Short",description=description,
                category=os.environ.get("YT_CATEGORY","22"),
                keywords=["podcast","shorts","clips"],
                privacy_status=os.environ.get("YT_PRIVACY_STATUS","public"),
                notify_subscribers=False,
            )
        except YouTubeUploadUncertainError as exc:
            with connect() as conn,conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE clips SET payload=jsonb_set(payload,'{platforms,youtube}',
                           %s::jsonb,true),updated_at=now() WHERE user_id=%s AND id=%s""",
                        (json.dumps({"status":"upload_uncertain","error":str(exc)[:400]}),user_id,clip_id))
        except Exception as exc:
            # Definite preflight/rejection errors are retriable independently of IG.
            with connect() as conn,conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE clips SET payload=jsonb_set(payload,'{platforms,youtube}',
                           %s::jsonb,true),updated_at=now() WHERE user_id=%s AND id=%s""",
                        (json.dumps({"status":"failed","error":str(exc)[:400]}),user_id,clip_id))
            raise
        else:
            with connect() as conn,conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE clips SET payload=jsonb_set(payload,'{platforms,youtube}',
                           %s::jsonb,true),updated_at=now() WHERE user_id=%s AND id=%s""",
                        (json.dumps({"status":"uploaded","video_id":result["video_id"],
                                    "url":result["url"]}),user_id,clip_id))
            return result
    return {"uncertain":True}
