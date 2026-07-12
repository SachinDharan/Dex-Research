# Data provenance & reproducibility

Canonical record of where every relation in the study comes from, how it was
validated, and how to rebuild it from scratch. One row per BigQuery relation;
the modules and SQL files named here are the ground truth and live in this
repo.

Study scope: Ethereum mainnet only, activity window **Nov 2025 – Feb 2026**
(`config/study.yaml`), with per-stage lookbacks noted below.

## 1. Data sources

The study uses exactly three external sources, each chosen for the one thing
it does best. No measure mixes sources without an explicit join documented
here.

| Source | Role | What it provides | Access | Citation |
| --- | --- | --- | --- | --- |
| **Dune Analytics** | Decoded protocol events | Qualifying-action discovery, swaps, supplies/borrows, ERC-20 approvals, Permit2 events, credit delegation | Execute SQL API (`dexresearch.dune_runner`); every query text in `queries/` | Dune decoded tables, listed per relation below |
| **BigQuery public `crypto_ethereum`** | Raw chain facts | Per-block base fees (EIP-1559 decomposition); internal ETH transfers to the block producer (out-of-band MEV payments) | Direct SQL in `dex-research` project (Google-hosted public dataset) | `bigquery-public-data.crypto_ethereum` (`blocks`, `traces`) |
| **Coinbase Exchange API** | ETH/USD prices | Hourly OHLC candles, 2022-08 → 2026-03 | Keyless public REST (`fetch.eth_usd_prices`) | "Coinbase ETH-USD hourly close" |

Why three sources rather than one: Dune is the only source with maintained
decoded event tables for all four protocols; the BigQuery public dataset is
free, Google-maintained, and citable for raw chain fields that need no
decoding; and USD prices are off-chain by nature. Gas → USD conversion happens
at **row level** (hourly price at `block_time`) before any aggregation — ETH
moved ~3× across the lookback, so converting aggregates with a single price
would be wrong (`process.prices`).

### Dune decoded tables read (complete list)

Derived from `grep FROM|JOIN queries/*/*.sql`:

`dex.trades`, `ethereum.transactions`, `ethereum.logs`, `tokens.erc20`,
`erc20_ethereum.evt_approval`,
`uniswap_v3_ethereum.permit2_evt_{approval,permit,lockdown}`,
`uniswap_v3_ethereum.permit2_call_permit{,witness}transferfrom`,
`aave_v3_ethereum.pool_evt_{supply,borrow}`,
`aave_v3_ethereum.variabledebttoken_evt_borrowallowancedelegated`,
`compound_v3_ethereum.c{usdc,usdt}v3_evt_{supply,withdraw}`.

Note the two deliberate uses of **raw** `ethereum.logs` instead of decoded
tables: Compound `allow_events` (the decoded Comet allowance table was found
stale/broken — see `docs/compound_v3_findings.md`) and parts of the Sushi
delta validation. Raw-log fetches are gated against pre-measured log counts.

## 2. Pipeline shape

```
                Dune (decoded events)          BigQuery public          Coinbase API
                       │                       crypto_ethereum               │
   queries/<arm>/*.sql │                      (blocks, traces)               │
                       ▼                              │                      ▼
        dexresearch.fetch.<arm>_*             fetch.base_fees        fetch.eth_usd_prices
        (staged, manifest-resumable,          fetch.coinbase_transfers      │
         hard validation gates)                       │                     │
                       ▼                              ▼                     ▼
        BigQuery: sushiswap_v2, uniswap_v4,     reference.block_base_fees,
        compound_v3, aave_v3 (immutable         reference.study_coinbase_transfers,
        fetch tables)                           reference.eth_usd_hourly
                       │                              │
                       └──────────────┬───────────────┘
                                      ▼
                     process.usd_views → <table>_usd views
              (per-row eth_usd, gas_cost_usd, base/priority fee,
               coinbase_transfer_eth — views, so fetch tables stay immutable)
                                      ▼
                     process.<arm>_analysis → data/analysis/*.csv
        (committed in datasets/analysis; raw per-event exports of the
         non-approval _usd views committed in datasets/raw_events)
```

Every Dune fetch is **manifest-resumable** (`data/executions/manifest.json`):
each job records its execution id and page offset, so credit exhaustion or
interruption never re-buys or re-executes anything. Every arm fetch ends in a
**hard validation gate**: BigQuery row counts must exactly equal an
independently pre-measured Dune funnel count, or the run fails.

## 3. Relation-by-relation provenance

"Module" = the `dexresearch.fetch`/`process` module that produced it.
"Gate" = the equality check that validated the load.

