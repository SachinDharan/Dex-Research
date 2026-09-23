-- compound_v3/supplies.sql — STAGE 1 of the Compound arm: ALL base-asset
-- Supply events on the stable Comets (cUSDCv3, cUSDTv3), window-scoped,
-- store-broad: manager/vault flow (Kiln, ERC-4337, proxies) is stored too
-- and separated downstream by tx_to — entry class is a flag, not a filter.
--
-- Wallet = tx `from` (the signer). The event's own `from` is the funds
-- source (the Bulker when entering through it) and `dst` the credited
-- account — both stored for on-behalf analysis, never used for attribution.
--
-- Source note: the unified compound_v3_ethereum.comet_evt_* tables are EMPTY
-- stubs (measured 2026-07-09); the per-market tables are the real ones.
--
-- Parameters:
--   {{window_start}}   inclusive
--   {{window_end}}     exclusive

WITH sup AS (
  SELECT 'cUSDCv3' AS market, contract_address, evt_tx_hash, evt_index,
         evt_block_time, evt_block_number, "from" AS supply_from, dst, amount,
         0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48 AS base_token, 'USDC' AS base_symbol
  FROM compound_v3_ethereum.cusdcv3_evt_supply
  WHERE evt_block_time >= TIMESTAMP '{{window_start}}'
    AND evt_block_time <  TIMESTAMP '{{window_end}}'
  UNION ALL
  SELECT 'cUSDTv3', contract_address, evt_tx_hash, evt_index,
         evt_block_time, evt_block_number, "from", dst, amount,
         0xdAC17F958D2ee523a2206206994597C13D831ec7, 'USDT'
  FROM compound_v3_ethereum.cusdtv3_evt_supply
  WHERE evt_block_time >= TIMESTAMP '{{window_start}}'
    AND evt_block_time <  TIMESTAMP '{{window_end}}'
)
SELECT
  to_hex(t."from")                         AS wallet,
  s.evt_block_time                         AS block_time,
  s.evt_block_number                       AS block_number,
  to_hex(s.evt_tx_hash)                    AS tx_hash,
  s.evt_index,
  s.market,
  to_hex(s.base_token)                     AS token,
  s.base_symbol                            AS token_symbol,
  CAST(s.amount AS varchar)                AS amount_raw,
  to_hex(s.supply_from)                    AS supply_from,
  to_hex(s.dst)                            AS supply_dst,
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
