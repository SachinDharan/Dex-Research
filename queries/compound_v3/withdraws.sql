-- compound_v3/withdraws.sql — base-asset Withdraw events on the stable
-- Comets, window-scoped. Comet has NO Borrow event: a borrow IS a Withdraw
-- that takes the base balance negative, so borrow-vs-withdraw is a
-- downstream classification over these rows (needs balance context), never
-- a fetch filter. Attribution mirrors supplies.sql: wallet = tx signer;
-- the event's src (debited account) and `to` (recipient) are stored.
--
-- Parameters:
--   {{window_start}}   inclusive
--   {{window_end}}     exclusive

WITH wd AS (
  SELECT 'cUSDCv3' AS market, evt_tx_hash, evt_index,
         evt_block_time, evt_block_number, src, "to" AS wd_to, amount,
         0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48 AS base_token, 'USDC' AS base_symbol
  FROM compound_v3_ethereum.cusdcv3_evt_withdraw
  WHERE evt_block_time >= TIMESTAMP '{{window_start}}'
    AND evt_block_time <  TIMESTAMP '{{window_end}}'
  UNION ALL
  SELECT 'cUSDTv3', evt_tx_hash, evt_index,
         evt_block_time, evt_block_number, src, "to", amount,
         0xdAC17F958D2ee523a2206206994597C13D831ec7, 'USDT'
  FROM compound_v3_ethereum.cusdtv3_evt_withdraw
  WHERE evt_block_time >= TIMESTAMP '{{window_start}}'
    AND evt_block_time <  TIMESTAMP '{{window_end}}'
)
SELECT
  to_hex(t."from")                         AS wallet,
  w.evt_block_time                         AS block_time,
  w.evt_block_number                       AS block_number,
  to_hex(w.evt_tx_hash)                    AS tx_hash,
  w.evt_index,
  w.market,
  to_hex(w.base_token)                     AS token,
  w.base_symbol                            AS token_symbol,
  CAST(w.amount AS varchar)                AS amount_raw,
  to_hex(w.src)                            AS withdraw_src,
  to_hex(w.wd_to)                          AS withdraw_to,
  to_hex(t."to")                           AS tx_to,
  t.gas_used,
  t.gas_price,
  CAST(t.gas_used AS double) * t.gas_price / 1e18  AS gas_cost_eth,
  t.max_priority_fee_per_gas,
  to_hex(bytearray_substring(t.data, 1, 4))        AS method_id
FROM wd w
LEFT JOIN ethereum.transactions t
  ON t.hash = w.evt_tx_hash
  AND t.block_date >= DATE '{{window_start}}'
  AND t.block_date <  DATE '{{window_end}}'
-- deterministic order so offset-based resume is stable across re-executions
ORDER BY w.evt_block_number, w.evt_tx_hash, w.evt_index
