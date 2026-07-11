# Sample data (for quick review)

Small excerpts of the analysis CSVs, committed so the outputs can be skimmed
without re-running the pipeline. The full files live in the local `data/`
cache (gitignored, re-fetchable from BigQuery/GCS) and range from a few MB to
~80 MB. Ethereum mainnet only; study window Nov 2025 – Feb 2026.

Four arms, one prefix each: `sushi_` (SushiSwap), `uniswap_` (Uniswap),
`compound_` (Compound V3), `aave_` (Aave V3).

## Summary files (complete, they're tiny)

- `<arm>_monthly.csv` — per-month activity: swaps, approvals, revocations,
  unlimited grants, revoke share.
- `<arm>_decile_metrics.csv` — wallet-activity deciles: tx counts, approval
  ratios, gas spend.
- `<arm>_default_vs_deliberate.csv` — paired-spender comparison of unlimited
  vs exact approvals (default-following vs deliberate amounts).
- `uniswap_entry_routers.csv` — entry-router breakdown for the Uniswap arm.
- `coinbase_transfer_rates.csv` — per-relation share of txs paying the block
  producer out-of-band (MEV coinbase transfers); see
  `docs/mev_out_of_band_findings.md`.

## Row samples (first 50 rows of the full files)

- `<arm>_wallet_features.sample.csv` — per-wallet behavioral features
  (activity, gas, approval habits, bot flags). Full files: 55k–200k+ wallets.
- `<arm>_outstanding_allowances.sample.csv` — per-approval-event rows with
  outstanding/unlimited status and gas cost.
- `aave_outstanding_delegation.sample.csv` — Aave credit-delegation events
  (approveDelegation), the arm's analogue of ERC-20 allowances.

Wallet addresses and tx hashes are public on-chain data.
