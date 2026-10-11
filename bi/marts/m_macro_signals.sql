-- m_macro_signals: マクロシグナル×業種の判定。1日×シグナル×業種1行（キー report_date, signal, sector_name）。
-- 元は analytics.macro_signals_daily（macro_report の「アクティブなマクロシグナル」節）。judgement＝tailwind（追い風）／headwind（逆風）／watch（注意）。
WITH r AS (
  SELECT * FROM `stock-analyzer-edinet.analytics.macro_signals_daily`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY report_date, signal, sector_name ORDER BY fetched_at DESC) = 1
)
SELECT
  report_date,
  signal,
  trigger_value,
  sector_name,
  judgement,
  CASE judgement WHEN 'tailwind' THEN '追い風' WHEN 'headwind' THEN '逆風' WHEN 'watch' THEN '注意' END AS judgement_ja,
  reason
FROM r
