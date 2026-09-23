"""Aave V3 arm fetch — stable Pool supplies, borrows, credit delegation,
plus the reused approvals/permit2 stages for the qualifying roster.

Population (funnel-gated, queries/aave_v3/funnel.sql, run 2026-07-09):
qualifying tx = emitted a Supply of USDC/USDT/DAI on the V3 Pool AND was
sent directly to the Pool; aggregator/manager flow (ParaSwap, Kyber, Kiln
vaults, Aave Umbrella, CoW solvers) is stored as contrast, separated
downstream by tx_to. Borrows are the SECONDARY action (no ERC-20 approval),
fetched broad with the same split.

    stage 1  supplies.sql          -> aave_v3.supplies   (broad, window)
             HARD GATE: per-month qualifying/contrast counts == funnel
    stage 2  borrows.sql           -> aave_v3.borrows    (broad, window)
             HARD GATE: same
    stage 3  delegation_events.sql per yearly chunk since V3 genesis
             (2023-01) -> aave_v3.delegation_events (variable debt, stables)
    stage 4  roster = qualifying supplier wallets
    stage 5  sushiswap_v2/approvals.sql (REUSED verbatim) -> approvals
    stage 6  sushiswap_v2/permit2_events.sql (REUSED)     -> permit2_events

~22k roster wallets: expect one or two key swaps (CreditError leaves
everything resumable — swap the .env key and re-run; completed jobs skip).

Run with:
    python -m dexresearch.fetch.aave_timeline
    python -m dexresearch.fetch.aave_timeline --status
"""
from __future__ import annotations

import argparse
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
from dexresearch.fetch.sushi_timeline import SCHEMAS as SUSHI_SCHEMAS
from dexresearch.fetch.sushi_timeline import _ADDR_RE, _chunks

_DATASET = "aave_v3"
_PREFIX = "aave_v3/"
_WALLET_BATCH = 8000
_DELEGATION_LOOKBACK_START = "2023-01-01"   # Aave V3 Ethereum genesis

POOL = "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2"   # verified via contract_mapping + entry profile

# Funnel gates (queries/aave_v3/funnel.sql, executed 2026-07-09,
# manifest-cached). Hard gates, not estimates.
_EXPECT_MONTH = {
    "supplies": {
        "2025-11": {"txs_all": 30752, "qualifying_txs": 18809, "qualifying_wallets": 9365, "contrast_txs": 11943},
        "2025-12": {"txs_all": 28462, "qualifying_txs": 12485, "qualifying_wallets": 6662, "contrast_txs": 15977},
        "2026-01": {"txs_all": 33775, "qualifying_txs": 12915, "qualifying_wallets": 6805, "contrast_txs": 20860},
        "2026-02": {"txs_all": 32084, "qualifying_txs": 11697, "qualifying_wallets": 5577, "contrast_txs": 20387},
    },
    "borrows": {
        "2025-11": {"txs_all": 24120, "qualifying_txs": 17998, "qualifying_wallets": 6292, "contrast_txs": 6122},
        "2025-12": {"txs_all": 18794, "qualifying_txs": 13631, "qualifying_wallets": 4898, "contrast_txs": 5163},
        "2026-01": {"txs_all": 21124, "qualifying_txs": 15051, "qualifying_wallets": 5324, "contrast_txs": 6073},
        "2026-02": {"txs_all": 29069, "qualifying_txs": 16489, "qualifying_wallets": 5669, "contrast_txs": 12580},
    },
}

_S = bigquery.SchemaField
_ACTION_FIELDS = [
    _S("wallet", "STRING"), _S("block_time", "TIMESTAMP"), _S("block_number", "INT64"),
    _S("tx_hash", "STRING"), _S("evt_index", "INT64"),
    _S("token", "STRING"), _S("token_symbol", "STRING"), _S("amount_raw", "STRING"),
    _S("evt_user", "STRING"), _S("on_behalf_of", "STRING"),
]
_GAS_FIELDS = [
    _S("tx_to", "STRING"), _S("gas_used", "INT64"), _S("gas_price", "INT64"),
    _S("gas_cost_eth", "FLOAT64"), _S("max_priority_fee_per_gas", "INT64"),
    _S("method_id", "STRING"),
]
SCHEMAS: dict[str, list[bigquery.SchemaField]] = {
    "supplies": _ACTION_FIELDS + _GAS_FIELDS,
    "borrows": _ACTION_FIELDS + [_S("interest_rate_mode", "INT64")] + _GAS_FIELDS,
    "delegation_events": [
        _S("wallet", "STRING"), _S("delegatee", "STRING"),
        _S("token", "STRING"), _S("token_symbol", "STRING"),
        _S("block_time", "TIMESTAMP"), _S("block_number", "INT64"),
        _S("tx_hash", "STRING"), _S("evt_index", "INT64"),
        _S("amount_raw", "STRING"), _S("is_revoke", "BOOL"),
        _S("gas_used", "INT64"), _S("gas_price", "INT64"), _S("gas_cost_eth", "FLOAT64"),
        _S("max_priority_fee_per_gas", "INT64"), _S("method_id", "STRING"),
    ],
    "approvals": SUSHI_SCHEMAS["approvals"],
    "permit2_events": SUSHI_SCHEMAS["permit2_events"],
}
_HEX_COLS = {"wallet", "tx_hash", "token", "counterparty", "tx_to", "method_id",
             "evt_user", "on_behalf_of", "delegatee"}


