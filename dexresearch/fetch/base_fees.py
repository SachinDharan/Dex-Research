"""Per-block base fees — the EIP-1559 fee decomposition, with zero Dune involvement.

Source: BigQuery public dataset `bigquery-public-data.crypto_ethereum.blocks`
(free, hosted by Google). One CTAS materializes block_number -> base_fee_per_gas
into `dex-research.reference.block_base_fees` for the full study span
(2022-08-01, Comet genesis — the deepest lookback any stored gas figure
reaches — through the study window end).

Every arm table already stores block_number and the *effective* gas_price, so
post-London the actual tip is an exact identity:

    priority_fee_per_gas = gas_price - base_fee_per_gas

The `<table>_usd` views (dexresearch.process.usd_views) join this table and
expose both columns; nothing is re-fetched from Dune.

Run with:
    python -m dexresearch.fetch.base_fees
"""
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from google.cloud import bigquery

from dexresearch.config import get_settings

PUBLIC_BLOCKS = "bigquery-public-data.crypto_ethereum.blocks"
START = "2022-08-01"
END = "2026-03-01"

DATASET = "reference"
TABLE = "block_base_fees"


def run() -> None:
    settings = get_settings()
    client = bigquery.Client(project=settings.GCP_PROJECT_ID)
    client.create_dataset(f"{settings.GCP_PROJECT_ID}.{DATASET}", exists_ok=True)
    table_id = f"{settings.GCP_PROJECT_ID}.{DATASET}.{TABLE}"

    print(f"[base_fees] materializing {table_id} from {PUBLIC_BLOCKS} "
          f"({START} -> {END})")
    client.query(f"""
        CREATE OR REPLACE TABLE `{table_id}` AS
        SELECT
          number    AS block_number,
          timestamp AS block_time,
          base_fee_per_gas
        FROM `{PUBLIC_BLOCKS}`
        WHERE timestamp >= TIMESTAMP '{START}'
          AND timestamp <  TIMESTAMP '{END}'
    """).result()

    row = next(iter(client.query(f"""
        SELECT COUNT(*) AS n,
               COUNTIF(base_fee_per_gas IS NULL) AS n_null,
               MIN(block_number) AS bn_min, MAX(block_number) AS bn_max
        FROM `{table_id}`
    """).result()))
    # post-London every block has a base fee; a NULL means a source gap
    if row.n == 0 or row.n_null > 0:
        raise SystemExit(f"[base_fees] validation failed: {row.n:,} rows, "
                         f"{row.n_null} NULL base fees")
    # contiguous block numbers <=> no missing blocks in the range
    if row.bn_max - row.bn_min + 1 != row.n:
        raise SystemExit(f"[base_fees] validation failed: blocks not contiguous "
                         f"({row.bn_min:,}..{row.bn_max:,} but {row.n:,} rows)")
    print(f"[base_fees] loaded {row.n:,} blocks "
          f"({row.bn_min:,}..{row.bn_max:,}), no gaps, no NULLs")


if __name__ == "__main__":
    run()
