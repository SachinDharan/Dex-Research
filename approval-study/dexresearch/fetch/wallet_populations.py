"""Fetch qualifying wallet populations per chain × month.

Produces one parquet per (chain, month) listing every wallet that performed
at least one tracked action on any protocol arm in that window, with the
per-arm action counts. Decile assignment lives downstream in
`process.deciles` (deciles are computed per action_type within each month).

Output:
    gs://<bucket>/raw/wallet_populations/<chain>_<YYYY-MM>.parquet

Schema:
    wallet         varbinary
    protocol       string   uniswap | sushiswap | aave_v3 | compound_v3
    action_type    string   swap | supply | borrow
    action_count   int64
    volume_usd     float64  nullable; populated for swaps

Run with:
    python -m dexresearch.fetch.wallet_populations
"""
from __future__ import annotations

from dexresearch.config import load_study


def run() -> None:
    study = load_study()
    # TODO: for each (chain, month):
    #   1. Run queries/wallet_populations.sql via dune.run_named_query("wallet_populations", ...)
    #      with params (chain, month_start, month_end).
    #   2. Upload to raw/wallet_populations/<chain>_<YYYY-MM>.parquet via
    #      gcs.upload_parquet, attaching metadata={query_id, execution_id, parameters}.
    _ = study
    raise NotImplementedError("Wire up the wallet_populations fetch.")


if __name__ == "__main__":
    run()
