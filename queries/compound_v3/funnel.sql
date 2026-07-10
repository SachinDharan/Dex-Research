-- compound_v3/funnel.sql — STAGE 0 of the Compound arm: one row per month
-- pinning the exact qualifying-population counts as validation gates, run
-- BEFORE any bulk fetch (same self-proving protocol as the other arms).
--
-- Qualifying tx: emitted a base-asset Supply on a STABLE Comet (cUSDCv3 /
-- cUSDTv3 — cUSDSv3 excluded: USDS is not a study stable and the market did
-- 196 supplies all window; cWETHv3 excluded: non-stable base) AND was sent
-- (`tx_to`) to a Comet or the MainnetBulker. Manager/vault flow (Kiln,
-- OKX, ERC-4337 EntryPoints, personal proxies) is counted as the contrast
-- column, not silently absorbed — the Sushi lesson.
--
-- Wallet = tx `from` (the signer), consistent with every other arm. The
-- Supply event's own `from` is the funds source (the Bulker when entering
-- through it), so it must NOT be used for attribution.
--
-- Decoded-table note (Compound's Dune quirks, measured 2026-07-09):
-- `comet_evt_*` are EMPTY stubs and `cometext_evt_approval` is stale
-- (43 rows, dead since 2025-02). Supplies come from the per-market tables;
-- allow() events come from raw ethereum.logs in a separate query.
--
-- Parameters:
--   {{window_start}}   study window start, inclusive
--   {{window_end}}     study window end, exclusive

WITH sup AS (
  SELECT evt_tx_hash, evt_block_time
  FROM compound_v3_ethereum.cusdcv3_evt_supply
  WHERE evt_block_time >= TIMESTAMP '{{window_start}}'
    AND evt_block_time <  TIMESTAMP '{{window_end}}'
  UNION ALL
  SELECT evt_tx_hash, evt_block_time
  FROM compound_v3_ethereum.cusdtv3_evt_supply
  WHERE evt_block_time >= TIMESTAMP '{{window_start}}'
    AND evt_block_time <  TIMESTAMP '{{window_end}}'
),
per_tx AS (
  SELECT s.evt_tx_hash                                AS tx_hash,
         MIN(date_trunc('month', s.evt_block_time))   AS month,
         COUNT(*)                                     AS sup_events,
         MIN(t."from")                                AS wallet,
         MIN(t."to")                                  AS tx_to
  FROM sup s
  JOIN ethereum.transactions t
    ON t.hash = s.evt_tx_hash
    AND t.block_date >= DATE '{{window_start}}'
    AND t.block_date <  DATE '{{window_end}}'
  GROUP BY 1
),
flagged AS (
  SELECT *,
         tx_to IN (0xc3d688b66703497daa19211eedff47f25384cdc3,   -- cUSDCv3
                   0x3afdc9bca9213a35503b077a6072f3d0d5ab0840)   -- cUSDTv3
           AS entry_comet,
         tx_to = 0xa397a8c2086c554b531c02e29f3291c9704b00c7      -- MainnetBulker
           AS entry_bulker
  FROM per_tx
)
SELECT
  CAST(month AS date)                                       AS month,
  COUNT(*)                                                  AS supply_txs_all,
  COUNT_IF(entry_comet)                                     AS entry_comet_txs,
  COUNT_IF(entry_bulker)                                    AS entry_bulker_txs,
  COUNT_IF(entry_comet OR entry_bulker)                     AS qualifying_txs,
  COUNT(DISTINCT IF(entry_comet OR entry_bulker, wallet))   AS qualifying_wallets,
  SUM(IF(entry_comet OR entry_bulker, sup_events, 0))       AS qualifying_events,
  COUNT_IF(NOT entry_comet AND NOT entry_bulker)            AS contrast_txs,
  COUNT(DISTINCT IF(NOT entry_comet AND NOT entry_bulker, wallet)) AS contrast_wallets
FROM flagged
GROUP BY 1
ORDER BY 1
