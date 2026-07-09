# SushiSwap V2 arm — findings

Results of `python -m dexresearch.process.sushi_analysis` against
`dex-research.sushiswap_v2` (Ethereum, 2025-11-01 → 2026-03-01, half-open).
Methodology rationale is in `sushiswap_v2_methodology.md`; the fetch story is in
`sushiswap_v2_run_log.md`.

All gas is reported in **ETH**. USD conversion is deferred — it needs a price
join at each row's `block_time`, and ETH moved materially across the window, so
a constant multiplier would be worse than an honest ETH figure.

---

## Headline

**Users do not choose their trust exposure — their interface chooses it for
them.** Holding the wallet *and* the token constant, the same user accepts an
unlimited allowance from one spender and an exact allowance from another. The
discordance is large and almost perfectly one-sided. Almost nobody edits the
default.

This is the finding that survives the dataset's known sampling bias, and it
speaks directly to the plan's research question about persistent exposure that
users "carry unknowingly."

Secondary: **SushiSwap's approval cost is a per-new-user cost, not a per-swap
cost.** This *contradicts* the research plan's prediction, for a reason given
below.

---

## 1. Population — "a SushiSwap swap" has three meanings

| population | txs | wallets |
|---|---|---|
| touched a Sushi pool (stable sold there) | 66,500 | 11,614 |
| …whose **first hop** was a Sushi pool | 47,215 | 8,145 |
| …entered via a **Sushi router** (`tx_to`) | **1,649** | **626** |

~97.5% of Sushi-pool volume is aggregator/MEV flow *through* Sushi liquidity,
not users choosing SushiSwap. The router-entry set is the study population,
because a 1inch user approves 1inch, never Sushi.

The router set is four contracts (see `SUSHI_ROUTERS`). An earlier count of
~1,015 txs / 257 wallets missed two of them — **RedSnwapper is the largest by
wallet count.** See the correction in `sushiswap_v2_methodology.md`.

---

## 2. Bots carry the cost, not users

349 of 11,614 wallets (3.0%) are flagged. They account for:

- **44.6%** of qualifying transactions
- **60.7%** of approvals
- **78.6%** of all approval gas

| | total | wallets paying | mean per payer |
|---|---|---|---|
| all wallets | 30.37 ETH | 8,244 | 0.0037 ETH |
| non-bot | 6.50 ETH | 8,191 | 0.0008 ETH |
| bots | 23.86 ETH | 53 | 0.4502 ETH |

The rules independently rediscover `0x5b43453fce…` — the exact wallet the
research plan cites as its bot example — without being told about it.

Scope-matched approval cost (non-bot, Sushi-router spender ÷ Sushi-router-entry
swaps): **0.1865 ETH / 0.4574 ETH = 40.8%**. Both sides restricted to the same
activity. The all-spender-over-Sushi-only ratio is *not* reported: its numerator
spans every spender while its denominator is pool-anchored.

---

## 3. Default or deliberate? (the headline)

The question — the plan's and Prof. Kim's — is *how many people take the default
vs change it themselves*.

First, a measurement trap. Unlimited-share must be **wallet-weighted**, not
event-weighted: repeat exact-approvers emit many events, one-shot
unlimited-approvers emit one.

| SushiSwap routers | unlimited share |
|---|---|
| event-weighted | 24.1% |
| wallet-weighted (ever unlimited) | **47.1%** of 652 wallets |

Then the test. Same `(wallet, token)`, two different spenders — token held
constant, so token preference cannot explain it:

| spender A | unlimited | spender B | unlimited | n pairs | discordance (A-unl/B-exact : reverse) |
|---|---|---|---|---|---|
| Uniswap Permit2 | 88% | MetaMask Swaps | 8% | 84 | **67 : 0** |
| OKX TokenApprove | 89% | LI.FI | 13% | 464 | **359 : 4** |
| Uniswap Permit2 | 79% | LI.FI | 11% | 212 | **146 : 3** |

