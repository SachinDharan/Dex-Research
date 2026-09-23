-- uniswap_v4/legs_broad.sql — OPTION-2 broad layer, table 2 of 2: one row per
-- LEG of every qualifying non-router pool-touch transaction (the nested swaps).
--
-- Candidate/qualifying/exclusion predicates are IDENTICAL to txs_broad.sql —
-- the two tables describe the same transaction set at different grain and are
-- reassembled by a BigQuery view (join on tx_hash). Tx-constant fields
-- (wallet, times, tx_to, gas, method_id) live in txs_broad only; token
-- symbols are joined locally in BigQuery, not bought per row here.
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
  SELECT t.tx_hash, t.tx_to, t.evt_index, t.block_number,
         t.project, t.version, t.token_sold_address,
         t.token_bought_address, t.amount_usd
  FROM dex.trades t
  JOIN cand c ON c.tx_hash = t.tx_hash
  WHERE t.blockchain = 'ethereum'
    AND t.block_month = DATE '{{month}}'
),
per_tx AS (
  SELECT
    tx_hash,
    MIN(tx_to)                            AS entry,
    MIN_BY(token_sold_address, evt_index) AS first_sold
  FROM legs
  GROUP BY tx_hash
),
qual AS (
  SELECT tx_hash FROM per_tx
  WHERE first_sold IN (
          0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48,
          0xdAC17F958D2ee523a2206206994597C13D831ec7,
          0x6B175474E89094C44Da98b954EedeAC495271d0F
        )
    AND entry NOT IN (
          0x66a9893cc07d91d95644aedd05d03f95e1dba8af,
          0xe592427a0aece92de3edee1f18e0157c05861564,
          0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45,
          0x7a250d5630b4cf539739df2c5dacb4c659f2488d,
          0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad,
          0xef1c6e67703c7bd7107eed8303fbe6ec2554bf6b
        )
)
SELECT
  to_hex(l.tx_hash)                 AS tx_hash,
  l.evt_index,
  l.project,
  l.version,
  to_hex(l.token_sold_address)      AS token,
  to_hex(l.token_bought_address)    AS counterparty,
  CAST(l.amount_usd AS double)      AS amount_usd
FROM legs l
JOIN qual q ON q.tx_hash = l.tx_hash
-- deterministic order so offset-based resume is stable across re-executions
ORDER BY l.block_number, l.tx_hash, l.evt_index
