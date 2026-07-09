-- uniswap_v4/broad_funnel.sql — one-row gate for the option-2 broad fetch.
--
-- Same candidate/qualifying predicates as txs_broad.sql / legs_broad.sql,
-- split by router-entry. Run BEFORE fetching each month; the runner records
-- the counts and refuses to accept a month whose loaded tables differ.
-- (Router figures double-check phase 1: they must match wallet_aggregates.)
--
-- Parameters:
--   month        calendar month start
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
          0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48,
          0xdAC17F958D2ee523a2206206994597C13D831ec7,
          0x6B175474E89094C44Da98b954EedeAC495271d0F
        )
),
legs AS (
  SELECT t.tx_hash, t.tx_from, t.tx_to, t.evt_index, t.token_sold_address
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
    COUNT(*)                              AS n_legs,
    MIN_BY(token_sold_address, evt_index) AS first_sold
  FROM legs
  GROUP BY tx_hash
),
qual AS (
  SELECT *,
         (entry IN (
            0x66a9893cc07d91d95644aedd05d03f95e1dba8af,
            0xe592427a0aece92de3edee1f18e0157c05861564,
            0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45,
            0x7a250d5630b4cf539739df2c5dacb4c659f2488d,
            0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad,
            0xef1c6e67703c7bd7107eed8303fbe6ec2554bf6b
          )) AS is_router
  FROM per_tx
  WHERE first_sold IN (
          0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48,
          0xdAC17F958D2ee523a2206206994597C13D831ec7,
          0x6B175474E89094C44Da98b954EedeAC495271d0F
        )
)
SELECT
  SUM(CASE WHEN NOT is_router THEN 1 ELSE 0 END)      AS broad_txs,
  SUM(CASE WHEN NOT is_router THEN n_legs ELSE 0 END) AS broad_legs,
  COUNT(DISTINCT CASE WHEN NOT is_router THEN wallet END) AS broad_wallets,
  SUM(CASE WHEN is_router THEN 1 ELSE 0 END)          AS router_txs,
  SUM(CASE WHEN is_router THEN n_legs ELSE 0 END)     AS router_legs
FROM qual