If the *user* had a standing preference, discordance would be symmetric and
small. It is neither. The allowance amount is a property of the **interface**.

Wallet-weighted "ever unlimited," across spenders, ranges from **3.9% (MetaMask
Swaps)** to **78.8% (OKX TokenApprove)** — drawn from the same wallet pool.

**Limitation:** this is chain-side evidence of the *outcome*. It shows the
choice is spender-determined. Confirming which front-end ships which default
requires a UI audit, not on-chain data.

---

## 4. Why this contradicts the research plan

The plan predicts: *"Because SushiSwap requires a fresh exact-amount approval
before each swap, the approval-to-swap ratio should track much closer to 1:1
across all deciles."*

**The premise is false.** Sushi's `Router02` is a plain ERC-20 allowance — a
user can approve unlimited once and never approve again. Among wallets with ≥2
Sushi router swaps:

- **25.2%** made *fewer* approvals than swaps (coasting on a prior allowance)
- **8.6%** made *zero* in-window Sushi approvals despite multiple swaps

If the protocol forced a per-swap approval, both would be zero.

With the premise gone, the prediction goes too. The ratio declines monotonically
with activity across six bins (Spearman ρ = **−0.32**, n=624):

| Sushi router swaps | wallets | median ratio | share ≥1 |
|---|---|---|---|
| 1 | 461 | 3.00 | 96% |
| 2 | 100 | 2.00 | 93% |
| 3 | 25 | 1.67 | 76% |
| 4–5 | 8 | 1.13 | 88% |
| 6–10 | 14 | 0.33 | 14% |
| 11+ | 16 | **0.00** | 6% |

The heaviest bin shows a median of **zero** approvals against a median of 26.5
swaps. SushiSwap behaves like the plan's *Uniswap* prediction: approve once,
then coast. Approval cost is charged **per new user**, not per swap.

---

## 5. The bound that governs every ratio

Candidate discovery was **pool-anchored**: a tx entering a Sushi router but
routed entirely through non-Sushi pools was never fetched. The numerator
(approvals to the router) is complete; the denominator (observed swaps) is not.
67 non-bot wallets approved a Sushi router with **zero** observed swaps.

Therefore **every approval-to-action ratio here is an upper bound.** Only
conclusions robust in the "true value is lower" direction are safe:

- ✅ "Heavy router users coast" (0.33 → lower still) — **safe**
- ❌ "Median 3.0 approvals per swap supports ≈1:1" (3.0 → lower) — **not safe**

Fix: `tx_to`-anchored candidate discovery.

Related: `router_primary` is named for what it measures — of the txs we can
*see*, ≥80% entered via a Sushi router. It does **not** mean the wallet is a
SushiSwap loyalist; its Uniswap-only trades are invisible to this fetch.

---

## 6. Deciles do not apply to this arm

**72.0%** of non-bot Sushi router users made exactly one qualifying swap, so
equal-frequency binning collapses (`pd.qcut` drops duplicate edges) and only 3
of 10 deciles are non-empty.

This is a finding, not a bug: once bots are removed, SushiSwap's router
population is essentially all one-shot users. The plan's D1–D10 design assumes
an activity spread this arm does not have. It remains meaningful for Uniswap.

---

## 7. Outstanding exposure at 2026-02-28

| | count |
|---|---|
| non-bot wallets with ≥1 live allowance | 8,680 |
| live (wallet, token, spender) triples | 47,271 |
| …**unlimited** (uint256 max) | **22,489 (47.6%)** |
| triples last acted on by a revoke | 6,742 (12.5%) |
| live allowances to a Sushi router | 1,666 (59.9% unlimited) |

**Read the unlimited count as the real number.** For unlimited triples,
"outstanding" is exactly the plan's persistent exposure — spending never
decrements `uint256` max. For the 24,782 *exact* triples it overstates: an exact
allowance is usually consumed by the swap it enabled, and ERC-20 emits no event
on spend. Events cannot distinguish consumed from unused — that needs
`allowance()` state reads at a block height.

