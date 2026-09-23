"""In-depth analysis of the SushiSwap V2 arm (`dex-research.sushiswap_v2`).

Answers three questions the research plan raises:

  1. Who are the wallets that entered through a **SushiSwap router** (as opposed
     to reaching Sushi liquidity through an aggregator), and what is their
     approval behavior? -> `cohorts`, `wallet_metrics`, `decile_metrics`
  2. Which wallets are **automated actors / MEV searchers**, and how do they
     differ? -> `bot_features`, `searcher_contracts`
  3. What does the approval-to-action ratio actually look like once bots are
     partitioned out, and does it match the plan's 1:1 prediction?

Everything runs in BigQuery + pandas. Outputs land in `data/analysis/*.csv`
for downstream use (e.g. a web app), and a report is printed to stdout.

Post-delta (2026-07-09): candidates are `swaps` UNION `swaps_txto_delta`
(router-entry population complete, verified vs the independent Dune funnel)
and approvals come from the `approvals_all` view.

Run with:
    python -m dexresearch.process.sushi_analysis
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

DATASET = "dex-research.sushiswap_v2"
OUT_DIR = Path("data/analysis")

# BigQuery returns TIMESTAMP as tz-aware UTC; naive bounds would not compare.
WINDOW_START = pd.Timestamp("2025-11-01", tz="UTC")
WINDOW_END = pd.Timestamp("2026-03-01", tz="UTC")  # half-open

# SushiSwap entry contracts, resolved against Dune `contracts.contract_mapping`
# (contract_project = 'Sushiswap'/'Sushi'). The `custody_owner` column of
# labels.owner_addresses is NOT usable here — it tags CoW's GPv2Settlement and
# the ERC-4337 EntryPoint as "sushiswap".
SUSHI_ROUTERS = {
    "0xd9e1ce17f2641f24ae83637ab66a2cca9c378b9f": "Router02 (UniswapV2Router02)",
    "0xac4c6e212a361c968f1725b4d055b47e63f80b75": "RedSnwapper",
    "0xe43ca1dee3f0fc1e2df73a0745674545f11a59f5": "RouteProcessor4",
    "0xd2b37ade14708bf18904047b1e31f8166d39612b": "RouteProcessor9_2",
}

# Cosmetic only — used to name the non-Sushi entry contracts in the report.
# Anything unresolved stays `unknown` rather than being guessed at.
KNOWN_CONTRACTS = {
    "0x1231deb6f5749ef6ce6943a275a1d3e7486f4eae": "LI.FI Diamond",
    "0x111111125421ca6dc452d289314280a0f8842a65": "1inch AggregationRouterV6",
    "0x1111111254eeb25477b68fb85ed929f73a960582": "1inch AggregationRouterV5",
    "0x1111111254fb6c44bac0bed2854e76f90643097d": "1inch AggregationRouterV4",
    "0x881d40237659c251811cec9c364ef91dc08d300c": "MetaMask Swaps",
    "0x0000000000001ff3684f28c67538d4d072c22734": "0x AllowanceHolder",
    "0xdef1c0ded9bec7f1a1670819833240f027b25eff": "0x ExchangeProxy",
    "0x9008d19f58aabd9ed0d60971565aa8510560ab41": "CoW GPv2Settlement",
    "0xc92e8bdf79f0507f65a392b0ab4667716bfe0110": "CoW GPv2VaultRelayer",
    "0x000000000022d473030f116ddee9f6b43ac78ba3": "Uniswap Permit2",
    "0xdef171fe48cf0115b1d80b88dc8eab59176fee57": "ParaSwap AugustusSwapper",
    "0x6a000f20005980200259b80c5102003040001068": "ParaSwap AugustusV6.2",
    "0x216b4b4ba9f3e719726886d34a177484278bfcae": "ParaSwap TokenTransferProxy",
    "0x6131b5fae19ea4f9d964eac0408e4408b66337b5": "KyberSwap MetaAggregatorV2",
    "0xcf5540fffcdc3d510b18bfca6d2b9987b0772559": "Odos RouterV2",
    "0x40aa958dd87fc8305b97f2ba922cddca374bcd7f": "OKX TokenApprove",
    "0x2e1dee213ba8d7af0934c49a23187babeaca8764": "OKX DexRouter",
    "0xf6801d319497789f934ec7f83e142a9536312b08": "OKX DexRouter (DAG)",
    "0x6352a56caadc4f1e25cd6c75970fa768a3304e64": "OpenOcean ExchangeProxy",
    "0x00000000009726632680fb29d3f7a9734e3010e2": "Rainbow Router",
    "0xbbbfd134e9b44bfb5123898ba36b01de7ab93d98": "Reservoir ApprovalProxy",
    "0x3a23f943181408eac424116af7b7790c94cb97a5": "Socket Gateway",
    "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45": "Uniswap SwapRouter02",
    "0xe592427a0aece92de3edee1f18e0157c05861564": "Uniswap SwapRouter",
    "0xc36442b4a4522e871399cd717abdd847ab11fe88": "Uniswap V3 NonfungiblePositionManager",
    "0x5c7bcd6e7de5423a257d81b442095a1a6ced35c5": "Across SpokePool V2",
    "0x51c72848c68a965f66fa7a88855f9f7784502a7f": "Wintermute (market maker)",
    "0xe08d97e151473a848c3d9ca3f323cb720472d015": "0xc0ffeebabe (searcher)",
    "0xa69babef1ca67a37ffaf7a485dfff3382056e78c": "Symbolic Capital Partners",
    "0x5050e08626c499411b5d0e0b5af0e83d3fd82edf": "Symbolic Capital Partners",
    "0xbd3fa81b58ba92a82136038b25adec7066af3155": "Circle TokenMessenger",
    "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2": "Aave V3 Pool",
    "0xc3d688b66703497daa19211eedff47f25384cdc3": "Compound cUSDCv3",
    "0xba12222222228d8ba445958a75a0704d566bf2c8": "Balancer Vault",
}

# A wallet is "router_primary" when at least this share of its qualifying
# transactions entered through a Sushi router. Chosen (not derived) — the
# distribution is bimodal at 0 and 1, so the report also prints the histogram
# so the threshold can be re-litigated without re-running the queries.
ROUTER_PRIMARY_THRESHOLD = 0.80

# Behavioral bot signals; a wallet trips the composite flag at >= 2 of 4.
# All are in-schema and reproducible — Etherscan's "MEV Bot" tag is not.
BOT_ZERO_PRIO_FRAC = 0.50   # majority of txs pay no priority fee => private orderflow
BOT_MAX_LEGS = 10           # 10+ swap hops in one tx is a routing/arb cycle
BOT_TXS_PER_ACTIVE_DAY = 20
BOT_APPROVALS_PER_ACTIVE_DAY = 10

# Sustained approval spam is on its own sufficient — it is the approvals analog
# of the plan's >3-sigma action rule. The total-count floor is load-bearing: a
# human doing a one-day revoke.cash sweep trips the per-day rate (median 27
# approvals lifetime) but never the floor, while the automated wallets run
# 200+/day across every day of the window.
BOT_SUSTAINED_APPROVALS = 100


def _client() -> bigquery.Client:
    return bigquery.Client(project="dex-research")


def _q(sql: str) -> pd.DataFrame:
    return _client().query(sql).to_dataframe()


def zero_prio_share(s: pd.Series) -> float:
    """Share of txs paying no priority fee.

    NULL means a legacy (pre-EIP-1559, type-0) tx, which has no priority-fee
    field — it pays the whole gas price to the builder, so it is the opposite
    of the private-orderflow signal. Treat NULL as not-zero, never as missing.
    """
    return float((s.fillna(-1) == 0).mean())


def label(addr: str) -> str:
    if addr is None:
        return "(contract creation / null)"
    if addr in SUSHI_ROUTERS:
        return f"SushiSwap {SUSHI_ROUTERS[addr]}"
    return KNOWN_CONTRACTS.get(addr, "unknown")


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

def load_tx_level() -> pd.DataFrame:
    """One row per qualifying transaction, with routing + gas features.

    Candidates come from BOTH discovery anchors, disjoint by construction:
    `swaps` (pool-anchored: has a Sushi stable-selling leg) and
    `swaps_txto_delta` (tx_to-anchored remainder: entered a Sushi router but
    routed entirely past the old anchor). Router-entry txs are complete;
    non-router pool-touch txs are the aggregator/MEV contrast population.

    Tables are leg-level; a tx_hash repeats once per hop. Gas columns are
    tx-level and therefore identical across a tx's legs, so MAX() is a
    dedupe, not an aggregate.
    """
    cols = """wallet, block_time, tx_hash, tx_to, evt_index, project,
              token_symbol, counter_symbol, amount_usd,
              gas_used, gas_cost_eth, max_priority_fee_per_gas, method_id"""
    return _q(f"""
        WITH legs AS (
          SELECT {cols} FROM `{DATASET}.swaps`
          UNION ALL
          SELECT {cols} FROM `{DATASET}.swaps_txto_delta`
        )
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
          # the FIRST leg's notional is what the user sold; summing every leg
          # would count each hop of a multi-hop route as fresh volume
          ARRAY_AGG(amount_usd    ORDER BY evt_index LIMIT 1)[OFFSET(0)] AS amount_usd,
          # first executed leg decides what the user started with
          ARRAY_AGG(token_symbol  ORDER BY evt_index LIMIT 1)[OFFSET(0)] AS first_token,
          ARRAY_AGG(counter_symbol ORDER BY evt_index LIMIT 1)[OFFSET(0)] AS first_counter,
          ARRAY_AGG(project       ORDER BY evt_index LIMIT 1)[OFFSET(0)] AS first_project,
          LOGICAL_OR(project = "sushiswap")    AS touches_sushi_pool
        FROM legs
        GROUP BY tx_hash
    """)


def load_approvals() -> pd.DataFrame:
    """One row per approve() event, base + delta wallets (approvals_all view).
    amount_raw is a decimal string (uint256), so it is kept as a string and
    parsed in Python — int64 would overflow."""
    return _q(f"""
        SELECT wallet, block_time, block_number, tx_hash, token_symbol,
               counterparty AS spender, amount_raw, is_revoke,
               gas_used, gas_price, gas_cost_eth, max_priority_fee_per_gas,
               base_fee_per_gas, priority_fee_per_gas, eth_usd, gas_cost_usd
        FROM `{DATASET}.approvals_all_usd`
    """)


# --------------------------------------------------------------------------
# Feature construction
# --------------------------------------------------------------------------

def build_wallet_features(tx: pd.DataFrame, appr: pd.DataFrame) -> pd.DataFrame:
    tx = tx.copy()
    tx["is_router_entry"] = tx["tx_to"].isin(SUSHI_ROUTERS)
    tx["day"] = tx["block_time"].dt.floor("D")

    g = tx.groupby("wallet")
    feats = pd.DataFrame({
        "qualifying_txs": g.size(),
        "router_entry_txs": g["is_router_entry"].sum(),
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
    feats["router_entry_share"] = feats["router_entry_txs"] / feats["qualifying_txs"]
    feats["txs_per_active_day"] = feats["qualifying_txs"] / feats["active_days"]

    # Approvals: gas is a tx-level property but a tx can emit several approve()
    # events (batched smart wallets, USDT's revoke-then-set). Summing
    # gas_cost_eth over rows would double count, so dedupe on tx_hash first.
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

    sushi_appr = in_window[in_window["spender"].isin(SUSHI_ROUTERS)]
    sushi_feats = pd.DataFrame({
        "sushi_approvals": sushi_appr.groupby("wallet").size(),
        # Scope-matched gas: only approvals whose spender is a Sushi router.
        # Attribution is approximate — one approve() tx can carry events for
        # several spenders, and the whole tx's gas is charged to each.
        "sushi_approval_gas_eth": (
            sushi_appr.drop_duplicates("tx_hash").groupby("wallet")["gas_cost_eth"].sum()
        ),
    })

    router_swap_gas = (
        tx[tx["is_router_entry"]].groupby("wallet")["gas_cost_eth"].sum()
        .rename("router_entry_swap_gas_eth")
    )

    feats = feats.join([appr_feats, gas_by_wallet, sushi_feats, router_swap_gas])
    for c in ["approvals_in_window", "approval_active_days", "distinct_spenders",
              "approval_gas_eth", "sushi_approvals", "sushi_approval_gas_eth",
              "router_entry_swap_gas_eth"]:
        feats[c] = feats[c].fillna(0)

    feats["approvals_per_active_day"] = np.where(
        feats["approval_active_days"] > 0,
        feats["approvals_in_window"] / feats["approval_active_days"].replace(0, np.nan),
        0.0,
    )

    # Plan metric: approvals / qualifying actions. Reported two ways —
    # `_all` counts every spender (a wallet's 1inch approval included), while
    # `_sushi` matches the Sushi-router spender to Sushi-router-entry swaps.
    # Only the matched one tests the plan's 1:1 SushiSwap hypothesis.
    feats["approval_to_action_all"] = feats["approvals_in_window"] / feats["qualifying_txs"]
    feats["approval_to_action_sushi"] = np.where(
        feats["router_entry_txs"] > 0,
        feats["sushi_approvals"] / feats["router_entry_txs"].replace(0, np.nan),
        np.nan,
    )
    return feats.reset_index()


def apply_bot_flags(feats: pd.DataFrame) -> pd.DataFrame:
    out = feats.copy()

    # The plan's rule, via the shared helper. flag_bots groups by
    # (protocol, action_type), so supply constant columns for this single arm.
    tmp = out.assign(protocol="sushiswap", action_type="swap",
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


def outstanding_allowances(appr: pd.DataFrame) -> pd.DataFrame:
    """Latest approval per (wallet, token, spender) as of the end of the window.

    Uses the full lookback (2022-11 onward) — an allowance set in 2023 and never
    revoked is still outstanding in Feb 2026, and is invisible to a
    window-only view.
    """
    hist = appr[appr["block_time"] < WINDOW_END].sort_values("block_time")
    latest = hist.groupby(["wallet", "token_symbol", "spender"], as_index=False).last()
    latest["outstanding"] = latest["approval_type"] != "revocation"
    latest["is_unlimited"] = latest["approval_type"] == "unlimited"
    return latest


# Spenders whose approval UX differs sharply, used for the within-wallet test in
# section 11. Pairs are (unlimited-leaning, exact-leaning).
UX_PAIRS = [
    ("0x000000000022d473030f116ddee9f6b43ac78ba3", "0x881d40237659c251811cec9c364ef91dc08d300c"),
    ("0x40aa958dd87fc8305b97f2ba922cddca374bcd7f", "0x1231deb6f5749ef6ce6943a275a1d3e7486f4eae"),
    ("0x000000000022d473030f116ddee9f6b43ac78ba3", "0x1231deb6f5749ef6ce6943a275a1d3e7486f4eae"),
]


def default_vs_deliberate(win: pd.DataFrame, human_set: set) -> pd.DataFrame:
    """Does the WALLET or the INTERFACE choose unlimited-vs-exact?

    Compares the same (wallet, token) approving two different spenders. Holding
    token constant rules out a token-preference explanation, so a large,
    one-sided discordance can only come from the spender's own approval UX.
    """
    nz = win[win["wallet"].isin(human_set) & (win["approval_type"] != "revocation")].copy()
    nz["unlimited"] = nz["approval_type"] == "unlimited"
    per = nz.groupby(["wallet", "token_symbol", "spender"])["unlimited"].max().unstack()

    rows = []
    for a, b in UX_PAIRS:
        if a not in per.columns or b not in per.columns:
            continue
        sub = per[[a, b]].dropna()
        if len(sub) < 20:
            continue
        rows.append({
            "spender_a": label(a), "spender_b": label(b), "pairs": len(sub),
            "unlimited_a": sub[a].mean(), "unlimited_b": sub[b].mean(),
            "a_unl_b_exact": int(((sub[a] == 1) & (sub[b] == 0)).sum()),
            "b_unl_a_exact": int(((sub[a] == 0) & (sub[b] == 1)).sum()),
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def h(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def fmt_eth(x: float) -> str:
    return f"{x:.4f} ETH"


def run() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pd.set_option("display.width", 200, "display.max_columns", 40)

    print("Loading from BigQuery ...")
    tx = load_tx_level()
    appr = classify_approvals(load_approvals())
    print(f"  {len(tx):,} qualifying txs | {len(appr):,} approval events")

    feats = apply_bot_flags(build_wallet_features(tx, appr))
    tx["is_router_entry"] = tx["tx_to"].isin(SUSHI_ROUTERS)

    # ---------------------------------------------------------------- funnel
    h("1. POPULATION FUNNEL — 'a SushiSwap swap' has three different meanings")
    # Candidates are the union of two discovery anchors (pool-touch, router
    # tx_to). Router-entry is COMPLETE post-delta; pool-touch stays the
    # aggregator/MEV contrast population.
    n_all = len(tx)
    n_pool = int(tx["touches_sushi_pool"].sum())
    n_firsthop = int((tx["first_project"] == "sushiswap").sum())
    n_router = int(tx["is_router_entry"].sum())
    n_router_no_pool = int((tx["is_router_entry"] & ~tx["touches_sushi_pool"]).sum())
    for name, n, w in [
        ("candidates (either anchor)", n_all, tx["wallet"].nunique()),
        ("touched a Sushi pool", n_pool, tx.loc[tx["touches_sushi_pool"], "wallet"].nunique()),
        ("first hop was a Sushi pool", n_firsthop, tx.loc[tx["first_project"] == "sushiswap", "wallet"].nunique()),
        ("entered via a Sushi ROUTER", n_router, tx.loc[tx["is_router_entry"], "wallet"].nunique()),
    ]:
        print(f"  {name:<32} {n:>7,} txs  {w:>6,} wallets  ({n / n_all:6.2%} of candidates)")
    print(f"\n  Router-entry txs touching NO Sushi pool: {n_router_no_pool:,} "
          f"({n_router_no_pool / n_router:.1%} of router-entry) — the population the"
          "\n  pool-anchored fetch could not see: Sushi's own routers shop the trade out.")

    print("\n  Router-entry broken down by contract:")
    rb = (tx[tx["is_router_entry"]].groupby("tx_to")
          .agg(txs=("tx_hash", "size"), wallets=("wallet", "nunique")))
    for addr, r in rb.sort_values("txs", ascending=False).iterrows():
        print(f"    {SUSHI_ROUTERS[addr]:<32} {r.txs:>5,} txs  {r.wallets:>4,} wallets")

    print("\n  Top NON-Sushi entry contracts (aggregator / MEV flow through Sushi liquidity):")
    nb = (tx[~tx["is_router_entry"]].groupby("tx_to")
          .agg(txs=("tx_hash", "size"), wallets=("wallet", "nunique"),
               mean_legs=("legs", "mean"),
               zero_prio=("max_priority_fee_per_gas", zero_prio_share)))
    for addr, r in nb.sort_values("txs", ascending=False).head(12).iterrows():
        print(f"    {str(addr)[:12]}… {label(addr):<30} {r.txs:>6,} txs {r.wallets:>5,} w "
              f"legs={r.mean_legs:4.1f} zero_prio={r.zero_prio:.0%}")

    # ------------------------------------------------------------------ bots
    h("2. BOT / MEV-SEARCHER PARTITION")
    n_bot = int(feats["is_bot"].sum())
    print(f"  {n_bot:,} of {len(feats):,} wallets flagged ({n_bot / len(feats):.1%}) — "
          f"they account for {feats.loc[feats['is_bot'], 'qualifying_txs'].sum() / feats['qualifying_txs'].sum():.1%} "
          f"of qualifying txs and "
          f"{feats.loc[feats['is_bot'], 'approvals_in_window'].sum() / max(feats['approvals_in_window'].sum(), 1):.1%} of approvals.")
    print(f"\n  {'signal':<22}{'wallets':>9}")
    for c in ["private_orderflow", "long_routes", "high_frequency", "approval_spam"]:
        print(f"  {c:<22}{int(feats[c].sum()):>9,}")
    print(f"  {'3-sigma (plan rule)':<22}{int(feats['is_bot_3sigma'].sum()):>9,}")
    print(f"  {'behavioral (>=2 sig)':<22}{int(feats['is_bot_behavioral'].sum()):>9,}")
    print(f"  {'sustained appr. spam':<22}{int(feats['is_bot_approval_spam'].sum()):>9,}")

    both = int((feats["is_bot_3sigma"] & feats["is_bot_behavioral"]).sum())
    only3 = int((feats["is_bot_3sigma"] & ~feats["is_bot_behavioral"]).sum())
    onlyb = int((~feats["is_bot_3sigma"] & feats["is_bot_behavioral"]).sum())
    print(f"\n  Agreement: both={both:,}  3-sigma only={only3:,}  behavioral only={onlyb:,}")
    print("  The 3-sigma rule alone misses low-volume searchers; the behavioral")
    print("  signals catch them. Neither is ground truth — they are proxies.")

    print("\n  Top 12 flagged wallets by qualifying txs:")
    cols = ["wallet", "qualifying_txs", "max_legs", "zero_prio_frac",
            "txs_per_active_day", "approvals_in_window", "bot_signal_count"]
    top = feats[feats["is_bot"]].nlargest(12, "qualifying_txs")[cols]
    for _, r in top.iterrows():
        print(f"    {r.wallet[:12]}… txs={r.qualifying_txs:>6,} legs_max={r.max_legs:>3} "
              f"zp={r.zero_prio_frac:.0%} tx/day={r.txs_per_active_day:>6.1f} "
              f"appr={int(r.approvals_in_window):>6,} sig={r.bot_signal_count}")

    # ------------------------------------------------------------- searchers
    h("3. SEARCHER CONTRACTS — MEV infrastructure visible in the entry set")
    sc = (tx.groupby("tx_to").agg(
        txs=("tx_hash", "size"), wallets=("wallet", "nunique"),
        mean_legs=("legs", "mean"), max_legs=("legs", "max"),
        zero_prio=("max_priority_fee_per_gas", zero_prio_share),
        gas_eth=("gas_cost_eth", "sum")).query("txs >= 100"))
    sc["searcher_score"] = (
        (sc["zero_prio"] >= 0.5).astype(int)
        + (sc["mean_legs"] >= 4).astype(int)
        + (sc["txs"] / sc["wallets"] >= 20).astype(int)
    )
    sc["name"] = [label(a) for a in sc.index]
    sc = sc.sort_values(["searcher_score", "txs"], ascending=False)
    print("  Ranked by searcher score (zero-priority-fee + long routes + few wallets/many txs):\n")
    print(f"  {'contract':<15}{'name':<28}{'txs':>7}{'w':>6}{'legs':>6}{'zeroP':>7}{'score':>6}")
    for addr, r in sc.head(14).iterrows():
        print(f"  {str(addr)[:13]:<15}{r['name'][:27]:<28}{r.txs:>7,}{r.wallets:>6,}"
              f"{r.mean_legs:>6.1f}{r.zero_prio:>7.0%}{r.searcher_score:>6}")

    print(textwrap.dedent("""
      NOTE: max_priority_fee_per_gas = 0 means the tx paid the builder out of
      band (a direct coinbase transfer) — it is evidence of private orderflow,
      NOT proof of the plan's 'tried to hide from MEV but leaked to the public
      mempool' story. Confirming that leak needs mempool data this dataset
      does not contain."""))

    # ----------------------------------------------------------- the cohorts
    h("4. SUSHI ROUTER USERS vs AGGREGATOR USERS (bots excluded)")
    human = feats[~feats["is_bot"]].copy()
    human["cohort"] = np.select(
        [human["router_entry_share"] >= ROUTER_PRIMARY_THRESHOLD,
         human["router_entry_txs"] > 0],
        ["router_primary", "router_mixed"],
        default="aggregator_only",
    )
    # "router_primary" means: of this wallet's CANDIDATE txs (router-entry is
    # complete; pool-touch adds its aggregator trades through Sushi liquidity),
    # >=80% entered via a Sushi router. A wallet's Uniswap-only / 1inch-only
    # trades that never touch Sushi remain invisible, so this is Sushi-relative
    # loyalty, not a full trading profile. Note median_approvals is counted
    # across ALL spenders, which is why it dwarfs median_txs.
    summary = human.groupby("cohort").agg(
        wallets=("wallet", "size"),
        median_txs=("qualifying_txs", "median"),
        median_approvals=("approvals_in_window", "median"),
        # median only: single automated wallets make every mean here unusable
        median_ratio_all=("approval_to_action_all", "median"),
        approval_gas_eth=("approval_gas_eth", "sum"),
        swap_gas_eth=("swap_gas_eth", "sum"),
    )
    print(summary.to_string(float_format=lambda x: f"{x:,.3f}"))

    print("\n  Distribution of router_entry_share (why the threshold barely matters):")
    hist = pd.cut(human.loc[human["router_entry_txs"] > 0, "router_entry_share"],
                  bins=[0, .2, .4, .6, .8, 1.0], include_lowest=True).value_counts().sort_index()
    for iv, n in hist.items():
        print(f"    {str(iv):<16} {n:>5,} wallets")

    # --------------------------------------------------- the headline metric
    h("5. APPROVAL-TO-ACTION RATIO — testing the plan's 1:1 SushiSwap claim")
    router_users = human[human["router_entry_txs"] > 0]
    print(f"  Sushi router users (non-bot): {len(router_users):,} wallets\n")
    print("  (a) MATCHED  — Sushi-router approvals / Sushi-router-entry swaps")
    r = router_users["approval_to_action_sushi"].dropna()
    print(f"      n={len(r):,}  median={r.median():.3f}  p90={r.quantile(.9):.3f}  "
          f"max={r.max():.0f}  share==0: {(r == 0).mean():.1%}  share>=1: {(r >= 1).mean():.1%}")
    print(f"      (mean={r.mean():.3f} is not reportable — a single wallet at "
          f"{r.max():.0f} carries it; use the median.)")
    print("\n  (b) UNMATCHED — all approvals (any spender) / all qualifying swaps")
    r2 = router_users["approval_to_action_all"].dropna()
    print(f"      n={len(r2):,}  median={r2.median():.3f}  p90={r2.quantile(.9):.3f}")
    print("\n  (b) is inflated: it credits a wallet's 1inch/Permit2 approvals to")
    print("  SushiSwap. Only (a) speaks to SushiSwap's approval mechanics.")

    # Post-delta, the denominator is complete: every router-entry qualifying tx
    # in the window is fetched (tx_to-anchored, verified against the
    # independent Dune funnel), so (a) is a point estimate, not an upper bound.
    approvers = int((human["sushi_approvals"] > 0).sum())
    residual = int(((human["sushi_approvals"] > 0) & (human["router_entry_txs"] == 0)).sum())
    print(f"\n  POINT ESTIMATE (denominator complete post-delta). Residual: {residual:,} of")
    print(f"  {approvers:,} non-bot Sushi-router approvers ({residual / max(approvers, 1):.1%}) still show zero")
    print("  router-entry swaps — now attributable to real scope edges, not lost data:")
    print("  the approval lookback reaches 2022 but swaps only cover the window, and a")
    print("  swap whose FIRST leg sold a non-study token (e.g. ETH-in) does not qualify.")

    # -------------------------------------------------------------- deciles
    h("6. DECILES (non-bot router users, ranked by qualifying tx count)")
    d = router_users.assign(protocol="sushiswap", action_type="swap",
                            action_count=router_users["qualifying_txs"])
    one_shot = (d["action_count"] == 1).mean()
    d = assign_deciles(d)
    dm = d.groupby("decile").agg(
        wallets=("wallet", "size"),
        median_actions=("action_count", "median"),
        median_approvals=("approvals_in_window", "median"),
        median_ratio_sushi=("approval_to_action_sushi", "median"),
        approval_gas_eth=("approval_gas_eth", "sum"),
    )
    print(dm.to_string(float_format=lambda x: f"{x:,.3f}"))
    if len(dm) < 10:
        print(f"\n  DEGENERATE: only {len(dm)} of 10 deciles are non-empty. {one_shot:.1%} of")
        print("  non-bot Sushi router users made exactly ONE qualifying swap, so equal-frequency")
        print("  bins collapse (pd.qcut drops duplicate edges). This is a finding, not a bug —")
        print("  the plan's D1-D10 design assumes an activity spread this arm does not have.")
        print("  Once bots are removed, SushiSwap's router population is essentially all one-shot")
        print("  users. Decile analysis is meaningful for Uniswap, not here.")

    # ------------------------------------------------------- approval types
    h("7. APPROVAL TYPE MIX — unlimited vs exact vs revocation")
    win = appr[(appr["block_time"] >= WINDOW_START) & (appr["block_time"] < WINDOW_END)]
    human_set = set(human["wallet"])
    for name, sub in [
        ("all wallets, all spenders", win),
        ("non-bot wallets, all spenders", win[win["wallet"].isin(human_set)]),
        ("non-bot wallets, Sushi router spender", win[win["wallet"].isin(human_set) & win["spender"].isin(SUSHI_ROUTERS)]),
    ]:
        if len(sub) == 0:
            continue
        mix = sub["approval_type"].value_counts(normalize=True)
        print(f"  {name:<42} n={len(sub):>7,}  " + "  ".join(
            f"{k}={mix.get(k, 0):.1%}" for k in ["unlimited", "exact", "revocation"]))

    # --------------------------------------------------- outstanding exposure
    h("8. OUTSTANDING ALLOWANCE AT END OF PERIOD (2026-02-28)")
    out = outstanding_allowances(appr)
    out_h = out[out["wallet"].isin(human_set)]
    live = out_h[out_h["outstanding"]]
    print(f"  Non-bot wallets with >=1 live allowance : {live['wallet'].nunique():,}")
    print(f"  Live (wallet, token, spender) triples   : {len(live):,}")
    print(f"  ...of which UNLIMITED (uint256 max)     : {int(live['is_unlimited'].sum()):,} "
          f"({live['is_unlimited'].mean():.1%})")
    revoked = out_h[~out_h["outstanding"]]
    print(f"  Triples whose last action was a REVOKE  : {len(revoked):,} "
          f"({len(revoked) / max(len(out_h), 1):.1%}) — the users who cleaned up")

    live_sushi = live[live["spender"].isin(SUSHI_ROUTERS)]
    print(f"\n  Live allowances to a Sushi router: {len(live_sushi):,} "
          f"({live_sushi['is_unlimited'].mean() if len(live_sushi) else 0:.1%} unlimited)")

    print(textwrap.dedent(f"""
      CAVEAT: 'outstanding' here means the last approve() was non-zero. For the
      {int(live['is_unlimited'].sum()):,} UNLIMITED triples that is exactly the plan's
      persistent exposure — spending never decrements uint256 max. For the
      {int((~live['is_unlimited']).sum()):,} EXACT triples it overstates: an exact allowance is
      usually consumed by the very swap it enabled, and ERC-20 emits no event on
      spend. Events cannot distinguish 'consumed' from 'unused' — that needs
      allowance() state reads at a block height, not event data. Read the
      unlimited count as the real number."""))
    print("\n  Top spenders holding live allowances from non-bot wallets:")
    ts = live.groupby("spender").agg(triples=("wallet", "size"), wallets=("wallet", "nunique"),
                                     unlimited=("is_unlimited", "mean"))
    for addr, r in ts.nlargest(10, "wallets").iterrows():
        print(f"    {str(addr)[:12]}… {label(addr):<30} {r.wallets:>5,} wallets  "
              f"{r.unlimited:.0%} unlimited")

    # ----------------------------------------------------------------- gas
    h("9. APPROVAL GAS COST (ETH + USD at each event's hourly Coinbase close)")
    print("  The plan's metric is absolute approval gas per wallet:\n")
    total_gas = feats["approval_gas_eth"].sum()
    for name, sub in [("all wallets", feats), ("non-bot", human), ("bots", feats[feats["is_bot"]])]:
        tot = sub["approval_gas_eth"].sum()
        n = max((sub["approval_gas_eth"] > 0).sum(), 1)
        print(f"  {name:<12} total={fmt_eth(tot):<16} wallets_paying={n:>6,}  "
              f"mean/payer={fmt_eth(tot / n):<14} share={tot / total_gas:.1%}")

    # A ratio of approval gas to swap gas is only meaningful if BOTH sides cover
    # the same activity. All-spender approval gas over Sushi-only swap gas would
    # divide a wallet's full approval bill by the slice of its swaps we can see.
    ra = human["sushi_approval_gas_eth"].sum()
    rs = human["router_entry_swap_gas_eth"].sum()
    print(f"\n  Scope-matched (non-bot): Sushi-router approval gas {fmt_eth(ra)} vs")
    print(f"  Sushi-router-entry swap gas {fmt_eth(rs)}  ->  {ra / rs:.1%}")
    print("  Both sides are restricted to the Sushi router, so this ratio is comparable.")
    print("  The all-spender ratio is NOT reported: its numerator spans every spender")
    print("  while its denominator only covers Sushi-touching swaps (scope mismatch).")

    # USD: converted per event at its hour's price BEFORE summing — ETH moved
    # ~3x over the lookback, so converting the ETH sums would be wrong.
    bots_set = set(feats.loc[feats["is_bot"], "wallet"])
    wa = win.drop_duplicates("tx_hash").copy()
    wa["usd"] = gas_usd(wa)
    print(f"\n  USD (hourly Coinbase close, in-window): "
          f"all=${wa['usd'].sum():,.0f}  "
          f"non-bot=${wa.loc[~wa['wallet'].isin(bots_set), 'usd'].sum():,.0f}  "
          f"bots=${wa.loc[wa['wallet'].isin(bots_set), 'usd'].sum():,.0f}")
    sa_u = wa[~wa["wallet"].isin(bots_set) & wa["spender"].isin(SUSHI_ROUTERS)]["usd"].sum()
    txu = tx[tx["is_router_entry"] & tx["wallet"].isin(human_set)].copy()
    rs_u = gas_usd(txu).sum()
    print(f"  Scope-matched USD (non-bot): approvals ${sa_u:,.0f} vs "
          f"router-entry swaps ${rs_u:,.0f} -> {sa_u / rs_u:.1%}")

    # -------------------------------------------------------------- monthly
    h("10. MONTHLY — the Dec/Jan tax-loss-harvesting window")
    win2 = win[win["wallet"].isin(human_set)].copy()
    win2["month"] = win2["block_time"].dt.to_period("M").astype(str)
    txh = tx[tx["wallet"].isin(human_set)].copy()
    txh["month"] = txh["block_time"].dt.to_period("M").astype(str)
    unl = win2[win2["approval_type"] == "unlimited"]
    m = pd.DataFrame({
        "swaps": txh.groupby("month").size(),
        "router_entry_swaps": txh[txh["is_router_entry"]].groupby("month").size(),
        "approvals": win2.groupby("month").size(),
        "revocations": win2[win2["approval_type"] == "revocation"].groupby("month").size(),
        "unlimited": unl.groupby("month").size(),
        # breadth: is a spike more wallets, or the same wallets doing more?
        "unl_wallets": unl.groupby("month")["wallet"].nunique(),
    }).fillna(0).astype(int)
    m["unl_per_wallet"] = (m["unlimited"] / m["unl_wallets"]).round(2)
    m["revoke_share"] = (m["revocations"] / m["approvals"]).round(3)
    print(m.to_string())
    # Breadth check for any monthly spike: if the top-3 wallets' share of
    # unlimited events falls while wallet count rises, the move is broad-based
    # (consistent with tax-loss harvesting), not a few accounts churning.
    top3 = (unl.groupby(["month", "wallet"]).size().groupby("month")
            .apply(lambda s: s.nlargest(3).sum() / s.sum()))
    print("\n  Top-3 wallets' share of unlimited events by month (breadth check):")
    print("   " + "  ".join(f"{mth}={sh:.1%}" for mth, sh in top3.items()))

    # ------------------------------------------------- default vs deliberate
    h("11. DEFAULT OR DELIBERATE? — does the wallet or the interface pick unlimited?")
    # The plan and Prof. Kim both ask how many users "just take the default" vs
    # change it. Event-weighted shares answer the wrong question: repeat
    # exact-approvers emit many events, one-shot unlimited-approvers emit one.
    sushi_nz = win[win["wallet"].isin(human_set) & win["spender"].isin(SUSHI_ROUTERS)
                   & (win["approval_type"] != "revocation")]
    per_w = (sushi_nz.assign(u=sushi_nz["approval_type"] == "unlimited")
             .groupby("wallet")["u"].mean())
    print(f"  SushiSwap routers, event-weighted unlimited : "
          f"{(sushi_nz['approval_type'] == 'unlimited').mean():.1%}")
    print(f"  SushiSwap routers, wallet-weighted (ever)   : {(per_w > 0).mean():.1%} "
          f"of {len(per_w):,} wallets")
    print("  These differ because a minority of repeat exact-approvers dominate the")
    print("  event count. The people-level number is the wallet-weighted one.\n")

    dvd = default_vs_deliberate(win, human_set)
    print("  Within-wallet, token held constant — same (wallet, token), two spenders:\n")
    for _, r in dvd.iterrows():
        print(f"  {r.spender_a} ({r.unlimited_a:.0%} unlimited)  vs  "
              f"{r.spender_b} ({r.unlimited_b:.0%} unlimited)")
        print(f"    n={r.pairs:,} pairs   discordance {r.a_unl_b_exact}:{r.b_unl_a_exact}")
    print(textwrap.dedent("""
      If the USER had a standing preference, discordance would be symmetric and
      small. It is large and almost perfectly one-sided: the same wallet, on the
      same token, accepts unlimited from Permit2/OKX and exact from
      MetaMask/LI.FI. The approval amount is a property of the INTERFACE, not of
      the user. Almost nobody edits the default.

      This is chain-side evidence only. It shows the outcome is spender-determined;
      confirming which UI ships which default needs a front-end audit."""))
    dvd.to_csv(OUT_DIR / "default_vs_deliberate.csv", index=False)

    # -------------------------------------------------- top traders (Kim)
    h("12. TOP TRADERS vs AGGREGATORS — do heavy traders avoid aggregators?")
    # Kim's hypothesis: top traders would NOT want aggregators. Proxy here:
    # of a wallet's candidate txs, what share entered via a Sushi router
    # (deliberate venue choice) vs arrived through an aggregator/searcher?
    # Only Sushi-touching activity is visible, so this is a Sushi-relative
    # share, not a full trading profile.
    act = human[human["qualifying_txs"] > 0]
    for basis in ["qualifying_txs", "volume_usd"]:
        ranked = act.sort_values(basis, ascending=False)
        cuts = {
            "top 1%": ranked.head(max(len(ranked) // 100, 1)),
            "top 10%": ranked.head(max(len(ranked) // 10, 1)),
            "bottom 50%": ranked.tail(len(ranked) // 2),
        }
        print(f"\n  Ranked by {basis} — router-entry share of the cohort's txs:")
        for name, grp in cuts.items():
            share = grp["router_entry_txs"].sum() / grp["qualifying_txs"].sum()
            ever = (grp["router_entry_txs"] > 0).mean()
            print(f"    {name:<11} {len(grp):>6,} wallets  router share={share:6.1%}  "
                  f"ever used router={ever:6.1%}")

    # ------------------------------------------------------------------ save
    feats.to_csv(OUT_DIR / "wallet_features.csv", index=False)
    human.to_csv(OUT_DIR / "wallet_cohorts.csv", index=False)
    sc.to_csv(OUT_DIR / "searcher_contracts.csv")
    out.to_csv(OUT_DIR / "outstanding_allowances.csv", index=False)
    m.to_csv(OUT_DIR / "monthly.csv")
    if len(dm):
        dm.to_csv(OUT_DIR / "decile_metrics.csv")
    print(f"\nWrote CSVs to {OUT_DIR}/")


if __name__ == "__main__":
    run()
