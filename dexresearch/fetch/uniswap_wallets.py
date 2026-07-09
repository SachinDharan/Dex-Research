"""Harmonized Uniswap arm — STAGE 1: per-wallet aggregates, full population.

Loads queries/uniswap_v4/wallet_aggregates.sql (one execution per calendar
month) into native BigQuery table `uniswap_v4.wallet_aggregates`, one row per
(wallet, month). Downstream merges months; deciles / bot features / the Stage 2
sample frame all come from this table without leg-level cost.

Design notes (see docs/uniswap_arm_audit.md):
  * anchor is the resolved UNISWAP_ROUTERS entry set, first-leg-sold-stable;
    V4 is a measured attribute (v4_any_txs / v4_first_txs), never a filter
  * the legacy `raw.*` tables are untouched — they remain the pool-touch
    contrast population and cross-check slices
  * resume semantics identical to the Sushi fetchers: per-month manifest keys,
    paged appends, CreditError leaves everything resumable (swap the .env key
    and re-run)

Validation gates are the Dune funnel (query 7923299), measured 2026-07-09.

Run with:
    python -m dexresearch.fetch.uniswap_wallets            # study window
    python -m dexresearch.fetch.uniswap_wallets --status
    python -m dexresearch.fetch.uniswap_wallets --force    # reset stage-1 only
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import date
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd
from google.cloud import bigquery

from dexresearch import dune
from dexresearch.config import get_settings
from dexresearch.dune_runner import CreditError, DuneRunner, Job, _Ended
from dexresearch.fetch._common import study_window

_DATASET = "uniswap_v4"
_TABLE = "wallet_aggregates"
_META_KEY = "uniswap_v4/agg_meta"
_JOB_PREFIX = "uniswap_v4/aggregates/"

# Dune funnel, query 7923299 (2026-07-09) — hard gates for the whole window.
_EXPECT = {
    "qualifying_txs": 478_299, "wallets": 73_039,
    "v4_any_txs": 139_195, "v4_first_txs": 116_721, "sum_legs": 735_223,
}

_S = bigquery.SchemaField
SCHEMA = [
    _S("wallet", "STRING"), _S("month", "STRING"),
    _S("qualifying_txs", "INT64"), _S("v4_any_txs", "INT64"),
    _S("v4_first_txs", "INT64"), _S("ur_txs", "INT64"),
    _S("max_legs", "INT64"), _S("sum_legs", "INT64"),
    _S("zero_prio_txs", "INT64"), _S("gas_eth", "FLOAT64"),
    _S("volume_usd", "FLOAT64"),
    _S("first_seen", "TIMESTAMP"), _S("last_seen", "TIMESTAMP"),
]


def _client() -> bigquery.Client:
    return bigquery.Client(project=get_settings().GCP_PROJECT_ID)


def _table_id() -> str:
    return f"{get_settings().GCP_PROJECT_ID}.{_DATASET}.{_TABLE}"


def _months(window_start: str, window_end: str) -> list[tuple[str, str]]:
    """Calendar-month (start, next-start) pairs covering the half-open window."""
    def parse(s: str) -> date:
        y, m, d = (int(p) for p in s.split("-"))
        return date(y, m, d)

    def next_month(d: date) -> date:
        return date(d.year + (d.month == 12), d.month % 12 + 1, 1)

    out, cur, end = [], parse(window_start), parse(window_end)
    while cur < end:
        nxt = min(next_month(cur), end)
        out.append((cur.isoformat(), nxt.isoformat()))
        cur = nxt
    return out


def _coerce(df: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    for field in SCHEMA:
        col = df[field.name] if field.name in df.columns else pd.Series(pd.NA, index=df.index)
        if field.field_type == "TIMESTAMP":
            out[field.name] = pd.to_datetime(col, errors="coerce", utc=True)
        elif field.field_type == "INT64":
            out[field.name] = pd.to_numeric(col, errors="coerce").astype("Int64")
        elif field.field_type == "FLOAT64":
            out[field.name] = pd.to_numeric(col, errors="coerce")
        else:
            s = col.astype("string")
            if field.name == "wallet":
                s = s.str.lower().where(s.isna() | s.str.startswith("0x"), "0x" + s.str.lower())
            out[field.name] = s
    return out


def _append(df: pd.DataFrame) -> None:
    job_config = bigquery.LoadJobConfig(schema=SCHEMA, write_disposition="WRITE_APPEND")
    _client().load_table_from_dataframe(df, _table_id(), job_config=job_config).result()


def _run_job(runner: DuneRunner, job: Job) -> int:
    """Submit/wait/page one month into BQ, resumably (mirrors the Sushi loader)."""
    entry = runner._manifest.get(job.key, {})
    if entry.get("fetch_complete") and entry.get("sql_hash") == job.sql_hash():
        print(f"[uni] {job.key}: already complete, skipping")
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
            _append(_coerce(page_df))
            total += len(page_df)
            print(f"[uni]   {_DATASET}.{_TABLE} += {len(page_df):,} rows ({job.key} p{page_num})")
        runner._record(job, fetch_next_offset=nxt, fetch_pages_done=page_num)
    runner._record(job, fetch_complete=True)
    return total


def _validate() -> None:
    row = next(iter(_client().query(f"""
        SELECT SUM(qualifying_txs) AS qualifying_txs,
               COUNT(DISTINCT wallet) AS wallets,
               SUM(v4_any_txs) AS v4_any_txs,
               SUM(v4_first_txs) AS v4_first_txs,
               SUM(sum_legs) AS sum_legs
        FROM `{_table_id()}`""").result()))
    got = {k: int(getattr(row, k)) for k in _EXPECT}
    bad = {k: (got[k], _EXPECT[k]) for k in got if got[k] != _EXPECT[k]}
    if bad:
        raise SystemExit(f"[uni] stage-1 validation FAILED (got, expected): {bad}")
    print(f"[uni] gates PASSED: {got['qualifying_txs']:,} txs / {got['wallets']:,} wallets / "
          f"v4_any {got['v4_any_txs']:,} / v4_first {got['v4_first_txs']:,} / legs {got['sum_legs']:,}")


def _meta_hash(window_start: str, window_end: str) -> str:
    return hashlib.sha256(f"{window_start}|{window_end}".encode()).hexdigest()[:16]


def _keys(runner: DuneRunner) -> list[str]:
    return [k for k in runner._manifest if k.startswith("uniswap_v4/")]


def _reset(runner: DuneRunner) -> None:
    for k in _keys(runner):
        runner._manifest.pop(k, None)
    runner._write_manifest()
    _client().delete_table(_table_id(), not_found_ok=True)
    print(f"[uni] dropped {_DATASET}.{_TABLE} and forgot uniswap_v4/* jobs")


def status() -> None:
    runner = DuneRunner()
    for k in sorted(_keys(runner)):
        e = runner._manifest[k]
        if k == _META_KEY:
            print(f"[status]   {k}: window={e.get('window')}")
            continue
        state = "COMPLETE" if e.get("fetch_complete") else \
            f"offset {int(e.get('fetch_next_offset') or 0):,} ({e.get('state', 'not submitted')})"
        print(f"[status]   {k:<40}: {state}")
    try:
        n = next(iter(_client().query(
            f"SELECT COUNT(*) AS n FROM `{_table_id()}`").result())).n
        print(f"[status]   BQ {_TABLE}: {n:,} rows")
    except Exception:
        print(f"[status]   BQ {_TABLE}: table missing")


def run(*, window_start: str, window_end: str, force: bool = False) -> None:
    print(f"[uni] stage 1 window: {window_start} → {window_end} (legacy raw.* untouched)")
    runner = DuneRunner()
    if force:
        _reset(runner)

    meta = runner._manifest.get(_META_KEY, {})
    if meta.get("meta_hash") not in (None, _meta_hash(window_start, window_end)):
        raise SystemExit(
            f"[uni] manifest window {meta.get('window')!r} != {window_start}..{window_end}; "
            "use --force to reset stage 1 for a new window."
        )
    _client().create_dataset(bigquery.Dataset(f"{get_settings().GCP_PROJECT_ID}.{_DATASET}"),
                             exists_ok=True)
    runner._manifest[_META_KEY] = {
        "meta_hash": _meta_hash(window_start, window_end),
        "window": f"{window_start}..{window_end}",
    }
    runner._write_manifest()

    try:
        for month, month_end in _months(window_start, window_end):
            sql = dune.render_named_sql(
                "uniswap_v4/wallet_aggregates", {"month": month, "month_end": month_end})
            _run_job(runner, Job(key=f"{_JOB_PREFIX}{month}", sql=sql))
        _validate()
        print("[uni] STAGE 1 COMPLETE — sample frame ready in "
              f"{_DATASET}.{_TABLE}; Stage 2 (stratified detail fetch) can be sized from it")
    except CreditError:
        print(
            "[uni] Dune datapoint limit hit — progress saved per month in manifest.\n"
            "      Swap in a fresh API key in .env and re-run; completed months are\n"
            "      skipped and the interrupted one resumes (check --status)."
        )
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Uniswap arm stage 1: wallet aggregates (Dune -> BigQuery)")
    parser.add_argument("--window-start", help="override window start (ISO date)")
    parser.add_argument("--window-end", help="override window end, exclusive")
    parser.add_argument("--force", action="store_true", help="reset stage-1 table/jobs, then run")
    parser.add_argument("--status", action="store_true", help="print progress and BQ row count")
    args = parser.parse_args()

    if args.status:
        status()
    elif args.window_start and args.window_end:
        run(window_start=args.window_start, window_end=args.window_end, force=args.force)
    else:
        _, ws, we, _ = study_window()
        run(window_start=ws, window_end=we, force=args.force)
