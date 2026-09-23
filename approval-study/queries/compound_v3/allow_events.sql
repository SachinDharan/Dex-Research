-- compound_v3/allow_events.sql — Comet operator grants (allow/allowBySig),
-- ALL four Ethereum Comets, from raw logs.
--
-- Why raw logs: the decoded compound_v3_ethereum.cometext_evt_approval table
-- is STALE (43 rows, dead since 2025-02 — measured 2026-07-09). allow() is
-- implemented in CometExt and reached via the Comet proxy's fallback, so the
-- event is emitted FROM the Comet proxy address with the ERC-20 Approval
-- signature; value is uint256-max (grant) or 0 (revoke) — nothing else
-- exists on chain (verified over ~2.6k events). ALL owners are fetched
-- (population is tiny); scoping to the roster happens downstream.
--
-- Lookback runs from Comet genesis (cUSDCv3 launched 2022-08): a 2023 grant
-- still authorizes 2026 actions, same rationale as the ERC-20 lookback.
--
-- Parameters (yearly chunks keep each run under the execution cap):
--   {{chunk_start}}   inclusive
--   {{chunk_end}}     exclusive

SELECT
  CASE l.contract_address
    WHEN 0xc3d688b66703497daa19211eedff47f25384cdc3 THEN 'cUSDCv3'
    WHEN 0x3afdc9bca9213a35503b077a6072f3d0d5ab0840 THEN 'cUSDTv3'
    WHEN 0x5d409e56d886231adaf00c8775665ad0f9897b56 THEN 'cUSDSv3'
    WHEN 0xa17581a9e3356d9a858b789d68b4d866e593ae94 THEN 'cWETHv3'
  END                                      AS market,
  to_hex(l.contract_address)               AS comet,
  to_hex(bytearray_substring(l.topic1, 13, 20))  AS wallet,
  to_hex(bytearray_substring(l.topic2, 13, 20))  AS manager,
  l.block_time,
  l.block_number,
  to_hex(l.tx_hash)                        AS tx_hash,
  l.index                                  AS evt_index,
  (l.data = 0xffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff) AS is_grant,
  (l.data = 0x0000000000000000000000000000000000000000000000000000000000000000) AS is_revoke,
  t.gas_used,
  t.gas_price,
  CAST(t.gas_used AS double) * t.gas_price / 1e18  AS gas_cost_eth,
  t.max_priority_fee_per_gas,
  to_hex(bytearray_substring(t.data, 1, 4))        AS method_id
FROM ethereum.logs l
LEFT JOIN ethereum.transactions t
  ON t.hash = l.tx_hash
  AND t.block_date >= DATE '{{chunk_start}}'
  AND t.block_date <  DATE '{{chunk_end}}'
WHERE l.contract_address IN (
    0xc3d688b66703497daa19211eedff47f25384cdc3,   -- cUSDCv3
    0x3afdc9bca9213a35503b077a6072f3d0d5ab0840,   -- cUSDTv3
    0x5d409e56d886231adaf00c8775665ad0f9897b56,   -- cUSDSv3
    0xa17581a9e3356d9a858b789d68b4d866e593ae94)   -- cWETHv3
  AND l.topic0 = 0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925
  AND l.block_time >= TIMESTAMP '{{chunk_start}}'
  AND l.block_time <  TIMESTAMP '{{chunk_end}}'
-- deterministic order so offset-based resume is stable across re-executions
ORDER BY l.block_number, l.tx_hash, l.index
