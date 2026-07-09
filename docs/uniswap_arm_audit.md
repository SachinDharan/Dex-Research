# Uniswap arm — audit of the legacy fetch (2026-07-09)

> **Superseding decision (2026-07-09, "option 2"): the arm stores the FULL
> pool-touch universe leg-level**, matching the Sushi arm's philosophy, per
> user direction and with the ~60k-credit cost understood. See "Option 2"
> section at the end for the architecture and reproduction steps. The staged
> audit/design below is retained because phases 0–1–2 are how the router-entry
> core was (correctly) built and remain sub-steps of option 2.

What `dex-research.raw.{swaps,approvals,permit2_events}` (loaded by
`queries/uniswap_v4/timeline.sql` via the old one-shot GCS pipeline) actually
contains, what is wrong with it under the harmonized (SushiSwap-arm)
definitions, what is salvageable, and the re-fetch design.

## What is there

| table | rows | txs | wallets | time bounds |
|---|---|---|---|---|
| `raw.swaps` | 1,363,154 | 1,081,142 | 163,982 | 2025-11 → 2026-02 |
| `raw.approvals` | 104,557 | 104,440 | 44,998 | **2024-05** → 2026-02 |
| `raw.permit2_events` | 487,187 | 482,321 | 58,240 | 2025-11 → 2026-02 |

Tables are EXTERNAL (GCS parquet). Swaps schema: `wallet, block_time,
block_number, tx_hash, token, token_symbol, counterparty, counter_symbol,
amount_usd, gas_used, gas_price, gas_cost_eth, swap_count`.

## Defects (vs the SushiSwap-arm definitions)

