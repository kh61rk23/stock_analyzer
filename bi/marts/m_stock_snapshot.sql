-- m_stock_snapshot: スクリーナー通過銘柄の日次スナップショット（銘柄レポート md の正本）。1日1銘柄1行（キー snapshot_date, ticker）。
-- 元は analytics.stock_snapshot（stock_snapshot_writer が毎日 INSERT。2026-09-09 から）。同じ日・銘柄の重複は ingested_at が新しい行。
-- 単位は比率＝小数（0.22＝22%）、debt_equity＝倍にそろえる。2026-10-11 より前の行は書き込み側で単位が混ざっていたので、ここで直す:
--   dividend_yield … 2026-10-01 より前の日本株で 0.3 超は 1/10000、それ以外で 0.3 超は 1/100（％表記の行）
--   roe … 日本株で 1.5 超は 1/100（yfinance 由来の％表記）。fcf_margin・debt_equity・roic … yfinance 由来のみなので 1/100
--   equity_ratio … yfinance からの計算が 100% 超になる誤りがあったため、旧行は空。新しい行も 0〜1 の外は空
-- 事後リターン: analytics.price_daily_latest の終値で、記録日以降の最初の営業日（7日以内）を起点に 5・20 営業日後の騰落率。
--   まだその日が来ていない行は空。終値は分割調整済み（配当は含まない）。
WITH s AS (
  SELECT * FROM `stock-analyzer-edinet.analytics.stock_snapshot`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY snapshot_date, ticker ORDER BY ingested_at DESC) = 1
),
n AS (
  SELECT *,
    snapshot_date < DATE '2026-10-11' AS legacy
  FROM s
),
p AS (
  SELECT ticker, trade_date, close, ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY trade_date) AS rn
  FROM `stock-analyzer-edinet.analytics.price_daily_latest`
  WHERE trade_date >= DATE '2026-09-01' AND close > 0
),
a AS (
  SELECT n.ticker, n.snapshot_date, MIN(p.rn) AS rn0
  FROM n JOIN p ON p.ticker = n.ticker AND p.trade_date BETWEEN n.snapshot_date AND DATE_ADD(n.snapshot_date, INTERVAL 7 DAY)
  GROUP BY n.ticker, n.snapshot_date
)
SELECT
  n.snapshot_date AS snapshot_date,
  n.ticker AS ticker,
  n.market AS market,
  n.name AS name,
  n.sector_code AS sector_code,
  n.sector_name AS sector_name,
  n.composite_score AS composite_score,
  n.sector_score AS sector_score,
  n.value_score AS value_score,
  n.quality_score AS quality_score,
  n.technical_score AS technical_score,
  n.discovery_score AS discovery_score,
  n.per AS per,
  n.pbr AS pbr,
  IF(n.legacy AND n.market = 'JP' AND n.roe > 1.5, n.roe / 100, n.roe) AS roe,
  IF(n.legacy, n.roic / 100, n.roic) AS roic,
  IF(n.legacy, n.fcf_margin / 100, n.fcf_margin) AS fcf_margin,
  IF(n.legacy, n.debt_equity / 100, n.debt_equity) AS debt_equity,
  IF(n.legacy OR n.equity_ratio > 1 OR n.equity_ratio < 0, NULL, n.equity_ratio) AS equity_ratio,
  CASE WHEN n.legacy AND n.snapshot_date < DATE '2026-10-01' AND n.market = 'JP' AND n.dividend_yield > 0.3 THEN n.dividend_yield / 10000 WHEN n.legacy AND n.dividend_yield > 0.3 THEN n.dividend_yield / 100 ELSE n.dividend_yield END AS dividend_yield,
  n.market_cap_b AS market_cap_b,
  n.current_price AS current_price,
  n.rsi14 AS rsi14,
  n.macd_crossup AS macd_crossup,
  n.above_ma200 AS above_ma200,
  n.above_vwap AS above_vwap,
  n.week52_position_pct AS week52_position_pct,
  n.accrual_tier AS accrual_tier,
  n.size_tier AS size_tier,
  n.strategy_label AS strategy_label,
  n.analyst_reco AS analyst_reco,
  n.signal_text AS signal_text,
  p0.trade_date AS base_trade_date,
  p0.close AS base_close,
  p5.close / p0.close - 1 AS fwd_return_5d,
  p20.close / p0.close - 1 AS fwd_return_20d
FROM n
LEFT JOIN a ON a.ticker = n.ticker AND a.snapshot_date = n.snapshot_date
LEFT JOIN p AS p0 ON p0.ticker = n.ticker AND p0.rn = a.rn0
LEFT JOIN p AS p5 ON p5.ticker = n.ticker AND p5.rn = a.rn0 + 5
LEFT JOIN p AS p20 ON p20.ticker = n.ticker AND p20.rn = a.rn0 + 20