def _client() -> bigquery.Client:
    return bigquery.Client(project=get_settings().GCP_PROJECT_ID)


def _table_id(name: str) -> str:
    return f"{get_settings().GCP_PROJECT_ID}.{_DATASET}.{name}"


def _ensure_dataset() -> None:
    _client().create_dataset(f"{get_settings().GCP_PROJECT_ID}.{_DATASET}", exists_ok=True)


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
        print(f"[aave] {job.key}: already complete, skipping")
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
    start_offset = int(entry.get("fetch_next_offset") or 0)
    start_page = int(entry.get("fetch_pages_done", 0))

    total = 0
    for page_num, _offset, page_df, nxt in runner.fetch_pages(
        execution_id, start_offset=start_offset, start_page=start_page
    ):
        if len(page_df) > 0:
            _append(_coerce(page_df, SCHEMAS[table]), table)
            total += len(page_df)
            print(f"[aave]   {_DATASET}.{table} += {len(page_df):,} rows ({job.key} p{page_num})")
        runner._record(job, fetch_next_offset=nxt, fetch_pages_done=page_num)
    runner._record(job, fetch_complete=True)
    return total


def _delegation_chunks(window_end: str) -> list[tuple[str, str]]:
    bounds = []
    y, m, d = (int(p) for p in _DELEGATION_LOOKBACK_START.split("-"))
    cur = date(y, m, d)
    end = date(*(int(p) for p in window_end.split("-")))
    while cur < end:
        bounds.append(cur.isoformat())
        cur = date(cur.year + 1, cur.month, cur.day)
    bounds.append(end.isoformat())
    return list(zip(bounds, bounds[1:]))


def _validate_actions(table: str) -> None:
    rows = _client().query(f"""
        SELECT FORMAT_TIMESTAMP('%Y-%m', block_time) AS month,
               COUNT(DISTINCT tx_hash) AS txs_all,
               COUNT(DISTINCT IF(tx_to = '{POOL}', tx_hash, NULL)) AS qualifying_txs,
               COUNT(DISTINCT IF(tx_to = '{POOL}', wallet, NULL)) AS qualifying_wallets,
               COUNT(DISTINCT IF(tx_to != '{POOL}' OR tx_to IS NULL, tx_hash, NULL)) AS contrast_txs,
               COUNT(*) - COUNT(DISTINCT CONCAT(tx_hash, ':', CAST(evt_index AS STRING))) AS dups
        FROM `{_table_id(table)}` GROUP BY month
    """).result()
    bad = {}
    for r in rows:
        exp = _EXPECT_MONTH[table].get(r.month)
        if exp is None:
            bad[r.month] = "unexpected month"
            continue
        for k, v in exp.items():
            if getattr(r, k) != v:
                bad[f"{r.month}.{k}"] = (getattr(r, k), v)
        if r.dups:
            bad[f"{r.month}.dups"] = (r.dups, 0)
    if bad:
        raise SystemExit(f"[aave] {table} gate FAILED (got, expected): {bad}")
    print(f"[aave] {table} gates PASSED: all monthly qualifying/contrast counts match the funnel exactly")


def _wallet_batches() -> list[list[str]]:
    rows = _client().query(f"""
        SELECT DISTINCT wallet FROM `{_table_id('supplies')}`
        WHERE tx_to = '{POOL}' AND wallet IS NOT NULL
        ORDER BY wallet
    """).result()
    wallets = [r.wallet for r in rows]
    bad = [w for w in wallets if not _ADDR_RE.match(w)]
    if bad:
        raise ValueError(f"non-address wallet values in supplies table, e.g. {bad[:3]}")
    return [wallets[i:i + _WALLET_BATCH] for i in range(0, len(wallets), _WALLET_BATCH)]


