-- m_gov_stats: 日本の政府統計（CPI・鉱工業生産・賃金・小売・失業率・住宅着工・機械受注・輸出）。1取得日×統計1行（キー report_date, stat_name）。
-- 元は analytics.gov_stats（report_store.py が gov/gov_report_{date}.md の「取得統計サマリー」表から追記）。
-- value は表の最新値（unit に単位と対象期間）、period は統計の対象月（YYYY-MM）、mom_pct・yoy_pct は前月比・前年比（%）。signal＝tailwind／headwind。
-- 統計は月1回の公表なので、取得日が変わっても同じ値が続く。
WITH r AS (
  SELECT * FROM `stock-analyzer-edinet.analytics.gov_stats`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY report_date, stat_name ORDER BY fetched_at DESC) = 1
)
SELECT
  report_date,
  stat_name,
  value,
  unit,
  period,
  mom_pct,
  yoy_pct,
  signal
FROM r