Top spenders holding live allowances (non-bot), by wallets: OKX TokenApprove
2,311 (79% unlimited), LI.FI 2,177 (16%), Uniswap Permit2 2,128 (81%), 1inch V6
1,968 (47%), SushiSwap RedSnwapper 627 (55%).

---

## 8. Monthly — the Dec/Jan window

| month | swaps | router-entry | approvals | unlimited | unl. wallets | unl./wallet | revoke share |
|---|---|---|---|---|---|---|---|
| 2025-11 | 9,780 | 354 | 18,500 | 2,344 | 1,216 | 1.93 | 12.7% |
| 2025-12 | 8,910 | 482 | 21,801 | **5,624** | 1,546 | **3.64** | 13.2% |
| 2026-01 | 8,357 | 390 | 25,787 | 4,499 | 1,709 | 2.63 | 12.9% |
| 2026-02 | 9,769 | 153 | 21,606 | 2,859 | 1,520 | 1.88 | 10.6% |

December's unlimited-approval spike (+140%) is **broad-based**: distinct wallets
+27%, per-wallet intensity +89%, and the top-3 wallets' share of unlimited
events *falls* (12.8% Nov → 6.5% Dec). Consistent with the plan's
tax-loss-harvesting hypothesis. **Not proof of motive.**

---

## 9. Prof. Kim's notes, tested

| note | verdict |
|---|---|
| "How many just take the default vs change it themselves?" | **Answered** — §3. Almost nobody changes it; the interface decides. |
| "Only thing the permission captures, if it's your first time in that span, is your swap" | **Supported** — §4. Approval is a first-entry event. |
| "Top traders would not want to use the aggregators" | **Not supported.** Top 1% of wallets by activity use a Sushi router on 5.3% of txs; bottom 50% on 5.2%. No trend. What top traders avoid is *approvals* (median 0 in-window), not aggregators. |
| "Make sure within these atomic transactions there were true atomic transactions" | **Handled** — first-leg rule + tx-level dedupe. Median 2 legs, max 182; 49.7% single-leg. Mid-route stablecoin hops cannot qualify. |
| "Normal DEX user… people would swap on Coinbase, it's cheaper" | **Supported.** 72% of non-bot router users made exactly one swap in four months. |
| "USDC, 30 days, minimal" | **Feasible but thin.** A 30-day USDC-only router cut yields ~104–175 txs / ~41–107 wallets. USDC is the largest first-token (29,363 txs) but USDT has *more* router entries (878 vs 556). |

---

## 10. Note on the plan's stated token direction

The plan's Research Question says *stablecoin → ETH*; its Token Pairs section
says *ETH → stablecoin*. These conflict. **Only the stable-in direction requires
an `approve()` at all** — ETH is the native asset and needs no allowance. This
arm therefore measures stable-in, which is the only direction on which the study
question is well-posed. The Token Pairs line is the internal inconsistency.

---

## 11. Open items

1. **`tx_to`-anchored re-fetch** — removes the upper-bound caveat on every ratio (§5).
2. **Gas in USD** — join `prices.usd` at each row's `block_time`.
3. **Uniswap arm re-fetch** to the leg-level schema + first-leg rule, so the
   default-vs-deliberate finding (§3) gets a cross-protocol contrast.
4. **Front-end audit** to confirm which UI ships which approval default (§3).
5. Polygon arm; lending arms (Aave, Compound).

---

## Reproducing

```bash
.venv/bin/python -m dexresearch.process.sushi_analysis
```

Writes `data/analysis/*.csv` (gitignored — regenerable):
`wallet_features`, `wallet_cohorts`, `searcher_contracts`,
`outstanding_allowances`, `decile_metrics`, `monthly`, `default_vs_deliberate`.