### `sushiswap_v2` — SushiSwap V2 arm (population: 5,184 wallets / 31,949 router-entry txs)

| Relation | Kind | Module | Source SQL | Source data | Window / lookback | Gate |
| --- | --- | --- | --- | --- | --- | --- |
| `swaps` | table | `fetch.sushi_timeline` | `sushiswap_v2/swaps.sql` | `dex.trades` + `ethereum.transactions` | Nov 2025 – Feb 2026 | pre-measured funnel counts |
| `swaps_txto_delta` | table | `fetch.sushi_delta` | `sushiswap_v2/swaps_txto_delta.sql` | same, tx_to-anchored, anti-joined vs pool anchor | Nov 2025 – Feb 2026 | Dune query 7922955: 30,300 txs / 41,723 legs / 5,022 wallets, zero overlap with base |
| `approvals`, `approvals_delta` | tables | `fetch.sushi_timeline` / `fetch.sushi_delta` | `sushiswap_v2/approvals.sql` | `erc20_ethereum.evt_approval` (study stablecoins, ANY spender) | since Permit2 genesis, yearly chunks | per-batch manifest completeness |
| `permit2_events`, `permit2_events_delta` | tables | same pair | `sushiswap_v2/permit2_events.sql` | `uniswap_v3_ethereum.permit2_evt_*` incl. Lockdown | study window | per-batch manifest completeness |
| `swaps_router_entry` | view | `fetch.sushi_delta` | — | base ∪ delta, swap_count recomputed | — | **the study population** |
| `approvals_all`, `permit2_events_all` | views | `fetch.sushi_delta` | — | base ∪ delta | — | — |
| `*_dryrun_nov_w1` | tables | `fetch.sushi_timeline` (dry run) | — | 1-week Nov slice | — | superseded; kept for audit |

### `uniswap_v4` — Uniswap arm (population: 73,039 router-entry wallets, fetched in full)

| Relation | Kind | Module | Source SQL | Source data | Window / lookback | Gate |
| --- | --- | --- | --- | --- | --- | --- |
| `wallet_aggregates` | table | `fetch.uniswap_wallets` | `uniswap_v4/wallet_aggregates.sql` | `dex.trades` + `ethereum.transactions`, resolved `UNISWAP_ROUTERS` entry set | Nov 2025 – Feb 2026, per month | Dune funnel query 7923299 |
| `sample_wallets` | table | `fetch.uniswap_sample` stage A (+ topup insert) | BigQuery CTAS (free) | `wallet_aggregates`, deterministic FARM_FINGERPRINT strata | — | nested/reproducible strata; ends = full roster (73,039) |
| `swaps_sampled` | table | `fetch.uniswap_sample` + `fetch.uniswap_topup` | `uniswap_v4/swaps_sampled.sql` | `dex.trades`, leg-level | study window | totals = funnel (478,299 txs / 735,223 legs) AND per-wallet counts = `wallet_aggregates` |
| `approvals`, `permit2_events` | tables | same pair | **reused** `sushiswap_v2/approvals.sql`, `permit2_events.sql` | as in Sushi arm | Permit2 genesis / study window | per-batch manifest completeness |
| `txs_broad`, `legs_broad` | tables | `fetch.uniswap_broad` | `uniswap_v4/txs_broad.sql`, `legs_broad.sql` | `dex.trades` + `ethereum.transactions`, pool-touch NON-router txs | study window, per month | `broad_funnel.sql` counts recorded before fetch, equality after |
| `swaps_broad` | view | `fetch.uniswap_broad` | — | wide reassembly of txs+legs, symbols from owned rows | — | — |
| `swaps_option2` | view | `fetch.uniswap_broad` | — | router-entry ∪ broad = complete pool-touch population | — | — |
| `topup_wallets`, `token_symbols` | table / view | `fetch.uniswap_topup` / `fetch.uniswap_broad` | BigQuery-internal | — | — | — |

### `compound_v3` — Compound V3 arm (roster: 1,898 qualifying wallets)

Qualifying action: base-asset Supply on cUSDCv3/cUSDTv3 sent directly to a
Comet or the MainnetBulker; manager/vault flow stored as contrast, split by
`tx_to`.

