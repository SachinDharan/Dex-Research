"""Fetch lending borrow events for sampled wallets (Aave V3, Compound V3).

Output:
    gs://<bucket>/raw/borrows/<chain>_<YYYY-MM>.parquet

Schema:
    wallet         varbinary
    protocol       string       aave_v3 | compound_v3
    block_time     timestamp
    tx_hash        varbinary
    token_address  varbinary    asset borrowed
    amount_raw     decimal(38,0)
    amount_usd     float64

Run with:
    python -m dexresearch.fetch.borrows
"""
from __future__ import annotations


def run() -> None:
    raise NotImplementedError("Wire up the borrows fetch.")


if __name__ == "__main__":
    run()
