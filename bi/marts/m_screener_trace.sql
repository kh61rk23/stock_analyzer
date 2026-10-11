-- m_screener_trace: スクリーナーの全候補の日次（日本株・米国株、通過も落選も）。1日×市場×銘柄1行（キー report_date, market, ticker）。
-- 元は analytics.screener_trace_daily（report_store.py。2026-10-11 からは data/screener_trace/trace_{date}.json＝経路の全段と9指標の生の値、
-- それ以前は screener_trace_{date}.md と screener_rejections.json）。記録は 2026-09-30 から。
-- 通過: composite_score（総合点）・base_score（加点前）・score_path（基本点→各モジュールの加減点→上限）・kpi_*（9指標・表示専用で点数には使わない）。
-- 落選: reasons（日本株＝PER・PBR・ROE・時価総額・出来高、米国株＝ビジネスモデル別の PER・PBR・ROE・営業利益率・D/E・FCF・売上成長・アナリスト推奨、
--   取れなかった銘柄は「データ取得失敗」）。米国株は 2026-10-13 から（trace_us_{date}.json）。sector_code は米国ではビジネスモデル。通過銘柄の詳細な財務・テクニカルは m_stock_snapshot。
WITH r AS (
  SELECT * FROM `stock-analyzer-edinet.analytics.screener_trace_daily`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY report_date, market, ticker ORDER BY fetched_at DESC) = 1
)
SELECT
  report_date,
  market,
  ticker,
  name,
  sector_code,
  sector_name,
  passed,
  composite_score,
  base_score,
  composite_score - base_score AS bonus,
  score_path,
  reasons,
  kpi_pbr,
  kpi_per,
  kpi_fcf_yield,
  kpi_ev_ebitda,
  kpi_roic,
  kpi_net_cash_ratio,
  kpi_irr
FROM r
