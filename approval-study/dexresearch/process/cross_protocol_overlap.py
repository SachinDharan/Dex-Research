"""Cross-protocol default-vs-deliberate test (docs/cross_protocol_defaults.md).

Wallets present in BOTH the Uniswap roster and the Sushi router-entry
population faced two approval interfaces for the same act (authorizing a
swap router to spend a token): Uniswap's, which sets the ERC-20 grant to
Permit2/router at unlimited with no amount field exposed, and Sushi's,
which exposes the amount. Comparing the same wallet's grants on the two
paths isolates the interface effect — wallet identity (and, in the matched
variant, the token) is held constant.

Reads rosters from datasets/analysis wallet-features (falls back to
`git show` when datasets/ is sparse-checked-out away), approvals from
sushiswap_v2.approvals_all (store-broad: any spender, both rosters'
overlap is a subset of the Sushi roster by construction).

Writes data/analysis/cross_protocol_wallets.csv — one row per overlap
wallet with grant counts and ever-unlimited flags per side — and
cross_protocol_reverse_grants.csv — every grant (block number, tx hash) of
the non-bot wallets with an exact Uniswap-path grant AND an unlimited Sushi
grant, the rare direction of the test.

Run:
    python -m dexresearch.process.cross_protocol_overlap
"""
from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pandas as pd
from google.cloud import bigquery

from dexresearch.process.sushi_analysis import SUSHI_ROUTERS
from dexresearch.process.uniswap_analysis import UNI_PATH_SPENDERS

PROJECT = "dex-research"
ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "data" / "analysis"
NEAR_UNLIMITED = 2**255  # same coalescing as the per-arm analyses
HUGE_EXACT = 2**96 - 1  # flag only: max-of-a-smaller-uint grants that fall under the cutoff


def _features(name: str, usecols: list[str]) -> pd.DataFrame:
    path = ROOT / "datasets" / "analysis" / f"{name}.csv"
    if path.exists():
        return pd.read_csv(path, usecols=usecols)
    blob = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"HEAD:approval-study/datasets/analysis/{name}.csv"],
        capture_output=True, check=True).stdout
    return pd.read_csv(io.BytesIO(blob), usecols=usecols)


