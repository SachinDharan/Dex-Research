"""In-depth analysis of the Aave V3 arm (`dex-research.aave_v3`).

Fourth arm, harmonized with the other three. What is structurally different:

  1. Aave has REAL Borrow events (unlike Compound), so the borrow side is
     measured exactly — but borrows still need no ERC-20 approval, so the
     approval-to-action pairing stays on the supply side.
  2. The extra permission layer is CREDIT DELEGATION (BorrowAllowanceDelegated
     on the variable-debt tokens): an allowance to borrow against the
     delegator's collateral. Unlike Comet allow() it carries an amount, so
     the unlimited/exact classifier applies; genesis lookback makes the
     outstanding view exact.
  3. supplyWithPermit exists (EIP-2612); permit() emits a token Approval
     event, so the ERC-20 fetch captures that path — method_id separates it.

Population = supplies with tx_to = the V3 Pool; aggregator/manager flow
(ParaSwap, Kyber, Kiln vaults, Aave Umbrella, CoW solvers) is the stored
contrast. Bot thresholds, the classifier and the default-vs-deliberate test
are imported; as in the Compound arm the behavioral bot rule is >=2 of 3
signals (no route-length analog in lending).

Run with:
    python -m dexresearch.process.aave_analysis
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from google.cloud import bigquery

from dexresearch.classify import classify_allowance
from dexresearch.process.deciles import assign_deciles, flag_bots
from dexresearch.fetch.aave_timeline import POOL
from dexresearch.process.sushi_analysis import (
    BOT_APPROVALS_PER_ACTIVE_DAY,
    BOT_SUSTAINED_APPROVALS,
    BOT_TXS_PER_ACTIVE_DAY,
    BOT_ZERO_PRIO_FRAC,
    KNOWN_CONTRACTS,
    WINDOW_END,
    WINDOW_START,
    default_vs_deliberate,
    fmt_eth,
    h,
    zero_prio_share,
)

DATASET = "dex-research.aave_v3"
OUT_DIR = Path("data/analysis")

AAVE_CONTRACTS = {
    POOL: "Aave V3 Pool",
    "0xce6ced23118edeb23054e06118a702797b13fc2f": "Aave UmbrellaBatchHelper",
    "0xd2011d314acaa68e5401e7f5aec3be6d2c574dcf": "Kiln DeFi Vault",
    "0x4d431856295413906075dd40266d83624e09c672": "Kiln DeFi Vault",
    "0x7251febeabb01ec9de53ece7a96f1c951f886dd2": "OKX Swell proxy",
    "0xd7852e139a7097e119623de0751ae53a61efb442": "Aave ParaSwapDebtSwapAdapter",
    "0xb748952c7bc638f31775245964707bcc5ddfabfc": "Aave MigrationHelper",
}


def _client() -> bigquery.Client:
    return bigquery.Client(project="dex-research")


def _q(sql: str) -> pd.DataFrame:
    return _client().query(sql).to_dataframe()


def label(addr: str) -> str:
    if addr is None:
        return "(contract creation / null)"
    return AAVE_CONTRACTS.get(addr, KNOWN_CONTRACTS.get(addr, "unknown"))


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

def _load_action_txs(table: str) -> pd.DataFrame:
    """One row per tx. USD-equivalent from raw amounts (stables: DAI 1e18,
    USDC/USDT 1e6). Gas columns are tx-level so MAX() is a dedupe."""
    return _q(f"""
        SELECT
          tx_hash,
          ANY_VALUE(wallet)                    AS wallet,
          MIN(block_time)                      AS block_time,
          ANY_VALUE(tx_to)                     AS tx_to,
          COUNT(*)                             AS events,
          ANY_VALUE(token_symbol)              AS token_symbol,
          SUM(CAST(amount_raw AS BIGNUMERIC)
              / POW(10, IF(token_symbol = 'DAI', 18, 6))) AS amount_usd,
          MAX(gas_used)                        AS gas_used,
          MAX(gas_cost_eth)                    AS gas_cost_eth,
          MAX(max_priority_fee_per_gas)        AS max_priority_fee_per_gas,
          ANY_VALUE(method_id)                 AS method_id,
          LOGICAL_OR(wallet != on_behalf_of)   AS on_behalf
        FROM `{DATASET}.{table}`
        GROUP BY tx_hash
    """)


def load_approvals() -> pd.DataFrame:
    return _q(f"""
        SELECT wallet, block_time, tx_hash, token_symbol,
               counterparty AS spender, amount_raw, is_revoke,
               gas_used, gas_cost_eth, max_priority_fee_per_gas
        FROM `{DATASET}.approvals`
    """)


def load_delegation() -> pd.DataFrame:
    return _q(f"""
        SELECT wallet, delegatee, token_symbol, block_time, tx_hash,
               amount_raw, is_revoke, gas_cost_eth
        FROM `{DATASET}.delegation_events`
    """)


# --------------------------------------------------------------------------
# Features
# --------------------------------------------------------------------------

def build_wallet_features(tx: pd.DataFrame, appr: pd.DataFrame) -> pd.DataFrame:
    qt = tx[tx["is_entry"]].copy()
    qt["day"] = qt["block_time"].dt.floor("D")

    g = qt.groupby("wallet")
    feats = pd.DataFrame({
        "qualifying_txs": g.size(),
        "zero_prio_frac": g["max_priority_fee_per_gas"].apply(zero_prio_share),
        "active_days": g["day"].nunique(),
        "active_months": g["block_time"].apply(lambda s: s.dt.to_period("M").nunique()),
        "supply_gas_eth": g["gas_cost_eth"].sum(),
        "volume_usd": g["amount_usd"].sum(),
        "first_seen": g["block_time"].min(),
        "last_seen": g["block_time"].max(),
    })
    feats["txs_per_active_day"] = feats["qualifying_txs"] / feats["active_days"]

    in_window = appr[(appr["block_time"] >= WINDOW_START) & (appr["block_time"] < WINDOW_END)]
    ag = in_window.groupby("wallet")
    appr_feats = pd.DataFrame({
        "approvals_in_window": ag.size(),
        "approval_active_days": ag["block_time"].apply(lambda s: s.dt.floor("D").nunique()),
        "distinct_spenders": ag["spender"].nunique(),
    })
    gas_by_wallet = (
        in_window.drop_duplicates("tx_hash").groupby("wallet")["gas_cost_eth"].sum()
        .rename("approval_gas_eth")
    )

    pool = appr[appr["spender"] == POOL]
    pool_win = pool[(pool["block_time"] >= WINDOW_START) & (pool["block_time"] < WINDOW_END)]
    pool_feats = pd.DataFrame({
        "pool_approvals": pool_win.groupby("wallet").size(),
        "pool_approval_gas_eth": (
            pool_win.drop_duplicates("tx_hash").groupby("wallet")["gas_cost_eth"].sum()
        ),
        "pool_approvals_prewindow": (
            pool[pool["block_time"] < WINDOW_START].groupby("wallet").size()
        ),
    })

    feats = feats.join([appr_feats, gas_by_wallet, pool_feats])
    for c in ["approvals_in_window", "approval_active_days", "distinct_spenders",
              "approval_gas_eth", "pool_approvals", "pool_approval_gas_eth",
              "pool_approvals_prewindow"]:
        feats[c] = feats[c].fillna(0)

    feats["approvals_per_active_day"] = np.where(
        feats["approval_active_days"] > 0,
        feats["approvals_in_window"] / feats["approval_active_days"].replace(0, np.nan),
        0.0,
    )
    feats["approval_to_action_all"] = feats["approvals_in_window"] / feats["qualifying_txs"]
    feats["approval_to_action_pool"] = feats["pool_approvals"] / feats["qualifying_txs"]
    return feats.reset_index()


def apply_bot_flags(feats: pd.DataFrame) -> pd.DataFrame:
    out = feats.copy()
    tmp = out.assign(protocol="aave", action_type="supply",
                     action_count=out["qualifying_txs"])
    out["is_bot_3sigma"] = flag_bots(tmp, sd_threshold=3.0)["is_bot"].to_numpy()

    sig = pd.DataFrame({
        "private_orderflow": out["zero_prio_frac"] >= BOT_ZERO_PRIO_FRAC,
        "high_frequency": out["txs_per_active_day"] >= BOT_TXS_PER_ACTIVE_DAY,
        "approval_spam": out["approvals_per_active_day"] >= BOT_APPROVALS_PER_ACTIVE_DAY,
    })
    out = pd.concat([out, sig], axis=1)
    out["bot_signal_count"] = sig.sum(axis=1)
    out["is_bot_behavioral"] = out["bot_signal_count"] >= 2
    out["is_bot_approval_spam"] = (
        out["approval_spam"] & (out["approvals_in_window"] >= BOT_SUSTAINED_APPROVALS)
    )
    out["is_bot"] = (
        out["is_bot_3sigma"] | out["is_bot_behavioral"] | out["is_bot_approval_spam"]
    )
    return out


def classify_amounts(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["approval_type"] = [
        classify_allowance(int(v), near_unlimited_threshold=2**255)
        for v in out["amount_raw"]
    ]
    return out


def outstanding_allowances(appr: pd.DataFrame) -> pd.DataFrame:
    hist = appr[appr["block_time"] < WINDOW_END].sort_values("block_time")
    latest = hist.groupby(["wallet", "token_symbol", "spender"], as_index=False).last()
    latest["outstanding"] = latest["approval_type"] != "revocation"
    latest["is_unlimited"] = latest["approval_type"] == "unlimited"
    return latest


def outstanding_delegation(deleg: pd.DataFrame) -> pd.DataFrame:
    """Latest delegation per (wallet, token, delegatee) as of window end —
    EXACT (genesis lookback, no expiry)."""
    hist = deleg[deleg["block_time"] < WINDOW_END].sort_values("block_time")
    latest = hist.groupby(["wallet", "token_symbol", "delegatee"], as_index=False).last()
    latest["outstanding"] = latest["approval_type"] != "revocation"
    latest["is_unlimited"] = latest["approval_type"] == "unlimited"
    return latest


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def run() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pd.set_option("display.width", 200, "display.max_columns", 40)

    print("Loading from BigQuery ...")
    tx = _load_action_txs("supplies")
    bx = _load_action_txs("borrows")
    appr = classify_amounts(load_approvals())
    deleg = classify_amounts(load_delegation())
    print(f"  {len(tx):,} supply txs | {len(bx):,} borrow txs | "
          f"{len(appr):,} ERC-20 approvals | {len(deleg):,} delegation events")

    for df in (tx, bx):
        df["is_entry"] = df["tx_to"] == POOL
        df["amount_usd"] = df["amount_usd"].astype(float)

    feats = apply_bot_flags(build_wallet_features(tx, appr))

    # ------------------------------------------------------------ population
    h("1. POPULATION — stable supply flow on the Aave V3 Pool")
    qual = tx[tx["is_entry"]]
    print(f"  supply txs (broad)      {len(tx):>8,}  wallets {tx['wallet'].nunique():>7,}")
    print(f"  QUALIFYING (Pool direct){len(qual):>8,}  wallets {qual['wallet'].nunique():>7,}"
          f"   [{len(qual) / len(tx):.1%} of txs]")
    print(f"  contrast (managers)     {len(tx) - len(qual):>8,}  wallets "
          f"{tx.loc[~tx['is_entry'], 'wallet'].nunique():>7,}")
    print(f"\n  token mix (qualifying): "
          + "  ".join(f"{m}={n:,}" for m, n in qual["token_symbol"].value_counts().items()))
    print(f"  on-behalf supplies (onBehalfOf != signer): {qual['on_behalf'].mean():.1%}")
    # method mix, measured not guessed — supplyWithPermit (EIP-2612 one-tx
    # path) will show as its own selector if used
    print("  qualifying method mix: " + "  ".join(
        f"{mid}={n:,}" for mid, n in qual["method_id"].value_counts().head(4).items()))

    print("\n  Top contrast entry contracts (stored, not studied):")
    nb = (tx[~tx["is_entry"]].groupby("tx_to")
          .agg(txs=("tx_hash", "size"), wallets=("wallet", "nunique")))
    for addr, r in nb.sort_values("txs", ascending=False).head(8).iterrows():
        print(f"    {str(addr)[:12]}… {label(addr):<28} {r.txs:>6,} txs {r.wallets:>6,} wallets")

    # ------------------------------------------------------------------ bots
    h("2. BOT PARTITION (shared thresholds; >=2 of 3 signals)")
    n_bot = int(feats["is_bot"].sum())
    print(f"  {n_bot:,} of {len(feats):,} qualifying wallets flagged ({n_bot / len(feats):.1%}) — "
          f"{feats.loc[feats['is_bot'], 'qualifying_txs'].sum() / feats['qualifying_txs'].sum():.1%} of qualifying txs.")
    for c in ["private_orderflow", "high_frequency", "approval_spam"]:
        print(f"  {c:<22}{int(feats[c].sum()):>7,}")
    print(f"  {'3-sigma (plan rule)':<22}{int(feats['is_bot_3sigma'].sum()):>7,}")
    human = feats[~feats["is_bot"]].copy()
    human_set = set(human["wallet"])

    # -------------------------------------------------------------- deciles
    h("3. ACTIVITY STRUCTURE")
    d = human.assign(protocol="aave", action_type="supply",
                     action_count=human["qualifying_txs"])
    one_shot = (d["action_count"] == 1).mean()
    d = assign_deciles(d)
    dm = d.groupby("decile").agg(
        wallets=("wallet", "size"),
        median_txs=("action_count", "median"),
        max_txs=("action_count", "max"),
        median_active_months=("active_months", "median"),
        median_ratio_pool=("approval_to_action_pool", "median"),
    )
    print(f"  non-bot wallets: {len(d):,}; one-shot share {one_shot:.1%}; "
          f"{len(dm)} of 10 deciles non-empty\n")
    print(dm.to_string(float_format=lambda x: f"{x:,.3f}"))

    # ---------------------------------------------------- approval-to-action
    h("4. APPROVAL-TO-ACTION")
    print(f"  Non-bot wallets: {len(human):,}\n")
    r = human["approval_to_action_pool"]
    print("  (a) MATCHED — stable approvals to the Pool / qualifying supplies")
    print(f"      median={r.median():.3f}  p90={r.quantile(.9):.3f}  "
          f"share==0: {(r == 0).mean():.1%}  share>=1: {(r >= 1).mean():.1%}")
    coasting = (human["pool_approvals"] == 0) & (human["pool_approvals_prewindow"] > 0)
    print(f"      of the zero-in-window wallets, {int(coasting.sum()):,} "
          f"({coasting.sum() / max((r == 0).sum(), 1):.1%}) hold a PRE-WINDOW Pool approval")
    r2 = human["approval_to_action_all"]
    print("\n  (b) UNMATCHED — all stable approvals (any spender) / supplies")
    print(f"      median={r2.median():.3f}  p90={r2.quantile(.9):.3f}")
    print(f"\n  Cross-arm matched medians: Sushi 1.000 | Uniswap 1.000 | "
          f"Compound 1.000 | Aave {r.median():.3f}")

    # ----------------------------------------------------- approval type mix
    h("5. APPROVAL TYPE MIX")
    win = appr[(appr["block_time"] >= WINDOW_START) & (appr["block_time"] < WINDOW_END)]
    for name, sub in [
        ("ERC-20, non-bot, all spenders", win[win["wallet"].isin(human_set)]),
        ("ERC-20, non-bot, spender = the Pool", win[win["wallet"].isin(human_set) & (win["spender"] == POOL)]),
    ]:
        if len(sub) == 0:
            continue
        mix = sub["approval_type"].value_counts(normalize=True)
        print(f"  {name:<38} n={len(sub):>7,}  " + "  ".join(
            f"{k}={mix.get(k, 0):.1%}" for k in ["unlimited", "exact", "revocation"]))
    pool_nz = win[win["wallet"].isin(human_set) & (win["spender"] == POOL)
                  & (win["approval_type"] != "revocation")]
    per_w = (pool_nz.assign(u=pool_nz["approval_type"] == "unlimited")
             .groupby("wallet")["u"].max())
    print(f"\n  Pool spender, wallet-weighted ever-unlimited: {per_w.mean():.1%} of "
          f"{len(per_w):,} wallets")
    print("  (cross-arm: Sushi 20.5% | Compound Comets 73.2% | Permit2 91.6%)")

    # ------------------------------------------------------ credit delegation
    h("6. CREDIT DELEGATION — the fourth permission flavor (has an amount)")
    dw = deleg[deleg["block_time"] < WINDOW_END]
    print(f"  {len(dw):,} events since V3 genesis by {dw['wallet'].nunique():,} delegators; "
          f"roster (non-bot) delegators: {dw[dw['wallet'].isin(human_set)]['wallet'].nunique():,}")
    mix = dw[~dw["is_revoke"]]["approval_type"].value_counts(normalize=True)
    print(f"  grant amount mix: " + "  ".join(
        f"{k}={mix.get(k, 0):.1%}" for k in ["unlimited", "exact"]))
    od = outstanding_delegation(deleg)
    live_d = od[od["outstanding"]]
    print(f"  outstanding at window end (EXACT): {len(live_d):,} "
          f"(wallet, token, delegatee) triples, {live_d['is_unlimited'].mean():.1%} unlimited")
    print("\n  Top delegatees by delegator count:")
    td = dw[~dw["is_revoke"]].groupby("delegatee").agg(wallets=("wallet", "nunique"))
    for addr, r_ in td.nlargest(6, "wallets").iterrows():
        print(f"    {str(addr)[:12]}… {label(addr):<28} {int(r_.wallets):>6,} delegators")

    # ---------------------------------------------------------- outstanding
    h("7. OUTSTANDING ERC-20 ALLOWANCE AT END OF PERIOD (2026-02-28)")
    out = outstanding_allowances(appr)
    out_h = out[out["wallet"].isin(human_set)]
    live = out_h[out_h["outstanding"]]
    print(f"  Non-bot wallets with >=1 live allowance : {live['wallet'].nunique():,}")
    print(f"  Live (wallet, token, spender) triples   : {len(live):,} "
          f"({live['is_unlimited'].mean():.1%} unlimited)")
    live_p = live[live["spender"] == POOL]
    print(f"  ...to the Pool                          : {len(live_p):,} "
          f"({live_p['is_unlimited'].mean() if len(live_p) else 0:.1%} unlimited)")
    print("\n  Top spenders holding live allowances from non-bot wallets:")
    ts = live.groupby("spender").agg(wallets=("wallet", "nunique"),
                                     unlimited=("is_unlimited", "mean"))
    for addr, r_ in ts.nlargest(8, "wallets").iterrows():
        print(f"    {str(addr)[:12]}… {label(addr):<28} {int(r_.wallets):>6,} wallets  "
              f"{r_.unlimited:.0%} unlimited")

    # ------------------------------------------------------------------ gas
    h("8. PERMISSION GAS (window, scope-matched)")
    ga = human["pool_approval_gas_eth"].sum()
    gs = human["supply_gas_eth"].sum()
    print(f"  non-bot Pool-spender approval gas : {fmt_eth(ga)}")
    print(f"  qualifying supply gas             : {fmt_eth(gs)}")
    print(f"  permission overhead               : {ga / gs:.1%}"
          f"   (Sushi 17.1% | Uniswap 10.0% | Compound 18.4%)")

    # -------------------------------------------------------------- monthly
    h("9. MONTHLY")
    qual_h = qual[qual["wallet"].isin(human_set)].copy()
    qual_h["month"] = qual_h["block_time"].dt.to_period("M").astype(str)
    win_h = win[win["wallet"].isin(human_set)].copy()
    win_h["month"] = win_h["block_time"].dt.to_period("M").astype(str)
    bq = bx[bx["is_entry"] & bx["wallet"].isin(human_set)].copy()
    bq["month"] = bq["block_time"].dt.to_period("M").astype(str)
    m = pd.DataFrame({
        "supplies": qual_h.groupby("month").size(),
        "supply_wallets": qual_h.groupby("month")["wallet"].nunique(),
        "borrows": bq.groupby("month").size(),
        "erc20_approvals": win_h.groupby("month").size(),
        "erc20_unlimited": win_h[win_h["approval_type"] == "unlimited"].groupby("month").size(),
    }).fillna(0).astype(int)
    print(m.to_string())
    m.to_csv(OUT_DIR / "aave_monthly.csv")

    # ------------------------------------------------ default vs deliberate
    h("10. DEFAULT OR DELIBERATE? — replication no. 4")
    dvd = default_vs_deliberate(win, human_set)
    for _, r_ in dvd.iterrows():
        print(f"  {r_.spender_a} ({r_.unlimited_a:.0%} unlimited)  vs  "
              f"{r_.spender_b} ({r_.unlimited_b:.0%} unlimited)")
        print(f"    n={r_.pairs:,} pairs   discordance {r_.a_unl_b_exact}:{r_.b_unl_a_exact}")
    if len(dvd):
        dvd.to_csv(OUT_DIR / "aave_default_vs_deliberate.csv", index=False)

    # ------------------------------------------------------------- borrows
    h("11. BORROW SIDE — real events (unlike Compound)")
    bq_all = bx[bx["is_entry"]]
    print(f"  qualifying borrow txs: {len(bq_all):,} by {bq_all['wallet'].nunique():,} wallets")
    suppliers = set(qual["wallet"])
    both = bq_all[bq_all["wallet"].isin(suppliers)]["wallet"].nunique()
    print(f"  borrowers who also supplied stables in-window: {both:,} "
          f"({both / bq_all['wallet'].nunique():.1%})")
    print(f"  borrow token mix: "
          + "  ".join(f"{m_}={n:,}" for m_, n in bq_all["token_symbol"].value_counts().items()))
    print("\n  Borrows need no ERC-20 approval, so they stay out of the ratio —")
    print("  but each borrower's collateral was enabled by a supply-side approval.")

    # ------------------------------------------------------------------ save
    feats.to_csv(OUT_DIR / "aave_wallet_features.csv", index=False)
    dm.to_csv(OUT_DIR / "aave_decile_metrics.csv")
    out.to_csv(OUT_DIR / "aave_outstanding_allowances.csv", index=False)
    od.to_csv(OUT_DIR / "aave_outstanding_delegation.csv", index=False)
    print(f"\nWrote CSVs to {OUT_DIR}/ (aave_* prefixed)")


if __name__ == "__main__":
    run()
