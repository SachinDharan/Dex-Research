-- sushiswap_v2/permit2_events.sql — stage 3 of the SushiSwap arm fetch
--
-- All Permit2 activity by a batch of qualifying wallets, study window only.
-- Kept for cross-protocol contrast (Sushi's own flow never touches Permit2):
-- does the per-swap-approval crowd behave differently when it meets Permit2
-- elsewhere? Includes the Lockdown branch — revocations made INSIDE Permit2
-- (revoke.cash path) that a Permit/Approval-only fetch would miss.
--
-- Run per wallet batch so each execution fits the plan's 2-minute cap.
--
-- Parameters:
--   wallets_values   comma-separated parenthesised address literals, e.g. (0xabc..), (0xdef..)
--   {{window_start}}   study window start, inclusive
--   {{window_end}}     study window end, exclusive

WITH wallet_list(wallet) AS (
  VALUES {{wallets_values}}
),
unified AS (

  -- Permit event (AllowanceTransfer set by signature)
  SELECT
    'permit2_permit' AS record_type,
    p.owner, p.evt_block_time AS block_time, p.evt_block_number AS block_number,
    p.evt_tx_hash AS tx_hash, p.token, p.spender AS counterparty,
    CAST(p.amount AS varchar) AS amount_raw
  FROM uniswap_v3_ethereum.permit2_evt_permit p
  WHERE p.owner IN (SELECT wallet FROM wallet_list)
    AND p.evt_block_time >= TIMESTAMP '{{window_start}}'
    AND p.evt_block_time <  TIMESTAMP '{{window_end}}'

  UNION ALL

  -- Approval event (AllowanceTransfer set on-chain)
  SELECT
    'permit2_approval',
    a.owner, a.evt_block_time, a.evt_block_number, a.evt_tx_hash,
    a.token, a.spender, CAST(a.amount AS varchar)
  FROM uniswap_v3_ethereum.permit2_evt_approval a
  WHERE a.owner IN (SELECT wallet FROM wallet_list)
    AND a.evt_block_time >= TIMESTAMP '{{window_start}}'
    AND a.evt_block_time <  TIMESTAMP '{{window_end}}'

  UNION ALL

  -- SignatureTransfer (no event; a successful call).
  -- Dune encodes `permitted` as either a plain JSON string or an array of JSON
  -- strings depending on the ABI variant decoded. COALESCE handles both.
  SELECT
    'permit2_signature_transfer',
    c.owner, c.call_block_time, c.call_block_number, c.call_tx_hash,
    COALESCE(
      TRY(from_hex(substr(json_extract_scalar(json_parse(json_extract_scalar(c.permit, '$.permitted')),   '$.token'), 3))),
      TRY(from_hex(substr(json_extract_scalar(json_parse(json_extract_scalar(c.permit, '$.permitted[0]')), '$.token'), 3)))
    ),
    COALESCE(
      TRY(from_hex(substr(json_extract_scalar(c.transferDetails_text, '$.to'), 3))),
      TRY(from_hex(substr(json_extract_scalar(json_parse(c.transferDetails_array_text[1]), '$.to'), 3)))
    ),
    COALESCE(
      TRY(json_extract_scalar(json_parse(json_extract_scalar(c.permit, '$.permitted')),    '$.amount')),
      TRY(json_extract_scalar(json_parse(json_extract_scalar(c.permit, '$.permitted[0]')), '$.amount'))
    )
  FROM uniswap_v3_ethereum.permit2_call_permittransferfrom c
  WHERE c.owner IN (SELECT wallet FROM wallet_list)
    AND c.call_block_time >= TIMESTAMP '{{window_start}}'
    AND c.call_block_time <  TIMESTAMP '{{window_end}}'
    AND c.call_success

  UNION ALL

  -- SignatureTransfer with witness (Universal Router); same dual-variant JSON
  SELECT
    'permit2_signature_transfer_witness',
    c.owner, c.call_block_time, c.call_block_number, c.call_tx_hash,
    COALESCE(
      TRY(from_hex(substr(json_extract_scalar(json_parse(json_extract_scalar(c.permit, '$.permitted')),    '$.token'), 3))),
      TRY(from_hex(substr(json_extract_scalar(json_parse(json_extract_scalar(c.permit, '$.permitted[0]')), '$.token'), 3)))
    ),
    COALESCE(
      TRY(from_hex(substr(json_extract_scalar(c.transferDetails_text, '$.to'), 3))),
      TRY(from_hex(substr(json_extract_scalar(json_parse(c.transferDetails_array_text[1]), '$.to'), 3)))
    ),
    COALESCE(
      TRY(json_extract_scalar(json_parse(json_extract_scalar(c.permit, '$.permitted')),    '$.amount')),
      TRY(json_extract_scalar(json_parse(json_extract_scalar(c.permit, '$.permitted[0]')), '$.amount'))
    )
  FROM uniswap_v3_ethereum.permit2_call_permitwitnesstransferfrom c
  WHERE c.owner IN (SELECT wallet FROM wallet_list)
    AND c.call_block_time >= TIMESTAMP '{{window_start}}'
    AND c.call_block_time <  TIMESTAMP '{{window_end}}'
    AND c.call_success

  UNION ALL

  -- Lockdown — revocation INSIDE Permit2; zeroes a (token, spender)
  -- sub-approval without touching the ERC-20 layer
  SELECT
    'permit2_lockdown',
    l.owner, l.evt_block_time, l.evt_block_number, l.evt_tx_hash,
    l.token, l.spender, CAST(NULL AS varchar)
  FROM uniswap_v3_ethereum.permit2_evt_lockdown l
  WHERE l.owner IN (SELECT wallet FROM wallet_list)
    AND l.evt_block_time >= TIMESTAMP '{{window_start}}'
    AND l.evt_block_time <  TIMESTAMP '{{window_end}}'

)
SELECT
  u.record_type,
  to_hex(u.owner)                          AS wallet,
  u.block_time,
  u.block_number,
  to_hex(u.tx_hash)                        AS tx_hash,
  to_hex(u.token)                          AS token,
  te.symbol                                AS token_symbol,
  to_hex(u.counterparty)                   AS counterparty,
  u.amount_raw,
  (u.amount_raw = '0' OR u.record_type = 'permit2_lockdown')  AS is_revoke,
  t.gas_used,
  t.gas_price,
  CAST(t.gas_used AS double) * t.gas_price / 1e18  AS gas_cost_eth,
  t.max_priority_fee_per_gas,
  to_hex(bytearray_substring(t.data, 1, 4))        AS method_id
FROM unified u
LEFT JOIN ethereum.transactions t
  ON t.hash = u.tx_hash
  AND t.block_date >= DATE '{{window_start}}'
  AND t.block_date <  DATE '{{window_end}}'
LEFT JOIN tokens.erc20 te
  ON te.blockchain = 'ethereum'
  AND te.contract_address = u.token
-- deterministic order so offset-based resume is stable across re-executions
ORDER BY u.block_number, u.tx_hash, u.record_type
