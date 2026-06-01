# Dune queries

SQL templates, one subfolder per protocol arm (`queries/uniswap_v4/`, etc.),
version-controlled and reviewable.

## Uniswap V4 — one query, three datasets

`uniswap_v4/timeline.sql` is the whole fetch in a **single Dune execution**. It
derives the qualifying swap-wallet set once (a `swaps` CTE), then returns —
tagged by `record_type` — the swaps, those wallets' ERC-20 approvals to Permit2
(18-month lookback), and their Permit2 events (study window). No wallet list is
passed in; the wallet set never leaves Dune.

`dexresearch.fetch.timeline` runs it via Dune's Execute SQL endpoint
(`dexresearch.dune_runner`), then splits the one result by `record_type`:

| `record_type`        | GCS output                                       |
| -------------------- | ------------------------------------------------ |
| `swap`               | `raw/swaps/ethereum_uniswap_v4.parquet`          |
| `erc20_approval`     | `raw/approvals/ethereum_uniswap_v4.parquet`      |
| `permit2_*`          | `raw/permit2_events/ethereum_uniswap_v4.parquet` |

### Parameters
- `{{window_start}}`   study window start, inclusive (e.g. `2025-11-01`)
- `{{window_end}}`     study window end, exclusive (e.g. `2026-03-01`)
- `{{lookback_start}}` `window_start` minus 18 months (approval lower bound)

Values are substituted into the SQL text by `dune.render_named_sql` — nothing is
saved on dune.com and there are no query ids.

## Legacy / other arms

Other stages (and future protocol arms once their contracts are verified) may use
the saved-query path: write `queries/<name>.sql`, save it on dune.com, record the
id in `../config/study.yaml` under `dune_queries`, and run via
`dune.run_named_query("<name>")` (saved-query CRUD via API needs an Analyst plan;
Execute SQL needs only Read scope, which is why the active arm uses it).
