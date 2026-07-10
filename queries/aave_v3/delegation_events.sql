-- aave_v3/delegation_events.sql — credit delegation on the stable debt
-- tokens: Aave's fourth permission flavor (an allowance to BORROW against
-- the delegator's collateral). Variable-debt only — the stable-debt table
-- holds 2 events ever (measured 2026-07-09). ALL delegators are fetched
-- (57k events all-time); scoping to the roster happens downstream.
--
-- Lookback runs from Aave V3 Ethereum genesis (2023-01): an un-revoked
-- delegation from 2024 still authorizes borrowing in 2026.
--
-- Parameters (yearly chunks keep each run under the execution cap):
--   {{chunk_start}}   inclusive
--   {{chunk_end}}     exclusive

SELECT
  to_hex(d.fromUser)                       AS wallet,
  to_hex(d.toUser)                         AS delegatee,
  to_hex(d.asset)                          AS token,
  CASE d.asset
    WHEN 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48 THEN 'USDC'
    WHEN 0xdAC17F958D2ee523a2206206994597C13D831ec7 THEN 'USDT'
    WHEN 0x6B175474E89094C44Da98b954EedeAC495271d0F THEN 'DAI'
  END                                      AS token_symbol,
  d.evt_block_time                         AS block_time,
  d.evt_block_number                       AS block_number,
  to_hex(d.evt_tx_hash)                    AS tx_hash,
  d.evt_index,
  CAST(d.amount AS varchar)                AS amount_raw,
  (CAST(d.amount AS varchar) = '0')        AS is_revoke,
  t.gas_used,
  t.gas_price,
  CAST(t.gas_used AS double) * t.gas_price / 1e18  AS gas_cost_eth,
  t.max_priority_fee_per_gas,
  to_hex(bytearray_substring(t.data, 1, 4))        AS method_id
FROM aave_v3_ethereum.variabledebttoken_evt_borrowallowancedelegated d
LEFT JOIN ethereum.transactions t
  ON t.hash = d.evt_tx_hash
  AND t.block_date >= DATE '{{chunk_start}}'
  AND t.block_date <  DATE '{{chunk_end}}'
WHERE d.asset IN (0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48,
                  0xdAC17F958D2ee523a2206206994597C13D831ec7,
                  0x6B175474E89094C44Da98b954EedeAC495271d0F)
  AND d.evt_block_time >= TIMESTAMP '{{chunk_start}}'
  AND d.evt_block_time <  TIMESTAMP '{{chunk_end}}'
-- deterministic order so offset-based resume is stable across re-executions
ORDER BY d.evt_block_number, d.evt_tx_hash, d.evt_index
