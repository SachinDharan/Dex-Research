# Uniswap arm — findings

Results of `python -m dexresearch.process.uniswap_analysis` against
`dex-research.uniswap_v4` (Ethereum, 2025-11-01 → 2026-03-01, half-open).
Fetch design, router resolution and gate protocol are in
`uniswap_arm_audit.md`; the Sushi counterpart is `sushiswap_v2_findings.md`.

**Population status: COMPLETE and gate-validated.** The module refuses to run
on a drifted population: 478,299 qualifying txs / 73,039 wallets / 735,223
legs match the independent Dune funnel (7923299) exactly, and every wallet's
tx and leg counts reconcile against the separately-fetched
`wallet_aggregates`. Bot thresholds, the allowance classifier and the
default-vs-deliberate test are imported from the Sushi module, so the arms
cannot silently diverge.

Gas is in **ETH and USD** (per-event hourly Coinbase close,
`reference.eth_usd_hourly`). In-window approval gas: all $61,901 / non-bot
$60,142 / bots $1,759; scope-matched USD ratio 10.0% (identical to ETH).

---

## Plain-language summary (both results)

**The main analysis (73k Uniswap users): a wallet's security posture is set
by the app, not by the user.** 92% of Uniswap users have granted an
*unlimited* spending allowance vs 20% of SushiSwap users — not different
kinds of people, the same kind of user meeting two different pre-filled
defaults. The cleanest proof: when the same wallet approves the same token to
two different apps, it takes unlimited from one and exact from the other
1,510 times in one direction, 3 times in the other. Essentially nobody edits
the default. The trade-off is visible on both sides: Uniswap users pay less
in recurring approval fees (heavy traders approve almost never), but ~83,000
still-live unlimited allowances sit on-chain at window end — convenience up
front, standing exposure after. Both protocols show a median of "1 approval
per swap" for opposite reasons: on Sushi every swap needs a fresh exact
approval; on Uniswap most people swap exactly once and leave.

**The November deep slice: "Uniswap users" and "Uniswap volume" are almost
completely different things.** Only ~8% of what traded through Uniswap's
pools came in through Uniswap's own front door; the other 92% is bots, market
makers and other apps routing *through* the liquidity — and they are
different *people* (4% wallet overlap), not the same people using different
apps. Studying approvals by pool activity would mostly be studying bots and
other apps' users; anchoring on the entry contract was the right design.

In one sentence: **users' on-chain risk exposure is decided by whichever
interface they enter through — and identifying that entry point correctly
matters enormously, because the interface's own users are a small, distinct
minority of everything that touches the protocol.**

---

## Headline

**Same median, opposite mechanism.** Both arms land on a matched
approval-to-swap median of exactly **1.000** — but SushiSwap gets there via an
exact-amount default that pairs one approval with the single swap it enables,
while Uniswap gets there because most users are one-shot: they pay a one-time
**unlimited** approval to Permit2 and then leave. The moment activity rises,
the two arms diverge: Uniswap's top-1% traders run a median ratio of
**0.009** (amortization), where Sushi's heavy users coast on old allowances
for a different reason (nothing new to approve). The plan's "≈1:1 across all
deciles" clause fails in both arms, in opposite directions.

**The interface-decides finding replicates.** On 73k wallets sharing zero
fetch lineage with the Sushi arm, the within-wallet, token-held-constant
discordances are **1510:3**, **143:6**, **1217:3** — the same near-perfect
one-sidedness as Sushi's 93:0 / 365:5 / 216:3. The allowance amount is a
property of the interface, not the user.

---

## 1. Population — V4 is a routing outcome, not a user choice

478,299 router-entry txs / 73,039 wallets / 735,223 legs, Nov 2025 – Feb 2026.

| version exposure | txs | wallets |
|---|---|---|
| any leg on a V4 pool | 139,195 (29.1%) | 52,105 (71.3%) |
| **first** leg on V4 | 116,721 (24.4%) | 47,673 (65.3%) |
| any leg on V3 | 331,909 (69.4%) | 36,505 (50.0%) |
| any leg on V2 | 77,766 (16.3%) | 12,611 (17.3%) |

Only 29% of txs touch V4, yet 71% of wallets do — the UniversalRouter spreads
V4 execution thinly across nearly everyone. A population conditioned on "V4
users" would have been selecting on the router's routing choice (the Sushi
pool-anchor bug one layer down); here V4 stays a measured flag.

Entry contracts split cleanly by era **and by species**:

