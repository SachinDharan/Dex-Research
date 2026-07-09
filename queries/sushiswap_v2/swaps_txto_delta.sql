-- sushiswap_v2/swaps_txto_delta.sql — DELTA stage 1: router-entry txs the
-- pool-anchored fetch missed.
--
-- The tx_to-anchored population (31,949 qualifying txs, see
-- swaps_tx_to_anchored.sql) overlaps the loaded dataset in exactly the 1,649
-- txs that ALSO had a sushiswap stable-selling leg — the old cand predicate.
-- This query re-anchors on tx_to and then ANTI-JOINS that old predicate, so it
-- returns only the missing txs. The 1,649 overlap txs need no re-fetch: both
-- queries pull ALL legs of a candidate tx and apply the identical first-leg
-- rule, so their existing rows are what this query would produce.
--
-- Verified on Dune 2026-07-09 (query 7922955, one-row funnel):
--   overlap: 1,649 txs / 626 wallets / 2,655 legs — all three EXACTLY match
--   the loaded BQ table, confirming both the anti-join predicate and the
--   identical-rows claim. Delta: 30,300 txs / 41,723 legs / 5,022 wallets
--   (464 of which also appear in the overlap). 30,300 + 1,649 = 31,949 and
--   wallet union = 5,184, so overlap + delta tile the population exactly.
--   41,723 rows x 19 cols ~= 793k datapoints ~= 380 credits for this stage.
--
-- Loads into a NEW table (`swaps_txto_delta`), never appended to `swaps` —
-- the validated original stays intact for provenance; the study population is
-- a BigQuery union view (legs of `swaps` txs whose tx_to ∈ SUSHI_ROUTERS,
-- plus this table).
--
-- Schema note: `swap_count` is deliberately DROPPED here. Computed in-query it
-- would count only delta txs; recompute it in BigQuery over the union view
-- (the stored column in `swaps` is population-relative and stale too).
--
-- Validation gates after load:
--   delta txs ∩ swaps txs = 0
--   union distinct router-entry txs = 31,949 ; union wallets = 5,184
--
-- Parameters:
--   {{window_start}}   study window start, inclusive
--   {{window_end}}     study window end, exclusive

WITH cand AS (
  /* Anchor on the ENTRY CONTRACT (user intent). Canonical set mirrors
     SUSHI_ROUTERS in dexresearch/process/sushi_analysis.py. */
  SELECT DISTINCT tx_hash
  FROM dex.trades
  WHERE blockchain = 'ethereum'
    AND block_month >= DATE '{{window_start}}'
    AND block_month <  DATE '{{window_end}}'
    AND block_time  >= TIMESTAMP '{{window_start}}'
    AND block_time  <  TIMESTAMP '{{window_end}}'
    AND tx_to IN (
          0xd9e1ce17f2641f24ae83637ab66a2cca9c378b9f,  -- Router02 (UniswapV2Router02)
          0xac4c6e212a361c968f1725b4d055b47e63f80b75,  -- RedSnwapper
          0xe43ca1dee3f0fc1e2df73a0745674545f11a59f5,  -- RouteProcessor4
          0xd2b37ade14708bf18904047b1e31f8166d39612b   -- RouteProcessor9_2
        )
),
already_fetched AS (
  /* Exact replica of the superseded swaps.sql cand predicate. Any tx matching
     it that also qualifies below is already in dex-research.sushiswap_v2.swaps
     (same legs CTE, same first-leg rule ⇒ identical rows). */
  SELECT DISTINCT tx_hash
  FROM dex.trades
  WHERE blockchain = 'ethereum'
    AND project = 'sushiswap'
    AND block_month >= DATE '{{window_start}}'
    AND block_month <  DATE '{{window_end}}'
    AND block_time  >= TIMESTAMP '{{window_start}}'
    AND block_time  <  TIMESTAMP '{{window_end}}'
    AND token_sold_address IN (
          0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48,  -- USDC
          0xdAC17F958D2ee523a2206206994597C13D831ec7,  -- USDT
          0x6B175474E89094C44Da98b954EedeAC495271d0F   -- DAI
        )
),
delta_cand AS (
  SELECT c.tx_hash
  FROM cand c
  LEFT JOIN already_fetched a ON a.tx_hash = c.tx_hash
  WHERE a.tx_hash IS NULL
),
legs AS (
  SELECT
    t.tx_hash, t.tx_from, t.tx_to, t.taker, t.evt_index,
    t.project, t.project_contract_address,
    t.block_time, t.block_number,
    t.token_sold_address, t.token_bought_address,
    t.token_sold_symbol, t.token_bought_symbol,
    t.amount_usd
  FROM dex.trades t
  JOIN delta_cand c ON c.tx_hash = t.tx_hash
  WHERE t.blockchain = 'ethereum'
    AND t.block_month >= DATE '{{window_start}}'
    AND t.block_month <  DATE '{{window_end}}'
),
qualifying AS (
  /* First executed leg proves the user STARTED with the stablecoin. */
  SELECT tx_hash
  FROM (
    SELECT tx_hash, token_sold_address,
           ROW_NUMBER() OVER (PARTITION BY tx_hash ORDER BY evt_index) AS rn
    FROM legs
  )
  WHERE rn = 1
    AND token_sold_address IN (
          0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48,  -- USDC
          0xdAC17F958D2ee523a2206206994597C13D831ec7,  -- USDT
          0x6B175474E89094C44Da98b954EedeAC495271d0F   -- DAI
        )
),
swaps AS (
  SELECT l.* FROM legs l JOIN qualifying q ON q.tx_hash = l.tx_hash
)
SELECT
  to_hex(s.tx_from)                        AS wallet,
  s.block_time,
  s.block_number,
  to_hex(s.tx_hash)                        AS tx_hash,
  to_hex(s.token_sold_address)             AS token,
  s.token_sold_symbol                      AS token_symbol,
  to_hex(s.token_bought_address)           AS counterparty,
  s.token_bought_symbol                    AS counter_symbol,
  CAST(s.amount_usd AS double)             AS amount_usd,
  to_hex(s.tx_to)                          AS tx_to,
  s.evt_index,
  s.project,
  to_hex(s.project_contract_address)       AS pool,
  to_hex(s.taker)                          AS taker,
  t.gas_used,
  t.gas_price,
  CAST(t.gas_used AS double) * t.gas_price / 1e18  AS gas_cost_eth,
  t.max_priority_fee_per_gas,
  to_hex(bytearray_substring(t.data, 1, 4))        AS method_id
FROM swaps s
LEFT JOIN ethereum.transactions t
  ON t.hash = s.tx_hash
  AND t.block_date >= DATE '{{window_start}}'
  AND t.block_date <  DATE '{{window_end}}'
-- deterministic order so offset-based resume is stable across re-executions
ORDER BY s.block_number, s.tx_hash, s.evt_index
