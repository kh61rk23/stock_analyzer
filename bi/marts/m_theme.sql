-- m_theme: テーマ(subtheme)×銘柄1行（キー theme_name, ticker）。edinet.ticker_subtheme_mapping の実在銘柄だけ。
-- 元表は同じ ticker×subtheme が取り込みのたびに重複して入る（実測 4 時点）ため、updated_at が最新の1行に絞る。
-- source='json_sync' の行は ticker が 'SECTOR_<業種コード>' のプレースホルダ（業種単位のテーマ定義）で銘柄ではないので除く
-- （テーマの定義は m_theme_def）。実在銘柄は source='config_representative'＝業種ごとの代表銘柄（2〜3社）に、
-- その業種のテーマ全部を付けたもの。会社ごとに選んだテーマではない（同じ業種の代表銘柄は同じテーマ集合を持つ）。
-- ticker は日本株が '6501.T'。証券コード4桁で edinet.company と結合する（同じ sec_code4 が複数社なら上場中を優先して1社）。
-- 財務は会社ごとに「売上が入っている最新の fiscal_year」1年分と、その前年（fiscal_year - 1 が実在するときだけ）の売上。
-- sales_growth は前年比（小数。0.1＝+10%）。前年が無い会社は空。
WITH mp AS (
  SELECT
    ticker,
    market,
    subtheme AS theme_name,
    sector_code,
    relevance,
    source,
    ROW_NUMBER() OVER (PARTITION BY ticker, subtheme, sector_code ORDER BY updated_at DESC) AS rn
  FROM `stock-analyzer-edinet.edinet.ticker_subtheme_mapping`
  WHERE NOT STARTS_WITH(ticker, 'SECTOR_')
),
co AS (
  SELECT
    edinet_code,
    SUBSTR(sec_code, 1, 4) AS sec_code4,
    name,
    industry,
    ROW_NUMBER() OVER (PARTITION BY SUBSTR(sec_code, 1, 4) ORDER BY IF(is_delisted, 1, 0), edinet_code) AS rn
  FROM `stock-analyzer-edinet.edinet.company`
  WHERE sec_code IS NOT NULL
),
fin AS (
  SELECT
    edinet_code,
    fiscal_year,
    net_sales,
    operating_income,
    net_income,
    ROW_NUMBER() OVER (PARTITION BY edinet_code ORDER BY fiscal_year DESC) AS rn
  FROM `stock-analyzer-edinet.edinet.financials`
  WHERE net_sales IS NOT NULL
),
sm AS (
  SELECT sector_code, market, name_ja, supply_chain_position
  FROM `stock-analyzer-edinet.edinet.sub_theme_master`
)
SELECT
  mp.theme_name,
  mp.market,
  mp.sector_code,
  sm.name_ja AS sector_name_ja,
  sm.supply_chain_position,
  mp.ticker,
  REPLACE(mp.ticker, '.T', '') AS sec_code4,
  co.edinet_code,
  co.name,
  co.industry,
  mp.relevance,
  mp.source,
  fin.fiscal_year,
  fin.net_sales,
  fin.operating_income,
  fin.net_income,
  fin.operating_income / NULLIF(fin.net_sales, 0) AS operating_margin,
  fin.net_sales / NULLIF(prev.net_sales, 0) - 1 AS sales_growth
FROM mp
LEFT JOIN sm ON sm.sector_code = mp.sector_code AND sm.market = mp.market
LEFT JOIN co ON co.sec_code4 = REPLACE(mp.ticker, '.T', '') AND co.rn = 1 AND mp.market = 'JP'
LEFT JOIN fin ON fin.edinet_code = co.edinet_code AND fin.rn = 1
LEFT JOIN `stock-analyzer-edinet.edinet.financials` AS prev ON prev.edinet_code = fin.edinet_code AND prev.fiscal_year = fin.fiscal_year - 1
WHERE mp.rn = 1
