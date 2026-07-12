"""In-depth analysis of the Uniswap arm (`dex-research.uniswap_v4`).

The `sushi_analysis` counterpart, harmonized so the two arms' numbers are
directly comparable. Structural differences that shape everything below:

  1. The study population is router-entry BY CONSTRUCTION (tx_to anchored at
     fetch), so there is no pool-touch contrast cohort here — the broad layer
     is November-scoped and analyzed separately.
  2. Uniswap's approval system is TWO-LAYER: an ERC-20 approve() to Permit2
     (or directly to a legacy router), then Permit2 sub-approvals
     (permit/approval, uint160, with an expiry we did not store) gating each
     spender. Approval-to-action must be reported per layer, never summed.
  3. V4 is a measured per-leg attribute, never a population filter
     (conditioning on V4 execution = selecting on the router's routing choice).

Table note: `swaps_sampled` is named for its stage-2 origin but holds the
COMPLETE router-entry population post-topup (verified vs Dune funnel 7923299).
`approvals` has the 2022-11 lookback; `permit2_events` are window-only.

Bot thresholds, the allowance classifier and the default-vs-deliberate test
are imported from the Sushi module so the arms cannot silently diverge.

Run with:
    python -m dexresearch.process.uniswap_analysis
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
from dexresearch.process.sushi_analysis import (
    BOT_APPROVALS_PER_ACTIVE_DAY,
    BOT_MAX_LEGS,
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

DATASET = "dex-research.uniswap_v4"
OUT_DIR = Path("data/analysis")

# Resolved 2026-07-09 via contracts.contract_mapping (see uniswap_arm_audit.md;
# Dune mislabels V2 Router02 as "UniswapV2Factory").
UNISWAP_ROUTERS = {
    "0x66a9893cc07d91d95644aedd05d03f95e1dba8af": "UniversalRouter (current)",
    "0xe592427a0aece92de3edee1f18e0157c05861564": "SwapRouter (V3)",
    "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45": "SwapRouter02",
    "0x7a250d5630b4cf539739df2c5dacb4c659f2488d": "V2 Router02",
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad": "UniversalRouter (2023)",
    "0xef1c6e67703c7bd7107eed8303fbe6ec2554bf6b": "UniversalRouter (2022)",
}
PERMIT2 = "0x000000000022d473030f116ddee9f6b43ac78ba3"
# The Uniswap approval path at the ERC-20 layer: Permit2 (modern flow) or a
# router approved directly (legacy V2/V3 flow). Matched metrics use this set.
UNI_PATH_SPENDERS = set(UNISWAP_ROUTERS) | {PERMIT2}

# Permit2 amounts are uint160. The Uniswap interface signs max-uint160 permits;
# coalesce near-max the same way the ERC-20 layer coalesces near-uint256-max.
UINT160_NEAR_MAX = 2**159

P2_GRANTS = {"permit2_permit", "permit2_approval"}          # standing sub-allowances
P2_TRANSFERS = {"permit2_signature_transfer", "permit2_signature_transfer_witness"}

# Fetch-time gates (uniswap_arm_audit.md, Dune funnel 7923299). The module
# refuses to report on a population that drifted from the verified one.
GATES = {
    "txs": 478_299,
    "wallets": 73_039,
    "legs": 735_223,
    "v4_any_txs": 139_195,
    "v4_first_txs": 116_721,
}


def _client() -> bigquery.Client:
    return bigquery.Client(project="dex-research")


def _q(sql: str) -> pd.DataFrame:
    return _client().query(sql).to_dataframe()


def label(addr: str) -> str:
    if addr is None:
        return "(contract creation / null)"
    if addr in UNISWAP_ROUTERS:
        return f"Uniswap {UNISWAP_ROUTERS[addr]}"
    if addr == PERMIT2:
        return "Uniswap Permit2"
    return KNOWN_CONTRACTS.get(addr, "unknown")


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

def load_tx_level() -> pd.DataFrame:
    """One row per qualifying transaction, with routing + gas + version flags.

    Tables are leg-level; gas columns are tx-level so MAX() is a dedupe.
    Version flags mirror the funnel definitions: v4_any = any leg on a V4
    pool, v4_first = the qualifying (first) leg itself executed on V4.
    """
    tx = _q(f"""
        SELECT
          tx_hash,
          ANY_VALUE(wallet)                    AS wallet,
          MIN(block_time)                      AS block_time,
          ANY_VALUE(tx_to)                     AS tx_to,
          COUNT(*)                             AS legs,
          MAX(gas_used)                        AS gas_used,
          MAX(gas_cost_eth)                    AS gas_cost_eth,
          MAX(max_priority_fee_per_gas)        AS max_priority_fee_per_gas,
          ANY_VALUE(method_id)                 AS method_id,
          # first leg's notional = what the user sold; summing legs would
          # count every hop of a multi-hop route as fresh volume
          ARRAY_AGG(amount_usd    ORDER BY evt_index LIMIT 1)[OFFSET(0)] AS amount_usd,
          ARRAY_AGG(token_symbol  ORDER BY evt_index LIMIT 1)[OFFSET(0)] AS first_token,
          ARRAY_AGG(counter_symbol ORDER BY evt_index LIMIT 1)[OFFSET(0)] AS first_counter,
          ARRAY_AGG(project       ORDER BY evt_index LIMIT 1)[OFFSET(0)] AS first_project,
          ARRAY_AGG(version       ORDER BY evt_index LIMIT 1)[OFFSET(0)] AS first_version,
          LOGICAL_OR(project = "uniswap" AND version = "4") AS v4_any,
          LOGICAL_OR(project = "uniswap" AND version = "3") AS v3_any,
          LOGICAL_OR(project = "uniswap" AND version = "2") AS v2_any
        FROM `{DATASET}.swaps_sampled`
        GROUP BY tx_hash
    """)
    tx["v4_first"] = (tx["first_project"] == "uniswap") & (tx["first_version"] == "4")
    return tx


def load_approvals() -> pd.DataFrame:
    """ERC-20 approve() events, any spender, lookback to Permit2 genesis.
    amount_raw stays a string — uint256 overflows int64."""
    return _q(f"""
        SELECT wallet, block_time, block_number, tx_hash, token_symbol,
               counterparty AS spender, amount_raw, is_revoke,
               gas_used, gas_price, gas_cost_eth, max_priority_fee_per_gas,
               base_fee_per_gas, priority_fee_per_gas, eth_usd, gas_cost_usd
        FROM `{DATASET}.approvals_usd`
    """)


def load_permit2() -> pd.DataFrame:
    return _q(f"""
        SELECT record_type, wallet, block_time, tx_hash, token_symbol,
               counterparty, amount_raw, is_revoke, gas_cost_eth
        FROM `{DATASET}.permit2_events`
    """)


def validate_gates(tx: pd.DataFrame) -> None:
    """Hard gates: population counts vs the fetch-time Dune funnel, and a
    per-wallet reconciliation against the independently-fetched aggregates."""
    got = {
        "txs": len(tx),
        "wallets": tx["wallet"].nunique(),
        "legs": int(tx["legs"].sum()),
        "v4_any_txs": int(tx["v4_any"].sum()),
        "v4_first_txs": int(tx["v4_first"].sum()),
    }
    bad = {k: (got[k], v) for k, v in GATES.items() if got[k] != v}
    if bad:
        raise RuntimeError(f"population gate FAILED: {bad}")

    mism = _q(f"""
        WITH agg AS (
          SELECT wallet, SUM(qualifying_txs) AS qt, SUM(sum_legs) AS sl
          FROM `{DATASET}.wallet_aggregates` GROUP BY wallet
        ),
        txl AS (
          SELECT wallet, COUNT(DISTINCT tx_hash) AS qt, COUNT(*) AS sl
          FROM `{DATASET}.swaps_sampled` GROUP BY wallet
        )
        SELECT COUNT(*) AS n FROM agg FULL JOIN txl USING (wallet)
        WHERE agg.qt IS DISTINCT FROM txl.qt OR agg.sl IS DISTINCT FROM txl.sl
    """)["n"].iloc[0]
    if mism:
        raise RuntimeError(f"{mism} wallets disagree with wallet_aggregates")
    print(f"  gates OK: {got['txs']:,} txs / {got['wallets']:,} wallets / "
          f"{got['legs']:,} legs; per-wallet aggregates reconcile exactly")


# --------------------------------------------------------------------------
# Feature construction
# --------------------------------------------------------------------------

def build_wallet_features(tx: pd.DataFrame, appr: pd.DataFrame,
                          p2: pd.DataFrame) -> pd.DataFrame:
    tx = tx.copy()
    tx["day"] = tx["block_time"].dt.floor("D")

    g = tx.groupby("wallet")
    feats = pd.DataFrame({
        "qualifying_txs": g.size(),
        "v4_any_txs": g["v4_any"].sum(),
        "v4_first_txs": g["v4_first"].sum(),
        "max_legs": g["legs"].max(),
        "mean_legs": g["legs"].mean(),
        "zero_prio_frac": g["max_priority_fee_per_gas"].apply(zero_prio_share),
        "active_days": g["day"].nunique(),
        "distinct_entry_contracts": g["tx_to"].nunique(),
        "swap_gas_eth": g["gas_cost_eth"].sum(),
        "volume_usd": g["amount_usd"].sum(),
        "first_seen": g["block_time"].min(),
        "last_seen": g["block_time"].max(),
    })
    feats["txs_per_active_day"] = feats["qualifying_txs"] / feats["active_days"]

    # ERC-20 layer. Window-scoped for rates; the pre-window flag captures the
    # amortization story (an approval from 2023 still authorizes 2026 swaps).
    in_window = appr[(appr["block_time"] >= WINDOW_START) & (appr["block_time"] < WINDOW_END)]
    ag = in_window.groupby("wallet")
    appr_feats = pd.DataFrame({
        "approvals_in_window": ag.size(),
        "approval_active_days": ag["block_time"].apply(lambda s: s.dt.floor("D").nunique()),
        "distinct_spenders": ag["spender"].nunique(),
    })
    # gas is tx-level but a tx can emit several approve() events — dedupe first
    gas_by_wallet = (
        in_window.drop_duplicates("tx_hash").groupby("wallet")["gas_cost_eth"].sum()
        .rename("approval_gas_eth")
    )

    uni = appr[appr["spender"].isin(UNI_PATH_SPENDERS)]
    uni_win = uni[(uni["block_time"] >= WINDOW_START) & (uni["block_time"] < WINDOW_END)]
    uni_feats = pd.DataFrame({
        "uni_approvals": uni_win.groupby("wallet").size(),
        "uni_approval_gas_eth": (
            uni_win.drop_duplicates("tx_hash").groupby("wallet")["gas_cost_eth"].sum()
        ),
        "uni_approvals_prewindow": (
            uni[uni["block_time"] < WINDOW_START].groupby("wallet").size()
        ),
    })

    # Permit2 layer (window-only by fetch). Grants = standing sub-allowances;
    # signature transfers are per-swap pulls with no persistent exposure.
    p2u = p2[p2["counterparty"].isin(UNISWAP_ROUTERS)]
    p2_feats = pd.DataFrame({
        "p2_router_grants": (
            p2u[p2u["record_type"].isin(P2_GRANTS) & ~p2u["is_revoke"]]
            .groupby("wallet").size()
        ),
        "p2_sig_transfers": p2[p2["record_type"].isin(P2_TRANSFERS)].groupby("wallet").size(),
        "p2_lockdowns": p2[p2["record_type"] == "permit2_lockdown"].groupby("wallet").size(),
    })

    feats = feats.join([appr_feats, gas_by_wallet, uni_feats, p2_feats])
    for c in ["approvals_in_window", "approval_active_days", "distinct_spenders",
              "approval_gas_eth", "uni_approvals", "uni_approval_gas_eth",
              "uni_approvals_prewindow", "p2_router_grants", "p2_sig_transfers",
              "p2_lockdowns"]:
        feats[c] = feats[c].fillna(0)

    feats["approvals_per_active_day"] = np.where(
        feats["approval_active_days"] > 0,
        feats["approvals_in_window"] / feats["approval_active_days"].replace(0, np.nan),
        0.0,
    )

    # The plan's ratio, per layer. `_all` counts every spender (inflated by a
    # wallet's 1inch/CoW approvals); `_uni` matches the Uniswap ERC-20 path
    # (Permit2 or a router) to qualifying swaps; `_p2` counts Permit2
    # sub-grants to Uniswap routers per swap. Only the matched ones test the
    # arm's approval mechanics; never sum layers (one flow produces both).
    feats["approval_to_action_all"] = feats["approvals_in_window"] / feats["qualifying_txs"]
    feats["approval_to_action_uni"] = feats["uni_approvals"] / feats["qualifying_txs"]
    feats["grant_to_action_p2"] = feats["p2_router_grants"] / feats["qualifying_txs"]
    return feats.reset_index()


def apply_bot_flags(feats: pd.DataFrame) -> pd.DataFrame:
    out = feats.copy()
    tmp = out.assign(protocol="uniswap", action_type="swap",
                     action_count=out["qualifying_txs"])
    out["is_bot_3sigma"] = flag_bots(tmp, sd_threshold=3.0)["is_bot"].to_numpy()

    sig = pd.DataFrame({
        "private_orderflow": out["zero_prio_frac"] >= BOT_ZERO_PRIO_FRAC,
        "long_routes": out["max_legs"] >= BOT_MAX_LEGS,
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


def classify_permit2_grants(p2: pd.DataFrame) -> pd.DataFrame:
    """Grant records only — transfers are not allowances and lockdowns carry
    no amount. uint160 semantics, mirroring the ERC-20 near-max coalescing."""
    g = p2[p2["record_type"].isin(P2_GRANTS)].copy()
    amt = g["amount_raw"].map(int)
    g["approval_type"] = np.select(
        [g["is_revoke"], amt >= UINT160_NEAR_MAX], ["revocation", "unlimited"], "exact")
    return g


def outstanding_allowances(appr: pd.DataFrame) -> pd.DataFrame:
    """Latest ERC-20 approval per (wallet, token, spender) as of window end,
    over the full 2022-11 lookback. The Permit2 layer is NOT reported this way:
    its events are window-only AND sub-grants expire (expiry not stored), so a
    'live grant' claim would overstate. The ERC-20 layer is the root exposure —
    a max approve() to Permit2 outlives every sub-grant."""
    hist = appr[appr["block_time"] < WINDOW_END].sort_values("block_time")
    latest = hist.groupby(["wallet", "token_symbol", "spender"], as_index=False).last()
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
    tx = load_tx_level()
    appr = classify_approvals(load_approvals())
    p2 = load_permit2()
    print(f"  {len(tx):,} qualifying txs | {len(appr):,} ERC-20 approvals | "
          f"{len(p2):,} permit2 events")
    validate_gates(tx)

    feats = apply_bot_flags(build_wallet_features(tx, appr, p2))
    p2g = classify_permit2_grants(p2)

    # ------------------------------------------------------------ population
    h("1. POPULATION — router-entry by construction; V4 is a measured attribute")
    n = len(tx)
    print(f"  {n:,} qualifying txs / {tx['wallet'].nunique():,} wallets / "
          f"{int(tx['legs'].sum()):,} legs  (Nov 2025 – Feb 2026, gates exact)\n")
    for name, mask in [
        ("any leg on a V4 pool", tx["v4_any"]),
        ("FIRST leg on a V4 pool", tx["v4_first"]),
        ("any leg on V3", tx["v3_any"]),
        ("any leg on V2", tx["v2_any"]),
    ]:
        w = tx.loc[mask, "wallet"].nunique()
        print(f"  {name:<26} {int(mask.sum()):>8,} txs ({mask.mean():5.1%})  "
              f"{w:>7,} wallets ({w / tx['wallet'].nunique():5.1%})")
    print(textwrap.dedent("""
      A 'V4 user' is mostly an artifact of the router's routing choice: far
      more wallets touch V4 than choose it, and conditioning the population on
      V4 execution would have selected on that choice (the Sushi pool-anchor
      bug one layer down). Every V4 number downstream is a flag, not a filter."""))

    # --------------------------------------------------------- entry routers
    h("2. ENTRY ROUTER PROFILE")
    rb = (tx.groupby("tx_to").agg(
        txs=("tx_hash", "size"), wallets=("wallet", "nunique"),
        mean_legs=("legs", "mean"),
        zero_prio=("max_priority_fee_per_gas", zero_prio_share),
        v4_share=("v4_any", "mean")))
    print(f"  {'router':<28}{'txs':>9}{'wallets':>9}{'txs/w':>8}{'legs':>6}{'zeroP':>7}{'v4':>6}")
    for addr, r in rb.sort_values("txs", ascending=False).iterrows():
        print(f"  {UNISWAP_ROUTERS[addr]:<28}{r.txs:>9,}{r.wallets:>9,}"
              f"{r.txs / r.wallets:>8.1f}{r.mean_legs:>6.1f}{r.zero_prio:>7.0%}{r.v4_share:>6.0%}")
    rb.to_csv(OUT_DIR / "uniswap_entry_routers.csv")

    # ------------------------------------------------------------------ bots
    h("3. BOT / MEV-SEARCHER PARTITION (same rules as the Sushi arm)")
    n_bot = int(feats["is_bot"].sum())
    print(f"  {n_bot:,} of {len(feats):,} wallets flagged ({n_bot / len(feats):.1%}) — "
          f"{feats.loc[feats['is_bot'], 'qualifying_txs'].sum() / feats['qualifying_txs'].sum():.1%} "
          f"of qualifying txs, "
          f"{feats.loc[feats['is_bot'], 'approvals_in_window'].sum() / max(feats['approvals_in_window'].sum(), 1):.1%} "
          f"of ERC-20 approvals.")
    print(f"\n  {'signal':<22}{'wallets':>9}")
    for c in ["private_orderflow", "long_routes", "high_frequency", "approval_spam"]:
        print(f"  {c:<22}{int(feats[c].sum()):>9,}")
    print(f"  {'3-sigma (plan rule)':<22}{int(feats['is_bot_3sigma'].sum()):>9,}")
    print(f"  {'behavioral (>=2 sig)':<22}{int(feats['is_bot_behavioral'].sum()):>9,}")
    print(f"  {'sustained appr. spam':<22}{int(feats['is_bot_approval_spam'].sum()):>9,}")

    human = feats[~feats["is_bot"]].copy()
    human_set = set(human["wallet"])

    per_router_bot = (
        tx.assign(is_bot=tx["wallet"].isin(set(feats.loc[feats["is_bot"], "wallet"])))
        .groupby("tx_to")["is_bot"].mean())
    print("\n  Bot share of each router's txs (SwapRouter V3's 854-wallet flow):")
    for addr, share in per_router_bot.sort_values(ascending=False).items():
        print(f"    {UNISWAP_ROUTERS[addr]:<28} {share:6.1%}")

    # --------------------------------------------------------------- deciles
    h("4. DECILES — the analysis Sushi could not support")
    d = human.assign(protocol="uniswap", action_type="swap",
                     action_count=human["qualifying_txs"])
    one_shot = (d["action_count"] == 1).mean()
    d = assign_deciles(d)
    dm = d.groupby("decile").agg(
        wallets=("wallet", "size"),
        median_txs=("action_count", "median"),
        max_txs=("action_count", "max"),
        median_approvals=("approvals_in_window", "median"),
        median_ratio_uni=("approval_to_action_uni", "median"),
        median_grants_p2=("grant_to_action_p2", "median"),
        ever_v4=("v4_any_txs", lambda s: (s > 0).mean()),
        approval_gas_eth=("approval_gas_eth", "sum"),
    )
    print(f"  non-bot wallets: {len(d):,}; one-shot share {one_shot:.1%}; "
          f"{len(dm)} of 10 deciles non-empty\n")
    print(dm.to_string(float_format=lambda x: f"{x:,.3f}"))
    print(textwrap.dedent("""
      The plan's coasting prediction reads off the two ratio columns: if
      approvals amortize, both fall with activity. One-shot wallets pay the
      setup cost per swap by definition; heavy wallets should approach zero."""))
    dm.to_csv(OUT_DIR / "uniswap_decile_metrics.csv")

    # ---------------------------------------------------- approval-to-action
    h("5. APPROVAL-TO-ACTION — two layers, reported separately")
    print(f"  Non-bot wallets: {len(human):,}\n")
    print("  (a) ERC-20 MATCHED — approvals to Permit2/routers / qualifying swaps")
    r = human["approval_to_action_uni"]
    print(f"      median={r.median():.3f}  p90={r.quantile(.9):.3f}  "
          f"share==0: {(r == 0).mean():.1%}  share>=1: {(r >= 1).mean():.1%}")
    coasting = (human["uni_approvals"] == 0) & (human["uni_approvals_prewindow"] > 0)
    print(f"      of the zero-in-window wallets, {coasting.sum():,} "
          f"({coasting.sum() / max((r == 0).sum(), 1):.1%}) hold a PRE-WINDOW "
          f"Uniswap-path approval\n      (lookback to 2022-11) — coasting, not missing data.")
    print("\n  (b) PERMIT2 GRANTS — sub-approvals to Uniswap routers / qualifying swaps")
    r2 = human["grant_to_action_p2"]
    print(f"      median={r2.median():.3f}  p90={r2.quantile(.9):.3f}  "
          f"share==0: {(r2 == 0).mean():.1%}")
    print("\n  (c) UNMATCHED — all ERC-20 approvals (any spender) / qualifying swaps")
    r3 = human["approval_to_action_all"]
    print(f"      median={r3.median():.3f}  p90={r3.quantile(.9):.3f}   "
          f"(inflated: credits 1inch/CoW approvals to Uniswap)")
    print(textwrap.dedent(f"""
      Layers must not be summed: one modern flow emits an ERC-20 approve to
      Permit2 AND a Permit2 permit. The Sushi matched median is 1.000
      (exact-amount default, one-shot users); (a) is the comparable figure.
      Permit2 grants expire (expiry not stored), so (b) counts re-grants as
      well as first grants — an upper bound on standing-allowance creation.
      Sig-transfer pulls (no standing allowance): {int(human['p2_sig_transfers'].sum()):,}
      events across {int((human['p2_sig_transfers'] > 0).sum()):,} non-bot wallets."""))

    # ----------------------------------------------------- approval type mix
    h("6. APPROVAL TYPE MIX — ERC-20 layer vs Permit2 layer")
    win = appr[(appr["block_time"] >= WINDOW_START) & (appr["block_time"] < WINDOW_END)]
    for name, sub in [
        ("ERC-20, non-bot, all spenders", win[win["wallet"].isin(human_set)]),
        ("ERC-20, non-bot, spender = Permit2", win[win["wallet"].isin(human_set) & (win["spender"] == PERMIT2)]),
        ("ERC-20, non-bot, spender = a router", win[win["wallet"].isin(human_set) & win["spender"].isin(UNISWAP_ROUTERS)]),
        ("Permit2 grants, non-bot, to routers", p2g[p2g["wallet"].isin(human_set) & p2g["counterparty"].isin(UNISWAP_ROUTERS)]),
    ]:
        if len(sub) == 0:
            continue
        mix = sub["approval_type"].value_counts(normalize=True)
        print(f"  {name:<40} n={len(sub):>7,}  " + "  ".join(
            f"{k}={mix.get(k, 0):.1%}" for k in ["unlimited", "exact", "revocation"]))

    # wallet-weighted: repeat exact-approvers dominate event counts
    for name, sub in [
        ("ERC-20 -> Permit2", win[win["wallet"].isin(human_set) & (win["spender"] == PERMIT2)
                                  & (win["approval_type"] != "revocation")]),
        ("Permit2 -> routers", p2g[p2g["wallet"].isin(human_set)
                                   & p2g["counterparty"].isin(UNISWAP_ROUTERS)
                                   & (p2g["approval_type"] != "revocation")]),
    ]:
        per_w = (sub.assign(u=sub["approval_type"] == "unlimited")
                 .groupby("wallet")["u"].max())
        print(f"\n  {name}: wallet-weighted ever-unlimited "
              f"{per_w.mean():.1%} of {len(per_w):,} wallets"
              f"  (Sushi routers: 20.5% — the cross-arm contrast)")

    # ---------------------------------------------------------- outstanding
    h("7. OUTSTANDING ERC-20 ALLOWANCE AT END OF PERIOD (2026-02-28)")
    out = outstanding_allowances(appr)
    out_h = out[out["wallet"].isin(human_set)]
    live = out_h[out_h["outstanding"]]
    print(f"  Non-bot wallets with >=1 live allowance : {live['wallet'].nunique():,}")
    print(f"  Live (wallet, token, spender) triples   : {len(live):,}")
    print(f"  ...of which UNLIMITED                   : {int(live['is_unlimited'].sum()):,} "
          f"({live['is_unlimited'].mean():.1%})")
    live_p2 = live[live["spender"] == PERMIT2]
    print(f"  ...to Permit2 specifically              : {len(live_p2):,} "
          f"({live_p2['is_unlimited'].mean() if len(live_p2) else 0:.1%} unlimited)")
    revoked = out_h[~out_h["outstanding"]]
    print(f"  Triples whose last action was a REVOKE  : {len(revoked):,} "
          f"({len(revoked) / max(len(out_h), 1):.1%})")
    ld = int(feats.loc[~feats["is_bot"], "p2_lockdowns"].sum())
    print(f"  Permit2 Lockdown events (window, non-bot): {ld:,} — revocations INSIDE "
          f"Permit2\n    that leave the ERC-20 approval to Permit2 itself standing.")
    print(textwrap.dedent("""
      The live-to-Permit2 unlimited triples are the arm's persistent-exposure
      number: the sub-grant layer expires, the root ERC-20 approval does not.
      Exact triples overstate as in the Sushi arm (spend emits no event)."""))
    print("\n  Top spenders holding live allowances from non-bot wallets:")
    ts = live.groupby("spender").agg(triples=("wallet", "size"),
                                     wallets=("wallet", "nunique"),
                                     unlimited=("is_unlimited", "mean"))
    for addr, r in ts.nlargest(10, "wallets").iterrows():
        print(f"    {str(addr)[:12]}… {label(addr):<38} {int(r.wallets):>6,} wallets  "
              f"{r.unlimited:.0%} unlimited")
    out.to_csv(OUT_DIR / "uniswap_outstanding_allowances.csv", index=False)

    # ------------------------------------------------------------------ gas
    h("8. APPROVAL GAS COST (ETH + USD at each event's hourly Coinbase close)")
    total_gas = feats["approval_gas_eth"].sum()
    for name, sub in [("all wallets", feats), ("non-bot", human),
                      ("bots", feats[feats["is_bot"]])]:
        tot = sub["approval_gas_eth"].sum()
        payers = max(int((sub["approval_gas_eth"] > 0).sum()), 1)
        print(f"  {name:<12} total={fmt_eth(tot):<16} wallets_paying={payers:>7,}  "
              f"mean/payer={fmt_eth(tot / payers):<14} share={tot / total_gas:.1%}")
    ra = human["uni_approval_gas_eth"].sum()
    rs = human["swap_gas_eth"].sum()
    print(f"\n  Scope-matched (non-bot): Uniswap-path approval gas {fmt_eth(ra)} vs")
    print(f"  router-entry swap gas {fmt_eth(rs)}  ->  {ra / rs:.1%}"
          f"   (Sushi arm: 17.1%)")

    # USD converted per event at its hour's price BEFORE summing
    bots_set = set(feats.loc[feats["is_bot"], "wallet"])
    wa = win.drop_duplicates("tx_hash").copy()
    wa["usd"] = gas_usd(wa)
    print(f"\n  USD (hourly Coinbase close, in-window): "
          f"all=${wa['usd'].sum():,.0f}  "
          f"non-bot=${wa.loc[~wa['wallet'].isin(bots_set), 'usd'].sum():,.0f}  "
          f"bots=${wa.loc[wa['wallet'].isin(bots_set), 'usd'].sum():,.0f}")
    ra_u = wa[~wa["wallet"].isin(bots_set) & wa["spender"].isin(UNI_PATH_SPENDERS)]["usd"].sum()
    rs_u = gas_usd(tx[tx["wallet"].isin(human_set)]).sum()
    print(f"  Scope-matched USD (non-bot): approvals ${ra_u:,.0f} vs "
          f"router-entry swaps ${rs_u:,.0f} -> {ra_u / rs_u:.1%}")

    # -------------------------------------------------------------- monthly
    h("9. MONTHLY")
    winh = win[win["wallet"].isin(human_set)].copy()
    winh["month"] = winh["block_time"].dt.to_period("M").astype(str)
    txh = tx[tx["wallet"].isin(human_set)].copy()
    txh["month"] = txh["block_time"].dt.to_period("M").astype(str)
    p2h = p2g[p2g["wallet"].isin(human_set)].copy()
    p2h["month"] = p2h["block_time"].dt.to_period("M").astype(str)
    m = pd.DataFrame({
        "swaps": txh.groupby("month").size(),
        "v4_first_swaps": txh[txh["v4_first"]].groupby("month").size(),
        "erc20_approvals": winh.groupby("month").size(),
        "erc20_unlimited": winh[winh["approval_type"] == "unlimited"].groupby("month").size(),
        "erc20_revocations": winh[winh["approval_type"] == "revocation"].groupby("month").size(),
        "p2_grants": p2h[p2h["approval_type"] != "revocation"].groupby("month").size(),
    }).fillna(0).astype(int)
    print(m.to_string())
    m.to_csv(OUT_DIR / "uniswap_monthly.csv")

    # ------------------------------------------------ default vs deliberate
    h("10. DEFAULT OR DELIBERATE? — replication on an independent population")
    dvd = default_vs_deliberate(win, human_set)
    print("  Same (wallet, token), two spenders — the Sushi arm's headline test,")
    print("  rerun on 73k Uniswap-router wallets that share zero fetch lineage:\n")
    for _, r in dvd.iterrows():
        print(f"  {r.spender_a} ({r.unlimited_a:.0%} unlimited)  vs  "
              f"{r.spender_b} ({r.unlimited_b:.0%} unlimited)")
        print(f"    n={r.pairs:,} pairs   discordance {r.a_unl_b_exact}:{r.b_unl_a_exact}")
    print(textwrap.dedent("""
      Sushi arm discordances were 93:0 / 365:5 / 216:3. A one-sided repeat here
      means the interface-decides finding generalizes across user populations;
      chain-side evidence only, front-end audit still needed for WHICH default."""))
    dvd.to_csv(OUT_DIR / "uniswap_default_vs_deliberate.csv", index=False)

    # ------------------------------------------------------ heavy traders/V4
    h("11. HEAVY TRADERS — V4 adoption and approval amortization by activity")
    act = human[human["qualifying_txs"] > 0]
    ranked = act.sort_values("qualifying_txs", ascending=False)
    cuts = {
        "top 1%": ranked.head(max(len(ranked) // 100, 1)),
        "top 10%": ranked.head(max(len(ranked) // 10, 1)),
        "bottom 50%": ranked.tail(len(ranked) // 2),
    }
    print(f"  {'cohort':<12}{'wallets':>8}{'med txs':>9}{'ever V4':>9}"
          f"{'med ratio_uni':>15}{'med grants_p2':>15}")
    for name, grp in cuts.items():
        print(f"  {name:<12}{len(grp):>8,}{grp['qualifying_txs'].median():>9.0f}"
              f"{(grp['v4_any_txs'] > 0).mean():>9.1%}"
              f"{grp['approval_to_action_uni'].median():>15.3f}"
              f"{grp['grant_to_action_p2'].median():>15.3f}")

    # ------------------------------------------------------------------ save
    feats.to_csv(OUT_DIR / "uniswap_wallet_features.csv", index=False)
    print(f"\nWrote CSVs to {OUT_DIR}/ (uniswap_* prefixed)")


if __name__ == "__main__":
    run()