def load_grants(overlap: set[str]) -> pd.DataFrame:
    client = bigquery.Client(project=PROJECT)
    job = client.query(
        """
        SELECT wallet, token_symbol, counterparty AS spender,
               amount_raw, is_revoke, block_time, block_number, tx_hash
        FROM `dex-research.sushiswap_v2.approvals_all`
        WHERE wallet IN UNNEST(@wallets) AND counterparty IN UNNEST(@spenders)
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ArrayQueryParameter("wallets", "STRING", sorted(overlap)),
            bigquery.ArrayQueryParameter(
                "spenders", "STRING", sorted(UNI_PATH_SPENDERS | set(SUSHI_ROUTERS))),
        ]))
    ap = pd.DataFrame([dict(r) for r in job.result()])
    ap["side"] = ap.spender.map(
        lambda s: "uniswap" if s in UNI_PATH_SPENDERS else "sushi")
    grants = ap[~ap.is_revoke].copy()
    grants["unlimited"] = grants.amount_raw.map(lambda v: int(v) >= NEAR_UNLIMITED)
    return grants


def two_by_two(per: pd.DataFrame, label: str) -> None:
    both = per.dropna(subset=["uniswap", "sushi"])
    n = len(both)
    uu = int(((both.uniswap == 1) & (both.sushi == 1)).sum())
    ue = int(((both.uniswap == 1) & (both.sushi == 0)).sum())
    eu = int(((both.uniswap == 0) & (both.sushi == 1)).sum())
    ee = int(((both.uniswap == 0) & (both.sushi == 0)).sum())
    print(f"\n{label} (n={n:,})")
    print(f"  unlimited on both              : {uu:>5,} ({uu/n:.1%})")
    print(f"  unlimited@uniswap, exact@sushi : {ue:>5,} ({ue/n:.1%})")
    print(f"  exact@uniswap, unlimited@sushi : {eu:>5,} ({eu/n:.1%})")
    print(f"  exact on both                  : {ee:>5,} ({ee/n:.1%})")
    print(f"  unlimited share: uniswap {both.uniswap.mean():.1%}, sushi {both.sushi.mean():.1%}"
          f"  discordance {ue:,}:{eu:,}")


def reverse_grants(hg: pd.DataFrame) -> pd.DataFrame:
    """All grants of wallets with >=1 exact Uniswap grant and >=1 unlimited Sushi grant."""
    exact_uni = set(hg.loc[(hg.side == "uniswap") & ~hg.unlimited, "wallet"])
    unl_sushi = set(hg.loc[(hg.side == "sushi") & hg.unlimited, "wallet"])
    out = hg[hg.wallet.isin(exact_uni & unl_sushi)].copy()

    # strict = the pair never went unlimited on Uniswap (the 2x2's reverse cell)
    ever = out.groupby(["wallet", "token_symbol", "side"])["unlimited"].max().unstack()
    strict = ever[(ever.uniswap == 0) & (ever.sushi == 1)].index
    out["pair_strict_reverse"] = pd.MultiIndex.from_frame(
        out[["wallet", "token_symbol"]]).isin(strict)
    out["huge_exact"] = ~out.unlimited & (out.amount_raw.map(int) >= HUGE_EXACT)
    cols = ["wallet", "token_symbol", "side", "spender", "block_number", "block_time",
            "tx_hash", "amount_raw", "unlimited", "huge_exact", "pair_strict_reverse"]
    return out[cols].sort_values(["wallet", "block_number", "tx_hash"])


def run() -> None:
    uni = _features("uniswap_wallet_features", ["wallet", "is_bot"])
    sushi = _features("sushi_wallet_features", ["wallet", "router_entry_txs", "is_bot"])

    overlap = set(uni.wallet) & set(sushi.loc[sushi.router_entry_txs > 0, "wallet"])
    bots = set(uni.loc[uni.is_bot, "wallet"]) | set(sushi.loc[sushi.is_bot, "wallet"])
    human = overlap - bots
    print(f"overlap wallets {len(overlap):,} ({len(human):,} non-bot)")

    grants = load_grants(overlap)
    print(f"grant events to the {len(UNI_PATH_SPENDERS) + len(SUSHI_ROUTERS)} spenders: {len(grants):,}")

    hg = grants[grants.wallet.isin(human)]
    two_by_two(hg.groupby(["wallet", "side"])["unlimited"].max().unstack(),
               "non-bot wallets with grants on both paths")
    two_by_two(hg.groupby(["wallet", "token_symbol", "side"])["unlimited"].max().unstack(),
               "matched (wallet, token) pairs on both paths, non-bot")

    per = grants.groupby(["wallet", "side"]).agg(
        grants=("unlimited", "size"),
        unlimited_grants=("unlimited", "sum"),
        ever_unlimited=("unlimited", "max")).reset_index()
    piv = per.pivot(index="wallet", columns="side",
                    values=["grants", "unlimited_grants", "ever_unlimited"])
    piv.columns = [f"{side}_{metric}" for metric, side in piv.columns]
    piv = piv.reset_index()
    piv["is_bot"] = piv.wallet.isin(bots)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    piv.to_csv(OUT_DIR / "cross_protocol_wallets.csv", index=False)
    print(f"\nWrote {OUT_DIR / 'cross_protocol_wallets.csv'} ({len(piv):,} wallets)")

    rev = reverse_grants(hg)
    rev.to_csv(OUT_DIR / "cross_protocol_reverse_grants.csv", index=False)
    print(f"Wrote {OUT_DIR / 'cross_protocol_reverse_grants.csv'} "
          f"({len(rev):,} grants, {rev.wallet.nunique()} wallets)")


if __name__ == "__main__":
    run()
