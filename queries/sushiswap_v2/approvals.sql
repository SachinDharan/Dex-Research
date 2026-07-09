-- sushiswap_v2/approvals.sql — stage 2 of the SushiSwap arm fetch
--
-- Every ERC-20 approve() on the study stablecoins by a batch of qualifying
-- wallets, within one time chunk. Spender is deliberately UNPINNED — Sushi
-- approvals go to its routers (not Permit2); aggregator and Permit2 approvals
-- by the same wallets are captured too and classified downstream.
--
-- Run per (wallet batch x time chunk) so each execution fits the plan's
-- 2-minute cap. Chunks span from Permit2 genesis (2022-11-01) to window end so
-- outstanding-allowance-at-end-of-period sees the latest approval ever.
--
-- Parameters:
--   {{wallets}}      comma-separated 0x-prefixed address literals (unquoted)
--   {{chunk_start}}  approval time slice start, inclusive
--   {{chunk_end}}    approval time slice end, exclusive

SELECT
  to_hex(a.owner)                          AS wallet,
  a.evt_block_time                         AS block_time,
  a.evt_block_number                       AS block_number,
  to_hex(a.evt_tx_hash)                    AS tx_hash,
  to_hex(a.contract_address)               AS token,
  te.symbol                                AS token_symbol,
  to_hex(a.spender)                        AS counterparty,
  CAST(a.value AS varchar)                 AS amount_raw,
  (CAST(a.value AS varchar) = '0')         AS is_revoke,
  t.gas_used,
  t.gas_price,
  CAST(t.gas_used AS double) * t.gas_price / 1e18  AS gas_cost_eth,
  t.max_priority_fee_per_gas,
  to_hex(bytearray_substring(t.data, 1, 4))        AS method_id
FROM erc20_ethereum.evt_approval a
LEFT JOIN ethereum.transactions t
  ON t.hash = a.evt_tx_hash
  AND t.block_date >= DATE '{{chunk_start}}'
  AND t.block_date <  DATE '{{chunk_end}}'
LEFT JOIN tokens.erc20 te
  ON te.blockchain = 'ethereum'
  AND te.contract_address = a.contract_address
WHERE a.evt_block_time >= TIMESTAMP '{{chunk_start}}'
  AND a.evt_block_time <  TIMESTAMP '{{chunk_end}}'
  AND a.contract_address IN (
        0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48,  -- USDC
        0xdAC17F958D2ee523a2206206994597C13D831ec7,  -- USDT
        0x6B175474E89094C44Da98b954EedeAC495271d0F   -- DAI
      )
  AND a.owner IN ({{wallets}})
-- deterministic order so offset-based resume is stable across re-executions
ORDER BY a.evt_block_number, a.evt_tx_hash, a.evt_index
