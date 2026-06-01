-- approvals.sql
--
-- Every ERC-20 approve() event emitted by a batch of wallets within the
-- study window, on a single chain. Gas data lives in gas_measurements.sql
-- and is joined later on tx_hash.
--
-- Parameters:
--   {{chain}}        ethereum | polygon
--   {{month_start}}  inclusive
--   {{month_end}}    exclusive
--   {{wallets}}      comma-separated 0x-prefixed addresses
--
-- Schema produced:
--   wallet         varbinary
--   block_time     timestamp
--   tx_hash        varbinary
--   token_address  varbinary
--   spender        varbinary
--   value          decimal(38, 0)   uint256 allowance amount

SELECT
  owner                                  AS wallet,
  evt_block_time                         AS block_time,
  evt_tx_hash                            AS tx_hash,
  contract_address                       AS token_address,
  spender,
  CAST(value AS DECIMAL(38, 0))          AS value
FROM erc20_{{chain}}.evt_Approval
WHERE evt_block_time >= TIMESTAMP '{{month_start}}'
  AND evt_block_time <  TIMESTAMP '{{month_end}}'
  AND owner IN ({{wallets}})
ORDER BY evt_block_time