| Relation | Kind | Module | Source SQL | Source data | Window / lookback | Gate |
| --- | --- | --- | --- | --- | --- | --- |
| `supplies` | table | `fetch.compound_timeline` | `compound_v3/supplies.sql` | `compound_v3_ethereum.c*v3_evt_supply` | study window | per-month counts = `funnel.sql` |
| `withdraws` | table | same | `compound_v3/withdraws.sql` | `c*v3_evt_withdraw` (borrows reconstructed downstream — Comet emits no Borrow event) | study window | per-month counts = funnel |
| `allow_events` | table | same | `compound_v3/allow_events.sql` | **raw `ethereum.logs`** (decoded Comet allowance table is stale) | since Comet genesis 2022-08, yearly chunks | in-window monthly counts = pre-measured log counts |
| `approvals`, `permit2_events` | tables | same | reused `sushiswap_v2/*.sql` | as in Sushi arm | Permit2 genesis / study window | per-batch manifest completeness |

### `aave_v3` — Aave V3 arm (roster: 22,132 qualifying supplier wallets)

Qualifying action: Supply of USDC/USDT/DAI on the V3 Pool
(`0x87870b…4fa4e2`), tx sent directly to the Pool; aggregator/manager flow
stored as contrast, split by `tx_to`.

| Relation | Kind | Module | Source SQL | Source data | Window / lookback | Gate |
| --- | --- | --- | --- | --- | --- | --- |
| `supplies` | table | `fetch.aave_timeline` | `aave_v3/supplies.sql` | `aave_v3_ethereum.pool_evt_supply` | study window | per-month counts = `funnel.sql` |
| `borrows` | table | same | `aave_v3/borrows.sql` | `pool_evt_borrow` (secondary action, no ERC-20 approval) | study window | per-month counts = funnel |
| `delegation_events` | table | same | `aave_v3/delegation_events.sql` | `variabledebttoken_evt_borrowallowancedelegated` (stables) | since V3 genesis 2023-01, yearly chunks | manifest completeness |
| `approvals`, `permit2_events` | tables | same | reused `sushiswap_v2/*.sql` | as in Sushi arm | Permit2 genesis / study window | per-batch manifest completeness |

### `reference` — non-Dune joins shared by every arm

| Relation | Kind | Module | Source | Span | Notes |
| --- | --- | --- | --- | --- | --- |
| `eth_usd_hourly` | table | `fetch.eth_usd_prices` | Coinbase Exchange candles API (keyless) | 2022-08-01 → 2026-03-01 hourly (~31k rows) | stored raw; tradeless hours NOT filled here |
| `eth_usd_hourly_filled` | view | `process.usd_views` | forward-fill of the above | same | what the `_usd` views join |
| `block_base_fees` | table | `fetch.base_fees` | `bigquery-public-data.crypto_ethereum.blocks` (one CTAS) | 2022-08-01 → 2026-03-01 | enables exact `priority_fee = gas_price − base_fee` post-London |
| `study_txs` | table | `fetch.coinbase_transfers` | union of all 17 arm relations (~3.5M distinct txs) | — | scope table for the traces scan |
| `study_coinbase_transfers` | table | `fetch.coinbase_transfers` | `bigquery-public-data.crypto_ethereum.traces` | study txs only | out-of-band MEV payments to `block.coinbase`; run 2026-07-10 (~1.5 TB scan); findings in `docs/mev_out_of_band_findings.md` |

### `<table>_usd` views (18, one per arm relation with gas columns)

`sushiswap_v2.swaps_all` (view, added 2026-07-11) unions `swaps` +
`swaps_txto_delta` — the full qualifying leg population, of which
`swaps_router_entry` is the router-entry subset — so it too gets a `_usd`
wrapper. The analysis loaders read from the `_usd` views, so every per-event
CSV carries `block_number`, `gas_price`, and the fee decomposition.

Created by `process.usd_views` (idempotent `CREATE OR REPLACE`). Each wraps
its base relation with per-row `eth_usd` (hourly close at `block_time`),
`gas_cost_usd`, `base_fee_per_gas` / `priority_fee_per_gas`, and
`coinbase_transfer_eth` (NULL = no out-of-band payment). **Views, not
columns**: the gate-validated fetch tables stay immutable, and a price or
base-fee refresh propagates everywhere without re-fetching.

### `raw` — legacy (contrast only)

`raw.swaps`, `raw.approvals`, `raw.permit2_events` are EXTERNAL tables over
the original single-query GCS fetch (`fetch.timeline`,
`queries/uniswap_v4/timeline.sql`). That fetch predates the harmonized
Uniswap arm and has known audit findings (`docs/uniswap_arm_audit.md` —
notably no `tx_to` stored). **No headline number reads from `raw.*`**; the
tables are retained as the pool-touch contrast population and as
cross-checks. The stub modules `fetch.{supplies,borrows,gas_measurements,
wallet_populations}` from the same era were never wired up and fetch nothing.