def status() -> None:
    runner = DuneRunner()
    keys = [k for k in runner._manifest if k.startswith(_PREFIX)]
    print(f"[status] {len(keys)} aave job(s) in manifest")
    for k in sorted(keys):
        e = runner._manifest[k]
        state = "COMPLETE" if e.get("fetch_complete") else \
            f"offset {int(e.get('fetch_next_offset') or 0):,} ({e.get('state', 'not submitted')})"
        print(f"[status]   {k:<50}: {state}")
    for table in SCHEMAS:
        try:
            n = next(iter(_client().query(
                f"SELECT COUNT(*) AS n FROM `{_table_id(table)}`").result())).n
            print(f"[status]   BQ {table:<18}: {n:,} rows")
        except Exception:
            print(f"[status]   BQ {table:<18}: table missing")


def run(*, window_start: str, window_end: str) -> None:
    print(f"[aave] window: {window_start} → {window_end}")
    _ensure_dataset()
    runner = DuneRunner()

    try:
        # ---- stage 1: supplies (broad) ---------------------------------------
        sql = dune.render_named_sql(
            "aave_v3/supplies",
            {"window_start": window_start, "window_end": window_end})
        n = _run_job(runner, Job(key=_PREFIX + "supplies", sql=sql), "supplies")
        print(f"[aave] stage 1 done ({n:,} rows this run)")
        _validate_actions("supplies")

        # ---- stage 2: borrows (broad) ----------------------------------------
        sql = dune.render_named_sql(
            "aave_v3/borrows",
            {"window_start": window_start, "window_end": window_end})
        n = _run_job(runner, Job(key=_PREFIX + "borrows", sql=sql), "borrows")
        print(f"[aave] stage 2 done ({n:,} rows this run)")
        _validate_actions("borrows")

        # ---- stage 3: credit delegation since V3 genesis ---------------------
        for chunk_start, chunk_end in _delegation_chunks(window_end):
            sql = dune.render_named_sql(
                "aave_v3/delegation_events",
                {"chunk_start": chunk_start, "chunk_end": chunk_end})
            _run_job(runner, Job(key=f"{_PREFIX}delegation/{chunk_start}", sql=sql),
                     "delegation_events")
        print("[aave] stage 3 done")

        # ---- stage 4/5: approvals for the qualifying roster ------------------
        batches = _wallet_batches()
        n_wallets = sum(len(b) for b in batches)
        print(f"[aave] roster: {n_wallets:,} qualifying wallets in {len(batches)} batch(es)")
        for bi, batch in enumerate(batches):
            wallets_sql = ", ".join(batch)
            for chunk_start, chunk_end in _chunks(window_end):
                sql = dune.render_named_sql(
                    "sushiswap_v2/approvals",
                    {"wallets": wallets_sql, "chunk_start": chunk_start, "chunk_end": chunk_end})
                key = f"{_PREFIX}approvals/b{bi:02d}/{chunk_start}"
                _run_job(runner, Job(key=key, sql=sql), "approvals")
        print("[aave] stage 5 done")

        # ---- stage 6: permit2 events for the roster --------------------------
        for bi, batch in enumerate(batches):
            sql = dune.render_named_sql(
                "sushiswap_v2/permit2_events",
                {"wallets_values": ", ".join(f"({w})" for w in batch),
                 "window_start": window_start, "window_end": window_end})
            _run_job(runner, Job(key=f"{_PREFIX}permit2/b{bi:02d}", sql=sql), "permit2_events")
        print("[aave] stage 6 done")

        print(f"[aave] COMPLETE — tables in dex-research.{_DATASET}; "
              "qualifying population = supplies WHERE tx_to = Pool")

    except CreditError:
        print(
            "[aave] Dune datapoint limit hit — progress saved per job in manifest.\n"
            "       Swap in a fresh API key in .env and re-run; completed jobs are\n"
            "       skipped and the interrupted one resumes (check --status)."
        )
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Aave V3 arm fetch")
    parser.add_argument("--window-start", help="override window start (ISO date)")
    parser.add_argument("--window-end", help="override window end, exclusive")
    parser.add_argument("--status", action="store_true", help="print fetch progress and BQ row counts")
    args = parser.parse_args()

    if args.status:
        status()
    elif args.window_start and args.window_end:
        run(window_start=args.window_start, window_end=args.window_end)
    else:
        _, ws, we, _ = study_window()
        run(window_start=ws, window_end=we)
