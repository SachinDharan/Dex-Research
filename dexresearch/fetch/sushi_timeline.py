"""SushiSwap V2 arm fetch — staged Dune executions straight into BigQuery.

The Execute SQL plan caps every execution at 2 minutes, so the arm is fetched
as many small executions instead of one monster query:

    stage 1  queries/sushiswap_v2/swaps.sql            -> sushiswap_v2.swaps
    stage 2  wallet set read back from BigQuery (free)
    stage 3  queries/sushiswap_v2/approvals.sql        -> sushiswap_v2.approvals
             one execution per (wallet batch x time chunk since Permit2 genesis)
    stage 4  queries/sushiswap_v2/permit2_events.sql   -> sushiswap_v2.permit2_events
             one execution per wallet batch, study window only

Read-cost discipline: each stage's SQL returns exactly its table's columns (no
UNION superset padding — Dune bills reads by rows x columns incl. NULLs), pages
append directly into native BQ tables via load_table_from_dataframe, and the
manifest tracks every job so nothing is re-executed or re-read.

Resume: every job (swaps, each approvals batch/chunk, each permit2 batch) has
its own manifest key with page offsets.  A changed window (dry run -> full run)
changes the meta hash, which drops the tables and forgets all sushi jobs for a
clean load.

Run with:
    python -m dexresearch.fetch.sushi_timeline                       # study window
    python -m dexresearch.fetch.sushi_timeline --window-start 2025-11-01 --window-end 2025-11-08
    python -m dexresearch.fetch.sushi_timeline --status              # where are we?
    python -m dexresearch.fetch.sushi_timeline --force               # reset + reload
"""
from __future__ import annotations

import argparse
import hashlib
import re
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

_DATASET = "sushiswap_v2"
_META_KEY = "sushiswap_v2/meta"

# Approval lookback reaches to Permit2 genesis, not window-18mo: "outstanding
# allowance at end of period" needs the latest approval ever.
_LOOKBACK_START = "2022-11-01"

_WALLET_BATCH = 8000        # ~350KB of literals per query, under Dune's 500KB SQL cap
_ADDR_RE = re.compile(r"^0x[0-9a-f]{40}$")

_S = bigquery.SchemaField
_GAS_TAIL = [
    _S("gas_used", "INT64"), _S("gas_price", "INT64"), _S("gas_cost_eth", "FLOAT64"),
    _S("max_priority_fee_per_gas", "INT64"), _S("method_id", "STRING"),
]
_CORE = [
    _S("wallet", "STRING"), _S("block_time", "TIMESTAMP"), _S("block_number", "INT64"),
    _S("tx_hash", "STRING"), _S("token", "STRING"), _S("token_symbol", "STRING"),
    _S("counterparty", "STRING"),
]

SCHEMAS: dict[str, list[bigquery.SchemaField]] = {
    "swaps": _CORE + [
        _S("counter_symbol", "STRING"), _S("amount_usd", "FLOAT64"),
        _S("tx_to", "STRING"), _S("evt_index", "INT64"), _S("project", "STRING"),
        _S("pool", "STRING"), _S("taker", "STRING"),
    ] + _GAS_TAIL + [_S("swap_count", "INT64")],
    "approvals": _CORE + [
        _S("amount_raw", "STRING"), _S("is_revoke", "BOOL"),
    ] + _GAS_TAIL,
    "permit2_events": [_S("record_type", "STRING")] + _CORE + [
        _S("amount_raw", "STRING"), _S("is_revoke", "BOOL"),
    ] + _GAS_TAIL,
}

# Dune's to_hex() returns bare uppercase hex; normalise to 0x-lowercase so
# Etherscan links paste cleanly and stage-3/4 wallet literals are valid SQL.
_HEX_COLS = {"wallet", "tx_hash", "token", "counterparty", "tx_to", "pool", "taker", "method_id"}


def _client() -> bigquery.Client:
    return bigquery.Client(project=get_settings().GCP_PROJECT_ID)


def _table_id(name: str) -> str:
    return f"{get_settings().GCP_PROJECT_ID}.{_DATASET}.{name}"


