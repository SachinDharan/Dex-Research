"""Out-of-band validator payments (coinbase transfers) within study transactions.

MEV searcher bundles routinely pay the block producer with an *internal* ETH
transfer to `block.coinbase` instead of (or on top of) the EIP-1559 priority
fee. Those payments are invisible in every gas-price field, so for any tx the
complete validator-side payment is:

    priority_fee_per_gas * gas_used  +  coinbase transfers inside the tx

This module documents them for the whole study:

  1. `reference.study_txs` — every distinct (tx_hash, block_number) across the
     17 arm relations (~3.5M txs).
  2. `reference.study_coinbase_transfers` — every successful internal ETH
     transfer whose recipient is that block's miner/fee recipient, from the
     public `bigquery-public-data.crypto_ethereum.traces` dataset.

The `<table>_usd` views (dexresearch.process.usd_views) join the result and
expose `coinbase_transfer_eth` per row (NULL when the tx paid nothing
out-of-band).

COST WARNING: the traces scan reads ~1.5 TB of the public dataset
(~$9 at on-demand pricing, first TB/month free). Run 2026-07-10; re-run only
if the arm tables gain new transactions.

Findings (2026-07-10 run): see docs/mev_out_of_band_findings.md. Headline:
26.3% of uniswap_v4.txs_broad txs carry a coinbase transfer (searcher bots in
the broad sample); qualifying swaps are exactly 0%; approvals 0.01-0.61%.

Run with:
    python -m dexresearch.fetch.coinbase_transfers
"""
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from google.cloud import bigquery

from dexresearch.config import get_settings

PUBLIC_TRACES = "bigquery-public-data.crypto_ethereum.traces"
PUBLIC_BLOCKS = "bigquery-public-data.crypto_ethereum.blocks"
START = "2022-08-01"
END = "2026-03-01"

DATASET = "reference"

# (dataset, relation) — the same 17 relations the _usd views wrap
ARM_RELATIONS = [
    ("sushiswap_v2", "swaps_router_entry"),
    ("sushiswap_v2", "approvals_all"),
    ("sushiswap_v2", "permit2_events_all"),
    ("uniswap_v4", "swaps_sampled"),
    ("uniswap_v4", "txs_broad"),
    ("uniswap_v4", "approvals"),
    ("uniswap_v4", "permit2_events"),
    ("compound_v3", "supplies"),
    ("compound_v3", "withdraws"),
    ("compound_v3", "allow_events"),
    ("compound_v3", "approvals"),
    ("compound_v3", "permit2_events"),
    ("aave_v3", "supplies"),
    ("aave_v3", "borrows"),
    ("aave_v3", "delegation_events"),
    ("aave_v3", "approvals"),
    ("aave_v3", "permit2_events"),
]


def run() -> None:
    settings = get_settings()
    project = settings.GCP_PROJECT_ID
    client = bigquery.Client(project=project)

    union = "\nUNION ALL\n".join(
        f"SELECT tx_hash, block_number, block_time FROM `{project}.{d}.{r}`"
        for d, r in ARM_RELATIONS)
    print(f"[coinbase] materializing {project}.{DATASET}.study_txs")
    client.query(f"""
        CREATE OR REPLACE TABLE `{project}.{DATASET}.study_txs` AS
        SELECT LOWER(tx_hash) AS tx_hash, block_number, MIN(block_time) AS block_time
        FROM ({union})
        GROUP BY 1, 2
    """).result()

    dry = client.query(_traces_sql(project), job_config=bigquery.QueryJobConfig(dry_run=True))
    print(f"[coinbase] traces scan will process {dry.total_bytes_processed / 1e12:.2f} TB "
          f"(~${6.25 * dry.total_bytes_processed / 1e12:.2f} on-demand) — running")
    client.query(_traces_sql(project)).result()

    row = next(iter(client.query(f"""
        SELECT (SELECT COUNT(*) FROM `{project}.{DATASET}.study_txs`) AS n_txs,
               COUNT(*) AS n_transfers,
               COUNT(DISTINCT tx_hash) AS n_hit,
               IFNULL(SUM(eth), 0) AS total_eth
        FROM `{project}.{DATASET}.study_coinbase_transfers`
    """).result()))
    print(f"[coinbase] {row.n_txs:,} study txs; {row.n_hit:,} "
          f"({100 * row.n_hit / row.n_txs:.2f}%) carry a coinbase transfer; "
          f"{row.n_transfers:,} transfers, {row.total_eth:,.1f} ETH total")


def _traces_sql(project: str) -> str:
    return f"""
        CREATE OR REPLACE TABLE `{project}.{DATASET}.study_coinbase_transfers` AS
        WITH miners AS (
          SELECT number AS block_number, LOWER(miner) AS miner
          FROM `{PUBLIC_BLOCKS}`
          WHERE timestamp >= TIMESTAMP '{START}' AND timestamp < TIMESTAMP '{END}'
        )
        SELECT
          s.tx_hash,
          s.block_number,
          s.block_time,
          m.miner,
          t.from_address,
          t.value / 1e18 AS eth,
          /* root-call sends to the miner would be direct payments; every
             observed transfer so far is internal (searcher-bundle pattern) */
          t.trace_address IS NOT NULL AND t.trace_address != '' AS is_internal
        FROM `{PUBLIC_TRACES}` t
        JOIN `{project}.{DATASET}.study_txs` s
          ON LOWER(t.transaction_hash) = s.tx_hash AND t.block_number = s.block_number
        JOIN miners m ON m.block_number = t.block_number
        WHERE t.block_timestamp >= TIMESTAMP '{START}'
          AND t.block_timestamp <  TIMESTAMP '{END}'
          AND LOWER(t.to_address) = m.miner
          AND t.value > 0
          AND t.status = 1
    """


if __name__ == "__main__":
    run()
