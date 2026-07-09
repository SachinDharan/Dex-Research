# SushiSwap V2 arm — methodology decisions

Decisions made (with Prof. Kim's guidance) during the July 2026 working sessions
that produced the `sushiswap_v2` dataset. This is the "why" document; the
operational story is in `sushiswap_v2_run_log.md`.

## Qualifying action

A transaction qualifies when its **first executed swap leg** (minimum
`evt_index`) sold USDC, USDT, or DAI — **any output token, including
stable→stable**.

- *Why first-leg:* proves the user *started* with the stablecoin. A naive
  "any leg sold a stable" filter wrongly includes ETH→USDC→TOKEN routes and
  mid-route hops of aggregator paths.
- *Why any output:* the ERC-20 approval is a property of (token spent,
  spender) — it is blind to what is bought. A USDC→USDT swap exercises the
  allowance, may trigger a fresh approval, and costs approval gas, so
  excluding it would undercount the approval-to-action ratio's denominator
  while its approvals still land in the numerator. Output-direction filters
  (`ends_non_stable`, ends-in-ETH) are **post-processing flags, never fetch
  filters**.
- The stablecoin list is a config parameter (`config/study.yaml`), not a
  concept: "stablecoin" is not an on-chain property, and USDC/USDT/DAI is the
  defensible, plan-pinned, dominant-volume set shared with the lending arms.

## Entry point vs. liquidity (the aggregator question)

Two different readings of "a SushiSwap swap" give wildly different populations
(Nov 2025–Feb 2026, Ethereum, measured on Dune before the fetch):

| population | count |
|---|---|
| qualifying txs touching a Sushi pool (stable sold there) | 66,500 |
| …whose **first hop** was a Sushi pool | 47,215 |
| …sent **directly to a Sushi router** (`tx_to`) | 1,649 |

~97.5% of Sushi-pool volume is aggregator/MEV flow *through* Sushi liquidity,
not users *choosing* SushiSwap. Because the study measures approval behavior
paired to spender contracts — and a 1inch user approves 1inch, never Sushi —
the **router-entry definition is the study population**. Counting pool-touch
trades would inflate the swap denominator with events that can never have a
matching Sushi approval. (Contrast: Uniswap Feb 2026 — 1.2M qualifying,
~130k router-entry, 11%.)

**Correction (2026-07-09).** The earlier figure of ~1,015 router-entry txs /
257 wallets counted only `Router02` + `RouteProcessor4`. Resolving every
high-traffic `tx_to` against Dune `contracts.contract_mapping` found two more
SushiSwap entry contracts, and **`RedSnwapper` is the largest by wallet count**:

| Sushi router (`tx_to`) | txs | wallets |
|---|---|---|
| `0xd9e1ce17…` Router02 (UniswapV2Router02) | 955 | 255 |
| `0xac4c6e21…` RedSnwapper | 634 | 406 |
| `0xe43ca1de…` RouteProcessor4 | 60 | 2 |
| `0xd2b37ade…` RouteProcessor9_2 | 0 | 0 (1,103 approvals, 1 wallet) |
| **union** | **1,649** | **626** |

Do **not** use `labels.owner_addresses.custody_owner` to build this set — it
tags CoW's `GPv2Settlement`, the ERC-4337 `EntryPoint`, and MetaMask's
`MetaBridge` as "sushiswap". Only `contract_project` / `contract_name` are
trustworthy. The canonical set lives in `SUSHI_ROUTERS` in
`dexresearch/process/sushi_analysis.py`.

Both definitions remain computable because the fetch **stores broad and
classifies later**: `tx_to` (entry contract) and approval `spender` are stored
raw and unfiltered; router identity is a downstream `router_labels` lookup
table that can be extended without re-fetching.

## Table grain

- `swaps` — **one row per swap leg** (474,147 legs / 66,500 txs). Same
  `tx_hash` across rows is one multi-hop trade decomposed into ordered hops
  (`evt_index`); max observed 111 legs in one MEV tx. Unique key:
  `(tx_hash, evt_index)`.
- `approvals`, `permit2_events` — **one row per event**. A tx_hash can
  legitimately repeat (batched smart-wallet approvals, USDT's
  revoke-then-set). Consequence: **sum gas over `DISTINCT tx_hash`**, never
  over rows.

## Approvals: spender unpinned, lookback to Permit2 genesis

- SushiSwap's flow uses **direct router approvals, not Permit2** — the
  Uniswap-arm query's `spender = Permit2` pin would return zero rows here.
  The Sushi fetch takes *all* approvals on the study stablecoins by
  qualifying wallets, any spender (also capturing aggregator approvals —
  Kim's top-1%-traders question — for free).
- Lookback runs from **2022-11-01 (Permit2 genesis)**, not window−18mo:
  "outstanding allowance at end of period" needs the latest approval ever.

## Permit2 events: kept for cross-protocol contrast

Sushi's own flow never touches Permit2; the table captures what the same
wallets do on Uniswap-style flows elsewhere. Includes the **Lockdown** branch
(`permit2_evt_lockdown`) — revocations made *inside* Permit2 (revoke.cash
path) that are invisible to Permit/Approval events and to the original
Uniswap-arm query. `is_revoke` covers zero-amount permits and lockdowns.

## Bots

Not filtered at fetch. Dry run found 4 wallets ≈79% of approval rows (one at
~200 approvals/day). Plan handling: flag >3σ action counts in post-processing
for separate treatment. Etherscan's "MEV Bot" tags are proprietary; the
reproducible proxies are in-schema: vanity `tx_to` (leading zeros), unknown
`method_id` selectors, `max_priority_fee_per_gas = 0`, legs-per-tx, action
counts.

**Implemented (2026-07-09)** in `process/sushi_analysis.py`. A wallet is a bot
if *any* of:

1. `action_count > mean + 3σ` — the plan's rule (54 wallets).
2. ≥2 of 4 behavioral signals (278 wallets): `zero_prio_frac ≥ 0.5`,
   `max_legs ≥ 10`, `≥20 txs/active-day`, `≥10 approvals/active-day`.
3. **Sustained approval spam**: ≥10 approvals/active-day *and* ≥100 total
   (38 wallets). The total floor is load-bearing — a human revoke.cash sweep
   trips the rate but has a median of 27 lifetime approvals, while the
   automated wallets run 200+/day on all 120 days of the window.

Result: 349 of 11,614 wallets (3.0%), carrying **44.6% of qualifying txs,
60.7% of approvals, and 78.6% of all approval gas**. The rules independently
rediscover `0x5b43453fce…`, the exact wallet the research plan cites as its
bot example. Rules 1 and 2 overlap on only 6 wallets — the 3σ rule alone
misses low-volume searchers, so neither is sufficient on its own.

`max_priority_fee_per_gas` is NULL for legacy (type-0) txs, which have no
priority-fee field and pay the builder in full. NULL must be treated as
*not* zero — the opposite of the private-orderflow signal.

## Known limitations / deferred

1. **Pool-anchored candidate discovery**: a tx entering via a Sushi router
   but routed 100% through non-Sushi pools is not fetched (RouteProcessor can
   do this). Negligible for counts; fixable by `tx_to`-anchored discovery.
2. **Cross-arm inconsistency**: the legacy Uniswap V4 fetch is stable→ETH
   only, tx-level, *without* the first-leg rule (it counts mid-route
   stable→ETH hops). Re-fetch under this arm's definition before cross-arm
   comparison.
3. **Gas in USD**: stored as `gas_cost_eth`; convert at each row's
   `block_time` via a prices table (ETH/USD moved materially Nov→Feb).
4. **Polygon arm**: deferred (plan flags low volume; token addresses
   unverified).
5. **Sampling**: the plan's decile sampling was *not* applied at fetch (all
   11,614 qualifying wallets fetched); deciles/samples are built in BigQuery.
