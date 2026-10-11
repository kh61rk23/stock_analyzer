-- m_etf_themes: テーマETFの熱量の日次。1日×テーマ1行（キー report_date, theme_name）。
-- 元は analytics.etf_theme_daily（2026-10-11 からは data/etf_cache の JSON、それ以前は md のランキング表。md 由来の日は arxiv_trend_score・件数が空）。
-- heat_score＝ETF モメンタム 60%＋arXiv トレンド 40%。signal＝hot／warming／neutral／cooling。heat_chg_5d は5つ前の記録日との差。
WITH r AS (
  SELECT * FROM `stock-analyzer-edinet.analytics.etf_theme_daily`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY report_date, theme_name ORDER BY fetched_at DESC) = 1
)
SELECT
  report_date,
  theme_name,
  etfs,
  heat_score,
  etf_momentum_score,
  arxiv_trend_score,
  arxiv_growth_rate,
  arxiv_recent_4w,
  arxiv_prev_4w,
  signal,
  heat_score - LAG(heat_score, 5) OVER (PARTITION BY theme_name ORDER BY report_date) AS heat_chg_5d,
  RANK() OVER (PARTITION BY report_date ORDER BY heat_score DESC) AS heat_rank
FROM r
