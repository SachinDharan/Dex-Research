"""Create `<table>_usd` BigQuery views: every arm table with gas columns,
wrapped with a per-row `eth_usd` price and `gas_cost_usd`.

Views, not physical columns — the fetch tables are gate-validated and stay
immutable; a view recomputes from `reference.eth_usd_hourly` so a price
refresh propagates everywhere. `eth_usd_hourly_filled` forward-fills the
handful of tradeless hours first so joins never produce NULL prices.

Run once (idempotent — CREATE OR REPLACE):
    python -m dexresearch.process.usd_views
"""
from __future__ import annotations

from google.cloud import bigquery

PROJECT = "dex-research"

# (dataset, relation) — relations may themselves be views (e.g. approvals_all)
TARGETS = [
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


def run() -> None:
    client = bigquery.Client(project=PROJECT)
    client.query(FILLED_VIEW).result()
    print("reference.eth_usd_hourly_filled created")

    for dataset, rel in TARGETS:
        client.query(f"""
            CREATE OR REPLACE VIEW `{PROJECT}.{dataset}.{rel}_usd` AS
            SELECT t.*,
                   p.close                    AS eth_usd,
                   t.gas_cost_eth * p.close   AS gas_cost_usd
            FROM `{PROJECT}.{dataset}.{rel}` t
            LEFT JOIN `{PROJECT}.reference.eth_usd_hourly_filled` p
              ON p.hour = TIMESTAMP_TRUNC(t.block_time, HOUR)
        """).result()
        print(f"{dataset}.{rel}_usd created")


if __name__ == "__main__":
    run()
