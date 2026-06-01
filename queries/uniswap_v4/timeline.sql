-- uniswap_v4/timeline.sql  —  the whole study fetch in ONE query
--
-- A single execution that returns three things for the same wallet population:
--   1. the qualifying Uniswap V4 stablecoin -> ETH swaps (native ETH or WETH pool currency),
--   2. those swappers' ERC-20 approvals to Permit2 (18-month lookback),
--   3. those swappers' Permit2 events (AllowanceTransfer + SignatureTransfer),
--      study window only.
--
-- The swap-wallet set is derived once (swaps -> swap_wallets) and reused by the
-- approval and permit2 branches, so NO wallet list is passed in and the data is
-- pulled in one shot. Each row is tagged with `record_type`; the fetch stage
-- splits the result into raw/swaps, raw/approvals, raw/permit2_events.
--
-- Parameters:
--   {{window_start}}   study window start, inclusive
--   {{window_end}}     study window end, exclusive
--   {{lookback_start}} window_start minus 18 months (approval lower bound)
--
-- Columns (nullable superset — only the relevant ones are set per record_type):
--   record_type    varchar    swap | erc20_approval | permit2_permit |
--                            permit2_approval | permit2_signature_transfer |
--                            permit2_signature_transfer_witness
--   wallet         varbinary
--   block_time     timestamp
--   block_number   bigint
--   tx_hash        varbinary
--   token          varbinary  swap: token sold; approval/permit2: token (null for sig transfers)
--   counterparty   varbinary  swap: token bought; approval/permit2: spender (null for sig transfers)
--   amount_raw     varchar    approval value / permit2 amount as decimal string (null for swaps/sig transfers)
--   amount_usd     double     swap only — compute downstream for approvals/permit2 from amount_raw + decimals + price
--   token_symbol   varchar    populated for ALL record types via tokens.erc20 lookup on `token`
--   counter_symbol varchar    swap: token_bought_symbol (NULL for non-swap rows — counterparty is a spender, not a token)
--   is_revoke      boolean    TRUE when an erc20_approval row sets allowance to 0 (revoke); FALSE for everything else
--   gas_used       bigint     actual gas units consumed by the transaction
--   gas_price      bigint     effective gas price in wei (base + priority fee post-EIP-1559)
--   gas_cost_eth   double     gas_used * gas_price / 1e18 — ETH spent on gas for this tx
--   swap_count     bigint     number of qualifying swaps this wallet made in the study window

