"""Create `<table>_usd` BigQuery views: every arm table with gas columns,
wrapped with a per-row `eth_usd` price, `gas_cost_usd`, and the EIP-1559 fee
decomposition (`base_fee_per_gas`, `priority_fee_per_gas`).

Views, not physical columns — the fetch tables are gate-validated and stay
immutable; a view recomputes from `reference.eth_usd_hourly` so a price
refresh propagates everywhere. `eth_usd_hourly_filled` forward-fills the
handful of tradeless hours first so joins never produce NULL prices.

Base fees join from `reference.block_base_fees` (dexresearch.fetch.base_fees)
on block_number. The stored gas_price is the *effective* price paid, so
priority_fee_per_gas = gas_price - base_fee_per_gas is exact post-London.

`coinbase_transfer_eth` joins from `reference.study_coinbase_transfers`
(dexresearch.fetch.coinbase_transfers): total out-of-band ETH the tx paid the
block producer via internal transfers — the MEV-bundle payment channel that
never shows up in gas-price fields. NULL means none; non-NULL flags likely
searcher/bot activity (see docs/mev_out_of_band_findings.md).

Run once (idempotent — CREATE OR REPLACE):
    python -m dexresearch.process.usd_views
"""
from __future__ import annotations

from google.cloud import bigquery

PROJECT = "dex-research"

# (dataset, relation) — relations may themselves be views (e.g. approvals_all)
TARGETS = [
    ("sushiswap_v2", "swaps_router_entry"),
    ("sushiswap_v2", "swaps_all"),
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

FILLED_VIEW = f"""
CREATE OR REPLACE VIEW `{PROJECT}.reference.eth_usd_hourly_filled` AS
WITH spine AS (
  SELECT hour FROM UNNEST(GENERATE_TIMESTAMP_ARRAY(
    (SELECT MIN(hour) FROM `{PROJECT}.reference.eth_usd_hourly`),
    (SELECT MAX(hour) FROM `{PROJECT}.reference.eth_usd_hourly`),
    INTERVAL 1 HOUR)) AS hour
)
SELECT s.hour,
       LAST_VALUE(p.close IGNORE NULLS)
         OVER (ORDER BY s.hour ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS close
FROM spine s
LEFT JOIN `{PROJECT}.reference.eth_usd_hourly` p USING (hour)
"""


# Full qualifying swap-leg population: both discovery anchors, disjoint by
# construction (pool-anchored + tx_to-anchored remainder); swaps_router_entry
# is its router-entry subset. Explicit column list because the delta table
# has no swap_count (stale in `swaps` anyway; recount legs per tx_hash).
_SWAP_COLS = """wallet, block_time, block_number, tx_hash, token, token_symbol,
    counterparty, counter_symbol, amount_usd, tx_to, evt_index, project, pool,
    taker, gas_used, gas_price, gas_cost_eth, max_priority_fee_per_gas, method_id"""

SWAPS_ALL_VIEW = f"""
CREATE OR REPLACE VIEW `{PROJECT}.sushiswap_v2.swaps_all` AS
SELECT {_SWAP_COLS} FROM `{PROJECT}.sushiswap_v2.swaps`
UNION ALL
SELECT {_SWAP_COLS} FROM `{PROJECT}.sushiswap_v2.swaps_txto_delta`
"""


def run() -> None:
    client = bigquery.Client(project=PROJECT)
    client.query(FILLED_VIEW).result()
    print("reference.eth_usd_hourly_filled created")
    client.query(SWAPS_ALL_VIEW).result()
    print("sushiswap_v2.swaps_all created")

    for dataset, rel in TARGETS:
        client.query(f"""
            CREATE OR REPLACE VIEW `{PROJECT}.{dataset}.{rel}_usd` AS
            SELECT t.*,
                   p.close                          AS eth_usd,
                   t.gas_cost_eth * p.close         AS gas_cost_usd,
                   b.base_fee_per_gas,
                   t.gas_price - b.base_fee_per_gas AS priority_fee_per_gas,
                   cb.coinbase_transfer_eth
            FROM `{PROJECT}.{dataset}.{rel}` t
            LEFT JOIN `{PROJECT}.reference.eth_usd_hourly_filled` p
              ON p.hour = TIMESTAMP_TRUNC(t.block_time, HOUR)
            LEFT JOIN `{PROJECT}.reference.block_base_fees` b
              ON b.block_number = t.block_number
            LEFT JOIN (
              SELECT tx_hash, SUM(eth) AS coinbase_transfer_eth
              FROM `{PROJECT}.reference.study_coinbase_transfers`
              GROUP BY tx_hash
            ) cb ON cb.tx_hash = LOWER(t.tx_hash)
        """).result()
        print(f"{dataset}.{rel}_usd created")


if __name__ == "__main__":
    run()
