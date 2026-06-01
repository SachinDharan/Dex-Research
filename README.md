# DexResearch

Quantifying the cost and behavior of ERC-20 `approve()` on DEX swaps and
lending protocols, for the Nov 2025 – Feb 2026 window. See
[`Research Plan.pdf`](./Research%20Plan.pdf) for the full scope and methodology.

## What this repo does

The pipeline pulls on-chain data from Dune Analytics, classifies every
`approve()` event a sampled wallet emits, and writes per-wallet and
per-decile metrics to a GCS bucket as Parquet.

Data flows in two layers — **fetch** (raw Dune extracts) and **process**
(derived analysis).

| Stage      | Module                                  | GCS prefix                         |
| ---------- | --------------------------------------- | ---------------------------------- |
| fetch      | `dexresearch.fetch.wallet_populations`  | `raw/wallet_populations/`          |
| fetch      | `dexresearch.fetch.timeline`            | `raw/swaps/`, `raw/approvals/`, `raw/permit2_events/` |
| fetch      | `dexresearch.fetch.supplies`            | `raw/supplies/`                    |
| fetch      | `dexresearch.fetch.borrows`             | `raw/borrows/`                     |
| fetch      | `dexresearch.fetch.gas_measurements`    | `raw/gas_measurements/`            |
| process    | `dexresearch.process.deciles`           | `processed/deciles/`               |
| process    | `dexresearch.process.metrics`           | `processed/wallet_metrics/`, `processed/decile_metrics/` |

The Uniswap V4 fetch (`fetch.timeline`) is one query split into three flat files:
`raw/swaps/ethereum_uniswap_v4.parquet`, `raw/approvals/ethereum_uniswap_v4.parquet`,
and `raw/permit2_events/ethereum_uniswap_v4.parquet`. Every uploaded parquet may
carry a sidecar `<blob>.meta.json` recording the execution id, source SQL,
parameters, and `fetched_at` timestamp.

The top-level `metadata/` prefix is reserved for future cross-run artifacts
(study manifests, schema registries) — currently unused.

## Setup

Prerequisites: Python 3.14, `gcloud` CLI, a Dune account, a GCP project with
billing enabled.

```bash
# 1. Activate the venv (created with Python 3.14)
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Authenticate to GCP (one-time per machine)
gcloud auth application-default login
gcloud config set project dex-research

# 4. Create the bucket if it doesn't exist
gcloud storage buckets create gs://dex-research-data --location=us-central1

# 5. Copy .env.example -> .env and fill in DUNE_API_KEY
cp .env.example .env
# then edit .env
```

## Dune queries

SQL templates live in [`queries/`](./queries/), one subfolder per protocol arm
(`queries/uniswap_v4/`, etc.) — version-controlled and reviewable.

The Uniswap V4 fetch is a **single query** (`queries/uniswap_v4/timeline.sql`)
that returns swaps + approvals + permit2 events together. It runs **directly** via
Dune's Execute SQL endpoint (`POST /api/v1/sql/execute`, Read scope) — no saved
query, no pasted ids, no dune.com step. `dexresearch.dune_runner` reads the `.sql`,
substitutes its `{{params}}` (`dune.render_named_sql`), submits it, polls, and
pages the result reads. It's async, resumable, and credit-aware:

- an execution-id **manifest** (`data/executions/manifest.json`) means a rerun
  reuses a completed execution instead of paying to run it again;
- a stage **skips** when its output parquet already exists (reads cost credits too);
- result reads are **paged**, avoiding the "response too large / not enough
  credits" error.

Legacy/other stages can still use the saved-query path
(`dune.run_named_query("<arm>/<dataset>")` against a numeric id in
`config/study.yaml`), since saved-query CRUD via API needs an Analyst plan.

## Layout

```
DexResearch/
├── .env / .env.example     # secrets and runtime config
├── config/study.yaml       # protocols, chains, months, addresses, Dune query IDs
├── queries/                # Dune SQL templates (mirrored on dune.com)
│   ├── uniswap_v4/         # active arm (this approval-cost study)
│   │   └── timeline.sql    # one query: swaps + approvals + permit2
│   ├── swaps.sql           # legacy generic templates (superseded; migrate per-arm)
│   ├── approvals.sql
│   ├── supplies.sql
│   ├── borrows.sql
│   ├── wallet_populations.sql
│   └── gas_measurements.sql
├── dexresearch/
│   ├── config.py           # loads .env + study.yaml
│   ├── dune.py             # Dune client wrapper (simple one-shot runs)
│   ├── dune_runner.py      # async, paged, resumable, credit-aware runner
│   ├── gcs.py              # GCS upload/download (with sidecar metadata)
│   ├── classify.py         # unlimited / exact / revocation classifier
│   ├── fetch/              # raw Dune extracts
│   │   ├── _common.py      # window dates + output-exists check
│   │   ├── timeline.py     # the Uniswap V4 fetch (one query -> 3 datasets)
│   │   ├── wallet_populations.py
│   │   ├── supplies.py
│   │   ├── borrows.py
│   │   └── gas_measurements.py
│   └── process/            # derived analysis
│       ├── deciles.py
│       └── metrics.py
├── data/                   # local parquet cache (gitignored)
└── Research Plan.pdf
```

## Running

### Approval-cost study (Uniswap V4)

One query, one command — no flags, no confirmation gate. It returns the swaps,
their wallets' Permit2 approvals (18-mo lookback), and their Permit2 events, then
writes the three datasets. Idempotent: it skips when the outputs already exist and
reuses cached executions, so re-running never re-spends credits; reads are paged.

```bash
source .venv/bin/activate

python -m dexresearch.fetch.timeline
#   or equivalently: python3 dexresearch/fetch/timeline.py
#   add --force to re-fetch even if the outputs already exist
```

Tune page size, engine tier, and concurrency under `dune:` in `config/study.yaml`.
Output goes to `data/` (local cache mirroring GCS) and `gs://$GCS_BUCKET/…`.

### Legacy multi-protocol flow (sample-first)

The original wallet-population sampling stages remain for the other protocol
arms once their contracts are verified (`TODO_VERIFY` in `config/study.yaml`):

```bash
python -m dexresearch.fetch.wallet_populations
python -m dexresearch.process.deciles
python -m dexresearch.fetch.gas_measurements
python -m dexresearch.process.metrics
```

## Reproducing

Anyone with read access to the GCS bucket and a Dune account can re-run any
stage independently: every stage's input is the previous stage's parquet at a
deterministic path, and every Dune query's SQL is in [`queries/`](./queries/).
