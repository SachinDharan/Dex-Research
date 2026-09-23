"""Harmonized Uniswap arm — STAGE 2: stratified-sample detail fetch.

Prereq: stage 1 (`fetch.uniswap_wallets`) loaded `uniswap_v4.wallet_aggregates`
for the FULL population. This module:

    stage A  builds `uniswap_v4.sample_wallets` in BigQuery (free) —
             deterministic strata via FARM_FINGERPRINT, reproducible (and
             nested: enlarging a stratum later only ADDS wallets, so a top-up
             fetch is an anti-join, never a refetch):
               * 21-100 tx band: ALL wallets (core coasting analysis)
               * 6-20 band: 3,000 wallets
               * 1 and 2-5 bands: 1,500 wallets each (weight up in analysis)
               * 100+ band: wallets with <=500 txs (searchers beyond that are
                 already characterized by the aggregates table; their legs
                 cost thousands of credits and add nothing about approvals)
    stage B  queries/uniswap_v4/swaps_sampled.sql per wallet batch
             -> uniswap_v4.swaps_sampled (leg-level, project+version per leg)
             HARD GATE: distinct txs and leg counts must EQUAL the
             wallet_aggregates sums for the sampled wallets.
    stage C  queries/sushiswap_v2/approvals.sql (REUSED verbatim: study
             stablecoins, ANY spender, yearly chunks since Permit2 genesis)
             -> uniswap_v4.approvals
    stage D  queries/sushiswap_v2/permit2_events.sql (REUSED: incl. Lockdown)
             -> uniswap_v4.permit2_events

Resume/credit semantics identical to the other fetchers: per-job manifest
keys under uniswap_v4/sample_*, CreditError leaves everything resumable —
swap the .env key and re-run.

Run with:
    python -m dexresearch.fetch.uniswap_sample
    python -m dexresearch.fetch.uniswap_sample --status
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd
from google.cloud import bigquery

from dexresearch import dune
from dexresearch.dune_runner import CreditError, DuneRunner, Job, _Ended
from dexresearch.fetch._common import study_window
from dexresearch.fetch.sushi_timeline import SCHEMAS as SUSHI_SCHEMAS
from dexresearch.fetch.sushi_timeline import _ADDR_RE, _chunks
from dexresearch.fetch.uniswap_wallets import _client, _META_KEY as _AGG_META_KEY

_DATASET = "uniswap_v4"
_PREFIX = "uniswap_v4/sample_"
_WALLET_BATCH = 8000
_LOOKBACK_START = "2022-11-01"   # Permit2 genesis, same rationale as the Sushi arm

_S = bigquery.SchemaField
SCHEMAS: dict[str, list[bigquery.SchemaField]] = {
    "swaps_sampled": [
        _S("wallet", "STRING"), _S("block_time", "TIMESTAMP"), _S("block_number", "INT64"),
        _S("tx_hash", "STRING"), _S("evt_index", "INT64"),
        _S("project", "STRING"), _S("version", "STRING"),
        _S("token", "STRING"), _S("token_symbol", "STRING"),
        _S("counterparty", "STRING"), _S("counter_symbol", "STRING"),
        _S("amount_usd", "FLOAT64"), _S("tx_to", "STRING"),
        _S("gas_used", "INT64"), _S("gas_price", "INT64"), _S("gas_cost_eth", "FLOAT64"),
        _S("max_priority_fee_per_gas", "INT64"), _S("method_id", "STRING"),
    ],
    "approvals": SUSHI_SCHEMAS["approvals"],
    "permit2_events": SUSHI_SCHEMAS["permit2_events"],
}
_HEX_COLS = {"wallet", "tx_hash", "token", "counterparty", "tx_to", "method_id"}


def _table_id(name: str) -> str:
    from dexresearch.config import get_settings
    return f"{get_settings().GCP_PROJECT_ID}.{_DATASET}.{name}"


def _coerce(df: pd.DataFrame, schema: list[bigquery.SchemaField]) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    for field in schema:
        col = df[field.name] if field.name in df.columns else pd.Series(pd.NA, index=df.index)
        if field.field_type == "TIMESTAMP":
            out[field.name] = pd.to_datetime(col, errors="coerce", utc=True)
        elif field.field_type == "INT64":
            out[field.name] = pd.to_numeric(col, errors="coerce").astype("Int64")
        elif field.field_type == "FLOAT64":
            out[field.name] = pd.to_numeric(col, errors="coerce")
        elif field.field_type == "BOOL":
            out[field.name] = col.astype("boolean")
        else:
            s = col.astype("string")
            if field.name in _HEX_COLS:
                s = s.str.lower().where(s.isna() | s.str.startswith("0x"), "0x" + s.str.lower())
            out[field.name] = s
    return out


def _append(df: pd.DataFrame, table: str) -> None:
    job_config = bigquery.LoadJobConfig(schema=SCHEMAS[table], write_disposition="WRITE_APPEND")
    _client().load_table_from_dataframe(df, _table_id(table), job_config=job_config).result()


def _run_job(runner: DuneRunner, job: Job, table: str) -> int:
    entry = runner._manifest.get(job.key, {})
    if entry.get("fetch_complete") and entry.get("sql_hash") == job.sql_hash():
        print(f"[uni2] {job.key}: already complete, skipping")
        return 0
    if entry.get("sql_hash") not in (None, job.sql_hash()):
        runner._forget(job)

    execution_id = runner.submit(job)
    try:
        runner.wait(job, execution_id)
    except _Ended:
        runner._forget(job)
        execution_id = runner.submit(job)
        runner.wait(job, execution_id)

    entry = runner._manifest.get(job.key, {})
    total = 0
    for page_num, offset, page_df, nxt in runner.fetch_pages(
        execution_id,
        start_offset=int(entry.get("fetch_next_offset") or 0),
        start_page=int(entry.get("fetch_pages_done", 0)),
    ):
        if len(page_df) > 0:
            _append(_coerce(page_df, SCHEMAS[table]), table)
            total += len(page_df)
            print(f"[uni2]   {_DATASET}.{table} += {len(page_df):,} rows ({job.key} p{page_num})")
        runner._record(job, fetch_next_offset=nxt, fetch_pages_done=page_num)
    runner._record(job, fetch_complete=True)
    return total


# --------------------------------------------------------------------------
# stage A: sample frame (BigQuery only, free, deterministic)
# --------------------------------------------------------------------------

def build_sample() -> None:
    _client().query(f"""
        CREATE OR REPLACE TABLE `{_table_id('sample_wallets')}` AS
        WITH w AS (
          SELECT wallet, SUM(qualifying_txs) AS txs, SUM(sum_legs) AS legs
          FROM `{_table_id('wallet_aggregates')}` GROUP BY wallet
        ),
        banded AS (
          SELECT *, CASE WHEN txs = 1 THEN 'a_1' WHEN txs <= 5 THEN 'b_2-5'
                         WHEN txs <= 20 THEN 'c_6-20' WHEN txs <= 100 THEN 'd_21-100'
                         ELSE 'e_100+' END AS band
          FROM w
        ),
        ranked AS (
          SELECT *, ROW_NUMBER() OVER (
            PARTITION BY band ORDER BY FARM_FINGERPRINT(wallet)) AS rn
          FROM banded
        )
        SELECT wallet, band, txs, legs FROM ranked
        WHERE band = 'd_21-100'
           OR (band = 'c_6-20' AND rn <= 3000)
           OR (band IN ('a_1', 'b_2-5') AND rn <= 1500)
           OR (band = 'e_100+' AND txs <= 500)
        """).result()
    df = _client().query(f"""
        SELECT band, COUNT(*) AS wallets, SUM(txs) AS txs, SUM(legs) AS legs
        FROM `{_table_id('sample_wallets')}` GROUP BY band ORDER BY band
        """).to_dataframe()
    print("[uni2] sample frame:")
    print(df.to_string(index=False))
    tot_legs = int(df["legs"].sum())
    print(f"[uni2] total: {int(df['wallets'].sum()):,} wallets, {int(df['txs'].sum()):,} txs, "
          f"{tot_legs:,} legs (~{tot_legs * 18 / 2100:,.0f} credits for stage B)")


def _sample_batches() -> list[list[str]]:
    rows = _client().query(
        f"SELECT wallet FROM `{_table_id('sample_wallets')}` ORDER BY wallet").result()
    wallets = [r.wallet for r in rows]
    bad = [w for w in wallets if not _ADDR_RE.match(w)]
    if bad:
        raise ValueError(f"non-address wallets in sample, e.g. {bad[:3]}")
    return [wallets[i:i + _WALLET_BATCH] for i in range(0, len(wallets), _WALLET_BATCH)]


def _validate_swaps() -> None:
    """Sampled swaps must reproduce the aggregate fetch exactly, per wallet."""
    row = next(iter(_client().query(f"""
        WITH got AS (
          SELECT wallet, COUNT(DISTINCT tx_hash) AS txs, COUNT(*) AS legs
          FROM `{_table_id('swaps_sampled')}` GROUP BY wallet
        )
        SELECT
          (SELECT SUM(txs) FROM `{_table_id('sample_wallets')}`)  AS want_txs,
          (SELECT SUM(txs) FROM got)                               AS got_txs,
          (SELECT SUM(legs) FROM `{_table_id('sample_wallets')}`) AS want_legs,
          (SELECT SUM(legs) FROM got)                              AS got_legs,
          (SELECT COUNT(*) FROM `{_table_id('sample_wallets')}` s
             LEFT JOIN got g USING (wallet)
             WHERE g.txs IS NULL OR g.txs != s.txs)                AS mismatched_wallets
        """).result()))
    if row.mismatched_wallets or row.want_txs != row.got_txs or row.want_legs != row.got_legs:
        raise SystemExit(
            f"[uni2] stage-B validation FAILED: txs {row.got_txs}/{row.want_txs}, "
            f"legs {row.got_legs}/{row.want_legs}, mismatched wallets {row.mismatched_wallets}")
    print(f"[uni2] stage-B gates PASSED: {row.got_txs:,} txs / {row.got_legs:,} legs, "
          "every sampled wallet matches its aggregate exactly")


def status() -> None:
    runner = DuneRunner()
    for k in sorted(k for k in runner._manifest if k.startswith(_PREFIX)):
        e = runner._manifest[k]
        state = "COMPLETE" if e.get("fetch_complete") else \
            f"offset {int(e.get('fetch_next_offset') or 0):,} ({e.get('state', 'not submitted')})"
        print(f"[status]   {k:<45}: {state}")
    for table in ["sample_wallets", *SCHEMAS]:
        try:
            n = next(iter(_client().query(
                f"SELECT COUNT(*) AS n FROM `{_table_id(table)}`").result())).n
            print(f"[status]   BQ {table:<16}: {n:,} rows")
        except Exception:
            print(f"[status]   BQ {table:<16}: table missing")


def run(*, window_start: str, window_end: str) -> None:
    print(f"[uni2] stage 2 window: {window_start} → {window_end}")
    runner = DuneRunner()
    agg = runner._manifest.get(_AGG_META_KEY, {})
    if agg.get("window") != f"{window_start}..{window_end}":
        raise SystemExit(f"[uni2] stage-1 window {agg.get('window')!r} mismatch — run stage 1 first.")

    try:
        # ---- stage A: sample frame (free, idempotent, deterministic) --------
        build_sample()
        batches = _sample_batches()
        print(f"[uni2] {sum(len(b) for b in batches):,} sampled wallets in {len(batches)} batch(es)")

        # ---- stage B: leg-level swaps ---------------------------------------
        for bi, batch in enumerate(batches):
            sql = dune.render_named_sql(
                "uniswap_v4/swaps_sampled",
                {"wallets": ", ".join(batch),
                 "window_start": window_start, "window_end": window_end})
            _run_job(runner, Job(key=f"{_PREFIX}swaps/b{bi:02d}", sql=sql), "swaps_sampled")
        _validate_swaps()

        # ---- stage C: approvals (Sushi query reused: stables, ANY spender) --
        for bi, batch in enumerate(batches):
            wallets_sql = ", ".join(batch)
            for chunk_start, chunk_end in _chunks(window_end):
                sql = dune.render_named_sql(
                    "sushiswap_v2/approvals",
                    {"wallets": wallets_sql, "chunk_start": chunk_start, "chunk_end": chunk_end})
                _run_job(runner, Job(key=f"{_PREFIX}approvals/b{bi:02d}/{chunk_start}", sql=sql),
                         "approvals")
        print("[uni2] stage C done")

        # ---- stage D: permit2 events incl. Lockdown (Sushi query reused) ----
        for bi, batch in enumerate(batches):
            sql = dune.render_named_sql(
                "sushiswap_v2/permit2_events",
                {"wallets_values": ", ".join(f"({w})" for w in batch),
                 "window_start": window_start, "window_end": window_end})
            _run_job(runner, Job(key=f"{_PREFIX}permit2/b{bi:02d}", sql=sql), "permit2_events")
        print("[uni2] STAGE 2 COMPLETE — detail tables live in uniswap_v4.*")

    except CreditError:
        print(
            "[uni2] Dune datapoint limit hit — progress saved per job in manifest.\n"
            "       Swap in a fresh API key in .env and re-run; completed jobs are\n"
            "       skipped and the interrupted one resumes (check --status)."
        )
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Uniswap arm stage 2: stratified sample detail fetch")
    parser.add_argument("--window-start", help="override window start (ISO date)")
    parser.add_argument("--window-end", help="override window end, exclusive")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    if args.status:
        status()
    elif args.window_start and args.window_end:
        run(window_start=args.window_start, window_end=args.window_end)
    else:
        _, ws, we, _ = study_window()
        run(window_start=ws, window_end=we)
