# Compound V3 arm — findings

Results of `python -m dexresearch.process.compound_analysis` against
`dex-research.compound_v3` (Ethereum, 2025-11-01 → 2026-03-01, half-open).
Fetch design and the Dune quirks are documented in
`queries/compound_v3/*.sql` headers and `dexresearch/fetch/compound_timeline.py`;
sibling docs: `sushiswap_v2_findings.md`, `uniswap_v4_findings.md`.

**Population status: COMPLETE and gate-validated.** 6,263 qualifying supply
txs / 1,898 wallets match the pre-measured funnel exactly, per month; allow()
events match the raw-logs probe and every one is binary. Bot thresholds, the
classifier and the default-vs-deliberate test are imported from the shared
modules (no route-length signal exists in lending, so the behavioral bot rule
is ≥2 of the remaining 3 signals).

Gas in **ETH and USD** (per-event hourly Coinbase close): window approvals
$473 + allow() $100 vs supplies $2,855 → 20.0% USD overhead (18.4% in ETH).

---

## Headline

**Matched approval-to-action median = 1.000 — in all three arms now, for a
third distinct reason.** Sushi: exact-amount default forces approval-per-swap.
Uniswap: one-shot users pay one unlimited setup for one swap. Compound: 54.5%
one-shot suppliers, and repeat suppliers amortize hard (median ratio falls
1.000 → 0.105 across activity bands; 98% of zero-in-window wallets hold a
pre-window Comet approval). The plan's "≈1:1 across all deciles" fails
everywhere, and the *reasons* differ by interface — which is the study's
point.

**Compound sits exactly between the other arms on the unlimited default:
73.2% wallet-weighted ever-unlimited to the Comets** (Sushi routers 20.5%,
Permit2 91.6%). And its second permission layer removes user choice entirely:
every allow() event ever emitted is uint256-max or zero — there is no amount
to edit. The Bulker check is perfect: **127 of 127** non-bot Bulker-entry
wallets hold the mandated full-control operator grant.

---

## 1. Population

| | txs | wallets |
|---|---|---|
| supply txs, broad (stable Comets) | 9,086 | 3,263 |
| **qualifying (Comet direct + Bulker)** | **6,263 (68.9%)** | **1,898** |
| — via Comet direct | 5,639 | — |
| — via MainnetBulker | 624 | 127 non-bot |
| contrast (managers/vaults) | 2,823 | 1,407 |

Market mix: cUSDCv3 4,238 / cUSDTv3 2,025. On-behalf supplies (dst ≠ signer):
0.0% of qualifying txs — entry anchoring and attribution coincide cleanly
here. The contrast layer is dominated by **Kiln DeFi vaults** (1,397 txs /
~990 wallets across three contracts — white-label earn products), plus OKX
and ERC-4337 smart-account flow.

Bots: 16 wallets (0.8%) carrying 19.8% of qualifying txs — a far more human
arm than either DEX.

## 2. Activity structure

54.5% one-shot; 4 of 10 deciles non-empty (same qcut collapse as the DEX
arms — one-shot dominance is now a cross-protocol regularity, not a Sushi
quirk). The repeat bands show textbook amortization:

| band | wallets | median txs | median matched ratio | ever granted allow() |
|---|---|---|---|---|
| 1 | 1,356 | 1 | 1.000 | 36.9% |
| 2 | 157 | 3 | 0.333 | 61.1% |
| 3 | 183 | 4 | 0.250 | 66.7% |
| 4 | 186 | 9 | 0.105 | 81.7% |

allow() adoption **rises** with activity — heavy users buy the convenience
layer (Bulker batching) and its full-control exposure.

## 3. Approval type mix

Event-weighted, non-bot, in-window: Comet-spender approvals are 61.2%
unlimited / 31.0% exact (all-spender baseline for the same wallets: 25.7% /
60.4%). Wallet-weighted ever-unlimited to a Comet: **73.2%** of 1,533
wallets — between Sushi's 20.5% and Uniswap/Permit2's 91.6%, consistent with
compound.finance's own flow suggesting max while integrators request exact.

## 4. The allow() layer — the no-choice default regime

37,228 events since Comet genesis (2022-08): 31,031 grants / 6,197 revokes,
**every one uint256-max or zero** (gate-verified). Top manager by an order of
magnitude: the MainnetBulker (21,725 granting wallets since genesis; OKX and
1inch appear as operators too — aggregators taking Comet-level control).

Outstanding at window end — and unlike Permit2 sub-grants this is **exact**
(no expiry, genesis lookback): 1,140 live (wallet, market, manager) grants
across 824 non-bot roster wallets, 1,013 of them to the Bulker; only 8.0% of
triples end in a revoke. A live operator grant is full account control on
that market — strictly stronger than any ERC-20 unlimited allowance in the
other arms.

## 5. Outstanding ERC-20 exposure (2026-02-28)

10,696 live triples from 1,863 non-bot wallets (48.3% unlimited); 2,043 to a
Comet (76.4% unlimited). The same wallets also hold live allowances to Aave
V3 Pool (687 wallets), Permit2 (621), 1inch — heavy cross-protocol overlap
that the pooled default-vs-deliberate design can exploit.

## 6. Permission gas (scope-matched, window)

Comet approvals 0.169 + allow() 0.034 vs qualifying supply gas 1.109 ETH →
**18.4% overhead** (Sushi 17.1%, Uniswap 10.0%). Lifetime allow() stock for
these wallets is 0.710 ETH — reported as a stock, never ratioed against
window flow.

## 7. Default or deliberate — replication no. 3

Even at 1,898 wallets one pair clears the threshold: Permit2 (70% unlimited)
vs LI.FI (3%), n=30 pairs, discordance **20:0**. Three populations, three
protocols, zero exceptions to one-sidedness.

## 8. Withdraw side (bounds only)

Comet has no Borrow event — a borrow is a Withdraw taking the base balance
negative. Entry-anchored withdraw txs: 6,892 by 2,055 wallets, of which
1,951 txs / 922 wallets have **no in-window supply** (borrow-shaped or
pre-window depositors). Separating them needs pre-window balances — deferred;
the approval mechanics the study pairs live on the supply side (borrows
receive tokens and need no ERC-20 approval).

---

## Deferred / next

1. Aave V3 arm (single Pool `0x87870bca…`, decoded Supply/Borrow with
   `onBehalfOf`, credit delegation as its extra layer) — same playbook.
2. Cross-protocol pooled default-vs-deliberate (four arms, shared wallets).
3. Borrow classification via pre-window balance aggregates, if pursued.
4. Polygon.
