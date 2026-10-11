-- m_macro_indicators: マクロ指標の日次。1日×出どころ×指標1行（キー report_date, source, indicator）。
-- 元は analytics.macro_indicators_daily。source＝macro_report（ドル円・米10年債・WTI・金・VIX・S&P500・日経平均）／
-- bond_commodity（米国債の年限別利回り％と1か月差）。2026-10-11 より前の macro_report は有効3桁に丸めた値（日経 6.91e+04 など）。
WITH r AS (
  SELECT * FROM `stock-analyzer-edinet.analytics.macro_indicators_daily`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY report_date, source, indicator ORDER BY fetched_at DESC) = 1
)
SELECT
  report_date,
  source,
  indicator,
  value,
  change_1m,
  value - LAG(value) OVER (PARTITION BY source, indicator ORDER BY report_date) AS change_1d
FROM r