def _coerce(df: pd.DataFrame, schema: list[bigquery.SchemaField]) -> pd.DataFrame:
    """Select schema columns (missing -> NULL), coerce dtypes, normalise hex."""
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


def _drop_tables() -> None:
    for name in SCHEMAS:
        _client().delete_table(_table_id(name), not_found_ok=True)
    print(f"[sushi] dropped {_DATASET}.{{{','.join(SCHEMAS)}}} for fresh load")


def _chunks(window_end: str) -> list[tuple[str, str]]:
    """Yearly slices from Permit2 genesis to window end, for the approvals stage."""
    bounds = []
    y, m, d = (int(p) for p in _LOOKBACK_START.split("-"))
    cur = date(y, m, d)
    end = date(*(int(p) for p in window_end.split("-")))
    while cur < end:
        bounds.append(cur.isoformat())
        cur = date(cur.year + 1, cur.month, cur.day)
    bounds.append(end.isoformat())
    return list(zip(bounds, bounds[1:]))


def _wallet_batches() -> list[list[str]]:
    rows = _client().query(
        f"SELECT DISTINCT wallet FROM `{_table_id('swaps')}` WHERE wallet IS NOT NULL ORDER BY wallet"
    ).result()
    wallets = [r.wallet for r in rows]
    bad = [w for w in wallets if not _ADDR_RE.match(w)]
    if bad:
        raise ValueError(f"non-address wallet values in swaps table, e.g. {bad[:3]}")
    return [wallets[i:i + _WALLET_BATCH] for i in range(0, len(wallets), _WALLET_BATCH)]


def _run_job_to_table(runner: DuneRunner, job: Job, table: str) -> int:
    """Submit/wait/page one job into one BQ table, resumably. Returns rows loaded."""
    entry = runner._manifest.get(job.key, {})
    if entry.get("fetch_complete") and entry.get("sql_hash") == job.sql_hash():
        print(f"[sushi] {job.key}: already complete, skipping")
        return 0
    if entry.get("sql_hash") not in (None, job.sql_hash()):
        runner._forget(job)   # stale SQL — restart this job's paging

    execution_id = runner.submit(job)
    try:
        runner.wait(job, execution_id)
    except _Ended:
        runner._forget(job)
        execution_id = runner.submit(job)
        runner.wait(job, execution_id)

    entry = runner._manifest.get(job.key, {})
    start_offset = int(entry.get("fetch_next_offset") or 0)
    start_page = int(entry.get("fetch_pages_done", 0))

    total = 0
    for page_num, offset, page_df, nxt in runner.fetch_pages(
        execution_id, start_offset=start_offset, start_page=start_page
    ):
        if len(page_df) > 0:
            _append(_coerce(page_df, SCHEMAS[table]), table)
            total += len(page_df)
            print(f"[sushi]   {_DATASET}.{table} += {len(page_df):,} rows ({job.key} p{page_num})")
        runner._record(job, fetch_next_offset=nxt, fetch_pages_done=page_num)
    runner._record(job, fetch_complete=True)
    return total


def _meta_hash(window_start: str, window_end: str) -> str:
    return hashlib.sha256(f"{window_start}|{window_end}|{_LOOKBACK_START}".encode()).hexdigest()[:16]


def _sushi_keys(runner: DuneRunner) -> list[str]:
    return [k for k in runner._manifest if k.startswith("sushiswap_v2/")]


def status() -> None:
    runner = DuneRunner()
    keys = _sushi_keys(runner)
    print(f"[status] {len(keys)} sushi job(s) in manifest")
    for k in sorted(keys):
        e = runner._manifest[k]
        if k == _META_KEY:
            print(f"[status]   {k}: window={e.get('window')} hash={e.get('meta_hash')}")
            continue
        state = "COMPLETE" if e.get("fetch_complete") else \
            f"offset {int(e.get('fetch_next_offset') or 0):,} ({e.get('state', 'not submitted')})"
        print(f"[status]   {k:<45}: {state}")
    for table in SCHEMAS:
        try:
            n = next(iter(_client().query(
                f"SELECT COUNT(*) AS n FROM `{_table_id(table)}`").result())).n
            print(f"[status]   BQ {table:<15}: {n:,} rows")
        except Exception:
            print(f"[status]   BQ {table:<15}: table missing")


