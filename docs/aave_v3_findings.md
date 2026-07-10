# Aave V3 arm — findings

Results of `python -m dexresearch.process.aave_analysis` against
`dex-research.aave_v3` (Ethereum, 2025-11-01 → 2026-03-01, half-open).
Sibling docs: `sushiswap_v2_findings.md`, `uniswap_v4_findings.md`,
`compound_v3_findings.md`. Fourth and final Ethereum arm of the study.

**Population status: COMPLETE and gate-validated.** Supplies and borrows both
match the pre-measured funnel per month exactly; the 22,132-wallet roster
independently reproduces the entry-profile probe. Shared bot rules,
classifier and default-vs-deliberate test imported as in every arm.

All gas in **ETH**; USD conversion deferred.

---

## Headline

**Four arms, four interfaces, one number: the matched approval-to-action
median is 1.000 everywhere** — and everywhere for interface-specific reasons
(Aave: 58.8% one-shot suppliers plus a 1:1 approve-then-supply flow; heavy
users amortize to 0.200 by band 4, and 98.6% of zero-in-window wallets coast
on a pre-window Pool approval).

**The unlimited-default spectrum is now fully populated:**

| interface | wallet-weighted ever-unlimited |
|---|---|
| SushiSwap routers (exact default) | 20.5% |
| **Aave V3 Pool** | **44.8%** |
| Compound Comets | 73.2% |
| Uniswap Permit2 (unlimited default) | 91.6% |

Same token universe, same window, same measurement — the spread is 4.5× end
to end, and every point on it is an interface, not a user population.

**Credit delegation is the counter-example that proves the rule.** Aave's
fourth permission flavor is 97.6% **exact** — and its dominant delegatees are
Aave's *own* tooling (ParaSwapDebtSwapAdapter 4,030 delegators,
MigrationHelper 1,856). When the protocol writes the flow for itself, it
requests exact amounts; unlimited defaults are what interfaces give *users*.

---

## 1. Population

| | txs | wallets |
|---|---|---|
| supply txs, broad (stable reserves) | 125,073 | 36,021 |
| **qualifying (Pool direct)** | **55,906 (44.7%)** | **22,132** |
| contrast (aggregators/managers) | 69,167 | 15,372 |

Token mix: USDC 34,158 / USDT 21,156 / DAI 592. On-behalf supplies: 0.1%.
Method mix is measured, not guessed: `supply()` 42,404, **`supplyWithPermit`
11,523 (20.6%)** — a real EIP-2612 single-tx path, the largest
gasless-approval channel seen in any arm. Contrast layer: one single-wallet
bot at 9,781 txs, CoW solvers, Kyber/ParaSwap, Aave's UmbrellaBatchHelper,
and (for the third arm running) Kiln DeFi vaults.

Bots: 163 wallets (0.7%) carrying 15.3% of qualifying txs.

## 2. Activity structure

58.8% one-shot (fourth arm in a row where one-shot users dominate a
"power-user" protocol); 4 non-empty activity bands; matched ratio falls
1.000 → 0.200 by band 4 — the amortization gradient again.

## 3. Approval type mix

Aave's flow is **exact-leaning**: Pool-spender approvals are 27.6% unlimited /
63.7% exact event-weighted; 44.8% of wallets ever went unlimited. The same
wallets' all-spender baseline is 18.5% unlimited — Aave sits above its users'
own baseline but far below Permit2/Comet flows.

## 4. Credit delegation (the fourth permission flavor)

53,582 events since V3 genesis by 8,104 delegators (1,350 in the non-bot
roster); grants 97.6% exact; outstanding at window end (exact — genesis
lookback, no expiry): 13,980 triples, only 3.3% unlimited. Delegatees are
overwhelmingly Aave-built adapters (debt swap, V2→V3 migration) — protocol
tooling asking for precisely what it needs.

## 5. Outstanding ERC-20 exposure (2026-02-28)

122,919 live triples from 21,762 non-bot wallets (38.4% unlimited); 26,764
to the Pool (54.9% unlimited). The cross-protocol overlap table repeats:
these wallets also hold live allowances to Permit2 (6,689 wallets, 77%
unlimited), 1inch, CoW, MetaMask — the pooled default-vs-deliberate design
has plenty to work with.

## 6. Permission gas

Pool-spender approval gas 4.49 vs qualifying supply gas 15.28 ETH → **29.4%
overhead**, the highest of the four arms (Sushi 17.1%, Uniswap 10.0%,
Compound 18.4%). An exact-leaning default plus repeat supplies means paying
the approval tax repeatedly — the flip side of Uniswap's low overhead /
high standing exposure. The two costs trade off against each other, and the
interface picks the point.

## 7. Default or deliberate — replication no. 4

| spender A | unlimited | spender B | unlimited | n pairs | discordance |
|---|---|---|---|---|---|
| Uniswap Permit2 | 92% | MetaMask Swaps | 5% | 147 | **128 : 0** |
| OKX TokenApprove | 62% | LI.FI Diamond | 19% | 95 | **45 : 4** |
| Uniswap Permit2 | 83% | LI.FI Diamond | 7% | 291 | **223 : 3** |

Four independent populations, twelve pair-tests total, all one-sided.

## 8. Borrow side — real events, measured exactly

63,169 qualifying borrow txs by 14,346 wallets (USDT-leaning: 33,189 vs
USDC 29,030). Only 25.6% of stable borrowers also supplied stables in-window
— most borrow against non-stable collateral (ETH/BTC), the classic
leverage/liquidity pattern. Borrows need no ERC-20 approval and stay out of
the ratio.

---

## Deferred / next

1. Cross-protocol pooled analysis — four arms, shared wallets, one paired
   default-vs-deliberate design (the study's capstone).
2. Gas→USD price join; Polygon arms.
3. Compound borrow classification via pre-window balances, if pursued.
