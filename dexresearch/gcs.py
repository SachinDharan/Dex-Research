"""Google Cloud Storage helpers.

Auth uses Application Default Credentials. One-time setup:

    gcloud auth application-default login
    gcloud config set project dex-research

Bucket layout is flat filenames under stage-specific prefixes:

    gs://<bucket>/raw/wallet_populations/<chain>_<YYYY-MM>.parquet
    gs://<bucket>/raw/swaps/<chain>_uniswap_v4.parquet           # \
    gs://<bucket>/raw/approvals/<chain>_uniswap_v4.parquet       #  } all three split
    gs://<bucket>/raw/permit2_events/<chain>_uniswap_v4.parquet  # /  from fetch.timeline
    gs://<bucket>/raw/supplies/<chain>_<YYYY-MM>.parquet
    gs://<bucket>/raw/borrows/<chain>_<YYYY-MM>.parquet
    gs://<bucket>/raw/gas_measurements/<chain>_<YYYY-MM>.parquet
    gs://<bucket>/processed/...
    gs://<bucket>/metadata/...

Every parquet upload may attach a sidecar `<blob_path>.meta.json` capturing
the Dune query_id, execution_id, parameters, and fetched_at — set the
`metadata=` arg on upload_parquet().
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

import pandas as pd
from google.cloud import storage

from dexresearch.config import get_settings


@lru_cache
def _client() -> storage.Client:
    return storage.Client(project=get_settings().GCP_PROJECT_ID)


def bucket() -> storage.Bucket:
    return _client().bucket(get_settings().GCS_BUCKET)


def gs_uri(blob_path: str) -> str:
    return f"gs://{get_settings().GCS_BUCKET}/{blob_path}"


def upload_parquet(
    df: pd.DataFrame,
    blob_path: str,
    *,
    metadata: dict[str, Any] | None = None,
    keep_local: bool = True,
) -> str:
    """Write `df` to parquet locally, upload to gs://<bucket>/<blob_path>, and
    optionally write a `<blob_path>.meta.json` sidecar.

    Convention for `metadata` — include at least:
        query_id      Dune saved-query id
        execution_id  Dune execution id returned by run_query
        parameters    dict of params passed to the query
        fetched_at    ISO-8601 timestamp (auto-filled if missing)

    Prints row count and on-disk size on success.
    """
    settings = get_settings()
    local_path = settings.DATA_DIR / blob_path
    local_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[gcs] writing {len(df):,} rows to local parquet: {local_path}")
    df.to_parquet(local_path, index=False)
    size_bytes = local_path.stat().st_size
    print(f"[gcs] local write done ({_format_size(size_bytes)}), uploading to gs://{settings.GCS_BUCKET}/{blob_path}")

    bucket().blob(blob_path).upload_from_filename(local_path)
    uri = gs_uri(blob_path)
    print(f"[gcs] upload complete: {uri}")

    if metadata is not None:
        meta = dict(metadata)
        meta.setdefault("fetched_at", datetime.now(timezone.utc).isoformat())
        meta_text = json.dumps(meta, indent=2, default=str)
        meta_blob = f"{blob_path}.meta.json"
        local_meta = settings.DATA_DIR / meta_blob
        local_meta.parent.mkdir(parents=True, exist_ok=True)
        local_meta.write_text(meta_text)
        print(f"[gcs] uploading metadata sidecar: {meta_blob}")
        bucket().blob(meta_blob).upload_from_string(
            meta_text, content_type="application/json"
        )
        print(f"[gcs] metadata sidecar uploaded")

    if not keep_local:
        local_path.unlink()
        print(f"[gcs] removed local copy: {local_path}")

    print(f"[gcs] uploaded {uri} — {len(df):,} rows, {_format_size(size_bytes)}")
    return uri


def download_parquet(blob_path: str, *, use_cache: bool = True) -> pd.DataFrame:
    """Return a parquet as a DataFrame.

    When use_cache=True (default) and the local copy under DATA_DIR exists,
    read it directly from disk; otherwise fetch from GCS first.
    """
    settings = get_settings()
    local_path = settings.DATA_DIR / blob_path
    if use_cache and local_path.exists():
        print(f"[gcs] cache hit — reading from local: {local_path}")
        return pd.read_parquet(local_path)
    print(f"[gcs] downloading from GCS: gs://{settings.GCS_BUCKET}/{blob_path}")
    local_path.parent.mkdir(parents=True, exist_ok=True)
    bucket().blob(blob_path).download_to_filename(local_path)
    print(f"[gcs] download complete: {local_path}")
    return pd.read_parquet(local_path)


def read_parquet_direct(blob_path: str) -> pd.DataFrame:
    """Read parquet directly from GCS via gcsfs — no local caching.

    Useful for notebook exploration across many blobs where you don't want to
    fill the local data/ directory. Requires gcsfs (in requirements.txt).
    """
    return pd.read_parquet(gs_uri(blob_path))


def blob_exists(blob_path: str) -> bool:
    return bucket().blob(blob_path).exists()


def list_blobs(prefix: str) -> list[str]:
    """List blob names under a prefix (paths relative to the bucket)."""
    return [b.name for b in _client().list_blobs(get_settings().GCS_BUCKET, prefix=prefix)]


def _format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"