1. **No entry anchoring, and none reconstructible.** The query is
   pool-anchored (`project='uniswap' AND version='4'`) and `tx_to` was never
   stored — nor `evt_index`, `project`, `method_id`, `max_priority_fee_per_gas`.
   Aggregator/MEV flow through Uniswap pools is mixed in with no way to
   separate it post-hoc. (Feb 2026 measurement: the top two entry contracts
   for stable-sold Uniswap-leg txs are an unlabeled MEV contract and
   Wintermute — together > 2× UniversalRouter's tx count.)
2. **Output-pinned**: `token_bought IN (ETH, WETH)` only. Harmonized rule is
   any output (the approval is blind to what is bought); output direction must
   be a post-processing flag.
3. **No first-leg rule**: any stable→ETH leg qualifies, including mid-route
   hops of longer aggregator paths. 205,944 txs have multiple stored rows and
   the legs needed to disambiguate were not fetched.
4. **Approvals spender-pinned to Permit2** (correct for Uniswap's own flow,
   fatal for cross-arm comparison) and **lookback only to 2024-05**;
   outstanding-allowance-at-end-of-window needs Permit2 genesis (2022-11).
5. **Permit2 events lack the Lockdown branch** — revoke.cash-style
   revocations inside Permit2 are invisible.
6. `swap_count` baked in at fetch (population-relative; same staleness issue
   the Sushi arm hit).

## Salvageable vs not

- `raw.swaps` — **keep as the contrast population** ("V4-pool-touch
  stable→ETH flow", the analog of Sushi's aggregator-flow contrast). Cannot be
  upgraded in place; do not delete.
- `raw.approvals` — valid as the *Permit2-spender slice* for its 45k wallets;
  usable as a cross-check on the new fetch's overlap, not as study data.
- `raw.permit2_events` — mostly reusable for overlapping wallets (no Lockdown
  branch), same cross-check role.
- Unlike the Sushi arm, the anti-join delta trick **cannot** rescue the swaps
  table: the missing information is columns and legs, not rows.

## Router set (resolved 2026-07-09 via `contracts.contract_mapping`; Dune queries 7923235/7923239)

Entry contracts mapped to Uniswap among stable-sold Uniswap-leg txs,
Nov 2025 – Feb 2026:

| `tx_to` | name | txs | wallets |
|---|---|---|---|
| `0x66a9893c…` | UniversalRouter (current, V4-era) | 403,057 | 116,974 |
| `0xe592427a…` | SwapRouter (V3) | 121,525 | 854 |
| `0x68b34658…` | SwapRouter02 | 75,308 | 3,357 |
| `0x7a250d56…` | **UniswapV2 Router02** — Dune's mapping mislabels it "UniswapV2Factory" | 31,436 | 1,930 |
| `0x3fc91a3a…` | UniversalRouter (2023) | 3,173 | 525 |
| `0xef1c6e67…` | UniversalRouter (2022) | 78 | 18 |

Excluded: `NonfungiblePositionManager` (LP ops, not swap entry), direct
V2Pair calls. Note the two label traps now on record: `custody_owner` (Sushi
arm) and this Factory/Router02 mix-up — always eyeball the resolved names.
`SwapRouter`'s 854 wallets over 121k txs is bot-dominated flow; it stays in
the set (entry anchoring is about intent attribution, bot partition happens
downstream).

## Fetch status: COMPLETE (2026-07-09, three community keys end to end)

| table (`dex-research.uniswap_v4`) | rows | scope |
|---|---|---|
| `wallet_aggregates` | 90,355 (wallet, month) | FULL population; gates exact vs Dune funnel 7923299 (478,299 txs / 73,039 wallets / v4_any 139,195 / v4_first 116,721 / legs 735,223) |
| `sample_wallets` | 7,772 | deterministic strata (FARM_FINGERPRINT, nested): all of 21–100 band, 3,000 of 6–20, 1,500 each of 1 and 2–5, 100+ band capped at ≤500 txs |
| `swaps_sampled` | 261,325 legs / 148,976 txs | leg-level incl. `project`+`version`; per-wallet gate: EVERY sampled wallet reproduces its aggregate tx+leg counts exactly |
| `approvals` | 96,474 | sampled wallets, ANY spender, lookback 2022-11 (Sushi query reused) |
| `permit2_events` | 72,329 | sampled wallets, incl. Lockdown (Sushi query reused) |

Legacy `raw.*` untouched throughout. Next: harmonized analysis module over
these tables + cross-arm contrast with the Sushi findings.

## Re-fetch design (as run)

Scale forces a different shape than Sushi's store-everything (~500k+
router-entry txs before the first-leg rule; leg-level everything ≈ 10–20×
the Sushi arm's cost):

- **Stage 0** — one-row Dune funnel (like Sushi's query 7922955) to pin the
  exact population counts as hard validation gates. ~1 credit.
- **Stage 1** — **per-wallet aggregates for the FULL router-entry
  population**, aggregation done Dune-side: wallet, qualifying-tx count,
  per-router counts, first/last seen, volume, gas sums. ~120k rows ≈ 600
  credits. This yields true denominators, decile structure, and bot features
  for everyone.
- **Stage 2** — **stratified wallet sample** (the plan's decile sampling,
  finally applicable — Uniswap has the activity spread Sushi lacked):
  leg-level swaps + spender-UNPINNED approvals (lookback to 2022-11) +
  permit2 events incl. Lockdown, for the sampled wallets only, reusing the
  Sushi staged queries/loader nearly verbatim into a new native-table dataset.
- Old `raw.*` stays for provenance; overlap wallets cross-validate the new
  fetch.

Qualifying rule, harmonized: sent to a Uniswap router (`tx_to`), first
executed leg sold USDC/USDT/DAI, any output token.

**V4 is tracked as a measured attribute, not the fetch anchor** (decided
2026-07-09). The plan's arm stays V4-scoped in its tables, but conditioning
the *fetch* on V4 execution would select users on the UniversalRouter's
internal routing choice — the same endogenous-selection bug as Sushi's
pool anchor. So: Stage 1 aggregates carry `v4_txs` / `v4_first_txs` per
wallet; Stage 2 stores `project`+`version` per leg; every V4 definition
(any-leg, first-hop) is a post-processing flag. The approval side is
version-blind anyway (one Permit2 approval covers routing to any version),
so V4-scoped denominators must be paired with explicit "approvals per V4
swap" language.

Candidate prefilter note: anchoring on `tx_to` alone would make the legs
join enormous (all router traffic). It is safe to prefilter candidates to
txs with ≥1 stable-selling leg because Uniswap's routers execute only
Uniswap pools, all of which appear in `dex.trades` — so a first-leg-stable
tx necessarily has a stable-selling leg. This is NOT the Sushi Alice bug:
that arose because Sushi's routers execute *other venues'* pools.

---

# Option 2 — full pool-touch universe, leg level (decided 2026-07-09)

**Population** (identical rule to `sushiswap_v2/swaps.sql`): candidate = tx
with a Uniswap-pool leg that sold USDC/USDT/DAI in the window; qualifying =
its FIRST executed leg (min `evt_index`, across ALL legs, any project) sold
one of those stables — **any output token**. Measured Feb 2026 (Dune query
7928963): 1,197,799 qualifying txs / 121,731 wallets / 2,829,309 legs; the
window total is ~4.7M txs / ~11.3M legs, of which the router-entry core is
478,299 txs / 735,223 legs.

**Storage layout — why the broad layer is two tables.** Dune bills reads as
rows × columns; `wallet`, times, `tx_to`, the five gas fields and `method_id`
are constant across a tx's legs, so the broad layer buys them once per tx:

| table | grain | source query |
|---|---|---|
| `swaps_sampled` | leg-level, wide — the router-entry core (phase 1/2) | `queries/uniswap_v4/swaps_sampled.sql` |
| `txs_broad` | one row per qualifying NON-router tx | `queries/uniswap_v4/txs_broad.sql` |
| `legs_broad` | one row per leg of those txs | `queries/uniswap_v4/legs_broad.sql` |
| view `swaps_broad` | wide reassembly (`txs_broad ⋈ legs_broad`, symbols joined from owned rows) | created by the runner |
| view `swaps_option2` | **the complete population**, Sushi wide schema = `swaps_sampled ∪ swaps_broad` | created by the runner |

This normalization cuts the read bill ~34% (≈134M vs ≈203M datapoints) and is
invisible downstream — analysis reads `swaps_option2`, which has exactly the
Sushi arm's column layout plus `project`/`version` per leg.

**Gating protocol (self-proving, per month):** the runner first executes
`broad_funnel.sql` (one row: expected broad/router txs, legs, wallets) and
records the counts in the manifest *before* fetching; after loading the
month's two tables it refuses to proceed unless BigQuery reproduces those
counts exactly. Router figures in the funnel double-check phase 1.

**Reproduction, in order** (each step resumable; on `CreditError` swap
`.env` `DUNE_API_KEY` and re-run the same command — completed jobs skip):

```bash
python -m dexresearch.fetch.uniswap_wallets   # stage 1: full-population per-wallet aggregates
python -m dexresearch.fetch.uniswap_sample    # stage 2: stratified sample detail (historical; subset of topup)
python -m dexresearch.fetch.uniswap_topup     # phase 1: router-entry core to FULL coverage
python -m dexresearch.fetch.uniswap_broad     # option 2: non-router pool-touch universe
```

**Deliberately deferred:** approvals/permit2 for the ~380k non-router
wallets (their approvals belong to aggregator spenders; decide scope after
the broad tables exist). The router-entry wallets' approvals/permit2 — the
ones the study's ratios pair — are fetched by `uniswap_topup`.

**Cost record:** stages 0–2 + phase 1 ≈ 12k credits across 4 community keys;
option-2 broad layer ≈ 60k credits. Datapoint pricing ≈ 2,100/credit;
payload gotcha: `render_named_sql` substitutes params inside SQL comments,
so query headers must not contain literal placeholder braces.

## Final status (2026-07-09): descoped to a November deep slice — DONE

After ~10 keys the user stopped the broad treadmill. Final corpus:

- **Router-entry arm (study core): COMPLETE Nov–Feb.** `swaps_sampled`
  735,223 legs / 478,299 txs (gate exact, every wallet matches aggregates);
  `approvals` 604,168; `permit2_events` 271,754 — all 73,039 wallets.
- **Broad (non-router pool-touch) layer: November complete leg-level**
  (`txs_broad` 977,106 / `legs_broad` 1,975,503, month gate exact); Dec–Feb
  NOT fetched leg-level. Views `swaps_broad` and `swaps_option2` exist —
  **their broad half is November-scoped**; the router-entry half is
  full-window. Any analysis on them must say so.
- **All four broad months are exactly counted** (funnel gates in the
  manifest, `uniswap_v4/broad_funnel/*`):

  | month | broad txs | broad legs | broad wallets | router txs |
  |---|---|---|---|---|
  | 2025-11 | 977,106 | 1,975,503 | 116,645 | 81,927 |
  | 2025-12 | 891,087 | 1,959,850 | 91,621 | 113,502 |
  | 2026-01 | 906,315 | 2,106,017 | 101,920 | 152,747 |
  | 2026-02 | 1,067,507 | 2,603,801 | 104,508 | 130,123 |

  Router column sums to 478,299 — an independent reproduction of the
  fetched population. Full universe: 4,320,314 txs / 9,380,394 legs.
- **Deferred** (resume `fetch.uniswap_broad` with the full window to pick
  up exactly where this left off): Dec–Feb broad legs (~5.6M legs ≈ 25k
  credits), stage-1b per-wallet broad aggregates (~3k credits), approvals/
  permit2 for non-router wallets.

### Wrap-up integrity audit (2026-07-09)

Manifest: zero incomplete `uniswap_v4/*` jobs. Roster = 73,039 exactly.
Zero duplicate legs in `swaps_sampled`, zero duplicate txs in `txs_broad`,
zero duplicate approval rows. Two known data notes:

1. `legs_broad` holds 2 duplicate `(tx_hash, evt_index)` pairs — a
   `dex.trades` source quirk where a 1inch-LOP fill and the underlying pool
   swap are double-decoded at the same index (txs `0x833fb9f8…`,
   `0xe90111d7…`). Consistent with the funnel (gate still exact); neither is
   a first leg, so qualification is unaffected.
2. 985 of 73,039 wallets (1.3%) have zero rows in `approvals`: their only
   stable-token approval predates the 2022-11-01 lookback (e.g. 2021-era
   router approvals still being coasted on). Same deliberate lookback bound
   as the Sushi arm; treat "no approval observed" as "none since Permit2
   genesis", not "none ever".
