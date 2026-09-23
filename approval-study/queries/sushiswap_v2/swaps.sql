-- sushiswap_v2/swaps.sql — stage 1 of the SushiSwap arm fetch
--
-- !! SUPERSEDED (2026-07-09) by swaps_tx_to_anchored.sql. The `cand` CTE below
-- !! anchors on `project = 'sushiswap'`, which drops every tx that entered a
-- !! Sushi router but routed out to non-Sushi pools. Measured: this query
-- !! captures 1,649 of 31,949 qualifying router-entry txs (5.2%), 626 of 5,184
-- !! wallets. Kept for provenance — it produced dex-research.sushiswap_v2.
--
-- Qualifying transactions: the FIRST executed swap leg sold a study stablecoin
-- (any output token), discovered via a SushiSwap pool leg. Returns LEG-LEVEL
-- rows (every leg of every qualifying tx, any project) with tx_to / evt_index /
-- project / pool / taker, so entry-router vs first-hop vs output-direction are
-- post-processing flags, not fetch filters.
--
-- Stages 2-3 (approvals, permit2_events) take their wallet lists FROM the
-- BigQuery table this loads into — the Execute SQL plan caps executions at
-- 2 minutes, so the arm is decomposed instead of one-shot.
--
-- Parameters:
--   {{window_start}}   study window start, inclusive
--   {{window_end}}     study window end, exclusive

WITH cand AS (
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
legs AS (
  SELECT
    t.tx_hash, t.tx_from, t.tx_to, t.taker, t.evt_index,
    t.project, t.project_contract_address,
    t.block_time, t.block_number,
    t.token_sold_address, t.token_bought_address,
    t.token_sold_symbol, t.token_bought_symbol,
    t.amount_usd
  FROM dex.trades t
  JOIN cand c ON c.tx_hash = t.tx_hash
  WHERE t.blockchain = 'ethereum'
    AND t.block_month >= DATE '{{window_start}}'
    AND t.block_month <  DATE '{{window_end}}'
),
qualifying AS (
  SELECT tx_hash
  FROM (
    SELECT tx_hash, token_sold_address,
           ROW_NUMBER() OVER (PARTITION BY tx_hash ORDER BY evt_index) AS rn
    FROM legs
  )
  WHERE rn = 1
    AND token_sold_address IN (
          0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48,
          0xdAC17F958D2ee523a2206206994597C13D831ec7,
          0x6B175474E89094C44Da98b954EedeAC495271d0F
        )
),
swaps AS (
  SELECT l.* FROM legs l JOIN qualifying q ON q.tx_hash = l.tx_hash
),
swap_counts AS (
  SELECT tx_from, COUNT(DISTINCT tx_hash) AS swap_count
  FROM swaps GROUP BY tx_from
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
  to_hex(bytearray_substring(t.data, 1, 4))        AS method_id,
  sc.swap_count
FROM swaps s
JOIN swap_counts sc ON sc.tx_from = s.tx_from
LEFT JOIN ethereum.transactions t
  ON t.hash = s.tx_hash
  AND t.block_date >= DATE '{{window_start}}'
  AND t.block_date <  DATE '{{window_end}}'
-- deterministic order so offset-based resume is stable across re-executions
ORDER BY s.block_number, s.tx_hash, s.evt_index
