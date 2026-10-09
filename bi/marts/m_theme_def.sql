-- m_theme_def: テーマの定義1行（キー theme_name, sector_code, market）。edinet.sub_theme_master は業種1行に
-- テーマ名の JSON 配列（subthemes）を持つ（日本33業種＋米国11業種＝44行）ので、配列を1テーマ1行に開く。
-- company_count は m_theme に実在銘柄として紐づく社数（実在銘柄の無い米国のテーマは 0）。
-- theme_order は業種内でのテーマの並び（0始まり）。
WITH th AS (
  SELECT
    m.sector_code,
    m.market,
    m.name_ja AS sector_name_ja,
    m.supply_chain_position,
    theme_name,
    pos AS theme_order
  FROM `stock-analyzer-edinet.edinet.sub_theme_master` AS m, UNNEST(JSON_VALUE_ARRAY(m.subthemes)) AS theme_name WITH OFFSET AS pos
),
cnt AS (
  SELECT subtheme AS theme_name, sector_code, market, COUNT(DISTINCT ticker) AS company_count
  FROM `stock-analyzer-edinet.edinet.ticker_subtheme_mapping`
  WHERE NOT STARTS_WITH(ticker, 'SECTOR_')
  GROUP BY subtheme, sector_code, market
)
SELECT
  th.theme_name,
  th.market,
  th.sector_code,
  th.sector_name_ja,
  th.supply_chain_position,
  th.theme_order,
  COALESCE(cnt.company_count, 0) AS company_count
FROM th
LEFT JOIN cnt ON cnt.theme_name = th.theme_name AND cnt.sector_code = th.sector_code AND cnt.market = th.market
