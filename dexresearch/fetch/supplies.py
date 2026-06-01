"""Fetch lending supply (deposit collateral) events for sampled wallets.

Covers Aave V3 and Compound V3 on both chains.

Output:
    gs://<bucket>/raw/supplies/<chain>_<YYYY-MM>.parquet

Schema:
    wallet         varbinary
    protocol       string       aave_v3 | compound_v3
    block_time     timestamp
    tx_hash        varbinary
    token_address  varbinary    asset supplied
    amount_raw     decimal(38,0)
    amount_usd     float64

Run with:
    python -m dexresearch.fetch.supplies
"""
from __future__ import annotations


def run() -> None:
    raise NotImplementedError("Wire up the supplies fetch.")


if __name__ == "__main__":
    run()
