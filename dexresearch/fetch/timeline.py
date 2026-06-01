"""The whole study fetch in ONE Dune query (GCS-only CSV output).

Runs queries/uniswap_v4/timeline.sql — submits one execution, then pages results
into per-page CSV files in GCS. Each page is split by record_type into three
buckets, with irrelevant columns dropped per table:

    gs://<bucket>/raw/swaps/page_<NNNN>.csv
    gs://<bucket>/raw/approvals/page_<NNNN>.csv
    gs://<bucket>/raw/permit2_events/page_<NNNN>.csv

Progress is tracked in data/executions/manifest.json so the fetch resumes
exactly from the last page on credit-cap or network errors.  On the next run
(after the .env API key has been swapped) the manifest supplies the last saved
offset and the load resumes from that exact page, dropping new CSV files
alongside the existing ones.

Execution results expire on Dune after 24 h.  If the cached execution_id has
expired when the fetch resumes, the SQL is re-executed automatically and the
fetch restarts from page 1.

Run with:
    python -m dexresearch.fetch.timeline
    python -m dexresearch.fetch.timeline --force   # clear cache, re-execute SQL
"""
from __future__ import annotations

import sys
from io import StringIO
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from dexresearch import dune, gcs
from dexresearch.dune_runner import CreditError, DuneRunner, Job, _Ended
from dexresearch.fetch._common import study_window

GCS_SWAPS_PREFIX     = "raw/swaps"
GCS_APPROVALS_PREFIX = "raw/approvals"
GCS_PERMIT2_PREFIX   = "raw/permit2_events"

_JOB_KEY = "uniswap_v4/timeline"

# Columns to keep per output table (in display order).
# Wide-superset columns from the SQL are pruned per record_type so each CSV
# only carries columns that actually apply.
SWAP_COLUMNS = [
    "wallet", "block_time", "block_number", "tx_hash",
    "token", "token_symbol", "counterparty", "counter_symbol",
    "amount_usd",
    "gas_used", "gas_price", "gas_cost_eth", "swap_count",
]

APPROVAL_COLUMNS = [
    "wallet", "block_time", "block_number", "tx_hash",
    "token", "token_symbol", "counterparty",
    "amount_raw", "is_revoke",
    "gas_used", "gas_price", "gas_cost_eth", "swap_count",
]

PERMIT2_COLUMNS = [
    "record_type", "wallet", "block_time", "block_number", "tx_hash",
    "token", "token_symbol", "counterparty",
    "amount_raw",
    "gas_used", "gas_price", "gas_cost_eth", "swap_count",
]


def _upload_csv(df: pd.DataFrame, blob_path: str) -> None:
    """Stream a DataFrame to GCS as a single CSV blob."""
    buf = StringIO()
    df.to_csv(buf, index=False)
    gcs.bucket().blob(blob_path).upload_from_string(buf.getvalue(), content_type="text/csv")


def run(*, force: bool = False) -> None:
    _, window_start, window_end, lookback_start = study_window()
    print(f"[timeline] study window: {window_start} → {window_end}, approval lookback from {lookback_start}")

    sql = dune.render_named_sql(
        "uniswap_v4/timeline",
        {"window_start": window_start, "window_end": window_end, "lookback_start": lookback_start},
    )
    print(f"[timeline] SQL rendered ({len(sql)} chars)")

    runner = DuneRunner()
    job = Job(key=_JOB_KEY, sql=sql)

    if force:
        runner._forget(job)

    manifest_entry = runner._manifest.get(_JOB_KEY, {})
    if manifest_entry.get("fetch_complete") and not force:
        print("[timeline] fetch already complete for this execution — use --force to re-run")
        return

    # ---- submit / wait ---------------------------------------------------
    print(f"[timeline] submitting/checking job '{_JOB_KEY}'")
    execution_id = runner.submit(job)
    try:
        runner.wait(job, execution_id)
    except _Ended:
        print("[timeline] execution ended in bad state, re-submitting")
        runner._forget(job)
        execution_id = runner.submit(job)
        runner.wait(job, execution_id)

    # ---- determine resume point from manifest ----------------------------
    manifest_entry = runner._manifest.get(_JOB_KEY, {})
    raw_offset  = manifest_entry.get("fetch_next_offset")
    start_offset = int(raw_offset) if raw_offset is not None else 0
    start_page   = int(manifest_entry.get("fetch_pages_done", 0))
    is_resume    = start_offset > 0

    if is_resume:
        print(f"[timeline] resuming from page {start_page + 1}, offset={start_offset:,}")
    else:
        print("[timeline] starting fresh fetch")

    # ---- stream pages into GCS as CSV ------------------------------------
    try:
        for page_num, offset, page_df, nxt in runner.fetch_pages(
            execution_id, start_offset=start_offset, start_page=start_page
        ):
            if "record_type" not in page_df.columns or len(page_df) == 0:
                runner._record(job, fetch_next_offset=nxt, fetch_pages_done=page_num)
                continue

            rt = page_df["record_type"]

            splits = [
                (GCS_SWAPS_PREFIX,     page_df[rt == "swap"],                              SWAP_COLUMNS),
                (GCS_APPROVALS_PREFIX, page_df[rt == "erc20_approval"],                    APPROVAL_COLUMNS),
                (GCS_PERMIT2_PREFIX,   page_df[rt.str.startswith("permit2_", na=False)],  PERMIT2_COLUMNS),
            ]

            page_uploaded = 0
            for prefix, part, cols in splits:
                if len(part) == 0:
                    continue
                trimmed = part[[c for c in cols if c in part.columns]].copy()
                blob = f"{prefix}/page_{page_num:04d}.csv"
                _upload_csv(trimmed, blob)
                print(f"[timeline]   {blob} ← {len(trimmed):,} rows")
                page_uploaded += len(trimmed)

            runner._record(job, fetch_next_offset=nxt, fetch_pages_done=page_num)
            print(f"[timeline] page {page_num} done (offset={offset:,}, {page_uploaded:,} rows total)")

    except CreditError:
        print(
            "[timeline] Dune datapoint limit hit — progress saved to manifest.\n"
            "           Swap in a fresh API key in .env and re-run to continue."
        )
        raise

    # ---- mark complete ---------------------------------------------------
    runner._record(job, fetch_complete=True)
    print("[timeline] all pages uploaded to GCS — fetch complete")


if __name__ == "__main__":
    run(force="--force" in sys.argv)
