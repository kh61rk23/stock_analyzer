-- m_news: ニュース記事。1記事1行（キー url）。直近60日・要約は先頭300字。
-- 元は analytics.news_articles（report_store.py が data/news_cache/news_{date}.json の全記事を毎日追記。2026-09-11 から）。
-- 同じ記事（URL）は数日続けて取得されるので、最初に取得した日を first_seen、最後を last_seen として1行にまとめ、中身は最新の取得を採る。
-- board の parquet は毎日コミットされるので期間と字数を絞る。全期間・全文は analytics.news_articles を直接引く。
-- matched_sectors は業種コード（例 1016）や macro をカンマ区切り。sentiment_score は 0〜1（0.5 が中立）。
WITH r AS (
  SELECT *,
    MIN(report_date) OVER (PARTITION BY url) AS first_seen,
    MAX(report_date) OVER (PARTITION BY url) AS last_seen
  FROM `stock-analyzer-edinet.analytics.news_articles`
  WHERE report_date >= DATE_SUB(CURRENT_DATE('Asia/Tokyo'), INTERVAL 60 DAY)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY url ORDER BY report_date DESC, fetched_at DESC) = 1
)
SELECT
  url,
  first_seen,
  last_seen,
  title,
  SUBSTR(summary, 1, 300) AS summary_head,
  source,
  priority,
  lang,
  pub_date,
  matched_sectors,
  importance_score,
  investment_relevant,
  sentiment_label,
  sentiment_score
FROM r
