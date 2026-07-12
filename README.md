# DexResearch

Quantifying the cost and behavior of ERC-20 `approve()` across DEX swaps and
lending protocols on Ethereum mainnet, Nov 2025 – Feb 2026. See
[`Research Plan.pdf`](./Research%20Plan.pdf) for scope and
[`ResearchDraft.pdf`](./ResearchDraft.pdf) for the current draft.

The study has **four protocol arms** spanning the unlimited-default spectrum:

| Arm | Type | Population | Qualifying action |
| --- | --- | --- | --- |
| Uniswap (V4-era routers) | swap | 73,039 wallets (fetched in full) | router-entry tx whose first leg sold a study stablecoin |
| SushiSwap V2 | swap | 5,184 wallets / 31,949 txs | router-entry stablecoin swap |
| Compound V3 | lending | 1,898 wallets | base-asset Supply direct to a Comet / Bulker |
| Aave V3 | lending | 22,132 wallets | stablecoin Supply direct to the V3 Pool |

Study stablecoins: USDC, USDT, DAI. Contracts, window, and sampling knobs are
pinned in [`config/study.yaml`](./config/study.yaml).

## Data sources and provenance

Three external sources, each used for the one thing it does best:

1. **Dune Analytics** (decoded protocol events) — all arm fetches; every query
   is version-controlled SQL in [`queries/`](./queries/), executed via the
   Execute SQL API by `dexresearch.dune_runner` (async, paged, manifest-
   resumable, credit-aware).
2. **BigQuery public `crypto_ethereum`** (raw chain facts) — per-block base
   fees for the EIP-1559 decomposition, and internal ETH transfers to the
   block producer (out-of-band MEV payments).
3. **Coinbase Exchange API** (ETH/USD hourly closes) — row-level gas→USD
   conversion.

**[`docs/data_provenance.md`](./docs/data_provenance.md) is the canonical
record**: one row per BigQuery relation (source SQL, source tables, window,
validation gate), the full reproduction runbook, and the list of deliberate
methodological seams. Start there.

## Architecture

```
Dune (queries/<arm>/*.sql)      BigQuery public dataset      Coinbase API
        │                               │                        │
  fetch.<arm>_* modules          fetch.base_fees           fetch.eth_usd_prices
  (gate-validated loads)         fetch.coinbase_transfers        │
        ▼                               ▼                        ▼
  BigQuery datasets:             reference.block_base_fees, reference.eth_usd_hourly,
  sushiswap_v2, uniswap_v4,      reference.study_coinbase_transfers
  compound_v3, aave_v3                  │
        └───────────────┬───────────────┘
                        ▼
        process.usd_views → <table>_usd views (18)
   per-row eth_usd, gas_cost_usd, base/priority fee, coinbase_transfer_eth
                        ▼
        process.<arm>_analysis → data/analysis/*.csv
   (committed in datasets/analysis; raw events in datasets/raw_events)
```

Design invariants:

- **Fetch tables are immutable once gate-validated.** Every arm load must
  exactly match an independently pre-measured Dune funnel count, or it fails.
  Derived quantities (USD, fee decomposition) are *views* on top.
- **Everything is resumable.** `data/executions/manifest.json` tracks every
  execution id and page offset; credit exhaustion (`CreditError`) means "swap
  the `.env` Dune key and re-run" — nothing is re-bought.
- **One approval semantics across arms.** The Sushi arm's `approvals.sql` /
  `permit2_events.sql` are reused verbatim for all four rosters, so cross-arm
  approval comparisons are apples-to-apples.

## Reproducing

Full runbook with ordering and cost notes:
[`docs/data_provenance.md` §4](./docs/data_provenance.md). Short form:

```bash
# arm fetches (Dune → BigQuery)
python -m dexresearch.fetch.sushi_timeline && python -m dexresearch.fetch.sushi_delta
python -m dexresearch.fetch.uniswap_wallets && python -m dexresearch.fetch.uniswap_sample
python -m dexresearch.fetch.uniswap_topup   && python -m dexresearch.fetch.uniswap_broad
python -m dexresearch.fetch.compound_timeline
python -m dexresearch.fetch.aave_timeline

# reference joins (no Dune)
python -m dexresearch.fetch.eth_usd_prices
python -m dexresearch.fetch.base_fees
python -m dexresearch.fetch.coinbase_transfers

# derived views + analysis
python -m dexresearch.process.usd_views
python -m dexresearch.process.sushi_analysis
python -m dexresearch.process.uniswap_analysis
python -m dexresearch.process.compound_analysis
python -m dexresearch.process.aave_analysis
```

Every fetch module supports `--status`. Analysis CSVs land in
`data/analysis/` (gitignored); the full tables — analysis outputs plus raw
per-event exports with the fee decomposition — are committed under
[`datasets/`](./datasets/) (see its README for the file map and loader).

## Setup

Prerequisites: Python 3.14, `gcloud` CLI, a Dune account (community plan
works — expect to rotate API keys on long fetches), GCP project with billing.

```bash
source .venv/bin/activate
pip install -r requirements.txt
gcloud auth application-default login
gcloud config set project dex-research
cp .env.example .env   # then set DUNE_API_KEY
```

## Findings & docs

| Doc | What it holds |
| --- | --- |
| [`docs/data_provenance.md`](./docs/data_provenance.md) | **canonical provenance + reproduction runbook** |
| [`docs/sushiswap_v2_findings.md`](./docs/sushiswap_v2_findings.md) / [`_methodology`](./docs/sushiswap_v2_methodology.md) / [`_run_log`](./docs/sushiswap_v2_run_log.md) | Sushi arm results, definitions, fetch log |
| [`docs/uniswap_v4_findings.md`](./docs/uniswap_v4_findings.md) / [`docs/uniswap_arm_audit.md`](./docs/uniswap_arm_audit.md) | Uniswap arm results; audit of the legacy fetch |
| [`docs/compound_v3_findings.md`](./docs/compound_v3_findings.md) | Compound arm results (incl. binary `allow()` regime) |
| [`docs/aave_v3_findings.md`](./docs/aave_v3_findings.md) | Aave arm results (incl. credit delegation) |
| [`docs/mev_out_of_band_findings.md`](./docs/mev_out_of_band_findings.md) | coinbase-transfer scan: bots vs behavioral tables |

Headline thread across arms: the unlimited-vs-exact approval choice is a
property of the **spender's interface**, not of the user — the same wallet,
on the same token, accepts unlimited from one frontend and exact from another.

## Legacy

The original single-query GCS pipeline (`fetch.timeline`,
`raw.*` external tables, the never-wired `fetch.{supplies,borrows,
gas_measurements,wallet_populations}` stubs) predates the harmonized arms and
is retained only as the pool-touch contrast population and for audit — no
headline number reads from it. See `docs/data_provenance.md` §3 ("`raw` —
legacy") and `docs/uniswap_arm_audit.md`.