## 4. Reproduction runbook

Prerequisites: Python 3.14 venv (`requirements.txt`), `gcloud` auth against a
GCP project with billing, one or more Dune API keys in `.env`
(`DUNE_API_KEY`; community plan ≈ 2,500 credits/mo — the Uniswap broad fetch
alone consumed ~60k credits, so expect to rotate keys and rely on resume).

Order matters only where noted; each command is resumable and skips completed
work.

```bash
# ── Arm fetches (Dune → BigQuery, gate-validated) ──────────────────
python -m dexresearch.fetch.sushi_timeline        # sushiswap_v2 base
python -m dexresearch.fetch.sushi_delta           # + router-entry delta, views
python -m dexresearch.fetch.uniswap_wallets       # uniswap stage 1: aggregates
python -m dexresearch.fetch.uniswap_sample        # stage 2: stratified detail
python -m dexresearch.fetch.uniswap_topup         # stage 3: full 73,039 roster
python -m dexresearch.fetch.uniswap_broad         # non-router pool-touch universe
python -m dexresearch.fetch.compound_timeline     # compound_v3 arm
python -m dexresearch.fetch.aave_timeline         # aave_v3 arm

# ── Reference joins (no Dune) ───────────────────────────────────────
python -m dexresearch.fetch.eth_usd_prices        # Coinbase hourly closes
python -m dexresearch.fetch.base_fees             # public blocks → base fees
python -m dexresearch.fetch.coinbase_transfers    # public traces → MEV payments
                                                  # (~1.5 TB scan, ~$9; re-run only
                                                  #  if arm tables gain new txs)

# ── Derived layer ───────────────────────────────────────────────────
python -m dexresearch.process.usd_views           # 18 <table>_usd views

# ── Analysis (reads BigQuery, writes data/analysis/*.csv) ───────────
python -m dexresearch.process.sushi_analysis
python -m dexresearch.process.uniswap_analysis
python -m dexresearch.process.compound_analysis
python -m dexresearch.process.aave_analysis
```

Every `fetch.*` module supports `--status` to report manifest progress, and
resumes cleanly after `CreditError` (swap the `.env` key, re-run).

Determinism notes: the window and contract set are pinned in
`config/study.yaml`; the Uniswap sampling strata are deterministic
(`FARM_FINGERPRINT`) and nested, so re-runs and enlargements reproduce or
extend — never reshuffle — the sample; Dune decoded tables are append-only
for a closed historical window, so re-executing the same SQL over Nov 2025 –
Feb 2026 returns the same rows (the funnel gates would catch any drift).

## 5. Known seams (deliberate, and why)

These are the places where the method visibly changes shape mid-pipeline.
Each is intentional; this section is the paper's "threats to validity"
starting point.

1. **Sushi base + delta rather than one fetch.** The first Sushi fetch was
   pool-anchored and captured only 5.2% of the router-entry population. The
   fix was an *additive* tx_to-anchored delta fetch, gated for zero overlap,
   unioned in views — the validated base tables were never mutated. Findings
   were recomputed on the full population (`docs/sushiswap_v2_findings.md`).
2. **One approvals/permit2 SQL reused across all four arms.** The Sushi arm's
   `approvals.sql` / `permit2_events.sql` are reused verbatim (parameterized
   by roster) for Uniswap, Compound, and Aave. This is a feature: identical
   approval semantics across arms is what makes cross-arm comparison valid.
3. **Compound `allow()` from raw logs.** Dune's decoded Comet allowance table
   is stale; raw `ethereum.logs` with topic filtering is used instead, gated
   against independently measured log counts. Comet's allow is binary
   (allow/disallow), unlike ERC-20 amounts — handled downstream, never summed
   with ERC-20 allowances.
4. **Two-layer Uniswap approvals never summed.** ERC-20→Permit2 grants and
   Permit2-internal permits are different security objects; all Uniswap
   approval metrics keep the layers separate
   (`docs/uniswap_arm_audit.md`, memory of analysis decisions).
5. **Gas economics come from three sources by design.** Effective gas price
   is stored per event (Dune, at fetch time); base fee per block comes from
   the public dataset (identity: `priority = effective − base`); USD comes
   from Coinbase hourly closes. Each is the authoritative source for its
   field, and the join keys (`block_number`, `TIMESTAMP_TRUNC(block_time,
   HOUR)`) are exact.
6. **Legacy `raw.*` retained but quarantined** — see §3. It documents the
   original method's defects rather than hiding them.
