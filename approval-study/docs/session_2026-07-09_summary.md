# Session summary — 2026-07-09 (data-acquisition campaign, both DEX arms)

One-day sprint that took both study arms from defective/partial fetches to
complete, gate-validated datasets. Every number below was verified against
independently pre-measured Dune funnel counts before being accepted.

## SushiSwap arm — completed and re-analyzed

- **Problem:** the original pool-anchored fetch held 5.2% of the study
  population (RedSnwapper/RouteProcessor are aggregating routers; most Sushi
  users' txs contain zero Sushi-pool legs).
- **Fix (additive, nothing dropped or re-bought):** `swaps_txto_delta.sql`
  anchors on `tx_to` and anti-joins the old predicate — fetched only the
  30,300 missing txs / 41,723 legs / 5,022 wallets; approvals/permit2 for the
  4,507 genuinely new wallets. Study population = view
  `sushiswap_v2.swaps_router_entry` (**31,949 txs / 5,184 wallets**); use
  `approvals_all` / `permit2_events_all` downstream.
- **Findings recomputed on the full population** (`sushiswap_v2_findings.md`
  rewritten): matched approval-to-action median 3.0 → **1.000** (RedSnwapper's
  exact-amount default + mostly one-shot users — NOT per-swap forcing; heavy
  users still coast); Sushi wallet-weighted ever-unlimited 47.1% → **20.5%**;
  default-vs-deliberate headline strengthened (discordance 93:0, 365:5,
  216:3 — the interface, not the user, picks the allowance).

## Uniswap arm — rebuilt from scratch (new dataset `uniswap_v4`)

- **Audit:** legacy `raw.*` was pool-anchored, V4-only, stable→ETH-only, no
  first-leg rule, no `tx_to`/`evt_index` stored → unfixable in place; kept
  as contrast/cross-check only. Router set resolved via
  `contracts.contract_mapping` (6 contracts; note Dune mislabels V2 Router02
  as "UniswapV2Factory").
- **Harmonized rule (matches Sushi):** sent to a Uniswap router, FIRST
  executed leg sold USDC/USDT/DAI, any output token, ALL legs stored.
  **V4 is a measured per-leg attribute, never a fetch anchor** (conditioning
  on V4 execution = selecting on the router's routing choice — the Sushi bug
  one layer down). Only 29% of router-entry txs touch V4; 71% of wallets do.
- **Router-entry core (the study population): COMPLETE Nov–Feb**
  - `wallet_aggregates` — all 73,039 wallets, per month (gates exact)
  - `swaps_sampled` — **735,223 legs / 478,299 txs**; every wallet's counts
    equal its aggregates row exactly
  - `approvals` — 604,168 (any spender, lookback to Permit2 genesis 2022-11)
  - `permit2_events` — 271,754 (incl. Lockdown)
- **Broad layer (non-router pool-touch universe, "option 2")** — descoped by
  user decision after ~10 keys to a **November deep slice**: `txs_broad`
  977,106 / `legs_broad` 1,975,503 (normalized two-table layout, ~34%
  cheaper; views `swaps_broad` / `swaps_option2` reassemble the wide schema —
  **their broad half is November-scoped**). All four months exactly counted
  via manifest funnel gates; full universe = **4,320,314 txs / 9,380,394
  legs**; the funnels' router column independently sums to 478,299 ✓.

## Integrity audit (pre-commit)

Zero incomplete manifest jobs; roster exactly 73,039; no duplicate
legs/txs/approvals from loading. Two documented source notes: 2 double-decoded
`dex.trades` legs (1inch-LOP overlap; gates unaffected), and 985 wallets
(1.3%) whose only stable approval predates the 2022-11 lookback bound.

## Cost / logistics

~10 community API keys (~25k credits) end to end; every fetch resumable
across keys via the manifest (deterministic ORDER BY + page offsets).
Datapoints ≈ 2,100/credit; comment-placeholder render gotcha fixed in two
query headers (was doubling payloads).

## Commits

- `14a002b` Sushi delta fetch + recomputed findings
- `365fb3a` Uniswap arm rebuild (5 fetch modules, 6 queries, audit doc)

## Deferred (documented with resume commands in `uniswap_arm_audit.md`)

1. Dec–Feb broad legs (~25k credits; `python -m dexresearch.fetch.uniswap_broad`)
2. Stage-1b per-wallet aggregates over the broad universe (~3k credits)
3. Approvals/permit2 for non-router wallets (scope after 1b)
4. Gas→USD price join; Polygon arm

## Next session

1. Uniswap analysis module (the `sushi_analysis` counterpart over
   `swaps_sampled` + `approvals` + `permit2_events`)
2. Cross-protocol default-vs-deliberate contrast (Permit2 81–88% unlimited
   vs Sushi 12% exact-leaning, same within-wallet paired design)
3. November broad-slice MEV/aggregator post-mortem
