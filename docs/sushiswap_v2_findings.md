# SushiSwap V2 arm — findings

Results of `python -m dexresearch.process.sushi_analysis` against
`dex-research.sushiswap_v2` (Ethereum, 2025-11-01 → 2026-03-01, half-open).
Methodology rationale is in `sushiswap_v2_methodology.md`; the fetch story is in
`sushiswap_v2_run_log.md`.

**Population status: COMPLETE.** The pool-anchored sampling defect documented in
earlier versions of this file (the fetch held 5.2% of router-entry txs) was
resolved 2026-07-09 by the additive delta fetch. Every number below is computed
on the full, verified population: candidates are `swaps` ∪ `swaps_txto_delta`,
approvals come from the `approvals_all` view, and the router-entry set matches
the independent Dune funnel (31,949 txs / 5,184 wallets) exactly.

All gas is reported in **ETH**. USD conversion is deferred — it needs a price
join at each row's `block_time`, and ETH moved materially across the window, so
a constant multiplier would be worse than an honest ETH figure.

---

## Headline

**Users do not choose their trust exposure — their interface chooses it for
them.** Holding the wallet *and* the token constant, the same user accepts an
unlimited allowance from one spender and an exact allowance from another. The
discordance is large and almost perfectly one-sided. Almost nobody edits the
default. Completing the population made this *stronger* (§3).

Secondary, and revised by the complete data: **the typical SushiSwap user's
approval-to-swap ratio is exactly 1:1 — but for a different reason than the
plan gives.** The plan predicted ≈1:1 because it believed Sushi forces a fresh
approval per swap. It does not (a plain ERC-20 allowance persists). The ratio
is 1:1 because the modern Sushi flow (RedSnwapper) ships an **exact-amount
default**, and most users swap only once or twice — so one approval pairs with
one swap. Heavy users still coast on prior allowances (§4), so the plan's
"across all deciles" clause still fails.

---

## 1. Population — "a SushiSwap swap" has three meanings

| population | txs | wallets |
|---|---|---|
| candidates (either anchor) | 96,800 | 16,121 |
| touched a Sushi pool (stable sold there) | 67,115 | 11,812 |
| …whose **first hop** was a Sushi pool | 47,215 | 8,145 |
| entered via a **Sushi router** (`tx_to`) — *study population* | **31,949** | **5,184** |

Both directions of the entry/liquidity split are now measured, and both are
extreme:

- Most Sushi-*pool* volume is aggregator/MEV flow *through* Sushi liquidity —
  users who never approve Sushi (a 1inch user approves 1inch).
- Most Sushi-*router* volume never touches a Sushi pool: **92.9%** of
  router-entry txs (29,685 of 31,949) routed entirely through other venues,
  because Sushi's own routers aggregate outward. This is the population the
  pool-anchored fetch could not see.

Router-entry by contract: **RedSnwapper 30,933 txs / 5,103 wallets** (97% of
the population), Router02 955 / 255, RouteProcessor4 61 / 3. The modern
aggregating router *is* SushiSwap's user base; the legacy Router02 flow the
old sample over-represented is 3% of it.

---

## 2. Bots carry the cost, not users

436 of 16,121 wallets (2.7%) are flagged. They account for:

- **37.3%** of qualifying transactions
- **54.0%** of approvals
- **74.7%** of all approval gas

| | total | wallets paying | mean per payer |
|---|---|---|---|
| all wallets | 32.00 ETH | 12,656 | 0.0025 ETH |
| non-bot | 8.08 ETH | 12,531 | 0.0006 ETH |
| bots | 23.92 ETH | 125 | 0.1914 ETH |

The rules independently rediscover `0x5b43453fce…` — the exact wallet the
research plan cites as its bot example — without being told about it.

Scope-matched approval cost (non-bot, Sushi-router spender ÷ Sushi-router-entry
swaps): **0.73 ETH / 4.28 ETH = 17.1%**. Both sides restricted to the same
activity. (The old 40.8% figure was an artifact of the undercounted
denominator.) The all-spender-over-Sushi-only ratio is *not* reported — scope
mismatch.

---

## 3. Default or deliberate? (the headline)

The question — the plan's and Prof. Kim's — is *how many people take the default
vs change it themselves*.

First, a measurement trap. Unlimited-share must be **wallet-weighted**, not
event-weighted: repeat exact-approvers emit many events, one-shot
unlimited-approvers emit one.

| SushiSwap routers | unlimited share |
|---|---|
| event-weighted | 12.4% |
| wallet-weighted (ever unlimited) | **20.5%** of 4,944 wallets |

(The old sample said 47.1% wallet-weighted — that was the legacy-Router02
cohort. The complete, RedSnwapper-dominated population is far more
exact-leaning, which is itself interface-determined: the modern sushi.com flow
requests exact amounts.)

