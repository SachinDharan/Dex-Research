-- wallet_populations.sql
--
-- One row per (wallet, protocol, action_type) that performed at least one
-- qualifying action on a given chain within the month window. Unioned across
-- DEX swaps, Aave supply/borrow, and Compound supply/borrow.
--
-- Parameters:
--   {{chain}}        ethereum | polygon
--   {{month_start}}  inclusive  (e.g. 2025-11-01)
--   {{month_end}}    exclusive  (e.g. 2025-12-01)
--
-- Schema produced:
--   wallet         varbinary
--   protocol       varchar       uniswap | sushiswap | aave_v3 | compound_v3
--   action_type    varchar       swap | supply | borrow
--   action_count   bigint
--   volume_usd     double        nullable (populated for swaps)
--
-- NOTE: starter template — verify table/column names against current Dune
-- Spellbook before saving on dune.com. Lending sections in particular may
-- need adjustment depending on which Spellbook tables you settle on.

WITH stables AS (
  SELECT * FROM (VALUES
    ('ethereum', from_hex('a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48')),  -- USDC
    ('ethereum', from_hex('dac17f958d2ee523a2206206994597c13d831ec7')),  -- USDT
    ('ethereum', from_hex('6b175474e89094c44da98b954eedeac495271d0f')),  -- DAI
    ('polygon',  from_hex('2791bca1f2de4661ed88a30c99a7a9449aa84174')),  -- USDC.e
    ('polygon',  from_hex('c2132d05d31c914a87c6611c10748aeb04b58e8f')),  -- USDT
    ('polygon',  from_hex('8f3cf7ad23cd3cadbd9735aff958023239c6a063'))   -- DAI
  ) AS t(blockchain, token)
),

-- ETH/POL -> stablecoin swaps on Uniswap + SushiSwap
dex_swaps AS (
  SELECT
    tx_from                                       AS wallet,
    project                                       AS protocol,
    CAST('swap' AS VARCHAR)                       AS action_type,
    CAST(1 AS BIGINT)                             AS action_inc,
    COALESCE(amount_usd, 0)                       AS volume_usd
  FROM dex.trades
  WHERE blockchain = '{{chain}}'
    AND project IN ('uniswap', 'sushiswap')
    AND block_time >= TIMESTAMP '{{month_start}}'
    AND block_time <  TIMESTAMP '{{month_end}}'
    AND token_sold_symbol  IN ('ETH', 'WETH', 'POL', 'MATIC', 'WMATIC')
    AND token_bought_address IN (SELECT token FROM stables WHERE blockchain = '{{chain}}')
),

-- Aave V3 supply / borrow.  TODO_VERIFY table name.
aave_actions AS (
  SELECT
    tx_from                                       AS wallet,
    CAST('aave_v3' AS VARCHAR)                    AS protocol,
    event_type                                    AS action_type,
    CAST(1 AS BIGINT)                             AS action_inc,
    CAST(NULL AS DOUBLE)                          AS volume_usd
  FROM aave.aave_v3_lending_events  -- TODO_VERIFY
  WHERE blockchain = '{{chain}}'
    AND event_type IN ('supply', 'borrow')
    AND block_time >= TIMESTAMP '{{month_start}}'
    AND block_time <  TIMESTAMP '{{month_end}}'
),

-- Compound V3 supply / borrow.  TODO_VERIFY table name.
compound_actions AS (
  SELECT
    tx_from                                       AS wallet,
    CAST('compound_v3' AS VARCHAR)                AS protocol,
    event_type                                    AS action_type,
    CAST(1 AS BIGINT)                             AS action_inc,
    CAST(NULL AS DOUBLE)                          AS volume_usd
  FROM compound.compound_v3_events  -- TODO_VERIFY
  WHERE blockchain = '{{chain}}'
    AND event_type IN ('supply', 'borrow')
    AND block_time >= TIMESTAMP '{{month_start}}'
    AND block_time <  TIMESTAMP '{{month_end}}'
),

unioned AS (
  SELECT * FROM dex_swaps
  UNION ALL SELECT * FROM aave_actions
  UNION ALL SELECT * FROM compound_actions
)
SELECT
  wallet,
  protocol,
  action_type,
  CAST(SUM(action_inc) AS BIGINT)                 AS action_count,
  CAST(SUM(volume_usd) AS DOUBLE)                 AS volume_usd
FROM unioned
GROUP BY wallet, protocol, action_type
ORDER BY action_count DESC
