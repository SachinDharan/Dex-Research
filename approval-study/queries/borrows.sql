-- borrows.sql
--
-- Per-event borrow records on Aave V3 + Compound V3.
--
-- Parameters:
--   {{chain}}        ethereum | polygon
--   {{month_start}}  inclusive
--   {{month_end}}    exclusive
--   {{wallets}}      comma-separated 0x-prefixed addresses
--
-- Schema produced:
--   wallet         varbinary
--   protocol       varchar          aave_v3 | compound_v3
--   block_time     timestamp
--   tx_hash        varbinary
--   token_address  varbinary
--   amount_raw     decimal(38, 0)
--   amount_usd     double

WITH aave AS (
  SELECT
    tx_from                AS wallet,
    CAST('aave_v3' AS VARCHAR) AS protocol,
    block_time,
    tx_hash,
    reserve                AS token_address,
    CAST(amount AS DECIMAL(38, 0)) AS amount_raw,
    CAST(amount_usd AS DOUBLE)     AS amount_usd
  FROM aave.aave_v3_lending_events  -- TODO_VERIFY
  WHERE blockchain = '{{chain}}'
    AND event_type = 'borrow'
    AND block_time >= TIMESTAMP '{{month_start}}'
    AND block_time <  TIMESTAMP '{{month_end}}'
    AND tx_from IN ({{wallets}})
),
comp AS (
  SELECT
    tx_from                AS wallet,
    CAST('compound_v3' AS VARCHAR) AS protocol,
    block_time,
    tx_hash,
    asset                  AS token_address,
    CAST(amount AS DECIMAL(38, 0)) AS amount_raw,
    CAST(amount_usd AS DOUBLE)     AS amount_usd
  FROM compound.compound_v3_events  -- TODO_VERIFY
  WHERE blockchain = '{{chain}}'
    AND event_type = 'borrow'
    AND block_time >= TIMESTAMP '{{month_start}}'
    AND block_time <  TIMESTAMP '{{month_end}}'
    AND tx_from IN ({{wallets}})
)
SELECT * FROM aave
UNION ALL
SELECT * FROM comp
ORDER BY block_time
