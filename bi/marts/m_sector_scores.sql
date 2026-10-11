-- m_sector_scores: 業種スコアの日次推移。1日×市場×業種1行（キー report_date, market, sector_code）。
-- 元は analytics.sector_scores_daily（report_store.py が毎日の md/csv から追記。日本＝sectors/{code}_{date}.md の見出し、米国＝us_sector_scores_{date}.csv）。
-- 同じ日の作り直しは fetched_at が新しい行。score_chg_1d・score_chg_5d はその業種の1つ前・5つ前の記録日との差（記録日ベース）。
-- 日本の ret_* は空（md に無い）。米国の ret_* は ETF の騰落率（%）。
WITH r AS (
  SELECT * FROM `stock-analyzer-edinet.analytics.sector_scores_daily`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY report_date, market, sector_code ORDER BY fetched_at DESC) = 1
)
SELECT
  report_date,
  market,
  sector_code,
  sector_name,
  score,
  risk_level,
  ret_1m,
  ret_3m,
  ret_12m,
  signals,
  score - LAG(score) OVER (PARTITION BY market, sector_code ORDER BY report_date) AS score_chg_1d,
  score - LAG(score, 5) OVER (PARTITION BY market, sector_code ORDER BY report_date) AS score_chg_5d,
  RANK() OVER (PARTITION BY report_date, market ORDER BY score DESC) AS score_rank
FROM r
