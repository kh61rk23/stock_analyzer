-- m_etf_daily: テーマETFの個別 ETF の日次。1日×テーマ×ETF1行（キー report_date, theme_name, ticker）。
-- 元は analytics.etf_daily（2026-10-11 からは全テーマ、それ以前は md に出ていた上位テーマの ETF だけ）。
-- ret_1m・ret_3m・ma50_ratio（50日線乖離）・vol_ratio（出来高の前月比）はいずれも %。price は USD。
WITH r AS (
  SELECT * FROM `stock-analyzer-edinet.analytics.etf_daily`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY report_date, theme_name, ticker ORDER BY fetched_at DESC) = 1
)
SELECT
  report_date,
  theme_name,
  ticker,
  price,
  ret_1m,
  ret_3m,
  ma50_ratio,
  vol_ratio
FROM r
