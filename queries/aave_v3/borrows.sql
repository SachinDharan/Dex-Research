-- aave_v3/borrows.sql — Borrow events of the study stables on the V3 Pool,
-- window-scoped, store-broad (same rules as supplies.sql). Borrows are the
-- SECONDARY action: they need no ERC-20 approval (the borrower receives
-- tokens), so they never enter the approval-to-action pairing — but unlike
-- Compound, Aave emits real Borrow events, so the action itself is exact.
--
-- Parameters:
--   {{window_start}}   inclusive
--   {{window_end}}     exclusive

WITH bor AS (
  SELECT evt_tx_hash, evt_index, evt_block_time, evt_block_number,
         reserve, "user" AS evt_user, onBehalfOf, amount, interestRateMode
  FROM aave_v3_ethereum.pool_evt_borrow
  WHERE reserve IN (0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48,
                    0xdAC17F958D2ee523a2206206994597C13D831ec7,
                    0x6B175474E89094C44Da98b954EedeAC495271d0F)
    AND evt_block_time >= TIMESTAMP '{{window_start}}'
    AND evt_block_time <  TIMESTAMP '{{window_end}}'
)
SELECT
  to_hex(t."from")                         AS wallet,
  b.evt_block_time                         AS block_time,
  b.evt_block_number                       AS block_number,
  to_hex(b.evt_tx_hash)                    AS tx_hash,
  b.evt_index,
  to_hex(b.reserve)                        AS token,
  CASE b.reserve
    WHEN 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48 THEN 'USDC'
    WHEN 0xdAC17F958D2ee523a2206206994597C13D831ec7 THEN 'USDT'
    WHEN 0x6B175474E89094C44Da98b954EedeAC495271d0F THEN 'DAI'
  END                                      AS token_symbol,
  CAST(b.amount AS varchar)                AS amount_raw,
  to_hex(b.evt_user)                       AS evt_user,
  to_hex(b.onBehalfOf)                     AS on_behalf_of,
  CAST(b.interestRateMode AS bigint)       AS interest_rate_mode,
  to_hex(t."to")                           AS tx_to,
  t.gas_used,
  t.gas_price,
  CAST(t.gas_used AS double) * t.gas_price / 1e18  AS gas_cost_eth,
  t.max_priority_fee_per_gas,
  to_hex(bytearray_substring(t.data, 1, 4))        AS method_id
FROM bor b
LEFT JOIN ethereum.transactions t
  ON t.hash = b.evt_tx_hash
  AND t.block_date >= DATE '{{window_start}}'
  AND t.block_date <  DATE '{{window_end}}'
-- deterministic order so offset-based resume is stable across re-executions
ORDER BY b.evt_block_number, b.evt_tx_hash, b.evt_index
