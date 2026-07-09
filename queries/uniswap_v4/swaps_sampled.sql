-- uniswap_v4/swaps_sampled.sql — STAGE 2 of the harmonized Uniswap arm:
-- LEG-LEVEL swaps for one batch of sampled wallets.
--
-- Population rule is identical to wallet_aggregates.sql (router-entry,
-- first-leg-sold-stable, any output; candidate prefilter safe because Uniswap
-- routers execute only Uniswap pools), restricted to the sampled wallets via
-- tx_from. `project`+`version` are stored per leg, so every V4 definition
-- (any-leg / first-hop) stays a post-processing flag. `pool`/`taker` are
-- deliberately dropped vs the Sushi schema (read-cost; version carries the
-- attribution this arm needs).
--
-- Hard gate after load: per sampled wallet, COUNT(DISTINCT tx_hash) and leg
-- counts must EQUAL the wallet_aggregates sums — the aggregate fetch and this
-- one implement the same rule, so any drift is a bug.
--
-- Parameters (render substitutes inside comments too, so the wallet
-- placeholder is not written literally here — run-log gotcha #4):
--   wallets          comma-separated varbinary wallet literals (one batch)
--   window_start     study window start, inclusive
--   window_end       study window end, exclusive

WITH cand AS (
  SELECT DISTINCT tx_hash
  FROM dex.trades
  WHERE blockchain = 'ethereum'
    AND block_month >= DATE '{{window_start}}'
    AND block_month <  DATE '{{window_end}}'
    AND block_time  >= TIMESTAMP '{{window_start}}'
    AND block_time  <  TIMESTAMP '{{window_end}}'
    AND tx_to IN (
          0x66a9893cc07d91d95644aedd05d03f95e1dba8af,  -- UniversalRouter (current)
          0xe592427a0aece92de3edee1f18e0157c05861564,  -- SwapRouter (V3)
          0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45,  -- SwapRouter02
          0x7a250d5630b4cf539739df2c5dacb4c659f2488d,  -- V2 Router02
          0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad,  -- UniversalRouter (2023)
          0xef1c6e67703c7bd7107eed8303fbe6ec2554bf6b   -- UniversalRouter (2022)
        )
    AND token_sold_address IN (
          0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48,  -- USDC
          0xdAC17F958D2ee523a2206206994597C13D831ec7,  -- USDT
          0x6B175474E89094C44Da98b954EedeAC495271d0F   -- DAI
        )
    AND tx_from IN ({{wallets}})
),
legs AS (
  SELECT t.tx_hash, t.tx_from, t.tx_to, t.evt_index,
         t.project, t.version, t.block_time, t.block_number,
         t.token_sold_address, t.token_bought_address,
         t.token_sold_symbol, t.token_bought_symbol, t.amount_usd
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
)
SELECT
  to_hex(l.tx_from)                        AS wallet,
  l.block_time,
  l.block_number,
  to_hex(l.tx_hash)                        AS tx_hash,
  l.evt_index,
  l.project,
  l.version,
  to_hex(l.token_sold_address)             AS token,
  l.token_sold_symbol                      AS token_symbol,
  to_hex(l.token_bought_address)           AS counterparty,
  l.token_bought_symbol                    AS counter_symbol,
  CAST(l.amount_usd AS double)             AS amount_usd,
  to_hex(l.tx_to)                          AS tx_to,
  t.gas_used,
  t.gas_price,
  CAST(t.gas_used AS double) * t.gas_price / 1e18  AS gas_cost_eth,
  t.max_priority_fee_per_gas,
  to_hex(bytearray_substring(t.data, 1, 4))        AS method_id
FROM legs l
JOIN qualifying q ON q.tx_hash = l.tx_hash
LEFT JOIN ethereum.transactions t
  ON t.hash = l.tx_hash
  AND t.block_date >= DATE '{{window_start}}'
  AND t.block_date <  DATE '{{window_end}}'
-- deterministic order so offset-based resume is stable across re-executions
ORDER BY l.block_number, l.tx_hash, l.evt_index
