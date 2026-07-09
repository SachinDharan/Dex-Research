"""Harmonized Uniswap arm — TOP-UP to FULL coverage (user decision 2026-07-09).

Extends the stage-2 sample to the entire 73,039-wallet population, Sushi-style
store-everything, without re-buying anything: the fetch list is population
wallets MINUS already-fetched sample wallets (anti-join, ~65,267 wallets).
Appends into the same detail tables:

    stage A  `uniswap_v4.topup_wallets` (BigQuery CTAS, free) = wallet_aggregates
             wallets not in sample_wallets, with the same band labels
    stage B  queries/uniswap_v4/swaps_sampled.sql per batch -> swaps_sampled
    stage C  queries/sushiswap_v2/approvals.sql             -> approvals
    stage D  queries/sushiswap_v2/permit2_events.sql        -> permit2_events
    final    gates: totals equal the Dune funnel (478,299 txs / 735,223 legs)
             and EVERY wallet's tx+leg counts equal wallet_aggregates; then
             topup wallets are inserted into sample_wallets so that table
             becomes the full fetched roster.

Job keys live under uniswap_v4/full_* so the completed sample_* jobs are never
disturbed. Credit exhaustion mid-run is expected (needs several keys): swap
.env DUNE_API_KEY and re-run; completed jobs skip, the interrupted one resumes.

Run with:
    python -m dexresearch.fetch.uniswap_topup
    python -m dexresearch.fetch.uniswap_topup --status
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dexresearch import dune
from dexresearch.dune_runner import CreditError, DuneRunner, Job
from dexresearch.fetch._common import study_window
from dexresearch.fetch.sushi_timeline import _ADDR_RE, _chunks
from dexresearch.fetch.uniswap_sample import (
    _WALLET_BATCH,
    _client,
    _run_job,
    _table_id,
)
from dexresearch.fetch.uniswap_wallets import _EXPECT as _FUNNEL

_PREFIX = "uniswap_v4/full_"


def build_topup() -> None:
    _client().query(f"""
        CREATE OR REPLACE TABLE `{_table_id('topup_wallets')}` AS
        WITH w AS (
          SELECT wallet, SUM(qualifying_txs) AS txs, SUM(sum_legs) AS legs
          FROM `{_table_id('wallet_aggregates')}` GROUP BY wallet
        )
        SELECT wallet,
               CASE WHEN txs = 1 THEN 'a_1' WHEN txs <= 5 THEN 'b_2-5'
                    WHEN txs <= 20 THEN 'c_6-20' WHEN txs <= 100 THEN 'd_21-100'
                    ELSE 'e_100+' END AS band,
               txs, legs
        FROM w
        WHERE wallet NOT IN (SELECT wallet FROM `{_table_id('sample_wallets')}`)
        """).result()
    row = next(iter(_client().query(f"""
        SELECT COUNT(*) AS wallets, SUM(txs) AS txs, SUM(legs) AS legs
        FROM `{_table_id('topup_wallets')}`""").result()))
    print(f"[topup] frame: {row.wallets:,} wallets / {row.txs:,} txs / {row.legs:,} legs "
          f"(~{row.legs * 18 / 2100:,.0f} credits for stage B)")


def _batches() -> list[list[str]]:
    rows = _client().query(
        f"SELECT wallet FROM `{_table_id('topup_wallets')}` ORDER BY wallet").result()
    wallets = [r.wallet for r in rows]
    bad = [w for w in wallets if not _ADDR_RE.match(w)]
    if bad:
        raise ValueError(f"non-address wallets in topup frame, e.g. {bad[:3]}")
    return [wallets[i:i + _WALLET_BATCH] for i in range(0, len(wallets), _WALLET_BATCH)]


def _validate_full() -> None:
    """The self-proving gate: the union of sample + topup must reproduce the
    independently-measured funnel and match wallet_aggregates per wallet."""
    row = next(iter(_client().query(f"""
        WITH agg AS (
          SELECT wallet, SUM(qualifying_txs) AS txs, SUM(sum_legs) AS legs
          FROM `{_table_id('wallet_aggregates')}` GROUP BY wallet
        ),
        got AS (
          SELECT wallet, COUNT(DISTINCT tx_hash) AS txs, COUNT(*) AS legs
          FROM `{_table_id('swaps_sampled')}` GROUP BY wallet
        )
        SELECT
          (SELECT SUM(txs) FROM got)   AS got_txs,
          (SELECT SUM(legs) FROM got)  AS got_legs,
          (SELECT COUNT(*) FROM got)   AS got_wallets,
          (SELECT COUNT(*) FROM agg a LEFT JOIN got g USING (wallet)
             WHERE g.txs IS NULL OR g.txs != a.txs OR g.legs != a.legs) AS mismatched
        """).result()))
    ok = (row.got_txs == _FUNNEL["qualifying_txs"]
          and row.got_legs == _FUNNEL["sum_legs"]
          and row.got_wallets == _FUNNEL["wallets"]
          and row.mismatched == 0)
    if not ok:
        raise SystemExit(
            f"[topup] FULL-COVERAGE GATE FAILED: txs {row.got_txs:,}/{_FUNNEL['qualifying_txs']:,}, "
            f"legs {row.got_legs:,}/{_FUNNEL['sum_legs']:,}, wallets {row.got_wallets:,}/"
            f"{_FUNNEL['wallets']:,}, mismatched wallets {row.mismatched:,}")
    print(f"[topup] FULL-COVERAGE GATES PASSED: {row.got_txs:,} txs / {row.got_legs:,} legs / "
          f"{row.got_wallets:,} wallets — every wallet matches its aggregate exactly")


def status() -> None:
    runner = DuneRunner()
    for k in sorted(k for k in runner._manifest if k.startswith(_PREFIX)):
        e = runner._manifest[k]
        state = "COMPLETE" if e.get("fetch_complete") else \
            f"offset {int(e.get('fetch_next_offset') or 0):,} ({e.get('state', 'not submitted')})"
        print(f"[status]   {k:<45}: {state}")
    for table in ("topup_wallets", "swaps_sampled", "approvals", "permit2_events"):
        try:
            n = next(iter(_client().query(
                f"SELECT COUNT(*) AS n FROM `{_table_id(table)}`").result())).n
            print(f"[status]   BQ {table:<16}: {n:,} rows")
        except Exception:
            print(f"[status]   BQ {table:<16}: table missing")


def run(*, window_start: str, window_end: str) -> None:
    print(f"[topup] window: {window_start} → {window_end} — extending sample to FULL population")
    runner = DuneRunner()

    try:
        # ---- stage A: anti-join frame (free, idempotent) ---------------------
        build_topup()
        batches = _batches()
        print(f"[topup] {sum(len(b) for b in batches):,} wallets in {len(batches)} batch(es)")

        # ---- stage B: leg-level swaps for remaining wallets ------------------
        for bi, batch in enumerate(batches):
            sql = dune.render_named_sql(
                "uniswap_v4/swaps_sampled",
                {"wallets": ", ".join(batch),
                 "window_start": window_start, "window_end": window_end})
            _run_job(runner, Job(key=f"{_PREFIX}swaps/b{bi:02d}", sql=sql), "swaps_sampled")
        _validate_full()

        # ---- stage C: approvals ----------------------------------------------
        for bi, batch in enumerate(batches):
            wallets_sql = ", ".join(batch)
            for chunk_start, chunk_end in _chunks(window_end):
                sql = dune.render_named_sql(
                    "sushiswap_v2/approvals",
                    {"wallets": wallets_sql, "chunk_start": chunk_start, "chunk_end": chunk_end})
                _run_job(runner, Job(key=f"{_PREFIX}approvals/b{bi:02d}/{chunk_start}", sql=sql),
                         "approvals")
        print("[topup] stage C done")

        # ---- stage D: permit2 events -----------------------------------------
        for bi, batch in enumerate(batches):
            sql = dune.render_named_sql(
                "sushiswap_v2/permit2_events",
                {"wallets_values": ", ".join(f"({w})" for w in batch),
                 "window_start": window_start, "window_end": window_end})
            _run_job(runner, Job(key=f"{_PREFIX}permit2/b{bi:02d}", sql=sql), "permit2_events")

        # ---- roster + done ----------------------------------------------------
        _client().query(f"""
            INSERT INTO `{_table_id('sample_wallets')}` (wallet, band, txs, legs)
            SELECT wallet, band, txs, legs FROM `{_table_id('topup_wallets')}`
            WHERE wallet NOT IN (SELECT wallet FROM `{_table_id('sample_wallets')}`)
            """).result()
        print("[topup] COMPLETE — uniswap_v4 detail tables now cover the FULL population; "
              "sample_wallets is the full fetched roster")

    except CreditError:
        print(
            "[topup] Dune datapoint limit hit — progress saved per job in manifest.\n"
            "        Swap in a fresh API key in .env and re-run; completed jobs are\n"
            "        skipped and the interrupted one resumes (check --status)."
        )
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Uniswap arm: top-up sample to full population")
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