| router | txs | wallets | txs/wallet | bot share of txs |
|---|---|---|---|---|
| UniversalRouter (current) | 253,495 | 67,818 | 3.7 | 15.9% |
| SwapRouter (V3) | 120,916 | 815 | 148.4 | **86.9%** |
| SwapRouter02 | 72,674 | 2,928 | 24.8 | 76.8% |
| V2 Router02 | 28,612 | 1,906 | 15.0 | 58.9% |
| UniversalRouter (2023/2022) | 2,602 | 372 | 7.0 | ~39% |

The interface-facing router (UniversalRouter) is where the humans are; the
legacy contract-callable routers are bot highways. Entry anchoring plus a
downstream bot partition separates them; anchoring alone would not have.

## 2. Bots: few wallets, half the flow, almost none of the approval cost

**154 of 73,039 wallets (0.2%)** are flagged — but they carry **45.8% of
qualifying txs** and only 6.9% of ERC-20 approvals / **2.6% of approval gas**
(0.57 of 22.35 ETH). This inverts the Sushi arm, where bots paid 74.7% of
approval gas: Uniswap bots approve once (or use legacy allowances) and swap
hundreds of thousands of times; Sushi's approval-gas bill was itself
bot-generated spam.

## 3. Deciles — the analysis Sushi could not support, mostly

61.4% of non-bot wallets made exactly one qualifying swap, so equal-frequency
bins still collapse — to 4 usable activity bands rather than Sushi's 1. The
bands are enough to read the amortization gradient:

| band | wallets | median txs | median ERC-20 matched ratio | median Permit2 grants/swap |
|---|---|---|---|---|
| 1 (one-shot) | 55,848 | 1 | **1.000** | 1.000 |
| 2 | 4,700 | 3 | 0.333 | 0.667 |
| 3 | 5,674 | 5 | 0.200 | 0.500 |
| 4 (12+) | 6,663 | 12 | **0.056** | 0.263 |

Top 1% by activity (median 69 txs): matched ratio **0.009**, Permit2 grants
**0.045** per swap. Approvals amortize almost perfectly with activity — the
textbook allowance model the plan assumed Sushi would violate and Uniswap
would follow. It does.

## 4. Approval-to-action, two layers (never summed)

One modern Uniswap flow emits **two** events: an ERC-20 `approve()` to
Permit2 (root, persistent) and a Permit2 permit to the router (sub-grant,
expiring). Summing them double-counts a single user decision.

- **(a) ERC-20 matched** (spender = Permit2 or a router): median **1.000**,
  p90 1.000; 26.0% of wallets show zero in-window — and **90.3% of those hold
  a pre-window Uniswap-path approval** (lookback to 2022-11). Coasting, not
  missing data.
- **(b) Permit2 grants to routers**: median 1.000, p90 2.000. An upper bound
  on standing-allowance creation — expiry forces re-grants that the events
  cannot be distinguished from first grants (expiration was not stored).
- Signature-transfer pulls (per-swap, **no** standing allowance): 82,540
  events across 19,000 non-bot wallets — a real ephemeral-permission channel,
  but a minority one.

## 5. Approval type mix — the cross-arm contrast

Event-weighted, non-bot, in-window:

| layer | n | unlimited | exact | revocation |
|---|---|---|---|---|
| ERC-20 → any spender | 241,603 | 37.1% | 52.5% | 10.4% |
| ERC-20 → Permit2 | 69,938 | **75.3%** | 13.9% | 10.8% |
| ERC-20 → a Uniswap router directly | 11,299 | 29.9% | 64.2% | 5.9% |
| Permit2 → routers (uint160 semantics) | 151,992 | **94.6%** | 3.5% | 1.9% |

Wallet-weighted (the people-level number): **91.6%** of 49,355 wallets have
ever gone unlimited to Permit2; **98.2%** of 65,685 at the Permit2→router
layer. Against **SushiSwap's 20.5%**, this is the cleanest cross-protocol
statement of the study so far: near-identical user populations (§7 below
shows they overlap heavily in spender space) sit at 20% vs 92% unlimited
because RedSnwapper's default is exact and Uniswap's default is unlimited.
Note the third row: when users approve a Uniswap *router* directly (legacy
flow), they lean exact (64.2%) — the same users, one interface generation
apart.

## 6. Outstanding exposure at 2026-02-28

- 71,259 non-bot wallets hold ≥1 live allowance; 261,007 live
  (wallet, token, spender) triples, **56.6% unlimited**.
- **83,032 live triples point at Permit2, 91.3% unlimited** — the arm's
  persistent-exposure number. The sub-grant layer expires; the root ERC-20
  approval does not, and 3,278 in-window Lockdown events (revocations
  *inside* Permit2) all leave that root approval standing.
