"""Compute per-wallet and per-decile metrics from raw + processed data.

Inputs (read from GCS):
    processed/deciles/<chain>_<month>.parquet
    raw/approvals/<chain>_<month>.parquet
    raw/swaps/<chain>_<month>.parquet
    raw/supplies/<chain>_<month>.parquet
    raw/borrows/<chain>_<month>.parquet
    raw/gas_measurements/<chain>_<month>.parquet

Metrics from the research plan:
    - action_count                 (from deciles)
    - approval_count_total
    - approval_count_unlimited / _exact / _revocation
    - approval_to_action_ratio
    - approval_gas_usd
    - outstanding_allowance_end    last non-zero allowance per (wallet, token, spender)
                                   as of end of window

Outputs:
    gs://<bucket>/processed/wallet_metrics/<chain>_<YYYY-MM>.parquet
    gs://<bucket>/processed/decile_metrics/<chain>_<YYYY-MM>.parquet

Run with:
    python -m dexresearch.process.metrics
"""
from __future__ import annotations

from dexresearch.classify import classify_dataframe


def run() -> None:
    _ = classify_dataframe
    # TODO:
    #   1. Join sampled wallets (deciles) with raw/approvals on wallet.
    #   2. classify_dataframe(approvals).
    #   3. Join approvals + actions with gas_measurements on tx_hash for USD costs.
    #   4. Aggregate to wallet-level metrics.
    #   5. Aggregate to (decile, protocol, action_type) level: mean, median, p90.
    #   6. Upload both.
    raise NotImplementedError("Wire up the metrics computation.")


if __name__ == "__main__":
    run()
