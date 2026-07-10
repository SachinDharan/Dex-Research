"""November broad-slice post-mortem: the non-router pool-touch universe.

The broad layer (`txs_broad`/`legs_broad`) is COMPLETE leg-level for November
only (descoped 2026-07-09 after ~10 keys; Dec–Feb are funnel-counted but not
fetched). This module answers the Sushi arm's section-1 question from the
other side: of everything that swaps a stable through a Uniswap pool, how
much actually enters through a Uniswap router — and who owns the rest?

Everything aggregates BigQuery-side (the slice is ~2M legs; no leg-level
pandas). Complements `uniswap_analysis` (router-entry core, full window);
numbers here are NOVEMBER-ONLY and must be labeled as such downstream.

Run with:
    python -m dexresearch.process.uniswap_broad_november
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from google.cloud import bigquery

from dexresearch.process.uniswap_analysis import label
from dexresearch.process.sushi_analysis import h

DATASET = "dex-research.uniswap_v4"
OUT_DIR = Path("data/analysis")

# Fetch-time gates: broad_funnel manifest (2025-11) + router monthly count.
GATES = {"broad_txs": 977_106, "broad_legs": 1_975_503,
         "broad_wallets": 116_645, "router_txs_nov": 81_927}

NOV_END = "TIMESTAMP '2025-12-01'"


def _q(sql: str) -> pd.DataFrame:
    return bigquery.Client(project="dex-research").query(sql).to_dataframe()


def run() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    gate = _q(f"""
        SELECT
          (SELECT COUNT(*) FROM `{DATASET}.txs_broad`)               AS broad_txs,
          (SELECT COUNT(*) FROM `{DATASET}.legs_broad`)              AS broad_legs,
          (SELECT COUNT(DISTINCT wallet) FROM `{DATASET}.txs_broad`) AS broad_wallets,
          (SELECT COUNT(DISTINCT tx_hash) FROM `{DATASET}.swaps_sampled`
           WHERE block_time < {NOV_END})                             AS router_txs_nov
    """).iloc[0]
    bad = {k: (int(gate[k]), v) for k, v in GATES.items() if int(gate[k]) != v}
    if bad:
        raise RuntimeError(f"broad-slice gate FAILED: {bad}")
    print(f"  gates OK: {GATES['broad_txs']:,} broad txs / "
          f"{GATES['broad_legs']:,} legs / {GATES['broad_wallets']:,} wallets; "
          f"router Nov = {GATES['router_txs_nov']:,}")

    # ---------------------------------------------------------------- funnel
    h("1. NOVEMBER FUNNEL — who enters the Uniswap-pool universe, and how")
    tot = _q(f"""
        WITH v4 AS (
          SELECT tx_hash, LOGICAL_OR(project = 'uniswap' AND version = '4') AS v4_any
          FROM `{DATASET}.legs_broad` GROUP BY tx_hash
        )
        SELECT COUNT(*) AS txs, COUNTIF(v4.v4_any) AS v4_txs,
               COUNTIF(t.max_priority_fee_per_gas = 0) / COUNT(*) AS zero_prio
        FROM `{DATASET}.txs_broad` t JOIN v4 USING (tx_hash)
    """).iloc[0]
    rtr = _q(f"""
        SELECT COUNT(DISTINCT tx_hash) AS txs,
               COUNT(DISTINCT IF(project = 'uniswap' AND version = '4', tx_hash, NULL)) AS v4_txs,
               COUNT(DISTINCT wallet) AS wallets
        FROM `{DATASET}.swaps_sampled` WHERE block_time < {NOV_END}
    """).iloc[0]
    universe = int(tot.txs) + int(rtr.txs)
    print(f"  pool-touch universe               {universe:>10,} txs")
    print(f"  entered via a Uniswap ROUTER      {int(rtr.txs):>10,} txs  "
          f"({int(rtr.txs) / universe:.1%})   {int(rtr.wallets):,} wallets")
    print(f"  entered via anything else         {int(tot.txs):>10,} txs  "
          f"({int(tot.txs) / universe:.1%})   {int(gate['broad_wallets']):,} wallets")
    print(f"\n  V4-touching share: router-entry {int(rtr.v4_txs) / int(rtr.txs):.1%} vs "
          f"broad {int(tot.v4_txs) / int(tot.txs):.1%} — the router does not route to V4")
    print(f"  more than the aggregators do. Broad zero-priority share: {tot.zero_prio:.1%}.")

    ov = _q(f"""
        SELECT COUNT(*) AS broad_wallets, COUNTIF(r.wallet IS NOT NULL) AS also_router
        FROM (SELECT DISTINCT wallet FROM `{DATASET}.txs_broad`) b
        LEFT JOIN (SELECT DISTINCT wallet FROM `{DATASET}.swaps_sampled`) r USING (wallet)
    """).iloc[0]
    print(f"\n  Wallet overlap: {int(ov.also_router):,} of {int(ov.broad_wallets):,} broad "
          f"wallets ({int(ov.also_router) / int(ov.broad_wallets):.1%}) EVER enter via a")
    print("  Uniswap router (any month). The populations are close to disjoint: the")
    print("  study core and the aggregator/MEV flow are different people, not the")
    print("  same people on different days — approvals pair with entry, not liquidity.")

    # -------------------------------------------------------- entry contracts
    h("2. TOP NON-ROUTER ENTRY CONTRACTS (November)")
    prof = _q(f"""
        WITH v4 AS (
          SELECT tx_hash,
                 LOGICAL_OR(project = 'uniswap' AND version = '4') AS v4_any,
                 COUNT(DISTINCT project) AS n_projects
          FROM `{DATASET}.legs_broad` GROUP BY tx_hash
        )
        SELECT t.tx_to, COUNT(*) AS txs, COUNT(DISTINCT t.wallet) AS wallets,
               AVG(t.n_legs) AS mean_legs,
               COUNTIF(t.max_priority_fee_per_gas = 0) / COUNT(*) AS zero_prio,
               AVG(IF(v4.v4_any, 1, 0)) AS v4_share,
               AVG(IF(v4.n_projects > 1, 1, 0)) AS multi_venue
        FROM `{DATASET}.txs_broad` t JOIN v4 USING (tx_hash)
        GROUP BY 1 ORDER BY txs DESC LIMIT 25
    """)
    prof["name"] = [label(a) for a in prof["tx_to"]]
    # txs/wallet separates the two species sharing this table: searcher
    # infrastructure (23 wallets, 68k txs) vs retail aggregators (20k wallets)
    print(f"  {'contract':<15}{'name':<28}{'txs':>9}{'wallets':>9}{'txs/w':>8}"
          f"{'legs':>6}{'zeroP':>7}{'v4':>5}{'multiV':>7}")
    for _, r in prof.iterrows():
        print(f"  {str(r.tx_to)[:13]:<15}{r['name'][:27]:<28}{r.txs:>9,}{r.wallets:>9,}"
              f"{r.txs / r.wallets:>8.1f}{r.mean_legs:>6.1f}{r.zero_prio:>7.0%}"
              f"{r.v4_share:>5.0%}{r.multi_venue:>7.0%}")
    print("\n  NOTE: multiV = share of txs whose legs span >1 project. Broad txs can")
    print("  cross venues: the qualifying rule is FIRST leg (any venue) sold a stable")
    print("  AND >=1 Uniswap-pool stable-selling leg somewhere in the tx (see")
    print("  broad_funnel.sql). Unresolved contracts stay 'unknown', never guessed.")
    prof.to_csv(OUT_DIR / "uniswap_broad_nov_entries.csv", index=False)
    print(f"\nWrote {OUT_DIR}/uniswap_broad_nov_entries.csv")


if __name__ == "__main__":
    run()
