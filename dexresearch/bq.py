"""BigQuery helpers.

Auth uses Application Default Credentials (same as GCS).

Tables land in <GCP_PROJECT_ID>.<BQ_DATASET>.<table_name> and are queryable
immediately from BigQuery after load. Default write mode is WRITE_TRUNCATE
(replace the table on each run).
"""
from __future__ import annotations

from functools import lru_cache

import pandas as pd
from google.cloud import bigquery

from dexresearch.config import get_settings


@lru_cache
def _client() -> bigquery.Client:
    return bigquery.Client(project=get_settings().GCP_PROJECT_ID)


def _ensure_dataset() -> str:
    """Create the dataset if it doesn't exist. Returns the dataset id."""
    settings = get_settings()
    dataset_id = f"{settings.GCP_PROJECT_ID}.{settings.BQ_DATASET}"
    try:
        _client().get_dataset(dataset_id)
    except Exception:
        dataset = bigquery.Dataset(dataset_id)
        dataset.location = "US"
        _client().create_dataset(dataset, exists_ok=True)
        print(f"[bq] created dataset {dataset_id}")
    return dataset_id


def load_table(df: pd.DataFrame, table_name: str, *, write_disposition: str = "WRITE_TRUNCATE") -> str:
    """Load a DataFrame into a BigQuery table, replacing it by default.

    table_name is just the table part (e.g. 'uniswap_v4_swaps') — the project
    and dataset are pulled from config. The dataset is created automatically if
    it doesn't exist yet.

    Returns the full table id: '<project>.<dataset>.<table>'.
    """
    settings = get_settings()
    _ensure_dataset()
    full_id = f"{settings.GCP_PROJECT_ID}.{settings.BQ_DATASET}.{table_name}"

    print(f"[bq] loading {len(df):,} rows into {full_id} ({write_disposition}) ...")
    job_config = bigquery.LoadJobConfig(write_disposition=write_disposition)
    job = _client().load_table_from_dataframe(df, full_id, job_config=job_config)
    job.result()

    table = _client().get_table(full_id)
    print(f"[bq] done — {table.num_rows:,} rows, {len(table.schema)} columns in {full_id}")
    return full_id
