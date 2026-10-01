"""Durable document storage on any S3-compatible object store (AWS S3, Cloudflare R2, MinIO).

Render's free tier has no persistent disk - it's wiped on every deploy and
every idle spin-down. Everything under data/downloads/ and artifacts/output/
would vanish without this, since those are the actual retrieved filings and
generated reports (paths.py).

The PDF-parse cache (artifacts/cache/pdf_json/) is synced too, purely as a
speed measure: re-deriving it took ~1000s of every production run. It is
safe to persist because it is DETERMINISTIC - the same PDF always yields
byte-identical JSON - so a restored entry can never change an extracted
answer, and it self-invalidates on the source PDF's content hash.

artifacts/cache/gemini/ is deliberately NOT synced. Those are model
answers, not derived facts: if an analyst spots a wrong extraction and
re-runs, a restored response cache would serve the same wrong answer
straight back. Re-running must re-ask the model. Do not add it here.

The S3 API is the common denominator, so this is a thin boto3 wrapper rather than a bespoke
client. Credentials are optional: if the S3_* env vars aren't set, every
function here is a no-op, so local development against the plain filesystem
is unaffected.
"""
import logging
import os
from pathlib import Path

from competitor_analysis import paths

logger = logging.getLogger(__name__)

_ACCOUNT_ID = os.getenv("S3_ACCOUNT_ID")
_ACCESS_KEY = os.getenv("S3_ACCESS_KEY_ID")
_SECRET_KEY = os.getenv("S3_SECRET_ACCESS_KEY")
_BUCKET = os.getenv("S3_BUCKET_NAME")
# Optional overrides so the same code can talk to any S3-compatible store.
# S3_ENDPOINT_URL set -> used as-is and S3_ACCOUNT_ID is not needed (MinIO,
# custom endpoints). Unset with S3_ACCOUNT_ID (Cloudflare only) -> the R2 endpoint.
# Both unset -> boto3's default AWS resolution (needs S3_REGION for real S3).
_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL")
_REGION = os.getenv("S3_REGION")

_client = None
_client_checked = False


def _get_client():
    global _client, _client_checked
    if _client_checked:
        return _client
    _client_checked = True
    if not all([_ACCESS_KEY, _SECRET_KEY, _BUCKET]) or not (
            _ACCOUNT_ID or _ENDPOINT_URL or _REGION):
        logger.info("S3 env vars not set - document storage stays local-only.")
        return None
    import boto3
    from botocore.config import Config
    endpoint = _ENDPOINT_URL or (
        f"https://{_ACCOUNT_ID}.r2.cloudflarestorage.com" if _ACCOUNT_ID else None)
    _client = boto3.client(
        "s3",
        endpoint_url=endpoint,  # None -> default AWS S3 endpoint for the region
        aws_access_key_id=_ACCESS_KEY,
        aws_secret_access_key=_SECRET_KEY,
        region_name=_REGION or "auto",
        # Without an explicit bound, botocore's default socket timeouts can
        # still leave a stuck connection (unreachable endpoint, network
        # black-holing) hanging for minutes before it ever raises - and every
        # caller here already treats an S3 failure as non-fatal (see module
        # docstring), so there is no reason a slow/broken S3 should ever be
        # able to hang the request that's waiting on it (e.g. a file upload
        # sitting "pending" in the browser with nothing in the console).
        config=Config(connect_timeout=10, read_timeout=60,
                      retries={"max_attempts": 2, "mode": "standard"}),
    )
    return _client


def _key_for(local_path: Path) -> str:
    """S3 object key = path relative to Backend/, POSIX-separated, so it
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
        logger.exception("S3 upload failed for %s", local_path)


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
        logger.exception("S3 delete failed for %s", local_path)


def download_tree(prefix: str) -> None:
    """Pull everything under `prefix` (e.g. 'data/downloads') from S3 down
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
        logger.exception("S3 restore failed for prefix %s", prefix)


def restore_all() -> None:
    """Call once at API startup, before serving requests."""
    download_tree("data/downloads")
    download_tree("data/historical")
    download_tree("artifacts/output")
    # pdf_json only - never artifacts/cache/gemini (see module docstring).
    download_tree("artifacts/cache/pdf_json")


def sync_run_outputs() -> None:
    """Call after a pipeline run finishes (success, failure, or paused for
    review) so whatever it produced survives the next container restart."""
    upload_tree(paths.DOWNLOADS_DIR)
    upload_tree(paths.OUTPUT_DIR)
    # Uploaded even on a failed run: the parse cache is valid regardless of
    # whether the stages after it succeeded, and re-deriving it is the
    # single most expensive thing a fresh container does. Scoped to
    # PDF_JSON_CACHE, never CACHE_DIR, to keep the Gemini cache local.
    upload_tree(paths.PDF_JSON_CACHE)
