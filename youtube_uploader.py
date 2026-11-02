"""YouTube Shorts uploader for Clipper.

Credentials are supplied as a Google OAuth authorized-user JSON object in the
YT_TOKEN_JSON environment variable (GitHub Actions secret). Never commit tokens.
"""
import json
import os
import random
import re
import time
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
RETRIABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = 8
CHUNK_SIZE = 8 * 1024 * 1024
DEFAULT_TAGS = ["podcast", "shorts", "clips", "india"]


def sanitize_youtube_title(text, max_length=100):
    """Return a clean YouTube title (API limit is 100 characters)."""
    text = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "", str(text or ""))
    text = re.sub(r"[\u0000-\u001f\u007f]", " ", text)
    text = " ".join(text.replace('"', "").replace("'", "").split()).strip()
    if len(text) > max_length:
        text = text[:max_length].rsplit(" ", 1)[0].strip()
    return text if len(text) >= 3 else "Podcast Short"


def _credentials_from_environment():
    raw = os.getenv("YT_TOKEN_JSON", "").strip()
    if not raw:
        raise RuntimeError("YT_TOKEN_JSON is not configured (Google OAuth token JSON secret).")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("YT_TOKEN_JSON must contain valid authorized-user OAuth JSON.") from exc
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as exc:
        raise RuntimeError("YouTube upload dependencies are missing; install requirements.txt.") from exc
    credentials = Credentials.from_authorized_user_info(info, scopes=SCOPES)
    if not credentials.valid:
        if credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
            except RefreshError as exc:
                raise RuntimeError("YouTube OAuth refresh failed; reauthorize the Google account.") from exc
        else:
            raise RuntimeError("YouTube OAuth token is invalid or lacks a refresh token; reauthorize.")
    return credentials


def _youtube_service(credentials=None):
    credentials = credentials or _credentials_from_environment()
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError("YouTube upload dependencies are missing; install requirements.txt.") from exc
    return build("youtube", "v3", credentials=credentials, cache_discovery=False)


def check_youtube_account():
    """Validate OAuth and return the authenticated channel's basic identity."""
    service = _youtube_service()
    response = service.channels().list(part="id,snippet", mine=True).execute()
    channels = response.get("items") or []
    if not channels:
        raise RuntimeError("YouTube OAuth is valid, but no channel was returned for this account.")
    channel = channels[0]
    return {
        "channel_id": channel.get("id", ""),
        "channel_title": (channel.get("snippet") or {}).get("title", "YouTube channel"),
    }


def upload_to_youtube(video, title, description="", category="22",
                      keywords=None, privacy_status="public", notify_subscribers=False):
    """Upload a local MP4 and return {video_id, url}. Does not download remote URLs."""
    path = Path(video)
    if not path.is_file():
        raise FileNotFoundError(f"YouTube upload video does not exist: {path}")
    if path.stat().st_size <= 0:
        raise ValueError("YouTube upload video is empty.")
    if privacy_status not in {"public", "unlisted", "private"}:
        raise ValueError("privacy_status must be public, unlisted, or private")

    credentials = _credentials_from_environment()
    tags = keywords or DEFAULT_TAGS
    if isinstance(tags, str):
        tags = [tag.strip() for tag in tags.split(",") if tag.strip()]
    tags = [str(tag).strip()[:100] for tag in tags if str(tag).strip()][:500]

    body = {
        "snippet": {
            "title": sanitize_youtube_title(title),
            "description": str(description or "")[:5000],
            "tags": tags,
            "categoryId": str(category),
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
        },
    }
    try:
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise RuntimeError("YouTube upload dependencies are missing; install requirements.txt.") from exc
    youtube = _youtube_service(credentials)
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=MediaFileUpload(
            str(path), chunksize=CHUNK_SIZE, resumable=True, mimetype="video/mp4"
        ),
        notifySubscribers=bool(notify_subscribers),
    )

    response = None
    retry = 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                print(f"YouTube upload progress: {int(status.progress() * 100)}%")
        except HttpError as exc:
            if exc.resp.status not in RETRIABLE_STATUS_CODES:
                raise
            if retry >= MAX_RETRIES:
                raise RuntimeError(f"YouTube upload retry limit reached (HTTP {exc.resp.status}).") from exc
            retry += 1
            delay = min(60.0, random.uniform(0, 2 ** min(retry, 6)))
            print(f"Retryable YouTube HTTP {exc.resp.status}; retry {retry}/{MAX_RETRIES} in {delay:.1f}s")
            time.sleep(delay)
        except (OSError, TimeoutError) as exc:
            if retry >= MAX_RETRIES:
                raise RuntimeError("YouTube upload retry limit reached after network errors.") from exc
            retry += 1
            delay = min(60.0, random.uniform(0, 2 ** min(retry, 6)))
            print(f"Retryable YouTube network error; retry {retry}/{MAX_RETRIES} in {delay:.1f}s")
            time.sleep(delay)

    video_id = (response or {}).get("id")
    if not video_id:
        raise RuntimeError(f"YouTube API returned no video id: {response!r}")
    return {"video_id": video_id, "url": f"https://www.youtube.com/watch?v={video_id}"}
