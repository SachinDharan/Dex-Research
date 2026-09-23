"""SushiSwap V2 arm DELTA fetch — the router-entry txs the pool-anchored fetch missed.

Additive companion to fetch.sushi_timeline: the loaded dataset holds only the
1,649 router-entry txs that ALSO touched a Sushi pool (5.2% of the study
population). This module fetches the remainder without touching the validated
base tables — no drops, no appends to `swaps`/`approvals`/`permit2_events`:

    stage 1  queries/sushiswap_v2/swaps_txto_delta.sql -> sushiswap_v2.swaps_txto_delta
             (tx_to-anchored, anti-joined against the old pool-anchor predicate)
    gate     hard validation against pre-measured Dune truth (query 7922955):
             30,300 txs / 41,723 legs / 5,022 wallets, zero tx overlap with base
    stage 2  NEW wallet set = delta wallets minus base-swaps wallets (their
             approvals history is already complete — lookback to Permit2 genesis)
    stage 3  approvals.sql per (batch x yearly chunk)   -> approvals_delta
    stage 4  permit2_events.sql per batch               -> permit2_events_delta
    views    swaps_router_entry (study population, swap_count recomputed),
             approvals_all, permit2_events_all

Resume semantics are identical to sushi_timeline: per-job manifest keys under
sushiswap_v2/delta/, paged appends, CreditError leaves everything resumable —
swap the API key in .env and re-run.

Run with:
    python -m dexresearch.fetch.sushi_delta                # study window
    python -m dexresearch.fetch.sushi_delta --status
    python -m dexresearch.fetch.sushi_delta --force-delta  # reset DELTA tables only
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
from dexresearch.fetch.sushi_timeline import (
    SCHEMAS,
    _ADDR_RE,
    _META_KEY,
    _WALLET_BATCH,
    _chunks,
    _client,
    _meta_hash,
    _run_job_to_table,
    _table_id,
)
from dexresearch.process.sushi_analysis import SUSHI_ROUTERS

_DELTA_TABLES = ("swaps_txto_delta", "approvals_delta", "permit2_events_delta")
_DELTA_PREFIX = "sushiswap_v2/delta/"

# Pre-measured on Dune (query 7922955, 2026-07-09) against the SAME window and
# rules — these are hard gates, not estimates. A mismatch means the fetch and
# the population no longer agree and approvals must not proceed.
_EXPECT = {
    "delta_txs": 30_300, "delta_legs": 41_723, "delta_wallets": 5_022,
    "overlap_txs": 0, "union_txs": 31_949, "union_wallets": 5_184,
}

_ROUTERS_IN = ", ".join(f"'{a}'" for a in SUSHI_ROUTERS)


def _delta_keys(runner: DuneRunner) -> list[str]:
    return [k for k in runner._manifest if k.startswith(_DELTA_PREFIX)]


def _preflight(runner: DuneRunner, window_start: str, window_end: str) -> None:
    """Refuse to run unless the base fetch is present, complete, and same-window."""
    meta = runner._manifest.get(_META_KEY, {})
    if meta.get("meta_hash") != _meta_hash(window_start, window_end):
        raise SystemExit(
            f"[delta] manifest window {meta.get('window')!r} does not match "
            f"{window_start}..{window_end} — the delta is only additive against the "
            "base fetch of the same window. Run sushi_timeline first."
        )
    base = runner._manifest.get("sushiswap_v2/swaps", {})
    if not base.get("fetch_complete"):
        raise SystemExit("[delta] base swaps fetch is not complete — refusing to run.")
    row = next(iter(_client().query(f"""
        SELECT COUNT(DISTINCT tx_hash) AS txs, COUNT(DISTINCT wallet) AS wallets
        FROM `{_table_id('swaps')}` WHERE tx_to IN ({_ROUTERS_IN})""").result()))
    if (row.txs, row.wallets) != (1_649, 626):
        raise SystemExit(
            f"[delta] base table router-entry counts {row.txs}/{row.wallets} != 1,649/626 "
            "— base data drifted from the verified overlap; investigate before fetching."
        )
    print(f"[delta] preflight OK: base swaps intact (router-entry {row.txs:,} txs / {row.wallets} wallets)")


def _validate_delta_swaps() -> None:
    row = next(iter(_client().query(f"""
        WITH d AS (SELECT tx_hash, wallet, evt_index FROM `{_table_id('swaps_txto_delta')}`)
        SELECT
          (SELECT COUNT(*) FROM d)                                          AS delta_legs,
          (SELECT COUNT(DISTINCT tx_hash) FROM d)                           AS delta_txs,
          (SELECT COUNT(DISTINCT wallet) FROM d)                            AS delta_wallets,
          (SELECT COUNT(*) - COUNT(DISTINCT CONCAT(tx_hash, ':', CAST(evt_index AS STRING))) FROM d)
                                                                            AS dup_legs,
          (SELECT COUNT(DISTINCT d.tx_hash) FROM d
             JOIN `{_table_id('swaps')}` s ON s.tx_hash = d.tx_hash)        AS overlap_txs
        """).result()))
    got = {
        "delta_txs": row.delta_txs, "delta_legs": row.delta_legs,
        "delta_wallets": row.delta_wallets, "overlap_txs": row.overlap_txs,
    }
    bad = {k: (got[k], _EXPECT[k]) for k in got if got[k] != _EXPECT[k]}
    if row.dup_legs:
        bad["dup_legs"] = (row.dup_legs, 0)
    if bad:
        raise SystemExit(f"[delta] stage-1 validation FAILED (got, expected): {bad}")
    print(f"[delta] stage-1 gates PASSED: {got['delta_txs']:,} txs / "
          f"{got['delta_legs']:,} legs / {got['delta_wallets']:,} wallets, overlap 0, no dup legs")


def _new_wallet_batches() -> list[list[str]]:
    """Wallets in the delta with NO base-fetch coverage. Base approvals were
    fetched for every base-swaps wallet with lookback to Permit2 genesis, so
    any wallet already in base swaps has a complete history — only genuinely
    new wallets need stages 3/4."""
    rows = _client().query(f"""
        SELECT DISTINCT d.wallet
        FROM `{_table_id('swaps_txto_delta')}` d
        LEFT JOIN (SELECT DISTINCT wallet FROM `{_table_id('swaps')}`) s USING (wallet)
        WHERE d.wallet IS NOT NULL AND s.wallet IS NULL
        ORDER BY wallet""").result()
    wallets = [r.wallet for r in rows]
    bad = [w for w in wallets if not _ADDR_RE.match(w)]
    if bad:
        raise ValueError(f"non-address wallet values in delta table, e.g. {bad[:3]}")
    return [wallets[i:i + _WALLET_BATCH] for i in range(0, len(wallets), _WALLET_BATCH)]


def _create_views() -> None:
    """Study-population views over base + delta. swap_count is recomputed here:
    the stored column in `swaps` is relative to the superseded pool-anchored
    population and the delta deliberately does not carry one."""
    ds = _table_id("x").rsplit(".", 1)[0]
    cols = ", ".join(f.name for f in SCHEMAS["swaps_txto_delta"])
    client = _client()
    client.query(f"""
        CREATE OR REPLACE VIEW `{ds}.swaps_router_entry` AS
        WITH unioned AS (
          SELECT {cols} FROM `{ds}.swaps` WHERE tx_to IN ({_ROUTERS_IN})
          UNION ALL
          SELECT {cols} FROM `{ds}.swaps_txto_delta`
        ),
        wallet_counts AS (
          SELECT wallet, COUNT(DISTINCT tx_hash) AS swap_count FROM unioned GROUP BY wallet
        )
        SELECT u.*, wc.swap_count FROM unioned u JOIN wallet_counts wc USING (wallet)
        """).result()
    for base, delta, view in (
        ("approvals", "approvals_delta", "approvals_all"),
        ("permit2_events", "permit2_events_delta", "permit2_events_all"),
    ):
        client.query(f"""
            CREATE OR REPLACE VIEW `{ds}.{view}` AS
            SELECT * FROM `{ds}.{base}`
            UNION ALL
            SELECT * FROM `{ds}.{delta}`
            """).result()
    row = next(iter(client.query(f"""
        SELECT COUNT(DISTINCT tx_hash) AS txs, COUNT(DISTINCT wallet) AS wallets
        FROM `{ds}.swaps_router_entry`""").result()))
    if (row.txs, row.wallets) != (_EXPECT["union_txs"], _EXPECT["union_wallets"]):
        raise SystemExit(
            f"[delta] union view gate FAILED: {row.txs:,}/{row.wallets:,} != "
            f"{_EXPECT['union_txs']:,}/{_EXPECT['union_wallets']:,}")
    print(f"[delta] views created; swaps_router_entry = {row.txs:,} txs / {row.wallets:,} wallets (gate PASSED)")


def _reset_delta(runner: DuneRunner) -> None:
    for k in _delta_keys(runner):
        runner._manifest.pop(k, None)
    runner._write_manifest()
    for name in _DELTA_TABLES:
        _client().delete_table(_table_id(name), not_found_ok=True)
    print(f"[delta] dropped {{{','.join(_DELTA_TABLES)}}} and forgot {_DELTA_PREFIX}* jobs")


def status() -> None:
    runner = DuneRunner()
    keys = _delta_keys(runner)
    print(f"[status] {len(keys)} delta job(s) in manifest")
    for k in sorted(keys):
        e = runner._manifest[k]
        state = "COMPLETE" if e.get("fetch_complete") else \
            f"offset {int(e.get('fetch_next_offset') or 0):,} ({e.get('state', 'not submitted')})"
        print(f"[status]   {k:<50}: {state}")
    for table in _DELTA_TABLES:
        try:
            n = next(iter(_client().query(
                f"SELECT COUNT(*) AS n FROM `{_table_id(table)}`").result())).n
            print(f"[status]   BQ {table:<22}: {n:,} rows")
        except Exception:
            print(f"[status]   BQ {table:<22}: table missing")


def run(*, window_start: str, window_end: str, force_delta: bool = False) -> None:
    print(f"[delta] window: {window_start} → {window_end} (delta fetch, base tables untouched)")
    runner = DuneRunner()
    if force_delta:
        _reset_delta(runner)
    _preflight(runner, window_start, window_end)

    try:
        # ---- stage 1: delta swaps -------------------------------------------
        sql = dune.render_named_sql(
            "sushiswap_v2/swaps_txto_delta",
            {"window_start": window_start, "window_end": window_end})
        n = _run_job_to_table(runner, Job(key=_DELTA_PREFIX + "swaps", sql=sql), "swaps_txto_delta")
        print(f"[delta] stage 1 done ({n:,} rows this run)")
        _validate_delta_swaps()

        # ---- stage 2: NEW wallets only --------------------------------------
        batches = _new_wallet_batches()
        n_wallets = sum(len(b) for b in batches)
        print(f"[delta] stage 2: {n_wallets:,} NEW wallets in {len(batches)} batch(es)")

        # ---- stage 3: approvals for new wallets ------------------------------
        for bi, batch in enumerate(batches):
            wallets_sql = ", ".join(batch)
            for chunk_start, chunk_end in _chunks(window_end):
                sql = dune.render_named_sql(
                    "sushiswap_v2/approvals",
                    {"wallets": wallets_sql, "chunk_start": chunk_start, "chunk_end": chunk_end})
                key = f"{_DELTA_PREFIX}approvals/b{bi:02d}/{chunk_start}"
                _run_job_to_table(runner, Job(key=key, sql=sql), "approvals_delta")
        print("[delta] stage 3 done")

        # ---- stage 4: permit2 events for new wallets -------------------------
        for bi, batch in enumerate(batches):
            sql = dune.render_named_sql(
                "sushiswap_v2/permit2_events",
                {"wallets_values": ", ".join(f"({w})" for w in batch),
                 "window_start": window_start, "window_end": window_end})
            key = f"{_DELTA_PREFIX}permit2/b{bi:02d}"
            _run_job_to_table(runner, Job(key=key, sql=sql), "permit2_events_delta")
        print("[delta] stage 4 done")

        # ---- views + final gate ----------------------------------------------
        _create_views()
        print("[delta] COMPLETE — study population is view sushiswap_v2.swaps_router_entry; "
              "use approvals_all / permit2_events_all downstream")

    except CreditError:
        print(
            "[delta] Dune datapoint limit hit — progress saved per job in manifest.\n"
            "        Swap in a fresh API key in .env and re-run; completed jobs are\n"
            "        skipped and the interrupted one resumes (check --status)."
        )
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SushiSwap V2 delta fetch (additive; base tables untouched)")
    parser.add_argument("--window-start", help="override window start (ISO date)")
    parser.add_argument("--window-end", help="override window end, exclusive")
    parser.add_argument("--force-delta", action="store_true", help="reset DELTA tables/jobs only, then run")
    parser.add_argument("--status", action="store_true", help="print delta fetch progress and BQ row counts")
    args = parser.parse_args()

    if args.status:
        status()
    elif args.window_start and args.window_end:
        run(window_start=args.window_start, window_end=args.window_end, force_delta=args.force_delta)
    else:
        _, ws, we, _ = study_window()
        run(window_start=ws, window_end=we, force_delta=args.force_delta)
