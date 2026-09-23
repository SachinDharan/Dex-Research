-- aave_v3/supplies.sql — STAGE 1 of the Aave arm: ALL Supply events of the
-- study stables on the V3 Pool, window-scoped, store-broad: aggregator and
-- manager flow (ParaSwap, Kyber, Kiln vaults, Aave Umbrella, CoW solvers)
-- is stored too and separated downstream by tx_to — entry class is a flag,
-- not a filter.
--
-- Wallet = tx `from` (the signer). The event's `user` (funds source) and
-- `onBehalfOf` (aToken recipient) are stored for on-behalf analysis, never
-- used for attribution.
--
-- Parameters:
--   {{window_start}}   inclusive
--   {{window_end}}     exclusive

WITH sup AS (
  SELECT evt_tx_hash, evt_index, evt_block_time, evt_block_number,
         reserve, "user" AS evt_user, onBehalfOf, amount
  FROM aave_v3_ethereum.pool_evt_supply
  WHERE reserve IN (0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48,
                    0xdAC17F958D2ee523a2206206994597C13D831ec7,
                    0x6B175474E89094C44Da98b954EedeAC495271d0F)
    AND evt_block_time >= TIMESTAMP '{{window_start}}'
    AND evt_block_time <  TIMESTAMP '{{window_end}}'
)
SELECT
  to_hex(t."from")                         AS wallet,
  s.evt_block_time                         AS block_time,
  s.evt_block_number                       AS block_number,
  to_hex(s.evt_tx_hash)                    AS tx_hash,
  s.evt_index,
  to_hex(s.reserve)                        AS token,
  CASE s.reserve
    WHEN 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48 THEN 'USDC'
    WHEN 0xdAC17F958D2ee523a2206206994597C13D831ec7 THEN 'USDT'
    WHEN 0x6B175474E89094C44Da98b954EedeAC495271d0F THEN 'DAI'
  END                                      AS token_symbol,
  CAST(s.amount AS varchar)                AS amount_raw,
  to_hex(s.evt_user)                       AS evt_user,
  to_hex(s.onBehalfOf)                     AS on_behalf_of,
  to_hex(t."to")                           AS tx_to,
  t.gas_used,
  t.gas_price,
  CAST(t.gas_used AS double) * t.gas_price / 1e18  AS gas_cost_eth,
  t.max_priority_fee_per_gas,
  to_hex(bytearray_substring(t.data, 1, 4))        AS method_id
FROM sup s
LEFT JOIN ethereum.transactions t
  ON t.hash = s.evt_tx_hash
  AND t.block_date >= DATE '{{window_start}}'
  AND t.block_date <  DATE '{{window_end}}'
-- deterministic order so offset-based resume is stable across re-executions
ORDER BY s.evt_block_number, s.evt_tx_hash, s.evt_index
