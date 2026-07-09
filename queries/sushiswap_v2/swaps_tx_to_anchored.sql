-- sushiswap_v2/swaps_tx_to_anchored.sql — CORRECTED stage 1 of the SushiSwap arm
--
-- Replaces swaps.sql, whose `cand` CTE anchored on `project = 'sushiswap'`.
-- That pool-anchor silently dropped 95% of the study population: RedSnwapper
-- and RouteProcessor are *aggregating* routers, so a user on sushi.com whose
-- trade Sushi shops out to a Uniswap or Curve pool is a genuine SushiSwap user,
-- with a genuine SushiSwap approval, whose tx contains ZERO sushiswap legs.
--
-- Measured on Dune, same window and same first-leg rule:
--     pool-anchored (swaps.sql) :  1,649 qualifying txs /   626 wallets
--     tx_to-anchored (this file): 31,949 qualifying txs / 5,184 wallets
--
-- The defect is undetectable from inside the loaded dataset — every fetched tx
-- has a sushiswap stablecoin-selling leg by construction. See
-- docs/sushiswap_v2_methodology.md, "Known limitations", item 1.
--
-- Qualifying transaction: sent directly to a SushiSwap router (`tx_to`), whose
-- FIRST executed swap leg sold a study stablecoin (any output token). Returns
-- LEG-LEVEL rows — every leg of every qualifying tx, any project — so
-- first-hop, output-direction and pool-mix stay post-processing flags.
--
-- NOTE: stage 2/3 (approvals, permit2_events) read their wallet list from the
-- BigQuery table this loads into. Re-running this WILL enlarge that wallet set
-- (626 -> ~5,184), so approvals must be re-fetched, not appended to.
--
-- Parameters:
--   {{window_start}}   study window start, inclusive
--   {{window_end}}     study window end, exclusive

WITH cand AS (
  -- Anchor on the ENTRY CONTRACT (user intent), never on pool membership.
  -- Canonical set mirrors SUSHI_ROUTERS in dexresearch/process/sushi_analysis.py.
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
  -- First executed leg proves the user STARTED with the stablecoin. A naive
  -- "any leg sold a stable" test would admit ETH->USDC->TOKEN routes.
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
