-- m_arxiv_papers: arXiv の論文。1論文1行（キー paper_id）。元は analytics.arxiv_papers（arxiv_monitor が週1回追記・arxiv_store.py）。
-- 同じ論文が別の回にも入ったときは fetched_at が新しい行（過去の md レポートから取り込んだ行は題名が55字で切れ・要旨なしのことがあるため、
-- 取得時の行が後から入れば置き換わる）。keywords は採点に当たったキーワードを「|」区切り。
-- investment_score は arxiv_monitor の点数（キーワード×機関×引用×カテゴリ×再現性×新しさ）。is_idea は ARXIV_INVESTMENT_THRESHOLD 以上。
WITH r AS (
  SELECT * FROM `stock-analyzer-edinet.analytics.arxiv_papers`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY paper_id ORDER BY fetched_at DESC, (summary IS NOT NULL) DESC) = 1
)
SELECT
  paper_id,
  fetched_date,
  DATE_TRUNC(fetched_date, MONTH) AS fetched_month,
  published_date,
  primary_category,
  categories,
  title,
  title_ja,
  summary,
  authors,
  url,
  investment_score,
  is_idea,
  keywords,
  source
FROM r
