"""ETH/USD hourly price series — the gas→USD join, with zero Dune involvement.

Source: Coinbase Exchange public candles API (keyless, free). Hourly OHLC
from 2022-08-01 (Comet genesis — the deepest lookback any stored gas figure
reaches) to the study window end. ~31k rows, loaded into
`dex-research.reference.eth_usd_hourly`; analysis modules join on
TIMESTAMP_TRUNC(block_time, HOUR) via dexresearch.process.prices.

Hourly close is the chosen granularity: intra-hour ETH moves are noise
relative to the honesty gained over a constant multiplier, and the series is
citable ("Coinbase ETH-USD hourly close"). Missing hours (no trades) are NOT
filled here — the consumer forward-fills, so the stored table stays raw.

Run with:
    python -m dexresearch.fetch.eth_usd_prices
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd
import requests
from google.cloud import bigquery

from dexresearch.config import get_settings

API = "https://api.exchange.coinbase.com/products/ETH-USD/candles"
GRANULARITY = 3600
BATCH_HOURS = 300          # API cap per request
START = datetime(2022, 8, 1, tzinfo=timezone.utc)
END = datetime(2026, 3, 1, tzinfo=timezone.utc)

DATASET = "reference"
TABLE = "eth_usd_hourly"

SCHEMA = [
    bigquery.SchemaField("hour", "TIMESTAMP"),
    bigquery.SchemaField("open", "FLOAT64"),
    bigquery.SchemaField("high", "FLOAT64"),
    bigquery.SchemaField("low", "FLOAT64"),
    bigquery.SchemaField("close", "FLOAT64"),
    bigquery.SchemaField("volume", "FLOAT64"),
]


def fetch() -> pd.DataFrame:
    rows: list[list[float]] = []
    cur = START
    n_req = 0
    while cur < END:
        chunk_end = min(cur + timedelta(hours=BATCH_HOURS), END)
        for attempt in range(5):
            resp = requests.get(API, params={
                "granularity": GRANULARITY,
                "start": cur.isoformat(),
                "end": chunk_end.isoformat(),
            }, timeout=30)
            if resp.status_code == 200:
                rows.extend(resp.json())
                break
            time.sleep(2 ** attempt)   # public rate limit / transient errors
        else:
            raise RuntimeError(f"candles request kept failing at {cur}")
        n_req += 1
        if n_req % 20 == 0:
            print(f"  {n_req} requests, {len(rows):,} candles (at {cur:%Y-%m-%d})")
        cur = chunk_end
        time.sleep(0.15)               # stay well under the public rate limit

    df = pd.DataFrame(rows, columns=["ts", "low", "high", "open", "close", "volume"])
    df["hour"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    df = (df.drop(columns="ts").drop_duplicates("hour")
          .sort_values("hour").reset_index(drop=True))
    return df[["hour", "open", "high", "low", "close", "volume"]]


def validate(df: pd.DataFrame) -> None:
    expected = int((END - START).total_seconds() // 3600)
    missing = expected - len(df)
    bad = df[(df["close"] <= 0) | (df["close"] > 100_000)]
    if len(bad):
        raise SystemExit(f"[prices] {len(bad)} candles with implausible close values")
    if missing > expected * 0.001:
        raise SystemExit(f"[prices] {missing} of {expected} hours missing (>0.1%) — investigate")
    print(f"[prices] validation OK: {len(df):,} of {expected:,} hours "
          f"({missing} missing, forward-filled by the consumer); "
          f"close range ${df['close'].min():,.0f}–${df['close'].max():,.0f}")


def load(df: pd.DataFrame) -> None:
    settings = get_settings()
    client = bigquery.Client(project=settings.GCP_PROJECT_ID)
    client.create_dataset(f"{settings.GCP_PROJECT_ID}.{DATASET}", exists_ok=True)
    job_config = bigquery.LoadJobConfig(schema=SCHEMA, write_disposition="WRITE_TRUNCATE")
    table_id = f"{settings.GCP_PROJECT_ID}.{DATASET}.{TABLE}"
    client.load_table_from_dataframe(df, table_id, job_config=job_config).result()
    print(f"[prices] loaded {len(df):,} rows into {table_id}")

    local = settings.DATA_DIR / "reference" / "eth_usd_hourly.csv"
    local.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(local, index=False)
    print(f"[prices] local copy: {local}")


def run() -> None:
    print(f"[prices] fetching Coinbase ETH-USD hourly candles {START:%Y-%m-%d} → {END:%Y-%m-%d}")
    df = fetch()
    validate(df)
    load(df)


if __name__ == "__main__":
    run()