def run(*, window_start: str, window_end: str, force: bool = False) -> None:
    print(f"[sushi] window: {window_start} → {window_end}, approvals lookback from {_LOOKBACK_START}")
    runner = DuneRunner()

    meta = runner._manifest.get(_META_KEY, {})
    fresh = force or meta.get("meta_hash") != _meta_hash(window_start, window_end)
    if fresh:
        print("[sushi] new window (or --force) — resetting all sushi jobs and tables")
        for k in _sushi_keys(runner):
            runner._manifest.pop(k, None)
        runner._write_manifest()
        _drop_tables()
        runner._manifest[_META_KEY] = {
            "meta_hash": _meta_hash(window_start, window_end),
            "window": f"{window_start}..{window_end}",
        }
        runner._write_manifest()

    try:
        # ---- stage 1: swaps ------------------------------------------------
        sql = dune.render_named_sql(
            "sushiswap_v2/swaps", {"window_start": window_start, "window_end": window_end})
        n = _run_job_to_table(runner, Job(key="sushiswap_v2/swaps", sql=sql), "swaps")
        print(f"[sushi] stage 1 done ({n:,} rows this run)")

        # ---- stage 2: wallet set from BQ ------------------------------------
        batches = _wallet_batches()
        n_wallets = sum(len(b) for b in batches)
        print(f"[sushi] stage 2: {n_wallets:,} wallets in {len(batches)} batch(es)")

        # ---- stage 3: approvals (batch x chunk) -----------------------------
        for bi, batch in enumerate(batches):
            wallets_sql = ", ".join(batch)
            for chunk_start, chunk_end in _chunks(window_end):
                sql = dune.render_named_sql(
                    "sushiswap_v2/approvals",
                    {"wallets": wallets_sql, "chunk_start": chunk_start, "chunk_end": chunk_end})
                key = f"sushiswap_v2/approvals/b{bi:02d}/{chunk_start}"
                _run_job_to_table(runner, Job(key=key, sql=sql), "approvals")
        print("[sushi] stage 3 done")

        # ---- stage 4: permit2 events (batch) ---------------------------------
        # wallets go in ONCE as a VALUES CTE — inlining the list into all five
        # UNION branches blew past Dune's SQL size cap and crashed the parser
        for bi, batch in enumerate(batches):
            sql = dune.render_named_sql(
                "sushiswap_v2/permit2_events",
                {"wallets_values": ", ".join(f"({w})" for w in batch),
                 "window_start": window_start, "window_end": window_end})
            key = f"sushiswap_v2/permit2/b{bi:02d}"
            _run_job_to_table(runner, Job(key=key, sql=sql), "permit2_events")
        print("[sushi] stage 4 done — fetch complete, data live in BigQuery")

    except CreditError:
        print(
            "[sushi] Dune datapoint limit hit — progress saved per job in manifest.\n"
            "        Swap in a fresh API key in .env and re-run; completed jobs are\n"
            "        skipped and the interrupted one resumes (check --status)."
        )
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SushiSwap V2 arm fetch (staged Dune -> BigQuery)")
    parser.add_argument("--window-start", help="override window start (ISO date), e.g. 2025-11-01")
    parser.add_argument("--window-end", help="override window end, exclusive, e.g. 2025-11-08")
    parser.add_argument("--force", action="store_true", help="reset all sushi jobs and reload")
    parser.add_argument("--status", action="store_true", help="print fetch progress and BQ row counts")
    args = parser.parse_args()

    if args.status:
        status()
    elif args.window_start and args.window_end:
        run(window_start=args.window_start, window_end=args.window_end, force=args.force)
    else:
        _, ws, we, _ = study_window()
        run(window_start=ws, window_end=we, force=args.force)