Then the test. Same `(wallet, token)`, two different spenders — token held
constant, so token preference cannot explain it:

| spender A | unlimited | spender B | unlimited | n pairs | discordance (A-unl/B-exact : reverse) |
|---|---|---|---|---|---|
| Uniswap Permit2 | 88% | MetaMask Swaps | 7% | 115 | **93 : 0** |
| OKX TokenApprove | 87% | LI.FI | 13% | 484 | **365 : 5** |
| Uniswap Permit2 | 80% | LI.FI | 9% | 302 | **216 : 3** |

If the *user* had a standing preference, discordance would be symmetric and
small. It is neither. The allowance amount is a property of the **interface**.

**Limitation:** this is chain-side evidence of the *outcome*. It shows the
choice is spender-determined. Confirming which front-end ships which default
requires a UI audit, not on-chain data.

---

## 4. The approval-to-action ratio, on the complete population

The plan predicts: *"Because SushiSwap requires a fresh exact-amount approval
before each swap, the approval-to-swap ratio should track much closer to 1:1
across all deciles."*

Matched ratio (Sushi-router approvals ÷ Sushi-router-entry swaps), 5,110
non-bot router users — **a point estimate now, not an upper bound**:

- median **1.000**, p90 1.22, max 8
- **75.8%** of wallets at ratio ≥ 1; only **3.4%** at 0

So the plan's 1:1 lands at the median — but its *mechanism* is still wrong.
Sushi does not force a per-swap approval (a plain ERC-20 allowance persists);
the residual coasting is visible as soon as activity rises:

| decile (by tx count) | wallets | median swaps | median matched ratio |
|---|---|---|---|
| 1 | 2,944 | 1 | 1.000 |
| 2–3 | 736 | 3–5 | 1.000 |
| 4 | 508 | 7 | 0.625 |
| 5 | 453 | 11 | 0.333 |
| 6 | 469 | 22 | 0.840 |

The 1:1 emerges because the population is dominated by one-and-done users whose
single exact approval pairs with their single swap — approval cost is charged
**per (new user × token)**, and most users never amortize it. Wallets in the
middle-activity deciles coast (ratio ⅓–⅝); the top decile's 0.84 partly
reflects USDT's revoke-then-set two-step and repeat exact approvals.

Residual scope note: 18 of 4,952 non-bot Sushi-router approvers (0.4%) show
zero router-entry swaps — attributable to window truncation and the first-leg
qualification rule, not missing data.

---

## 5. Sampling defect — RESOLVED

Earlier versions of this file carried a governing caveat: the fetch was
pool-anchored and held 5.2% of the study population. That was fixed by the
additive delta fetch on 2026-07-09 (`swaps_txto_delta.sql` — tx_to-anchored,
anti-joined against the old predicate; details in `sushiswap_v2_run_log.md`).
The router-entry population now matches the independent Dune funnel exactly,
and the delta was validated leg-for-leg against the overlap before any
downstream number was recomputed.

What the fix changed, for the record:

- **Counts** grew ~19× (1,649 → 31,949 txs); RedSnwapper went from a minority
  contract to 97% of the population.
- **§4's level** moved as predicted in the "upper bound" warning: the old
  median of 3.0 fell to **1.0** once the denominator was complete.
- **§3's conclusion** was unaffected (paired within-wallet design), and its
  discordance counts grew strictly more one-sided.

`router_primary` still means: of the txs we can *see* (Sushi-touching), ≥80%
entered via a Sushi router. It is Sushi-relative loyalty, not a full trading
profile — a wallet's Uniswap-only trades remain invisible to this arm.

---

## 6. Deciles remain a poor fit for this arm

**45.4%** of non-bot Sushi router users made exactly one qualifying swap
(down from 72% in the biased sample, but still dominant), so equal-frequency
binning collapses and only **6 of 10** deciles are non-empty.

The complete population has more activity spread than the old sample — deciles
4–6 are now populated and show the coasting gradient in §4 — but the plan's
D1–D10 design still cannot be filled as specified. It remains meaningful for
Uniswap.

---

## 7. Outstanding exposure at 2026-02-28

| | count |
|---|---|
| non-bot wallets with ≥1 live allowance | 13,060 |
| live (wallet, token, spender) triples | 69,653 |
| …**unlimited** (uint256 max) | **31,238 (44.8%)** |
| triples last acted on by a revoke | 9,719 (12.2%) |
| live allowances to a Sushi router | 7,917 (**30.2%** unlimited) |

