"""Compound V3 arm fetch — stable-Comet supplies, withdraws, allow() grants,
plus the reused approvals/permit2 stages for the qualifying roster.

Population (funnel-gated, queries/compound_v3/funnel.sql, run 2026-07-09):
qualifying tx = emitted a base-asset Supply on cUSDCv3/cUSDTv3 AND was sent
to a Comet or the MainnetBulker; manager/vault flow (Kiln, ERC-4337,
proxies) is stored as contrast, separated downstream by tx_to.

    stage 0  funnel.sql re-run (manifest-cached — free) sanity print
    stage 1  supplies.sql   -> compound_v3.supplies   (ALL supply txs, broad)
             HARD GATE: per-month qualifying/contrast counts == funnel
    stage 2  withdraws.sql  -> compound_v3.withdraws  (borrow reconstruction
             happens downstream — Comet has no Borrow event)
    stage 3  allow_events.sql per yearly chunk since Comet genesis (2022-08)
             -> compound_v3.allow_events (raw logs; decoded table is stale)
             HARD GATE: in-window monthly counts == pre-measured log counts
    stage 4  roster = qualifying wallets from supplies (tx_to anchored)
    stage 5  sushiswap_v2/approvals.sql (REUSED verbatim) -> approvals
    stage 6  sushiswap_v2/permit2_events.sql (REUSED)     -> permit2_events

Resume/credit semantics identical to the other fetchers: per-job manifest
keys under compound_v3/, CreditError leaves everything resumable — swap the
.env key and re-run; completed jobs skip.

Run with:
    python -m dexresearch.fetch.compound_timeline
    python -m dexresearch.fetch.compound_timeline --status
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

_DATASET = "compound_v3"
_PREFIX = "compound_v3/"
_WALLET_BATCH = 8000
_ALLOW_LOOKBACK_START = "2022-08-01"   # cUSDCv3 genesis

# Verified 2026-07-09: addresses ground-truthed from the per-market decoded
# event tables themselves, names cross-checked in contracts.contract_mapping.
STABLE_COMETS = {
    "0xc3d688b66703497daa19211eedff47f25384cdc3": "cUSDCv3",
    "0x3afdc9bca9213a35503b077a6072f3d0d5ab0840": "cUSDTv3",
}
BULKER = "0xa397a8c2086c554b531c02e29f3291c9704b00c7"   # MainnetBulker
ENTRY_SET = set(STABLE_COMETS) | {BULKER}

# Funnel gates (queries/compound_v3/funnel.sql, executed 2026-07-09 and
# manifest-cached). Hard gates, not estimates.
_EXPECT_MONTH = {
    "2025-11": {"supply_txs_all": 3049, "qualifying_txs": 1973, "qualifying_wallets": 773, "contrast_txs": 1076},
    "2025-12": {"supply_txs_all": 1647, "qualifying_txs": 1079, "qualifying_wallets": 552, "contrast_txs": 568},
    "2026-01": {"supply_txs_all": 2179, "qualifying_txs": 1475, "qualifying_wallets": 639, "contrast_txs": 704},
    "2026-02": {"supply_txs_all": 2211, "qualifying_txs": 1736, "qualifying_wallets": 570, "contrast_txs": 475},
}
# In-window allow-event counts, all four Comets (raw-logs probe, 2026-07-09).
_EXPECT_ALLOW_MONTH = {"2025-11": 757, "2025-12": 546, "2026-01": 552, "2026-02": 538}

_S = bigquery.SchemaField
_ACTION_FIELDS = [
    _S("wallet", "STRING"), _S("block_time", "TIMESTAMP"), _S("block_number", "INT64"),
    _S("tx_hash", "STRING"), _S("evt_index", "INT64"), _S("market", "STRING"),
    _S("token", "STRING"), _S("token_symbol", "STRING"), _S("amount_raw", "STRING"),
]
_GAS_FIELDS = [
    _S("tx_to", "STRING"), _S("gas_used", "INT64"), _S("gas_price", "INT64"),
    _S("gas_cost_eth", "FLOAT64"), _S("max_priority_fee_per_gas", "INT64"),
    _S("method_id", "STRING"),
]
SCHEMAS: dict[str, list[bigquery.SchemaField]] = {
    "supplies": _ACTION_FIELDS + [_S("supply_from", "STRING"), _S("supply_dst", "STRING")] + _GAS_FIELDS,
    "withdraws": _ACTION_FIELDS + [_S("withdraw_src", "STRING"), _S("withdraw_to", "STRING")] + _GAS_FIELDS,
    "allow_events": [
        _S("market", "STRING"), _S("comet", "STRING"), _S("wallet", "STRING"),
        _S("manager", "STRING"), _S("block_time", "TIMESTAMP"), _S("block_number", "INT64"),
        _S("tx_hash", "STRING"), _S("evt_index", "INT64"),
        _S("is_grant", "BOOL"), _S("is_revoke", "BOOL"),
        _S("gas_used", "INT64"), _S("gas_price", "INT64"), _S("gas_cost_eth", "FLOAT64"),
        _S("max_priority_fee_per_gas", "INT64"), _S("method_id", "STRING"),
    ],
    "approvals": SUSHI_SCHEMAS["approvals"],
    "permit2_events": SUSHI_SCHEMAS["permit2_events"],
}
_HEX_COLS = {"wallet", "tx_hash", "token", "counterparty", "tx_to", "method_id",
             "supply_from", "supply_dst", "withdraw_src", "withdraw_to",
             "comet", "manager"}


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
        print(f"[comp] {job.key}: already complete, skipping")
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
            print(f"[comp]   {_DATASET}.{table} += {len(page_df):,} rows ({job.key} p{page_num})")
        runner._record(job, fetch_next_offset=nxt, fetch_pages_done=page_num)
    runner._record(job, fetch_complete=True)
    return total


def _allow_chunks(window_end: str) -> list[tuple[str, str]]:
    """Yearly slices from Comet genesis to window end (allow stage only —
    the approvals stage reuses the Sushi arm's 2022-11 chunker)."""
    bounds = []
    y, m, d = (int(p) for p in _ALLOW_LOOKBACK_START.split("-"))
    cur = date(y, m, d)
    end = date(*(int(p) for p in window_end.split("-")))
    while cur < end:
        bounds.append(cur.isoformat())
        cur = date(cur.year + 1, cur.month, cur.day)
    bounds.append(end.isoformat())
    return list(zip(bounds, bounds[1:]))


def _entry_in() -> str:
    return ", ".join(f"'{a}'" for a in sorted(ENTRY_SET))


def _validate_supplies() -> None:
    rows = _client().query(f"""
        SELECT FORMAT_TIMESTAMP('%Y-%m', block_time) AS month,
               COUNT(DISTINCT tx_hash) AS supply_txs_all,
               COUNT(DISTINCT IF(tx_to IN ({_entry_in()}), tx_hash, NULL)) AS qualifying_txs,
               COUNT(DISTINCT IF(tx_to IN ({_entry_in()}), wallet, NULL)) AS qualifying_wallets,
               COUNT(DISTINCT IF(tx_to NOT IN ({_entry_in()}) OR tx_to IS NULL, tx_hash, NULL)) AS contrast_txs,
               COUNT(*) - COUNT(DISTINCT CONCAT(market, ':', tx_hash, ':', CAST(evt_index AS STRING))) AS dups
        FROM `{_table_id('supplies')}` GROUP BY month
    """).result()
    bad = {}
    for r in rows:
        exp = _EXPECT_MONTH.get(r.month)
        if exp is None:
            bad[r.month] = "unexpected month"
            continue
        for k, v in exp.items():
            if getattr(r, k) != v:
                bad[f"{r.month}.{k}"] = (getattr(r, k), v)
        if r.dups:
            bad[f"{r.month}.dups"] = (r.dups, 0)
    if bad:
        raise SystemExit(f"[comp] supplies gate FAILED (got, expected): {bad}")
    print("[comp] supplies gates PASSED: all monthly qualifying/contrast counts match the funnel exactly")


def _validate_allow() -> None:
    rows = _client().query(f"""
        SELECT FORMAT_TIMESTAMP('%Y-%m', block_time) AS month, COUNT(*) AS n
        FROM `{_table_id('allow_events')}`
        WHERE block_time >= TIMESTAMP '2025-11-01' AND block_time < TIMESTAMP '2026-03-01'
        GROUP BY month
    """).result()
    got = {r.month: r.n for r in rows}
    bad = {m: (got.get(m, 0), v) for m, v in _EXPECT_ALLOW_MONTH.items() if got.get(m, 0) != v}
    mixed = next(iter(_client().query(f"""
        SELECT COUNT(*) AS n FROM `{_table_id('allow_events')}`
        WHERE is_grant = is_revoke
    """).result())).n
    if mixed:
        bad["non_binary_events"] = (mixed, 0)
    if bad:
        raise SystemExit(f"[comp] allow gate FAILED (got, expected): {bad}")
    print("[comp] allow gates PASSED: in-window monthly counts match the logs probe; every event is binary")


def _wallet_batches() -> list[list[str]]:
    rows = _client().query(f"""
        SELECT DISTINCT wallet FROM `{_table_id('supplies')}`
        WHERE tx_to IN ({_entry_in()}) AND wallet IS NOT NULL
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
    print(f"[status] {len(keys)} compound job(s) in manifest")
    for k in sorted(keys):
        e = runner._manifest[k]
        state = "COMPLETE" if e.get("fetch_complete") else \
            f"offset {int(e.get('fetch_next_offset') or 0):,} ({e.get('state', 'not submitted')})"
        print(f"[status]   {k:<50}: {state}")
    for table in SCHEMAS:
        try:
            n = next(iter(_client().query(
                f"SELECT COUNT(*) AS n FROM `{_table_id(table)}`").result())).n
            print(f"[status]   BQ {table:<16}: {n:,} rows")
        except Exception:
            print(f"[status]   BQ {table:<16}: table missing")


def run(*, window_start: str, window_end: str) -> None:
    print(f"[comp] window: {window_start} → {window_end}")
    _ensure_dataset()
    runner = DuneRunner()

    try:
        # ---- stage 1: supplies (broad: entry + contrast) ---------------------
        sql = dune.render_named_sql(
            "compound_v3/supplies",
            {"window_start": window_start, "window_end": window_end})
        n = _run_job(runner, Job(key=_PREFIX + "supplies", sql=sql), "supplies")
        print(f"[comp] stage 1 done ({n:,} rows this run)")
        _validate_supplies()

        # ---- stage 2: withdraws ----------------------------------------------
        sql = dune.render_named_sql(
            "compound_v3/withdraws",
            {"window_start": window_start, "window_end": window_end})
        n = _run_job(runner, Job(key=_PREFIX + "withdraws", sql=sql), "withdraws")
        print(f"[comp] stage 2 done ({n:,} rows this run)")

        # ---- stage 3: allow events since Comet genesis -----------------------
        for chunk_start, chunk_end in _allow_chunks(window_end):
            sql = dune.render_named_sql(
                "compound_v3/allow_events",
                {"chunk_start": chunk_start, "chunk_end": chunk_end})
            _run_job(runner, Job(key=f"{_PREFIX}allow/{chunk_start}", sql=sql), "allow_events")
        print("[comp] stage 3 done")
        _validate_allow()

        # ---- stage 4/5: approvals for the qualifying roster ------------------
        batches = _wallet_batches()
        n_wallets = sum(len(b) for b in batches)
        print(f"[comp] roster: {n_wallets:,} qualifying wallets in {len(batches)} batch(es)")
        for bi, batch in enumerate(batches):
            wallets_sql = ", ".join(batch)
            for chunk_start, chunk_end in _chunks(window_end):
                sql = dune.render_named_sql(
                    "sushiswap_v2/approvals",
                    {"wallets": wallets_sql, "chunk_start": chunk_start, "chunk_end": chunk_end})
                key = f"{_PREFIX}approvals/b{bi:02d}/{chunk_start}"
                _run_job(runner, Job(key=key, sql=sql), "approvals")
        print("[comp] stage 5 done")

        # ---- stage 6: permit2 events for the roster --------------------------
        for bi, batch in enumerate(batches):
            sql = dune.render_named_sql(
                "sushiswap_v2/permit2_events",
                {"wallets_values": ", ".join(f"({w})" for w in batch),
                 "window_start": window_start, "window_end": window_end})
            _run_job(runner, Job(key=f"{_PREFIX}permit2/b{bi:02d}", sql=sql), "permit2_events")
        print("[comp] stage 6 done")

        print(f"[comp] COMPLETE — tables in dex-research.{_DATASET}; "
              "qualifying population = supplies WHERE tx_to IN entry set")

    except CreditError:
        print(
            "[comp] Dune datapoint limit hit — progress saved per job in manifest.\n"
            "       Swap in a fresh API key in .env and re-run; completed jobs are\n"
            "       skipped and the interrupted one resumes (check --status)."
        )
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compound V3 arm fetch")
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
