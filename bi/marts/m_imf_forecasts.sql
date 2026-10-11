-- m_imf_forecasts: IMF の見通し（World Economic Outlook）。1取得日×指標×国×年1行（キー report_date, indicator_code, country, year）。
-- 元は analytics.imf_forecasts（report_store.py が imf/imf_{date}.md の表から追記）。value は %（GDP 成長率・物価上昇率・失業率・経常収支/GDP・政府債務/GDP）。
-- revision は同じ指標・国・年の1つ前の取得日からの変化（IMF が見通しを改定した日だけ 0 以外になる）。
WITH r AS (
  SELECT * FROM `stock-analyzer-edinet.analytics.imf_forecasts`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY report_date, indicator_code, country, year ORDER BY fetched_at DESC) = 1
)
SELECT
  report_date,
  indicator_code,
  indicator_name,
  country,
  year,
  value,
  value - LAG(value) OVER (PARTITION BY indicator_code, country, year ORDER BY report_date) AS revision
FROM r