**Read the unlimited count as the real number.** For unlimited triples,
"outstanding" is exactly the plan's persistent exposure — spending never
decrements `uint256` max. For the 38,415 *exact* triples it overstates: an exact
allowance is usually consumed by the swap it enabled, and ERC-20 emits no event
on spend. Events cannot distinguish consumed from unused — that needs
`allowance()` state reads at a block height.

Top spenders holding live allowances (non-bot), by wallets: **SushiSwap
RedSnwapper 4,984 (28% unlimited)**, Uniswap Permit2 3,511 (81%), LI.FI 2,638
(14%), OKX TokenApprove 2,591 (76%), 1inch V6 2,537 (52%). The spread —
14% to 81% unlimited across spenders drawing on the same wallet pool — is §3's
finding restated at the exposure level.

---

## 8. Monthly — the Dec/Jan window

| month | swaps | router-entry | approvals | unlimited | unl. wallets | unl./wallet | revoke share |
|---|---|---|---|---|---|---|---|
| 2025-11 | 11,570 | 2,717 | 22,967 | 2,918 | 1,608 | 1.81 | 11.8% |
| 2025-12 | 10,486 | 2,577 | 27,704 | **8,235** | 2,071 | **3.98** | 11.9% |
| 2026-01 | 21,080 | **13,754** | 39,347 | 7,157 | 2,694 | 2.66 | 10.7% |
| 2026-02 | 17,586 | 9,142 | 28,602 | 3,719 | 2,042 | 1.82 | 9.5% |

Two separate events, previously invisible:

- **December's unlimited-approval spike is broad-based**: wallet count and
  per-wallet intensity both rise, and the top-3 wallets' share of unlimited
  events *falls* (10.2% Nov → 4.5% Dec). Consistent with the plan's
  tax-loss-harvesting hypothesis. Not proof of motive.
- **January's router-entry surge (2.6k → 13.8k txs)** is new — it is
  RedSnwapper flow the old fetch discarded. Whether it is organic growth or a
  front-end/campaign effect needs off-chain context; it does not coincide with
  an unlimited-approval spike, consistent with the exact-default flow.

---

## 9. Prof. Kim's notes, tested on the complete population

| note | verdict |
|---|---|
| "How many just take the default vs change it themselves?" | **Answered** — §3. Almost nobody changes it; the interface decides. Discordance up to 365:5. |
| "Only thing the permission captures, if it's your first time in that span, is your swap" | **Supported** — §4. The modal user is one approval : one swap; the approval is a first-entry event per (user, token). |
| "Top traders would not want to use the aggregators" | **Not supported, with nuance.** Aggregator-side entries carry 67% of the top-1%-by-txs cohort's Sushi-touching flow (router share 32.6%) and 79% of the top-1%-by-volume cohort's (20.6%). But 78.8% of top-volume wallets have used the Sushi router at least once — heavy traders use *both*, they just route most flow through aggregators. |
| "Make sure within these atomic transactions there were true atomic transactions" | **Handled** — first-leg rule + tx-level dedupe; mid-route stablecoin hops cannot qualify. |
| "Normal DEX user… people would swap on Coinbase, it's cheaper" | **Directionally supported.** 45.4% of non-bot router users made exactly one qualifying swap in four months; the median is 2. On-chain DEX swapping of stables is an occasional act for most wallets in this window. |
| "USDC, 30 days, minimal" | **Feasible.** The complete population is ~19× the old sample; a 30-day USDC-only router-entry cut is now thousands of txs, not ~100. |

---

## 10. Note on the plan's stated token direction

The plan's Research Question says *stablecoin → ETH*; its Token Pairs section
says *ETH → stablecoin*. These conflict. **Only the stable-in direction requires
an `approve()` at all** — ETH is the native asset and needs no allowance. This
arm therefore measures stable-in, which is the only direction on which the study
question is well-posed. The Token Pairs line is the internal inconsistency.

---

## 11. Open items

1. **Gas in USD** — join `prices.usd` at each row's `block_time`.
2. **Uniswap arm re-fetch** under the harmonized definition (tx_to-anchored,
   first-leg rule, any output, spender unpinned; one row per tx for the full
   population, leg-level for a sample) so §3 gets a cross-protocol contrast.
3. **Front-end audit** to confirm which UI ships which approval default (§3),
   including RedSnwapper's exact-amount flow.
4. **January surge attribution** (§8) — off-chain context.
5. Polygon arm; lending arms (Aave, Compound).

---

## Reproducing

```bash
.venv/bin/python -m dexresearch.process.sushi_analysis
```

Writes `data/analysis/*.csv` (gitignored — regenerable):
`wallet_features`, `wallet_cohorts`, `searcher_contracts`,
`outstanding_allowances`, `decile_metrics`, `monthly`, `default_vs_deliberate`.
