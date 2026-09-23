"""Harmonized Uniswap arm — OPTION 2: full pool-touch universe, leg level.

Fetches every qualifying pool-touch transaction that did NOT enter via a
Uniswap router (router-entry txs are already stored wide in `swaps_sampled`
by fetch.uniswap_topup — phase 1). Together the two are the complete
Sushi-style population: candidate = tx with a Uniswap leg selling a study
stablecoin; qualifying = first executed leg (across ALL legs) sold
USDC/USDT/DAI, any output.

Two normalized tables per month (Dune bills rows x columns; tx-constant
fields are bought once per tx, not once per leg — ~34% cheaper at 11M legs):

    uniswap_v4.txs_broad   one row per qualifying non-router tx (gas, tx_to…)
    uniswap_v4.legs_broad  one row per leg (project, version, tokens, usd)

Per-month protocol, self-gating:
    1. broad_funnel.sql  -> expected {broad_txs, broad_legs, broad_wallets}
                            recorded in the manifest BEFORE fetching
    2. txs_broad.sql     -> txs_broad
    3. legs_broad.sql    -> legs_broad
    4. BigQuery counts for the month must EQUAL the recorded expectations

After all months: views `swaps_broad` (wide reassembly, symbols joined from
already-owned rows) and `swaps_option2` (router-entry ∪ broad — the complete
population in the Sushi wide schema).

Credit exhaustion is expected many times over (~60k credits total): swap the
.env DUNE_API_KEY and re-run; completed jobs skip, the interrupted one
resumes at its page offset.

Run with:
    python -m dexresearch.fetch.uniswap_broad
    python -m dexresearch.fetch.uniswap_broad --status
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
from dexresearch.dune_runner import CreditError, DuneRunner, Job
from dexresearch.fetch._common import study_window
from dexresearch.fetch.uniswap_sample import _run_job  # generic submit/wait/page loop
from dexresearch.fetch.uniswap_sample import SCHEMAS as _SAMPLE_SCHEMAS
from dexresearch.config import get_settings
from dexresearch.fetch.uniswap_wallets import _client, _months

_DATASET = "uniswap_v4"
_PREFIX = "uniswap_v4/broad_"

_S = bigquery.SchemaField
BROAD_SCHEMAS: dict[str, list[bigquery.SchemaField]] = {
    "txs_broad": [
        _S("tx_hash", "STRING"), _S("wallet", "STRING"),
        _S("block_time", "TIMESTAMP"), _S("block_number", "INT64"),
        _S("tx_to", "STRING"), _S("n_legs", "INT64"),
        _S("gas_used", "INT64"), _S("gas_price", "INT64"), _S("gas_cost_eth", "FLOAT64"),
        _S("max_priority_fee_per_gas", "INT64"), _S("method_id", "STRING"),
    ],
    "legs_broad": [
        _S("tx_hash", "STRING"), _S("evt_index", "INT64"),
        _S("project", "STRING"), _S("version", "STRING"),
        _S("token", "STRING"), _S("counterparty", "STRING"), _S("amount_usd", "FLOAT64"),
    ],
}
# register with the shared coerce/append machinery in uniswap_sample
_SAMPLE_SCHEMAS.update(BROAD_SCHEMAS)


def _table_id(name: str) -> str:
    return f"{get_settings().GCP_PROJECT_ID}.{_DATASET}.{name}"


def _funnel_gate(runner: DuneRunner, month: str, month_end: str) -> dict[str, int]:
    """Run (or reuse) the one-row funnel for a month; return expected counts."""
    key = f"{_PREFIX}funnel/{month}"
    entry = runner._manifest.get(key, {})
    if "broad_txs" in entry:
        return {k: entry[k] for k in ("broad_txs", "broad_legs", "broad_wallets")}
    sql = dune.render_named_sql("uniswap_v4/broad_funnel",
                                {"month": month, "month_end": month_end})
    job = Job(key=key, sql=sql)
    execution_id = runner.submit(job)
    runner.wait(job, execution_id)
    _, _, df, _ = next(runner.fetch_pages(execution_id))
    row = df.iloc[0]
    expect = {k: int(row[k]) for k in ("broad_txs", "broad_legs", "broad_wallets")}
    runner._record(job, fetch_complete=True, **expect,
                   router_txs=int(row["router_txs"]), router_legs=int(row["router_legs"]))
    print(f"[broad] {month} funnel: {expect['broad_txs']:,} txs / {expect['broad_legs']:,} legs "
          f"/ {expect['broad_wallets']:,} wallets (router: {int(row['router_txs']):,} txs)")
    return expect


def _validate_month(month: str, month_end: str, expect: dict[str, int]) -> None:
    row = next(iter(_client().query(f"""
        WITH t AS (
          SELECT tx_hash, wallet FROM `{_table_id('txs_broad')}`
          WHERE block_time >= TIMESTAMP('{month}') AND block_time < TIMESTAMP('{month_end}')
        )
        SELECT
          (SELECT COUNT(*) FROM t)                    AS txs,
          (SELECT COUNT(DISTINCT wallet) FROM t)      AS wallets,
          (SELECT COUNT(*) FROM `{_table_id('legs_broad')}` l
             JOIN t USING (tx_hash))                  AS legs
        """).result()))
    got = {"broad_txs": row.txs, "broad_legs": row.legs, "broad_wallets": row.wallets}
    bad = {k: (got[k], expect[k]) for k in expect if got[k] != expect[k]}
    if bad:
        raise SystemExit(f"[broad] {month} validation FAILED (got, expected): {bad}")
    print(f"[broad] {month} gates PASSED: {got['broad_txs']:,} txs / {got['broad_legs']:,} legs")


def _create_views() -> None:
    ds = _table_id("x").rsplit(".", 1)[0]
    client = _client()
    # symbols come from rows already owned — never bought per broad leg
    client.query(f"""
        CREATE OR REPLACE VIEW `{ds}.token_symbols` AS
        SELECT token, ANY_VALUE(token_symbol) AS symbol
        FROM (
          SELECT token, token_symbol FROM `{ds}.swaps_sampled` WHERE token_symbol IS NOT NULL
          UNION ALL
          SELECT counterparty, counter_symbol FROM `{ds}.swaps_sampled` WHERE counter_symbol IS NOT NULL
        ) GROUP BY token""").result()
    client.query(f"""
        CREATE OR REPLACE VIEW `{ds}.swaps_broad` AS
        SELECT
          t.wallet, t.block_time, t.block_number, t.tx_hash,
          l.evt_index, l.project, l.version,
          l.token, ts.symbol AS token_symbol,
          l.counterparty, cs.symbol AS counter_symbol,
          l.amount_usd, t.tx_to,
          t.gas_used, t.gas_price, t.gas_cost_eth,
          t.max_priority_fee_per_gas, t.method_id
        FROM `{ds}.txs_broad` t
        JOIN `{ds}.legs_broad` l USING (tx_hash)
        LEFT JOIN `{ds}.token_symbols` ts ON ts.token = l.token
        LEFT JOIN `{ds}.token_symbols` cs ON cs.token = l.counterparty""").result()
    cols = ("wallet, block_time, block_number, tx_hash, evt_index, project, version, "
            "token, token_symbol, counterparty, counter_symbol, amount_usd, tx_to, "
            "gas_used, gas_price, gas_cost_eth, max_priority_fee_per_gas, method_id")
    client.query(f"""
        CREATE OR REPLACE VIEW `{ds}.swaps_option2` AS
        SELECT {cols} FROM `{ds}.swaps_sampled`
        UNION ALL
        SELECT {cols} FROM `{ds}.swaps_broad`""").result()
    print("[broad] views created: swaps_broad (wide reassembly), swaps_option2 (complete population)")


def status() -> None:
    runner = DuneRunner()
    for k in sorted(k for k in runner._manifest if k.startswith(_PREFIX)):
        e = runner._manifest[k]
        state = "COMPLETE" if e.get("fetch_complete") else \
            f"offset {int(e.get('fetch_next_offset') or 0):,} ({e.get('state', 'not submitted')})"
        print(f"[status]   {k:<40}: {state}")
    for table in BROAD_SCHEMAS:
        try:
            n = next(iter(_client().query(
                f"SELECT COUNT(*) AS n FROM `{_table_id(table)}`").result())).n
            print(f"[status]   BQ {table:<12}: {n:,} rows")
        except Exception:
            print(f"[status]   BQ {table:<12}: table missing")


def run(*, window_start: str, window_end: str) -> None:
    print(f"[broad] option-2 window: {window_start} → {window_end}")
    runner = DuneRunner()
    if not runner._manifest.get("uniswap_v4/full_swaps/b08", {}).get("fetch_complete"):
        raise SystemExit("[broad] phase 1 (uniswap_topup) swaps are not complete — "
                         "the router/broad split depends on it; finish phase 1 first.")
    try:
        for month, month_end in _months(window_start, window_end):
            expect = _funnel_gate(runner, month, month_end)
            for name in ("txs_broad", "legs_broad"):
                sql = dune.render_named_sql(f"uniswap_v4/{name}",
                                            {"month": month, "month_end": month_end})
                _run_job(runner, Job(key=f"{_PREFIX}{name}/{month}", sql=sql), name)
            _validate_month(month, month_end, expect)
        _create_views()
        print("[broad] OPTION 2 COMPLETE — swaps_option2 is the full pool-touch population")
    except CreditError:
        print(
            "[broad] Dune datapoint limit hit — progress saved per job in manifest.\n"
            "        Swap in a fresh API key in .env and re-run; completed jobs are\n"
            "        skipped and the interrupted one resumes (check --status)."
        )
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Uniswap arm option 2: full pool-touch broad fetch")
    parser.add_argument("--window-start")
    parser.add_argument("--window-end")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    if args.status:
        status()
    elif args.window_start and args.window_end:
        run(window_start=args.window_start, window_end=args.window_end)
    else:
        _, ws, we, _ = study_window()
        run(window_start=ws, window_end=we)
