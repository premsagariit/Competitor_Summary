"""Durable document storage on Cloudflare R2.

Render's free tier has no persistent disk - it's wiped on every deploy and
every idle spin-down. Everything under data/downloads/ and artifacts/output/
would vanish without this, since those are the actual retrieved filings and
generated reports (paths.py), not regenerable cache.

R2 is S3-compatible, so this is a thin boto3 wrapper rather than a bespoke
client. Credentials are optional: if the R2_* env vars aren't set, every
function here is a no-op, so local development against the plain filesystem
is unaffected.
"""
import logging
import os
from pathlib import Path

from competitor_analysis import paths

logger = logging.getLogger(__name__)

_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
_ACCESS_KEY = os.getenv("R2_ACCESS_KEY_ID")
_SECRET_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
_BUCKET = os.getenv("R2_BUCKET_NAME")

_client = None
_client_checked = False


def _get_client():
    global _client, _client_checked
    if _client_checked:
        return _client
    _client_checked = True
    if not all([_ACCOUNT_ID, _ACCESS_KEY, _SECRET_KEY, _BUCKET]):
        logger.info("R2 env vars not set - document storage stays local-only.")
        return None
    import boto3
    _client = boto3.client(
        "s3",
        endpoint_url=f"https://{_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=_ACCESS_KEY,
        aws_secret_access_key=_SECRET_KEY,
        region_name="auto",
    )
    return _client


def _key_for(local_path: Path) -> str:
    """R2 object key = path relative to Backend/, POSIX-separated, so it
    matches the layout restore_all() expects to find on the way back down."""
    return str(Path(local_path).resolve().relative_to(paths.BACKEND_ROOT)).replace("\\", "/")


def upload_file(local_path) -> None:
    client = _get_client()
    if client is None:
        return
    local_path = Path(local_path)
    if not local_path.is_file():
        return
    try:
        client.upload_file(str(local_path), _BUCKET, _key_for(local_path))
    except Exception:
        logger.exception("R2 upload failed for %s", local_path)


def upload_tree(local_dir) -> None:
    client = _get_client()
    if client is None:
        return
    local_dir = Path(local_dir)
    if not local_dir.is_dir():
        return
    for p in local_dir.rglob("*"):
        if p.is_file():
            upload_file(p)


def delete_file(local_path) -> None:
    client = _get_client()
    if client is None:
        return
    try:
        client.delete_object(Bucket=_BUCKET, Key=_key_for(Path(local_path)))
    except Exception:
        logger.exception("R2 delete failed for %s", local_path)


def download_tree(prefix: str) -> None:
    """Pull everything under `prefix` (e.g. 'data/downloads') from R2 down
    onto local disk, rooted at Backend/. Used once at startup to restore
    whatever a previous container instance had synced up."""
    client = _get_client()
    if client is None:
        return
    paginator = client.get_paginator("list_objects_v2")
    try:
        for page in paginator.paginate(Bucket=_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                dest = paths.BACKEND_ROOT / key
                dest.parent.mkdir(parents=True, exist_ok=True)
                client.download_file(_BUCKET, key, str(dest))
    except Exception:
        logger.exception("R2 restore failed for prefix %s", prefix)


def restore_all() -> None:
    """Call once at API startup, before serving requests."""
    download_tree("data/downloads")
    download_tree("artifacts/output")


def sync_run_outputs() -> None:
    """Call after a pipeline run finishes (success, failure, or paused for
    review) so whatever it produced survives the next container restart."""
    upload_tree(paths.DOWNLOADS_DIR)
    upload_tree(paths.OUTPUT_DIR)
