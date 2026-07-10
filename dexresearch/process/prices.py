"""ETH→USD conversion for gas figures, shared by all four analysis modules.

Reads `reference.eth_usd_hourly` (Coinbase hourly close, loaded by
fetch.eth_usd_prices), forward-fills the occasional tradeless hour, and
converts row-level (block_time, gas_cost_eth) pairs. Conversion must happen
at ROW level before any aggregation — ETH moved ~3x across the lookback, so
converting wallet-level sums with any single price would be wrong.
"""
from __future__ import annotations

from functools import lru_cache

import pandas as pd
from google.cloud import bigquery


@lru_cache(maxsize=1)
def eth_usd_hourly() -> pd.Series:
    """Hourly close indexed by hour (UTC), reindexed continuous + ffilled."""
    df = (bigquery.Client(project="dex-research")
          .query("SELECT hour, close FROM `dex-research.reference.eth_usd_hourly`")
          .to_dataframe())
    s = df.set_index("hour")["close"].sort_index()
    full = pd.date_range(s.index.min(), s.index.max(), freq="h", tz="UTC")
    return s.reindex(full).ffill()


def gas_usd(df: pd.DataFrame, gas_col: str = "gas_cost_eth",
            time_col: str = "block_time") -> pd.Series:
    """Row-level USD value of a gas column at each row's hourly ETH price."""
    prices = eth_usd_hourly()
    hours = df[time_col].dt.floor("h")
    return df[gas_col].astype(float) * hours.map(prices).astype(float)
