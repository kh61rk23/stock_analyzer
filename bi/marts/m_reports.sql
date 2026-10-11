-- m_reports: マクロ・業種の md レポート。1日×種類×キー1行（キー report_date, report_type, report_key）。直近31日分・本文は先頭3,000字。
-- 元は analytics.report_texts（全期間・全文）。board の parquet は毎日コミットされるので、肥大しないよう期間と字数を絞る。
-- 全文・全期間は BigQuery の analytics.report_texts を直接引く（分析室の「Claudeに聞く」や SQL 欄から）。
-- report_type: sector（日本の業種・report_key＝業種コード）／macro_report／bond_commodity／daily_movers／anomaly／us_sector_report／monthly_trend／policy_changes
WITH r AS (
  SELECT * FROM `stock-analyzer-edinet.analytics.report_texts`
  WHERE report_date >= DATE_SUB(CURRENT_DATE('Asia/Tokyo'), INTERVAL 31 DAY)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY report_date, report_type, report_key ORDER BY fetched_at DESC) = 1
)
SELECT
  report_date,
  report_type,
  report_key,
  title,
  SUBSTR(body_md, 1, 3000) AS body_head,
  chars,
  source_path
FROM r
