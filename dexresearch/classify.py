"""Classify ERC-20 approval events.

Per the research plan, each approve() event falls into one of three buckets:

  - unlimited   : allowance == uint256 max (2**256 - 1)
  - revocation  : allowance == 0
  - exact       : anything else (typically matches a specific upcoming tx value)

Some wallets issue "near-unlimited" approvals just below uint256 max. Pass a
`near_unlimited_threshold` (e.g., 2**255) to coalesce those into unlimited;
default is strict equality.
"""
from __future__ import annotations

import pandas as pd

UINT256_MAX = 2**256 - 1


def classify_allowance(value: int, near_unlimited_threshold: int | None = None) -> str:
    if value == 0:
        return "revocation"
    if value == UINT256_MAX:
        return "unlimited"
    if near_unlimited_threshold is not None and value >= near_unlimited_threshold:
        return "unlimited"
    return "exact"


def classify_dataframe(
    df: pd.DataFrame,
    allowance_col: str = "value",
    near_unlimited_threshold: int | None = None,
) -> pd.DataFrame:
    out = df.copy()
    out["approval_type"] = out[allowance_col].apply(
        lambda v: classify_allowance(int(v), near_unlimited_threshold)
    )
    return out
