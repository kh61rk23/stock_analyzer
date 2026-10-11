-- m_arxiv_keywords: arXiv の論文×採点キーワード。1論文1キーワード1行（キー paper_id, keyword）。
-- m_arxiv_papers と同じ重複の畳み方をしたうえで keywords（「|」区切り）を縦に並べる。キーワードの流行（月ごとの件数）を見るための表。
WITH r AS (
  SELECT * FROM `stock-analyzer-edinet.analytics.arxiv_papers`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY paper_id ORDER BY fetched_at DESC, (summary IS NOT NULL) DESC) = 1
)
SELECT
  r.paper_id AS paper_id,
  r.fetched_date AS fetched_date,
  DATE_TRUNC(r.fetched_date, MONTH) AS fetched_month,
  r.primary_category AS primary_category,
  TRIM(k) AS keyword,
  r.investment_score AS investment_score,
  r.is_idea AS is_idea
FROM r, UNNEST(SPLIT(r.keywords, '|')) AS k
WHERE r.keywords IS NOT NULL AND TRIM(k) != ''
