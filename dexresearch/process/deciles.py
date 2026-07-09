"""Assign deciles and flag likely bots; sample the analysis cohort.

Reads raw/wallet_populations/, computes deciles **per (protocol, action_type)
within each month**, flags wallets whose action_count exceeds mean + N*std as
likely bots, and writes the sampled cohort.

Sampling parameters live under `sampling.*` in config/study.yaml. Per the
research plan, the sample skews toward upper deciles (D8-D10).

Output:
    gs://<bucket>/processed/deciles/<chain>_<YYYY-MM>.parquet

Schema:
    wallet         varbinary
    protocol       string
    action_type    string
    action_count   int64
    decile         int8        1..10, ranked within (protocol, action_type)
    is_bot         bool
    sampled        bool        included in downstream cohort

Run with:
    python -m dexresearch.process.deciles
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from dexresearch.config import load_study


def assign_deciles(df: pd.DataFrame, action_col: str = "action_count") -> pd.DataFrame:
    """Add a `decile` column (1..10) ranked by action_count within
    (protocol, action_type) groups."""
    out = df.copy()
    out["decile"] = (
        out.groupby(["protocol", "action_type"])[action_col]
        .transform(lambda s: pd.qcut(s, 10, labels=False, duplicates="drop") + 1)
        .astype("int8")
    )
    return out


def flag_bots(
    df: pd.DataFrame, sd_threshold: float, action_col: str = "action_count"
) -> pd.DataFrame:
    """Flag wallets > mean + sd_threshold * std within each (protocol, action_type)."""
    out = df.copy()

    # transform, not apply: apply collapses to a DataFrame when there is only
    # one group, and re-orders rows when there are several.
    def _flag(s: pd.Series) -> pd.Series:
        return s > (s.mean() + sd_threshold * s.std(ddof=0))

    out["is_bot"] = (
        out.groupby(["protocol", "action_type"])[action_col].transform(_flag).astype(bool)
    )
    return out


def sample_cohort(
    df: pd.DataFrame,
    per_decile: int,
    upper_decile_oversample: float,
    rng: np.random.Generator | None = None,
) -> pd.DataFrame:
    """Sample wallets from each decile per (protocol, action_type).
    D8-D10 receive `upper_decile_oversample` × the base size."""
    rng = rng or np.random.default_rng(0)
    out = df.copy()
    out["sampled"] = False
    for (_protocol, _action, decile), group in out.groupby(
        ["protocol", "action_type", "decile"]
    ):
        n = int(round(per_decile * upper_decile_oversample)) if decile >= 8 else per_decile
        n = min(n, len(group))
        picks = rng.choice(group.index.to_numpy(), size=n, replace=False)
        out.loc[picks, "sampled"] = True
    return out


def run() -> None:
    study = load_study()
    sampling = study["sampling"]
    # TODO: for each (chain, month):
    #   1. download_parquet(f"raw/wallet_populations/{chain}_{month}.parquet")
    #   2. assign_deciles -> flag_bots -> sample_cohort (excluding is_bot from sampling)
    #   3. upload_parquet to processed/deciles/<chain>_<month>.parquet
    _ = sampling
    raise NotImplementedError("Wire up the deciles loop.")


if __name__ == "__main__":
    run()
