# Out-of-band validator payments (coinbase transfers) — findings

**Date:** 2026-07-10. **Module:** `dexresearch/fetch/coinbase_transfers.py`.
**Result tables:** `reference.study_txs`, `reference.study_coinbase_transfers`.
**Per-row flag:** `coinbase_transfer_eth` on every `<table>_usd` view.

## Why this was checked

The study now reports the full EIP-1559 fee decomposition per transaction
(`gas_used`, `gas_price`, `base_fee_per_gas`, `priority_fee_per_gas` — the
last derived as effective gas price minus the block base fee, an exact
post-London identity). That decomposition captures the *in-protocol* tip only.
MEV searcher bundles routinely pay the block producer out-of-band instead: an
internal ETH transfer to `block.coinbase` inside the transaction, invisible in
every gas-price field. If study transactions carried such payments, the
priority fee would understate what was actually paid for inclusion.

## Method

1. Collect every distinct (tx_hash, block_number) across the 17 arm relations
   → `reference.study_txs`, 3,485,370 transactions (Aug 2022 – Feb 2026 span).
2. Scan `bigquery-public-data.crypto_ethereum.traces` for successful internal
   ETH transfers within those transactions whose recipient equals the block's
   `miner` (the builder/validator fee recipient), joined via the public
   `blocks` table → `reference.study_coinbase_transfers`.
3. One-time scan cost ~1.47 TB (~$9 on-demand); results persisted, all
   downstream joins are against our own small tables.

## Findings

273,542 of 3,485,370 study transactions (7.85%) contain at least one such
payment — 273,774 transfers, 3,206 ETH total. Every observed transfer is an
*internal* call (the searcher-bundle pattern); none are direct sends. The
distribution is dust-with-a-fat-tail: median 0.0001 ETH, p99 0.06 ETH, max
1,299 ETH in a single transfer.

**The hits concentrate almost entirely (94%) in `uniswap_v4.txs_broad`**, the
unfiltered "every transaction these wallets made" sample, where 26.33% of
transactions carry a coinbase transfer. The top payer alone
(`0xa69babef1ca67a37ffaf7a485dfff3382056e78c`, a contract) made 102,778
transfers. Conclusion: the broad Uniswap sample contains genuine MEV
searcher bots.

Per relation (full table in `samples/coinbase_transfer_rates.csv`):

| population | rate |
|---|---|
| qualifying swaps (`swaps_router_entry`, `swaps_sampled`) | **exactly 0%** |
| approvals, all four arms | 0.01–0.61% |
| permit2 events | 0.28–2.84% |
| lending actions (supplies/borrows/withdraws) | 0.67–5.18% |
| `uniswap_v4.txs_broad` | **26.33%** |

## Interpretation for the paper

- `priority_fee_per_gas` is a complete tip measure for the approval and swap
  populations — the core of the study. Footnote-able as verified, not assumed.
- Gas aggregates over `txs_broad` should exclude or segment rows where
  `coinbase_transfer_eth IS NOT NULL`: those senders are bots whose true
  inclusion payment is dominated by the bribe, not the tip.
- Lending-action rates of 2–5% are higher than a retail-only population would
  suggest; whether those wallets are contracts/bots is an open follow-up
  before quoting the lending gas numbers as retail behavior.
- Remaining blind spot (acknowledged limitation, standard in MEV literature):
  genuinely off-chain arrangements — exclusive order flow, private
  builder deals — are unobservable on-chain. Block-level builder→proposer
  bids are public via MEV-Boost relay APIs but cannot be attributed to
  individual user transactions, so they are out of scope here.
