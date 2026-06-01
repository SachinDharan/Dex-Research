"""Fetch tx-level gas data joined with daily ETH/USD prices.

One row per relevant transaction (approvals + swaps + supplies + borrows
combined), keyed by tx_hash so the metrics stage can compute USD gas costs
for any action without re-querying Dune.

Output:
    gs://<bucket>/raw/gas_measurements/<chain>_<YYYY-MM>.parquet

Schema:
    tx_hash               varbinary
    block_time            timestamp
    gas_used              int64
    gas_price_wei         int64     effective_gas_price post-EIP-1559
    native_usd_price      float64   ETH (or POL) USD price at block_time's day
    gas_cost_native       float64   gas_used * gas_price / 1e18
    gas_cost_usd          float64   gas_cost_native * native_usd_price

Run with:
    python -m dexresearch.fetch.gas_measurements
"""
from __future__ import annotations


def run() -> None:
    # TODO: gather tx_hashes from raw/{approvals,swaps,supplies,borrows}/,
    # union them, chunk, call dune.run_named_query("gas_measurements", ...),
    # upload.
    raise NotImplementedError("Wire up the gas_measurements fetch.")


if __name__ == "__main__":
    run()
