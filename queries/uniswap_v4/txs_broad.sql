-- uniswap_v4/txs_broad.sql — OPTION-2 broad layer, table 1 of 2: one row per
-- qualifying POOL-TOUCH transaction that did NOT enter via a Uniswap router.
--
-- Population (mirrors sushiswap_v2/swaps.sql): candidate = tx with a Uniswap
-- leg that sold a study stablecoin; qualifying = FIRST executed leg (min
-- evt_index, across ALL legs, any project) sold USDC/USDT/DAI — any output.
-- Router-entry qualifying txs are EXCLUDED here because they are already
-- stored leg-level (wide schema) in `swaps_sampled` by the phase-1 fetch;
-- the union of the two is the complete option-2 population.
--
-- Why two tables (see docs/uniswap_arm_audit.md, "Option 2"): Dune bills
-- rows x columns, and wallet/times/tx_to/gas/method_id are constant across a
-- tx's legs. Storing them once per TX here and only per-leg fields in
-- legs_broad.sql cuts the read bill ~34% at 11M-leg scale. A BigQuery view
-- reassembles the wide Sushi-shaped schema downstream.
--
-- One execution per calendar month (2-min Execute SQL cap; natural resume
-- points). A tx's legs share one block, so month chunking never splits a tx.
--
-- Parameters:
--   month        calendar month start, e.g. 2025-11-01
--   month_end    next month start (exclusive)

WITH cand AS (
  SELECT DISTINCT tx_hash
  FROM dex.trades
  WHERE blockchain = 'ethereum'
    AND project = 'uniswap'
    AND block_month = DATE '{{month}}'
    AND block_time >= TIMESTAMP '{{month}}'
    AND block_time <  TIMESTAMP '{{month_end}}'
    AND token_sold_address IN (
          0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48,  -- USDC
          0xdAC17F958D2ee523a2206206994597C13D831ec7,  -- USDT
          0x6B175474E89094C44Da98b954EedeAC495271d0F   -- DAI
        )
),
legs AS (
  SELECT t.tx_hash, t.tx_from, t.tx_to, t.evt_index,
         t.block_time, t.block_number, t.token_sold_address
  FROM dex.trades t
  JOIN cand c ON c.tx_hash = t.tx_hash
  WHERE t.blockchain = 'ethereum'
    AND t.block_month = DATE '{{month}}'
),
per_tx AS (
  SELECT
    tx_hash,
    MIN(tx_from)                          AS wallet,
    MIN(tx_to)                            AS entry,
    MIN(block_time)                       AS block_time,
    MIN(block_number)                     AS block_number,
    COUNT(*)                              AS n_legs,
    MIN_BY(token_sold_address, evt_index) AS first_sold
  FROM legs
  GROUP BY tx_hash
),
qual AS (
  SELECT * FROM per_tx
  WHERE first_sold IN (
          0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48,
          0xdAC17F958D2ee523a2206206994597C13D831ec7,
          0x6B175474E89094C44Da98b954EedeAC495271d0F
        )
    -- router-entry txs live in swaps_sampled (phase 1); union downstream
    AND entry NOT IN (
          0x66a9893cc07d91d95644aedd05d03f95e1dba8af,  -- UniversalRouter (current)
          0xe592427a0aece92de3edee1f18e0157c05861564,  -- SwapRouter (V3)
          0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45,  -- SwapRouter02
          0x7a250d5630b4cf539739df2c5dacb4c659f2488d,  -- V2 Router02
          0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad,  -- UniversalRouter (2023)
          0xef1c6e67703c7bd7107eed8303fbe6ec2554bf6b   -- UniversalRouter (2022)
        )
)
SELECT
  to_hex(q.tx_hash)                        AS tx_hash,
  to_hex(q.wallet)                         AS wallet,
  q.block_time,
  q.block_number,
  to_hex(q.entry)                          AS tx_to,
  q.n_legs,
  t.gas_used,
  t.gas_price,
  CAST(t.gas_used AS double) * t.gas_price / 1e18  AS gas_cost_eth,
  t.max_priority_fee_per_gas,
  to_hex(bytearray_substring(t.data, 1, 4))        AS method_id
FROM qual q
LEFT JOIN ethereum.transactions t
  ON t.hash = q.tx_hash
  AND t.block_date >= DATE '{{month}}'
  AND t.block_date <  DATE '{{month_end}}'
-- deterministic order so offset-based resume is stable across re-executions
ORDER BY q.block_number, q.tx_hash
