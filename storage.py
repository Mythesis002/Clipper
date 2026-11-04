"""S3-compatible rendered-media storage (Cloudflare R2, MinIO, or AWS S3)."""
import os
from pathlib import Path


def _client():
    try:
        import boto3
    from botocore.config import Config
    except ImportError as exc:
        raise RuntimeError("Install boto3 for S3-compatible storage") from exc
    endpoint=os.environ.get("S3_ENDPOINT_URL","").strip() or None
    config=Config(signature_version="s3v4",s3={"addressing_style":"path"}) if endpoint else None
    return boto3.client(
        "s3",endpoint_url=endpoint,config=config,
        aws_access_key_id=os.environ.get("S3_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY"),
        region_name=os.environ.get("S3_REGION","auto"),
    )


def upload_file(local_path: str | Path, key: str) -> str:
    bucket=os.environ.get("S3_BUCKET","").strip()
    if not bucket: raise RuntimeError("S3_BUCKET is required")
    path=Path(local_path)
    if not path.is_file() or path.stat().st_size==0:
        raise ValueError("Cannot store missing/empty rendered media")
    _client().upload_file(str(path),bucket,key,ExtraArgs={"ContentType":"video/mp4"})
    return key


def download_file(key: str, local_path: str | Path) -> Path:
    bucket=os.environ.get("S3_BUCKET","").strip()
    if not bucket: raise RuntimeError("S3_BUCKET is required")
    target=Path(local_path); target.parent.mkdir(parents=True,exist_ok=True)
    _client().download_file(bucket,key,str(target))
    if not target.is_file() or target.stat().st_size==0:
        raise RuntimeError("Object storage returned an empty media file")
    return target
