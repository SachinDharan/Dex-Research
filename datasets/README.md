# Datasets

Complete, analysis-ready exports of the study's data. Everything here is a
plain CSV (some gzip-compressed, some split into parts to stay under GitHub's
file-size limits). The easiest way to read any table:

```python
from load import load          # load.py in this folder; requires pandas
df = load("uniswap_outstanding_allowances")
```

`python load.py` prints every available table name. Reading without the
helper also works: `pd.read_csv(...)` opens `.csv` and `.csv.gz` alike, and a
table split into `name.part1.csv`, `name.part2.csv`, ... is one logical table
— each part repeats the header, so concatenate them:

```python
import glob, pandas as pd
df = pd.concat((pd.read_csv(p) for p in sorted(glob.glob("analysis/uniswap_outstanding_allowances.part*.csv"))),
               ignore_index=True)
```

The study covers four protocols on Ethereum, each with the same file set:
Uniswap (`uniswap_*`), SushiSwap (`sushi_*`), Aave (`aave_*`), and
Compound (`compound_*`).

## `analysis/` — computed results

| Table (per protocol) | One row per | Contents |
|---|---|---|
| `*_outstanding_allowances` | wallet + token + spender | The **most recent** approval for each combination as of the study window's end — amount, whether unlimited, whether revoked — plus the full cost of the transaction that set it (see fee columns below). |
| `*_wallet_features` | wallet | Behavioral summary: transaction counts, active days/months, gas spent, volume, approval habits, and the bot-classification flags with the signals behind them. |
| `*_monthly` | calendar month | Counts of the protocol's core actions and of approvals (total / unlimited / revocations) per month. |
| `*_decile_metrics` | activity decile | Wallets ranked by activity and cut into ten buckets; medians per bucket. |
| `*_default_vs_deliberate` | spender pair | Same wallet + token approving two different apps: shows the app's interface, not the wallet, decides unlimited-vs-exact. |

Protocol-specific extras: `aave_outstanding_delegation` (credit delegation,
same shape as allowances), `compound_outstanding_allow` (Compound's binary
manager permission), `sushi_wallet_cohorts` (features + cohort label),
`sushi_searcher_contracts` and `uniswap_entry_routers` /
`uniswap_broad_nov_entries` (contract-level rosters), `coinbase_transfer_rates`
(share of transactions paying the block producer directly, a bot signal).

## `raw_events/` — full event history

Every individual on-chain event the study observed, one row per event, with
per-transaction fee detail. Unlike `analysis/`, nothing is summarized: all
supplies, borrows, withdrawals, swaps, delegation changes, manager grants,
and Permit2 events appear individually.

- `uniswap_txs_broad`, `uniswap_swaps_sampled`, `sushi_swaps_all` — swap
  activity (tx-level for the broad Uniswap set; leg-level elsewhere, so a
  multi-hop trade repeats its tx_hash once per hop with identical gas columns).
- `uniswap_legs_broad` — per-hop routing detail (venue, tokens) for the broad
  Uniswap set. Carries no gas columns; join to `uniswap_txs_broad` on
  `tx_hash` for fees.
- `aave_supplies` / `aave_borrows` / `aave_delegation_events`,
  `compound_supplies` / `compound_withdraws` / `compound_allow_events`,
  `*_permit2_events` — one row per event, with fees.

Approvals are the one event type not exported raw: `analysis/` ships them in
latest-per-(wallet, token, spender) form, which is what determines a wallet's
live exposure. The complete approval history exists in the project's BigQuery
dataset — ask if you need it.

## Fee columns (identical meaning everywhere)

| Column | Meaning |
|---|---|
| `block_number` | Ethereum block containing the transaction. |
| `gas_used` | Gas units the transaction consumed. |
| `gas_price` | **Effective** price paid per gas unit, in wei (this is what was actually charged, not a quoted cap). |
| `base_fee_per_gas` | The block's burned base fee per gas unit (EIP-1559), in wei. |
| `priority_fee_per_gas` | Realized tip to the block producer: `gas_price − base_fee_per_gas`, in wei. Zero usually means the transaction reached the builder privately. |
| `max_priority_fee_per_gas` | The tip **cap** the sender declared — an intent field; the realized tip above is what was paid. |
| `gas_cost_eth` | `gas_used × gas_price`, in ETH. |
| `eth_usd` | ETH/USD hourly close (Coinbase) at the transaction's hour. |
| `gas_cost_usd` | `gas_cost_eth × eth_usd`. |
| `coinbase_transfer_eth` | ETH paid to the block producer via direct internal transfer (out-of-band MEV payment channel). Empty means none; non-empty flags likely bot/searcher activity. |

Wei is ETH × 10⁻¹⁸; divide the per-gas columns by 10⁹ for the conventional
gwei. Amount columns (`amount_raw`) are token base units as decimal **strings**
— they overflow 64-bit integers, so parse with Python ints, not int64.
