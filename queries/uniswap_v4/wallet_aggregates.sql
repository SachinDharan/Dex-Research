-- uniswap_v4/wallet_aggregates.sql — STAGE 1 of the harmonized Uniswap arm:
-- per-wallet aggregates for the FULL router-entry population, one calendar
-- month per execution (keeps each run far under the Execute SQL 2-min cap and
-- gives the manifest natural resume points). Wallets appearing in several
-- months repeat; merge across months in BigQuery.
--
-- Qualifying tx: sent to a Uniswap router (tx_to), FIRST executed leg sold
-- USDC/USDT/DAI, any output token. V4 is a measured attribute (v4_any_txs /
-- v4_first_txs), never a fetch filter — conditioning the fetch on V4
-- execution would select users on the UniversalRouter's internal routing
-- choice (the Sushi pool-anchor bug, one layer down).
--
-- Candidate prefilter (>=1 stable-selling leg) is safe here: Uniswap routers
-- execute only Uniswap pools, all present in dex.trades, so a
-- first-leg-stable tx necessarily has a stable-selling leg. Sushi's routers
-- execute other venues' pools, which is why ITS pool-anchor lost 95%.
--
-- Validation gates (Dune funnel query 7923299, whole window): monthly sums
-- must reach 478,299 qualifying txs / 139,195 v4_any / 116,721 v4_first /
-- 735,223 legs; distinct wallets across months = 73,039.
--
-- Parameters:
--   {{month}}       calendar month start, e.g. 2025-11-01
--   {{month_end}}   next month start (exclusive)

WITH cand AS (
  SELECT DISTINCT tx_hash
  FROM dex.trades
  WHERE blockchain = 'ethereum'
    AND block_month = DATE '{{month}}'
    AND block_time >= TIMESTAMP '{{month}}'
    AND block_time <  TIMESTAMP '{{month_end}}'
    AND tx_to IN (
          0x66a9893cc07d91d95644aedd05d03f95e1dba8af,  -- UniversalRouter (current)
          0xe592427a0aece92de3edee1f18e0157c05861564,  -- SwapRouter (V3)
          0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45,  -- SwapRouter02
          0x7a250d5630b4cf539739df2c5dacb4c659f2488d,  -- V2 Router02 (mapping mislabels it "Factory")
          0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad,  -- UniversalRouter (2023)
          0xef1c6e67703c7bd7107eed8303fbe6ec2554bf6b   -- UniversalRouter (2022)
        )
    AND token_sold_address IN (
          0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48,  -- USDC
          0xdAC17F958D2ee523a2206206994597C13D831ec7,  -- USDT
          0x6B175474E89094C44Da98b954EedeAC495271d0F   -- DAI
        )
),
legs AS (
  SELECT t.tx_hash, t.tx_from, t.tx_to, t.evt_index, t.block_time,
         t.token_sold_address, t.project, t.version, t.amount_usd
  FROM dex.trades t
  JOIN cand c ON c.tx_hash = t.tx_hash
  WHERE t.blockchain = 'ethereum'
    AND t.block_month = DATE '{{month}}'
),
per_tx AS (
  SELECT
    tx_hash,
    MIN(tx_from)                                   AS wallet,
    MIN(tx_to)                                     AS entry,
    MIN(block_time)                                AS block_time,
    COUNT(*)                                       AS n_legs,
    MAX(CASE WHEN project = 'uniswap' AND version = '4' THEN 1 ELSE 0 END) AS v4_any,
    MIN_BY(token_sold_address, evt_index)          AS first_sold,
    MIN_BY(amount_usd, evt_index)                  AS first_amount_usd,
    (MIN_BY(project, evt_index) = 'uniswap'
     AND MIN_BY(version, evt_index) = '4')         AS v4_first
  FROM legs
  GROUP BY tx_hash
),
qual AS (
  SELECT p.*,
         CAST(t.gas_used AS double) * t.gas_price / 1e18 AS gas_cost_eth,
         t.max_priority_fee_per_gas
  FROM per_tx p
  LEFT JOIN ethereum.transactions t
    ON t.hash = p.tx_hash
    AND t.block_date >= DATE '{{month}}'
    AND t.block_date <  DATE '{{month_end}}'
  WHERE p.first_sold IN (
          0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48,
          0xdAC17F958D2ee523a2206206994597C13D831ec7,
          0x6B175474E89094C44Da98b954EedeAC495271d0F
        )
)
SELECT
  to_hex(wallet)                                        AS wallet,
  '{{month}}'                                           AS month,
  COUNT(*)                                              AS qualifying_txs,
  SUM(v4_any)                                           AS v4_any_txs,
  SUM(CASE WHEN v4_first THEN 1 ELSE 0 END)             AS v4_first_txs,
  SUM(CASE WHEN entry = 0x66a9893cc07d91d95644aedd05d03f95e1dba8af
           THEN 1 ELSE 0 END)                           AS ur_txs,
  MAX(n_legs)                                           AS max_legs,
  SUM(n_legs)                                           AS sum_legs,
  SUM(CASE WHEN max_priority_fee_per_gas = 0 THEN 1 ELSE 0 END) AS zero_prio_txs,
  SUM(gas_cost_eth)                                     AS gas_eth,
  SUM(first_amount_usd)                                 AS volume_usd,
  MIN(block_time)                                       AS first_seen,
  MAX(block_time)                                       AS last_seen
FROM qual
GROUP BY wallet
-- deterministic order so offset-based resume is stable across re-executions
ORDER BY 1
