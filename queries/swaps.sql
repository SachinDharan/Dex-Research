-- swaps.sql
--
-- Per-event swap records for a batch of sampled wallets.
--
-- Parameters:
--   {{chain}}        ethereum | polygon
--   {{month_start}}  inclusive
--   {{month_end}}    exclusive
--   {{wallets}}      comma-separated 0x-prefixed addresses
--
-- Schema produced:
--   wallet                varbinary
--   protocol              varchar
--   block_time            timestamp
--   tx_hash               varbinary
--   token_sold_address    varbinary
--   token_bought_address  varbinary
--   amount_usd            double

SELECT
  tx_from                       AS wallet,
  project                       AS protocol,
  block_time,
  tx_hash,
  token_sold_address,
  token_bought_address,
  CAST(amount_usd AS DOUBLE)    AS amount_usd
FROM dex.trades
WHERE blockchain = '{{chain}}'
  AND project IN ('uniswap', 'sushiswap')
  AND block_time >= TIMESTAMP '{{month_start}}'
  AND block_time <  TIMESTAMP '{{month_end}}'
  AND tx_from IN ({{wallets}})
ORDER BY block_time