WITH swaps AS (
  SELECT
    tx_from              AS wallet,
    block_time,
    block_number,
    tx_hash,
    token_sold_address,
    token_bought_address,
    token_sold_symbol,
    token_bought_symbol,
    amount_usd
  FROM dex.trades
  WHERE blockchain = 'ethereum'
    AND project = 'uniswap'
    AND version = '4'
    AND block_month >= DATE '{{window_start}}'       -- partition pruning
    AND block_month <  DATE '{{window_end}}'
    AND block_time  >= TIMESTAMP '{{window_start}}'   -- exact range
    AND block_time  <  TIMESTAMP '{{window_end}}'
    -- V4 ETH pools exist in two variants: native-ETH currency (address(0)) and WETH currency.
    -- Both represent user-intent "swap stable for ETH" — include both.
    AND token_bought_address IN (
          0x0000000000000000000000000000000000000000,  -- native ETH (V4 native-currency pools)
          0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2   -- WETH (V4 pools using WETH currency)
        )
    AND token_sold_address IN (
          0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48,  -- USDC
          0xdAC17F958D2ee523a2206206994597C13D831ec7,  -- USDT
          0x6B175474E89094C44Da98b954EedeAC495271d0F   -- DAI
        )
),
swap_wallets AS (
  SELECT DISTINCT wallet FROM swaps   -- derived once, no re-scan of dex.trades
),
swap_counts AS (
  SELECT wallet, COUNT(*) AS swap_count
  FROM swaps
  GROUP BY wallet
),
unified AS (

  -- 1) the qualifying swaps themselves
  SELECT
    'swap'                         AS record_type,
    wallet,
    block_time,
    block_number,
    tx_hash,
    token_sold_address             AS token,
    token_bought_address           AS counterparty,
    CAST(NULL AS varchar)          AS amount_raw,
    CAST(amount_usd AS double)     AS amount_usd,
    token_sold_symbol              AS token_symbol,
    token_bought_symbol            AS counter_symbol
  FROM swaps

  UNION ALL

  -- 2) those wallets' ERC-20 approvals to Permit2 (18-month lookback)
  SELECT
    'erc20_approval',
    a.owner,
    a.evt_block_time,
    a.evt_block_number,
    a.evt_tx_hash,
    a.contract_address,
    a.spender,
    CAST(a.value AS varchar),
    CAST(NULL AS double),
    CAST(NULL AS varchar),
    CAST(NULL AS varchar)
  FROM erc20_ethereum.evt_approval a
  JOIN swap_wallets w ON a.owner = w.wallet
  WHERE a.evt_block_time >= TIMESTAMP '{{lookback_start}}'
    AND a.evt_block_time <  TIMESTAMP '{{window_end}}'
    AND a.spender = 0x000000000022D473030F116dDEE9F6B43aC78BA3   -- Permit2
    AND a.contract_address IN (
          0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48,
          0xdAC17F958D2ee523a2206206994597C13D831ec7,
          0x6B175474E89094C44Da98b954EedeAC495271d0F
        )

  UNION ALL

  -- 3a) Permit2 Permit event (AllowanceTransfer set by signature), study window
  SELECT
    'permit2_permit',
    p.owner, p.evt_block_time, p.evt_block_number, p.evt_tx_hash,
    p.token, p.spender, CAST(p.amount AS varchar),
    CAST(NULL AS double), CAST(NULL AS varchar), CAST(NULL AS varchar)
  FROM uniswap_v3_ethereum.permit2_evt_permit p
  JOIN swap_wallets w ON p.owner = w.wallet
  WHERE p.evt_block_time >= TIMESTAMP '{{window_start}}'
    AND p.evt_block_time <  TIMESTAMP '{{window_end}}'

  UNION ALL

  -- 3b) Permit2 Approval event (AllowanceTransfer set on-chain), study window
  SELECT
    'permit2_approval',
    a.owner, a.evt_block_time, a.evt_block_number, a.evt_tx_hash,
    a.token, a.spender, CAST(a.amount AS varchar),
    CAST(NULL AS double), CAST(NULL AS varchar), CAST(NULL AS varchar)
  FROM uniswap_v3_ethereum.permit2_evt_approval a
  JOIN swap_wallets w ON a.owner = w.wallet
  WHERE a.evt_block_time >= TIMESTAMP '{{window_start}}'
    AND a.evt_block_time <  TIMESTAMP '{{window_end}}'

  UNION ALL

  -- 3c) Permit2 SignatureTransfer (no event; a successful call), study window
  -- Dune encodes `permitted` as either a plain JSON string or an array of JSON strings
  -- depending on the ABI variant decoded. COALESCE handles both.
  -- `to` address lives in transferDetails_text (single) or transferDetails_array_text[1] (batch).
  SELECT
    'permit2_signature_transfer',
    c.owner, c.call_block_time, c.call_block_number, c.call_tx_hash,
    COALESCE(
      TRY(from_hex(substr(json_extract_scalar(json_parse(json_extract_scalar(c.permit, '$.permitted')),   '$.token'), 3))),
      TRY(from_hex(substr(json_extract_scalar(json_parse(json_extract_scalar(c.permit, '$.permitted[0]')), '$.token'), 3)))
    )                                                                            AS token,
    COALESCE(
      TRY(from_hex(substr(json_extract_scalar(c.transferDetails_text, '$.to'), 3))),
      TRY(from_hex(substr(json_extract_scalar(json_parse(c.transferDetails_array_text[1]), '$.to'), 3)))
    )                                                                            AS counterparty,
    COALESCE(
      TRY(json_extract_scalar(json_parse(json_extract_scalar(c.permit, '$.permitted')),    '$.amount')),
      TRY(json_extract_scalar(json_parse(json_extract_scalar(c.permit, '$.permitted[0]')), '$.amount'))
    )                                                                            AS amount_raw,
    CAST(NULL AS double), CAST(NULL AS varchar), CAST(NULL AS varchar)
  FROM uniswap_v3_ethereum.permit2_call_permittransferfrom c
  JOIN swap_wallets w ON c.owner = w.wallet
  WHERE c.call_block_time >= TIMESTAMP '{{window_start}}'
    AND c.call_block_time <  TIMESTAMP '{{window_end}}'
    AND c.call_success

  UNION ALL

  -- 3d) Permit2 SignatureTransfer with witness (Universal Router), study window
  -- same dual-variant JSON structure as 3c
  SELECT
    'permit2_signature_transfer_witness',
    c.owner, c.call_block_time, c.call_block_number, c.call_tx_hash,
    COALESCE(
      TRY(from_hex(substr(json_extract_scalar(json_parse(json_extract_scalar(c.permit, '$.permitted')),    '$.token'), 3))),
      TRY(from_hex(substr(json_extract_scalar(json_parse(json_extract_scalar(c.permit, '$.permitted[0]')), '$.token'), 3)))
    )                                                                            AS token,
    COALESCE(
      TRY(from_hex(substr(json_extract_scalar(c.transferDetails_text, '$.to'), 3))),
      TRY(from_hex(substr(json_extract_scalar(json_parse(c.transferDetails_array_text[1]), '$.to'), 3)))
    )                                                                            AS counterparty,
    COALESCE(
      TRY(json_extract_scalar(json_parse(json_extract_scalar(c.permit, '$.permitted')),    '$.amount')),
      TRY(json_extract_scalar(json_parse(json_extract_scalar(c.permit, '$.permitted[0]')), '$.amount'))
    )                                                                            AS amount_raw,
    CAST(NULL AS double), CAST(NULL AS varchar), CAST(NULL AS varchar)
  FROM uniswap_v3_ethereum.permit2_call_permitwitnesstransferfrom c
  JOIN swap_wallets w ON c.owner = w.wallet
  WHERE c.call_block_time >= TIMESTAMP '{{window_start}}'
    AND c.call_block_time <  TIMESTAMP '{{window_end}}'
    AND c.call_success

)

SELECT
  u.record_type,
  u.wallet,
  u.block_time,
  u.block_number,
  u.tx_hash,
  u.token,
  u.counterparty,
  u.amount_raw,
  u.amount_usd,
  COALESCE(u.token_symbol, te.symbol)                                         AS token_symbol,
  u.counter_symbol,
  (u.record_type = 'erc20_approval' AND u.amount_raw = '0')                   AS is_revoke,
  t.gas_used,
  t.gas_price,
  CAST(t.gas_used AS double) * t.gas_price / 1e18                             AS gas_cost_eth,
  sc.swap_count
FROM unified u
LEFT JOIN swap_counts sc ON sc.wallet = u.wallet
LEFT JOIN ethereum.transactions t
  ON t.hash = u.tx_hash
  AND t.block_date >= DATE '{{lookback_start}}'
  AND t.block_date <  DATE '{{window_end}}'
LEFT JOIN tokens.erc20 te
  ON te.blockchain = 'ethereum'
  AND te.contract_address = u.token
