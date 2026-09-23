-- gas_measurements.sql
--
-- Tx-level gas data (gas_used, effective_gas_price) joined with daily ETH /
-- POL USD prices for a batch of transactions. Used by the metrics stage to
-- compute USD gas cost per action.
--
-- Parameters:
--   {{chain}}        ethereum | polygon
--   {{tx_hashes}}    comma-separated 0x-prefixed tx hashes
--
-- Schema produced:
--   tx_hash             varbinary
--   block_time          timestamp
--   gas_used            bigint
--   gas_price_wei       bigint
--   native_usd_price    double      ETH or POL USD at day(block_time)
--   gas_cost_native     double      gas_used * gas_price / 1e18
--   gas_cost_usd        double      gas_cost_native * native_usd_price
--
-- NOTE: prices.usd is a Spellbook table providing native-asset USD prices by
-- chain + minute. Bucketing to day is sufficient for cost accounting; switch
-- to minute-level if you need tighter alignment.

WITH txs AS (
  SELECT
    hash                                  AS tx_hash,
    block_time,
    gas_used,
    COALESCE(gas_price, effective_gas_price) AS gas_price_wei
  FROM {{chain}}.transactions
  WHERE hash IN ({{tx_hashes}})
),
native_prices AS (
  SELECT
    date_trunc('day', minute) AS day,
    AVG(price)                AS native_usd_price
  FROM prices.usd
  WHERE blockchain = '{{chain}}'
    AND symbol = CASE WHEN '{{chain}}' = 'polygon' THEN 'POL' ELSE 'ETH' END
  GROUP BY 1
)
SELECT
  t.tx_hash,
  t.block_time,
  t.gas_used,
  t.gas_price_wei,
  p.native_usd_price,
  (CAST(t.gas_used AS DOUBLE) * CAST(t.gas_price_wei AS DOUBLE)) / 1e18
                                          AS gas_cost_native,
  ((CAST(t.gas_used AS DOUBLE) * CAST(t.gas_price_wei AS DOUBLE)) / 1e18)
    * p.native_usd_price                  AS gas_cost_usd
FROM txs t
LEFT JOIN native_prices p
  ON p.day = date_trunc('day', t.block_time)