- 10.4% of triples end in a revoke — the cleanup crowd, same order as Sushi.
- Top live-allowance holders beyond Permit2: MetaMask Swaps (6,582 wallets),
  1inch V6 (6,320), Uniswap V3 NonfungiblePositionManager (5,368, 90%
  unlimited), Across SpokePool V2 (5,271, 90% unlimited).

Exact triples overstate exposure as in the Sushi arm (ERC-20 spend emits no
event); read the unlimited counts as the real number.

## 7. Default or deliberate — replication

Same (wallet, token), two spenders, non-bot wallets:

| spender A | unlimited | spender B | unlimited | n pairs | discordance |
|---|---|---|---|---|---|
| Uniswap Permit2 | 96% | MetaMask Swaps | 4% | 1,634 | **1510 : 3** |
| OKX TokenApprove | 68% | LI.FI Diamond | 18% | 275 | **143 : 6** |
| Uniswap Permit2 | 86% | LI.FI Diamond | 9% | 1,565 | **1217 : 3** |

An independent population, 5–14× the pair counts, the same one-sidedness.
Chain-side evidence only: it proves the outcome is spender-determined;
which UI ships which default still needs a front-end audit.

## 8. Gas

Total ERC-20 approval gas 22.35 ETH (non-bot 21.78; mean per paying wallet
0.0004 ETH). Scope-matched, non-bot: Uniswap-path approval gas 5.51 ETH vs
router-entry swap gas 55.14 ETH → **10.0%** (Sushi: 17.1%). The unlimited
default buys users a lower recurring approval overhead — the flip side of the
persistent exposure in §6.

## 9. Monthly

Flow is stable across the window (swaps 55.8k → 72.4k → 65.6k/month;
approvals track swaps; V4-first share flat at ~38–41% of qualifying swaps).
No Dec/Jan tax-loss-harvesting signature in approvals — unlike the Sushi
arm's spike, the December bump here (29.8k unlimited approvals vs 18.6k in
Nov) tracks the swap-count rise rather than outrunning it.

## 10. November broad slice — the 92% that never touches a Uniswap router

Results of `python -m dexresearch.process.uniswap_broad_november` over the
complete November leg-level broad layer (`txs_broad`/`legs_broad`, gates
exact; Dec–Feb are funnel-counted but not fetched — **every number here is
November-only**).

| November pool-touch universe | txs | wallets |
|---|---|---|
| total | 1,059,033 | — |
| entered via a Uniswap **router** (study core) | 81,927 (**7.7%**) | 20,632 |
| entered via anything else | 977,106 (92.3%) | 116,645 |

The Sushi arm's split, at 33× the scale and inverted emphasis: most of what
touches Uniswap liquidity is not Uniswap-the-product. Three species own the
non-router flow:

- **Market makers / searchers** (hundreds–thousands of txs per wallet,
  high zero-priority shares): Symbolic Capital Partners 110,897 txs / 434
  wallets (92% zero-prio), Wintermute 86,838 / 113, an unlabeled contract at
  68,018 txs / **23 wallets**, another at 39,400 txs / **3 wallets** (100%
  V4-touching — a V4-specialist searcher). Unresolved contracts stay
  `unknown`, never guessed.
- **Retail aggregators** (1–3 txs per wallet): OKX's new DAG DexRouter
  69,061 txs / 20,430 wallets (resolved via okxlabs' deployment history;
  together with the old DexRouter, OKX is the largest retail path into
  Uniswap pools at ~94k txs), MetaMask Swaps 26,547 / 18,297, 0x
  AllowanceHolder, LI.FI, 1inch V6, KyberSwap.
- **Intent settlement**: CoW GPv2Settlement 10,850 txs / 21 wallets — the
  "wallets" are solvers; CoW's end users never appear as `tx_from`, a
  structural reason entry-anchored approval pairing cannot see them.

Two checks that matter for the study design:

- **Wallet overlap is 4.3%** (5,009 of 116,645 broad wallets ever enter via
  a Uniswap router in any month). The study core and the aggregator/MEV flow
  are different *people*, not the same people on different days — pairing
  approvals with entry contracts, not with pool liquidity, was the right
  call.
- **V4-touching share is 33.6% (router) vs 33.9% (broad)** — the
  UniversalRouter does not route to V4 more than aggregators do; V4 exposure
  is a market-wide routing property, reinforcing V4-as-attribute.

---

## Deferred / next

1. Cross-protocol default-vs-deliberate paired design (Sushi exact-leaning vs
   Permit2 unlimited-leaning, same wallets where they overlap).
3. Dec–Feb broad legs if ever needed (resume `fetch.uniswap_broad`).
