-- aave_v3/funnel.sql — STAGE 0 of the Aave arm: one row per (month, action)
-- pinning exact population counts as validation gates, run BEFORE any bulk
-- fetch (same self-proving protocol as the other three arms).
--
-- Qualifying tx: emitted a Supply of USDC/USDT/DAI on the Aave V3 Pool AND
-- was sent (`tx_to`) directly to the Pool. The WrappedTokenGateway is
-- ETH-only and irrelevant for stable supplies; manager/aggregator flow
-- (ParaSwap, Kyber, CoW solvers, vault routers) is the stored contrast.
-- Borrows are counted with the same split — they are a SECONDARY action
-- (no ERC-20 approval needed to borrow); Aave, unlike Compound, has real
-- Borrow events.
--
-- Wallet = tx `from` (the signer), consistent with every arm. The event's
-- `user`/`onBehalfOf` fields are stored in the detail fetch, never used
-- for attribution.
--
-- Parameters:
--   {{window_start}}   inclusive
--   {{window_end}}     exclusive

WITH ev AS (
  SELECT 'supply' AS action, evt_tx_hash, evt_block_time
  FROM aave_v3_ethereum.pool_evt_supply
  WHERE reserve IN (0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48,
                    0xdAC17F958D2ee523a2206206994597C13D831ec7,
                    0x6B175474E89094C44Da98b954EedeAC495271d0F)
    AND evt_block_time >= TIMESTAMP '{{window_start}}'
    AND evt_block_time <  TIMESTAMP '{{window_end}}'
  UNION ALL
  SELECT 'borrow', evt_tx_hash, evt_block_time
  FROM aave_v3_ethereum.pool_evt_borrow
  WHERE reserve IN (0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48,
                    0xdAC17F958D2ee523a2206206994597C13D831ec7,
                    0x6B175474E89094C44Da98b954EedeAC495271d0F)
    AND evt_block_time >= TIMESTAMP '{{window_start}}'
    AND evt_block_time <  TIMESTAMP '{{window_end}}'
),
per_tx AS (
  SELECT e.action, e.evt_tx_hash AS tx_hash,
         MIN(date_trunc('month', e.evt_block_time)) AS month,
         COUNT(*)      AS events,
         MIN(t."from") AS wallet,
         MIN(t."to")   AS tx_to
  FROM ev e
  JOIN ethereum.transactions t
    ON t.hash = e.evt_tx_hash
    AND t.block_date >= DATE '{{window_start}}'
    AND t.block_date <  DATE '{{window_end}}'
  GROUP BY 1, 2
)
SELECT
  action,
  CAST(month AS date)                                      AS month,
  COUNT(*)                                                 AS txs_all,
  COUNT_IF(tx_to = 0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2)  AS qualifying_txs,
  COUNT(DISTINCT IF(tx_to = 0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2, wallet)) AS qualifying_wallets,
  SUM(IF(tx_to = 0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2, events, 0))         AS qualifying_events,
  COUNT_IF(tx_to != 0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2 OR tx_to IS NULL) AS contrast_txs
FROM per_tx
GROUP BY 1, 2
ORDER BY 1, 2
