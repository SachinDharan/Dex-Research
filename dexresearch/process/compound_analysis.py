"""In-depth analysis of the Compound V3 arm (`dex-research.compound_v3`).

Third arm of the study, harmonized with sushi_analysis / uniswap_analysis.
What is structurally different here:

  1. Lending is a REPEAT relationship with one contract — the amortization
     question is sharper than for swaps (top-ups against a standing approval).
  2. The permission system is two-layer but the second layer is BINARY:
     an ERC-20 approve() to the Comet (amount chosen — or not — by the
     interface) plus Comet allow() operator grants (uint256-max or zero,
     nothing else exists on chain; the Bulker path REQUIRES one). Compound is
     the study's third default regime: Sushi exact, Uniswap unlimited,
     Compound no-amount-choice-at-all.
  3. Comet has NO Borrow event — a borrow is a Withdraw taking the base
     balance negative. Withdraws are classified here only as window-scoped
     bounds (pre-window balances are unfetched), never as population.

Population = supplies with tx_to in the entry set (two stable Comets +
MainnetBulker); manager/vault flow (Kiln, ERC-4337, proxies) is the stored
contrast. Bot thresholds, the classifier and the default-vs-deliberate test
are imported so the arms cannot diverge; `long_routes` has no lending analog,
so the behavioral bot rule here is >=2 of the remaining 3 signals.

Run with:
    python -m dexresearch.process.compound_analysis
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import numpy as np
import pandas as pd
from google.cloud import bigquery

from dexresearch.classify import classify_allowance
from dexresearch.process.deciles import assign_deciles, flag_bots
from dexresearch.process.prices import gas_usd
from dexresearch.fetch.compound_timeline import BULKER, ENTRY_SET, STABLE_COMETS
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

DATASET = "dex-research.compound_v3"
OUT_DIR = Path("data/analysis")

# Entry/contrast labels beyond KNOWN_CONTRACTS (resolved 2026-07-09 via
# contract_mapping; Kiln vaults are white-label earn infrastructure).
COMPOUND_CONTRACTS = {
    **{a: f"Compound {n}" for a, n in STABLE_COMETS.items()},
    BULKER: "Compound MainnetBulker",
    "0xb9e62cb9b4ce8ec13c886fae67369da417ee2714": "Kiln DeFi Vault",
    "0x73be9526629e0c615ce22603e9efd2f3ef9b523a": "Kiln DeFi Vault",
    "0x804ee40b227b9003bb7bf2880cf502466544f208": "Kiln DeFi Vault",
    "0x7251febeabb01ec9de53ece7a96f1c951f886dd2": "OKX Swell proxy",
    "0x0000000071727de22e5e9d8baf0edac6f37da032": "ERC-4337 EntryPoint v0.7",
    "0x5ff137d4b0fdcd49dca30c7cf57e578a026d2789": "ERC-4337 EntryPoint v0.6",
}


def _client() -> bigquery.Client:
    return bigquery.Client(project="dex-research")


def _q(sql: str) -> pd.DataFrame:
    return _client().query(sql).to_dataframe()


def label(addr: str) -> str:
    if addr is None:
        return "(contract creation / null)"
    return COMPOUND_CONTRACTS.get(addr, KNOWN_CONTRACTS.get(addr, "unknown"))


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

def load_supply_txs() -> pd.DataFrame:
    """One row per supply TRANSACTION (broad: entry + contrast). A tx can in
    principle carry several Supply events; gas columns are tx-level so MAX()
    is a dedupe."""
    return _q(f"""
        SELECT
          tx_hash,
          ANY_VALUE(wallet)                    AS wallet,
          MIN(block_time)                      AS block_time,
          ANY_VALUE(tx_to)                     AS tx_to,
          COUNT(*)                             AS supply_events,
          ANY_VALUE(market)                    AS market,
          SUM(CAST(amount_raw AS BIGNUMERIC))  AS amount_raw_sum,
          MAX(gas_used)                        AS gas_used,
          MAX(gas_cost_eth)                    AS gas_cost_eth,
          MAX(max_priority_fee_per_gas)        AS max_priority_fee_per_gas,
          ANY_VALUE(method_id)                 AS method_id,
          LOGICAL_OR(wallet != supply_dst)     AS on_behalf
        FROM `{DATASET}.supplies`
        GROUP BY tx_hash
    """)


def load_withdraw_txs() -> pd.DataFrame:
    return _q(f"""
        SELECT tx_hash, ANY_VALUE(wallet) AS wallet, MIN(block_time) AS block_time,
               ANY_VALUE(tx_to) AS tx_to, ANY_VALUE(market) AS market,
               SUM(CAST(amount_raw AS BIGNUMERIC)) AS amount_raw_sum
        FROM `{DATASET}.withdraws`
        GROUP BY tx_hash
    """)


def load_approvals() -> pd.DataFrame:
    return _q(f"""
        SELECT wallet, block_time, block_number, tx_hash, token_symbol,
               counterparty AS spender, amount_raw, is_revoke,
               gas_used, gas_price, gas_cost_eth, max_priority_fee_per_gas,
               base_fee_per_gas, priority_fee_per_gas, eth_usd, gas_cost_usd
        FROM `{DATASET}.approvals_usd`
    """)


def load_allow() -> pd.DataFrame:
    return _q(f"""
        SELECT market, comet, wallet, manager, block_time, block_number, tx_hash,
               is_grant, is_revoke, gas_used, gas_price, gas_cost_eth,
               base_fee_per_gas, priority_fee_per_gas, eth_usd, gas_cost_usd
        FROM `{DATASET}.allow_events_usd`
    """)


# --------------------------------------------------------------------------
# Features
# --------------------------------------------------------------------------

def build_wallet_features(tx: pd.DataFrame, appr: pd.DataFrame,
                          allow: pd.DataFrame) -> pd.DataFrame:
    """Features for QUALIFYING wallets only (entry-anchored txs)."""
    qt = tx[tx["is_entry"]].copy()
    qt["day"] = qt["block_time"].dt.floor("D")

    g = qt.groupby("wallet")
    feats = pd.DataFrame({
        "qualifying_txs": g.size(),
        "bulker_txs": g["is_bulker"].sum(),
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

    comet = appr[appr["spender"].isin(STABLE_COMETS)]
    comet_win = comet[(comet["block_time"] >= WINDOW_START) & (comet["block_time"] < WINDOW_END)]
    comet_feats = pd.DataFrame({
        "comet_approvals": comet_win.groupby("wallet").size(),
        "comet_approval_gas_eth": (
            comet_win.drop_duplicates("tx_hash").groupby("wallet")["gas_cost_eth"].sum()
        ),
        "comet_approvals_prewindow": (
            comet[comet["block_time"] < WINDOW_START].groupby("wallet").size()
        ),
    })

    aw = allow[allow["block_time"] < WINDOW_END]
    aw_win = aw[aw["block_time"] >= WINDOW_START]
    allow_feats = pd.DataFrame({
        "allow_grants_ever": aw[aw["is_grant"]].groupby("wallet").size(),
        "allow_revokes_ever": aw[aw["is_revoke"]].groupby("wallet").size(),
        # lifetime stock vs window flow: only the window figure may be
        # ratioed against window supply gas (scope match)
        "allow_gas_eth_ever": aw.drop_duplicates("tx_hash").groupby("wallet")["gas_cost_eth"].sum(),
        "allow_gas_eth": aw_win.drop_duplicates("tx_hash").groupby("wallet")["gas_cost_eth"].sum(),
    })

    feats = feats.join([appr_feats, gas_by_wallet, comet_feats, allow_feats])
    for c in ["approvals_in_window", "approval_active_days", "distinct_spenders",
              "approval_gas_eth", "comet_approvals", "comet_approval_gas_eth",
              "comet_approvals_prewindow", "allow_grants_ever", "allow_revokes_ever",
              "allow_gas_eth_ever", "allow_gas_eth"]:
        feats[c] = feats[c].fillna(0)

    feats["approvals_per_active_day"] = np.where(
        feats["approval_active_days"] > 0,
        feats["approvals_in_window"] / feats["approval_active_days"].replace(0, np.nan),
        0.0,
    )
    feats["approval_to_action_all"] = feats["approvals_in_window"] / feats["qualifying_txs"]
    feats["approval_to_action_comet"] = feats["comet_approvals"] / feats["qualifying_txs"]
    return feats.reset_index()


def apply_bot_flags(feats: pd.DataFrame) -> pd.DataFrame:
    out = feats.copy()
    tmp = out.assign(protocol="compound", action_type="supply",
                     action_count=out["qualifying_txs"])
    out["is_bot_3sigma"] = flag_bots(tmp, sd_threshold=3.0)["is_bot"].to_numpy()

    # no route-length analog for lending, so >=2 of the remaining 3 signals
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


def classify_approvals(appr: pd.DataFrame) -> pd.DataFrame:
    out = appr.copy()
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


def outstanding_allow(allow: pd.DataFrame) -> pd.DataFrame:
    """Latest allow() per (wallet, market, manager) as of window end. Unlike
    Permit2 sub-grants these never expire and the fetch reaches Comet genesis,
    so this outstanding view is EXACT, not a bound."""
    hist = allow[allow["block_time"] < WINDOW_END].sort_values("block_time")
    latest = hist.groupby(["wallet", "market", "manager"], as_index=False).last()
    latest["outstanding"] = latest["is_grant"]
    return latest


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def run() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pd.set_option("display.width", 200, "display.max_columns", 40)

    print("Loading from BigQuery ...")
    tx = load_supply_txs()
    wd = load_withdraw_txs()
    appr = classify_approvals(load_approvals())
    allow = load_allow()
    print(f"  {len(tx):,} supply txs | {len(wd):,} withdraw txs | "
          f"{len(appr):,} ERC-20 approvals | {len(allow):,} allow events")

    tx["is_entry"] = tx["tx_to"].isin(ENTRY_SET)
    tx["is_bulker"] = tx["tx_to"] == BULKER
    # base assets are 6-decimal stables, so raw/1e6 is USD-equivalent
    tx["amount_usd"] = tx["amount_raw_sum"].astype(float) / 1e6

    feats = apply_bot_flags(build_wallet_features(tx, appr, allow))

    # ------------------------------------------------------------ population
    h("1. POPULATION — supply flow into the stable Comets")
    qual = tx[tx["is_entry"]]
    print(f"  supply txs (broad)      {len(tx):>7,}  wallets {tx['wallet'].nunique():>6,}")
    print(f"  QUALIFYING (entry set)  {len(qual):>7,}  wallets {qual['wallet'].nunique():>6,}"
          f"   [{len(qual) / len(tx):.1%} of txs]")
    print(f"    via Comet direct      {int((qual['tx_to'].isin(STABLE_COMETS)).sum()):>7,}")
    print(f"    via MainnetBulker     {int(qual['is_bulker'].sum()):>7,}")
    print(f"  contrast (managers)     {len(tx) - len(qual):>7,}  wallets "
          f"{tx.loc[~tx['is_entry'], 'wallet'].nunique():>6,}")
    print(f"\n  market mix (qualifying): "
          + "  ".join(f"{m}={n:,}" for m, n in qual["market"].value_counts().items()))
    print(f"  on-behalf supplies (dst != signer): {qual['on_behalf'].mean():.1%} of qualifying txs")

    print("\n  Top contrast entry contracts (stored, not studied):")
    nb = (tx[~tx["is_entry"]].groupby("tx_to")
          .agg(txs=("tx_hash", "size"), wallets=("wallet", "nunique")))
    for addr, r in nb.sort_values("txs", ascending=False).head(8).iterrows():
        print(f"    {str(addr)[:12]}… {label(addr):<28} {r.txs:>6,} txs {r.wallets:>6,} wallets")

    # ------------------------------------------------------------------ bots
    h("2. BOT PARTITION (shared thresholds; no route-length signal in lending)")
    n_bot = int(feats["is_bot"].sum())
    print(f"  {n_bot:,} of {len(feats):,} qualifying wallets flagged ({n_bot / len(feats):.1%}) — "
          f"{feats.loc[feats['is_bot'], 'qualifying_txs'].sum() / feats['qualifying_txs'].sum():.1%} of qualifying txs.")
    for c in ["private_orderflow", "high_frequency", "approval_spam"]:
        print(f"  {c:<22}{int(feats[c].sum()):>7,}")
    print(f"  {'3-sigma (plan rule)':<22}{int(feats['is_bot_3sigma'].sum()):>7,}")
    human = feats[~feats["is_bot"]].copy()
    human_set = set(human["wallet"])

    # -------------------------------------------------------------- deciles
    h("3. ACTIVITY STRUCTURE — lending is a repeat relationship")
    d = human.assign(protocol="compound", action_type="supply",
                     action_count=human["qualifying_txs"])
    one_shot = (d["action_count"] == 1).mean()
    d = assign_deciles(d)
    dm = d.groupby("decile").agg(
        wallets=("wallet", "size"),
        median_txs=("action_count", "median"),
        max_txs=("action_count", "max"),
        median_active_months=("active_months", "median"),
        median_ratio_comet=("approval_to_action_comet", "median"),
        allow_holders=("allow_grants_ever", lambda s: (s > 0).mean()),
    )
    print(f"  non-bot wallets: {len(d):,}; one-shot share {one_shot:.1%}; "
          f"{len(dm)} of 10 deciles non-empty\n")
    print(dm.to_string(float_format=lambda x: f"{x:,.3f}"))

    # ---------------------------------------------------- approval-to-action
    h("4. APPROVAL-TO-ACTION — ERC-20 layer (amount) and allow() layer (binary)")
    print(f"  Non-bot wallets: {len(human):,}\n")
    r = human["approval_to_action_comet"]
    print("  (a) ERC-20 MATCHED — stable approvals to a Comet / qualifying supplies")
    print(f"      median={r.median():.3f}  p90={r.quantile(.9):.3f}  "
          f"share==0: {(r == 0).mean():.1%}  share>=1: {(r >= 1).mean():.1%}")
    coasting = (human["comet_approvals"] == 0) & (human["comet_approvals_prewindow"] > 0)
    print(f"      of the zero-in-window wallets, {int(coasting.sum()):,} "
          f"({coasting.sum() / max((r == 0).sum(), 1):.1%}) hold a PRE-WINDOW Comet approval")
    print("\n  (b) allow() layer — operator grants are not per-action; holders:")
    holders = (human["allow_grants_ever"] > 0)
    print(f"      {int(holders.sum()):,} of {len(human):,} non-bot wallets "
          f"({holders.mean():.1%}) have EVER granted an operator")
    r2 = human["approval_to_action_all"]
    print("\n  (c) UNMATCHED — all stable approvals (any spender) / supplies")
    print(f"      median={r2.median():.3f}  p90={r2.quantile(.9):.3f}")
    print("\n  Cross-arm matched medians: Sushi 1.000 | Uniswap 1.000 | Compound "
          f"{r.median():.3f}")

    # ----------------------------------------------------- approval type mix
    h("5. APPROVAL TYPE MIX — what amount does the Compound flow request?")
    win = appr[(appr["block_time"] >= WINDOW_START) & (appr["block_time"] < WINDOW_END)]
    for name, sub in [
        ("ERC-20, non-bot, all spenders", win[win["wallet"].isin(human_set)]),
        ("ERC-20, non-bot, spender = a Comet", win[win["wallet"].isin(human_set) & win["spender"].isin(STABLE_COMETS)]),
    ]:
        if len(sub) == 0:
            continue
        mix = sub["approval_type"].value_counts(normalize=True)
        print(f"  {name:<38} n={len(sub):>6,}  " + "  ".join(
            f"{k}={mix.get(k, 0):.1%}" for k in ["unlimited", "exact", "revocation"]))
    comet_nz = win[win["wallet"].isin(human_set) & win["spender"].isin(STABLE_COMETS)
                   & (win["approval_type"] != "revocation")]
    per_w = (comet_nz.assign(u=comet_nz["approval_type"] == "unlimited")
             .groupby("wallet")["u"].max())
    print(f"\n  Comet spender, wallet-weighted ever-unlimited: {per_w.mean():.1%} of "
          f"{len(per_w):,} wallets")
    print("  (cross-arm: Sushi routers 20.5% | Permit2 91.6% — where does Compound sit?)")

    # ----------------------------------------------------------- allow layer
    h("6. THE allow() LAYER — binary, no amount choice exists")
    aw = allow[allow["block_time"] < WINDOW_END]
    print(f"  {len(aw):,} events since Comet genesis; grants "
          f"{int(aw['is_grant'].sum()):,} / revokes {int(aw['is_revoke'].sum()):,} — "
          "every one max-or-zero (gate-verified).")
    grants = aw[aw["is_grant"]]
    print("\n  Top managers granted operator control (all owners, since genesis):")
    tm = grants.groupby("manager").agg(grants_=("wallet", "size"), wallets=("wallet", "nunique"))
    for addr, r_ in tm.nlargest(8, "wallets").iterrows():
        print(f"    {str(addr)[:12]}… {label(addr):<28} {int(r_.wallets):>6,} wallets")

    oa = outstanding_allow(allow)
    oa_roster = oa[oa["wallet"].isin(human_set)]
    live = oa_roster[oa_roster["outstanding"]]
    print(f"\n  Roster (non-bot) outstanding at window end — EXACT (no expiry, genesis lookback):")
    print(f"    live (wallet, market, manager) grants : {len(live):,} across "
          f"{live['wallet'].nunique():,} wallets")
    print(f"    ...to the MainnetBulker               : {int((live['manager'] == BULKER).sum()):,}")
    print(f"    triples ending in a revoke            : {len(oa_roster) - len(live):,} "
          f"({(len(oa_roster) - len(live)) / max(len(oa_roster), 1):.1%})")

    bulker_wallets = set(human.loc[human["bulker_txs"] > 0, "wallet"])
    bulker_allowed = grants[grants["manager"] == BULKER]["wallet"].unique()
    covered = len(bulker_wallets & set(bulker_allowed))
    print(f"\n  Bulker check: {covered:,} of {len(bulker_wallets):,} non-bot Bulker-entry "
          f"wallets hold an allow(Bulker) grant\n  — the mandated full-control permission; "
          "any gap is smart-account/manager-signed flow.")

    # ---------------------------------------------------------- outstanding
    h("7. OUTSTANDING ERC-20 ALLOWANCE AT END OF PERIOD (2026-02-28)")
    out = outstanding_allowances(appr)
    out_h = out[out["wallet"].isin(human_set)]
    live_e = out_h[out_h["outstanding"]]
    print(f"  Non-bot wallets with >=1 live allowance : {live_e['wallet'].nunique():,}")
    print(f"  Live (wallet, token, spender) triples   : {len(live_e):,} "
          f"({live_e['is_unlimited'].mean():.1%} unlimited)")
    live_c = live_e[live_e["spender"].isin(STABLE_COMETS)]
    print(f"  ...to a Comet                           : {len(live_c):,} "
          f"({live_c['is_unlimited'].mean() if len(live_c) else 0:.1%} unlimited)")
    print("\n  Top spenders holding live allowances from non-bot wallets:")
    ts = live_e.groupby("spender").agg(wallets=("wallet", "nunique"),
                                       unlimited=("is_unlimited", "mean"))
    for addr, r_ in ts.nlargest(8, "wallets").iterrows():
        print(f"    {str(addr)[:12]}… {label(addr):<28} {int(r_.wallets):>5,} wallets  "
              f"{r_.unlimited:.0%} unlimited")

    # ------------------------------------------------------------------ gas
    h("8. PERMISSION GAS — Compound users pay for approve() AND allow()")
    ga = human["comet_approval_gas_eth"].sum()
    gl = human["allow_gas_eth"].sum()
    gs = human["supply_gas_eth"].sum()
    print(f"  non-bot Comet-spender approval gas (window) : {fmt_eth(ga)}")
    print(f"  non-bot allow() gas (window)                : {fmt_eth(gl)}")
    print(f"  non-bot allow() gas since genesis (stock)   : "
          f"{fmt_eth(human['allow_gas_eth_ever'].sum())}")
    print(f"  qualifying supply gas (window)              : {fmt_eth(gs)}")
    print(f"  scope-matched permission overhead (window)  : {(ga + gl) / gs:.1%}"
          f"   (Sushi 17.1% | Uniswap 10.0%)")

    # USD converted per event at its hour's price BEFORE summing
    ca = win[win["wallet"].isin(human_set) & win["spender"].isin(STABLE_COMETS)].drop_duplicates("tx_hash")
    al_w = allow[(allow["block_time"] >= WINDOW_START) & (allow["block_time"] < WINDOW_END)
                 & allow["wallet"].isin(human_set)].drop_duplicates("tx_hash")
    sup_h = qual[qual["wallet"].isin(human_set)]
    ga_u, gl_u, gs_u = gas_usd(ca).sum(), gas_usd(al_w).sum(), gas_usd(sup_h).sum()
    print(f"  USD (hourly Coinbase close, window): approvals ${ga_u:,.0f} + "
          f"allow ${gl_u:,.0f} vs supplies ${gs_u:,.0f} -> {(ga_u + gl_u) / gs_u:.1%}")

    # -------------------------------------------------------------- monthly
    h("9. MONTHLY")
    qual_h = qual[qual["wallet"].isin(human_set)].copy()
    qual_h["month"] = qual_h["block_time"].dt.to_period("M").astype(str)
    win_h = win[win["wallet"].isin(human_set)].copy()
    win_h["month"] = win_h["block_time"].dt.to_period("M").astype(str)
    al_w = allow[(allow["block_time"] >= WINDOW_START) & (allow["block_time"] < WINDOW_END)].copy()
    al_w["month"] = al_w["block_time"].dt.to_period("M").astype(str)
    m = pd.DataFrame({
        "supplies": qual_h.groupby("month").size(),
        "supply_wallets": qual_h.groupby("month")["wallet"].nunique(),
        "erc20_approvals": win_h.groupby("month").size(),
        "erc20_unlimited": win_h[win_h["approval_type"] == "unlimited"].groupby("month").size(),
        "allow_grants": al_w[al_w["is_grant"]].groupby("month").size(),
        "allow_revokes": al_w[al_w["is_revoke"]].groupby("month").size(),
    }).fillna(0).astype(int)
    print(m.to_string())
    m.to_csv(OUT_DIR / "compound_monthly.csv")

    # ------------------------------------------------ default vs deliberate
    h("10. DEFAULT OR DELIBERATE? — replication no. 3")
    dvd = default_vs_deliberate(win, human_set)
    if len(dvd) == 0:
        print("  No spender pair reaches the 20-wallet threshold in this arm's roster —")
        print("  1,898 wallets is thin for within-wallet pairs; the cross-arm pooled test")
        print("  (deferred item 1) is where this population contributes.")
    for _, r_ in dvd.iterrows():
        print(f"  {r_.spender_a} ({r_.unlimited_a:.0%} unlimited)  vs  "
              f"{r_.spender_b} ({r_.unlimited_b:.0%} unlimited)")
        print(f"    n={r_.pairs:,} pairs   discordance {r_.a_unl_b_exact}:{r_.b_unl_a_exact}")
    if len(dvd):
        dvd.to_csv(OUT_DIR / "compound_default_vs_deliberate.csv", index=False)

    # ------------------------------------------------------------ withdraws
    h("11. WITHDRAW SIDE — bounds only (borrow = negative-balance Withdraw)")
    wd["is_entry"] = wd["tx_to"].isin(ENTRY_SET)
    wdq = wd[wd["is_entry"]]
    suppliers = set(qual["wallet"])
    wd_only = wdq[~wdq["wallet"].isin(suppliers)]
    print(f"  entry-anchored withdraw txs (any wallet)       : {len(wdq):,} "
          f"({wdq['wallet'].nunique():,} wallets)")
    print(f"  ...by in-window suppliers (roster + bots)      : "
          f"{len(wdq) - len(wd_only):,}")
    print(f"  ...by wallets with NO in-window supply         : {len(wd_only):,} "
          f"({wd_only['wallet'].nunique():,} wallets) — borrow-shaped or pre-window")
    print("  depositors; separating them needs pre-window balances (deferred, one")
    print("  extra aggregate query if the borrow arm is pursued).")
    print(textwrap.dedent("""
      The study's approval mechanics live on the SUPPLY side (borrows receive
      tokens; no ERC-20 approval). Withdraws are context, not population."""))

    # ------------------------------------------------------------------ save
    feats.to_csv(OUT_DIR / "compound_wallet_features.csv", index=False)
    dm.to_csv(OUT_DIR / "compound_decile_metrics.csv")
    out.to_csv(OUT_DIR / "compound_outstanding_allowances.csv", index=False)
    oa.to_csv(OUT_DIR / "compound_outstanding_allow.csv", index=False)
    print(f"\nWrote CSVs to {OUT_DIR}/ (compound_* prefixed)")


if __name__ == "__main__":
    run()
