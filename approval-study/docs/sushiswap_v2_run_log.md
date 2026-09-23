# SushiSwap V2 arm — fetch run log (July 2026)

Operational record of getting the arm from design to data. The methodology
rationale is in `sushiswap_v2_methodology.md`.

## Final result

Dataset `dex-research.sushiswap_v2` (BigQuery native tables, loaded directly
from Dune — no GCS/CSV intermediate). Window **2025-11-01 → 2026-03-01**
(half-open), Ethereum.

| table | rows | distinct txs | time bounds |
|---|---|---|---|
| `swaps` | 474,147 legs | 66,500 | 2025-11-01 00:08 → 2026-02-28 23:45 |
| `approvals` | 440,697 | 436,645 | 2022-11-01 → 2026-02-28 (lookback by design) |
| `permit2_events` | 210,600 | 209,715 | 2025-11-01 00:00 → 2026-02-28 23:59 |

Validation: 66,500 distinct qualifying txs **exactly matches** the independent
Dune funnel count run before the fetch; zero duplicate `(tx_hash, evt_index)`
legs; all four months present in all tables with smooth volumes; dry-run week
(Nov 1–8) preserved in `*_dryrun_nov_w1` snapshot tables.

Monthly swaps: Nov 16,465 txs / Dec 15,600 / Jan 15,529 / Feb 18,906.
Wallets: 11,614 total qualifying.

## Architecture (what finally worked)

`python -m dexresearch.fetch.sushi_timeline` — staged fetch, each Dune
execution small, pages appended straight into BigQuery:

1. `queries/sushiswap_v2/swaps.sql` — leg-level qualifying swaps (1 execution)
2. wallet set read back from the BQ `swaps` table (free)
3. `queries/sushiswap_v2/approvals.sql` — per (wallet batch of 8,000 × yearly
   chunk since 2022-11): 2 × 4 = 8 executions
4. `queries/sushiswap_v2/permit2_events.sql` — per wallet batch: 2 executions

Per-job manifest keys (`data/executions/manifest.json`) record page offsets;
`--status` prints the full checklist + BQ row counts; `--window-start/--end`
override for dry runs (window change → meta-hash change → clean table reset).

## Issues hit, in order

1. **Execute SQL API caps executions at 2 minutes** (plan-level;
   `performance` tier param is ignored — both "free" and default tiers died
   at exactly 124s with `FAILED_TYPE_EXECUTION_TIMEOUT`). The original
   one-query design (swaps + approvals-since-2022 + `ethereum.transactions`
   join) cannot fit. The dune.com web editor does not have this cap, which is
   why manual runs historically worked. **Fix:** decompose into the staged
   queries above; every one runs well under the cap.

2. **Datapoint economics.** Reads are billed at roughly **~2,100 datapoints
   per credit** (measured: 250k rows × 20 cols = 5M datapoints ≈ 2,367
   credits), datapoints = rows × columns *including NULLs*. Consequences:
   the leg-level everything-fetch cost ≈3 community accounts end to end;
   the earlier UNION-superset design (every row carrying every table's
   columns as NULLs) was billing padding — the staged per-table queries
   return exactly their own columns.

3. **Cross-account resume is unsafe without deterministic order.** Results
   are execution-scoped: a new API key cannot read the old account's
   execution, so the query re-executes — and without ORDER BY, Dune does not
   guarantee row order, silently corrupting offset-based resume. **Fix:**
   deterministic `ORDER BY (block_number, tx_hash, evt_index)` in all three
   queries; one 125k-row partial (fetched pre-ORDER BY) was discarded and
   re-fetched rather than resumed. After the fix, a mid-stage-1 account swap
   resumed at offset 250,000 and the final total (250,000 + 224,147 =
   474,147) matched the execution's row count exactly.

4. **Dune's SQL text cap (500KB) crashes opaquely.** The permit2 query
   inlined the 8,000-wallet IN-list into all five UNION branches → ~1.7MB
   SQL → instant `FAILED_TYPE_EXECUTION_CRASHED` (0.5s, no useful message).
   **Fix:** single `VALUES` CTE (`wallet_list`) referenced by all branches
   (~370KB). Related gotcha: `render_named_sql` substitutes `{{params}}`
   inside SQL *comments* too — a doc comment containing the placeholder
   doubled the payload.

5. **Two credential systems for GCP.** `bq` CLI uses the gcloud account;
   Python clients use Application Default Credentials — both must be on
   `decentralizedexchangeresearch@gmail.com`
   (`gcloud auth application-default login`). Symptom of drift: `bq` works
   while Python 403s on `bigquery.jobs.create`.

6. **stdout buffering**: launch long fetches with `python -u` or logs sit in
   the block buffer; the manifest is the reliable progress signal regardless.

## Credit accounting

- Account 1 (old .env key): already drained; died 5 pages into stage 1.
- Account 2: ~2,367 credits → 250k swap rows (stage 1 partial, later
  re-fetched under deterministic order).
- Account 3: finished swaps (224k rows), all approvals, all permit2.
- Exploration/verification via MCP across the sessions: ~10 credits.

## Delta run (2026-07-09): the missing 94.8% of the population

`python -m dexresearch.fetch.sushi_delta` — additive companion run under a
fresh community API key. Fetched the tx_to-anchored router-entry txs the
pool-anchored stage 1 missed (see methodology, limitation 1), **without
dropping or appending to any base table**: the delta SQL anti-joins the old
`cand` predicate, so overlap txs were never re-bought.

| table / view | rows | notes |
|---|---|---|
| `swaps_txto_delta` | 41,723 legs / 30,300 txs / 5,022 wallets | gate: 0 tx overlap with `swaps`, no dup legs |
| `approvals_delta` | 58,293 | 4,507 NEW wallets only (base wallets already complete) |
| `permit2_events_delta` | 6,853 | same wallet diff |
| view `swaps_router_entry` | 44,378 legs / **31,949 txs / 5,184 wallets** | study population; `swap_count` recomputed |
| view `approvals_all` / `permit2_events_all` | 498,990 / 217,453 | base ∪ delta |

Every count matched the pre-measured Dune truth (query 7922955) exactly; the
run enforced them as hard gates (preflight on base-table integrity, stage-1
gate before any approvals spend, union gate after view creation). Entire run
completed in one pass on one account, ~6 executions, no resume needed.

**Consequence for analysis:** bot labels and every finding must be recomputed
over `swaps_router_entry` + `approvals_all` — the population grew 19×.

## Aftermath backlog

git-tracked from this commit onward. Remaining: `router_labels` /
`bot_labels` lookups + per-tx classification view, decile sampling, gas→USD,
Uniswap arm re-fetch under the harmonized definition, Polygon decision.
