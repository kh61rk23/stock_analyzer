-- m_company_tax: 会社(company_id)×技術分類ノード1行（キー company_id, node_id）。
-- company_taxonomy_map は週次の map_id（…:日付）で同じ company_id×node_id が重複して入るため、updated_at が最新の1行に絞る。
-- company_id は上場会社が証券コード4桁（edinet.company の sec_code 先頭4桁）、業種単位の行は 'sector:JP:<業種コード>'
-- （entity_type='sector'・社名等は空）。company_id='nan' の行（欠損が文字列化したもの）は意味を持たないので除く。
-- 同じ sec_code4 が複数社に付く場合は上場中を優先して1社に絞る（m_company_country と同じ）。
-- 財務は会社ごとに「売上が入っている最新の fiscal_year」1年分。研究開発費は financial_items の rnd_expenses。
-- revenue_share_est は会社単位の推定値で、同じ会社の全ノードに同じ値が入る（足し合わせない）。valid_to が過去の行は除く。
WITH mp AS (
  SELECT
    company_id,
    node_id,
    relevance_score,
    confidence,
    revenue_share_est,
    confidence_type,
    auto_derived,
    verified_flag,
    ROW_NUMBER() OVER (PARTITION BY company_id, node_id ORDER BY updated_at DESC, map_id DESC) AS rn
  FROM `stock-analyzer-edinet.taxonomy.company_taxonomy_map`
  WHERE company_id <> 'nan' AND (valid_to IS NULL OR valid_to >= CURRENT_DATE())
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
    ROW_NUMBER() OVER (PARTITION BY edinet_code ORDER BY fiscal_year DESC) AS rn
  FROM `stock-analyzer-edinet.edinet.financials`
  WHERE net_sales IS NOT NULL
),
rnd AS (
  SELECT edinet_code, fiscal_year, MAX(value) AS rnd_expenses
  FROM `stock-analyzer-edinet.edinet.financial_items`
  WHERE item = 'rnd_expenses'
  GROUP BY edinet_code, fiscal_year
),
nd AS (
  SELECT
    a0.node_id,
    a0.node_name_ja,
    a0.node_level,
    a0.parent_id,
    CASE a0.node_level WHEN 1 THEN a0.node_id WHEN 2 THEN a1.node_id WHEN 3 THEN a2.node_id ELSE a3.node_id END AS domain_id,
    CASE a0.node_level WHEN 1 THEN a0.node_name_ja WHEN 2 THEN a1.node_name_ja WHEN 3 THEN a2.node_name_ja ELSE a3.node_name_ja END AS domain_name_ja,
    CASE a0.node_level WHEN 1 THEN a0.node_name_ja WHEN 2 THEN CONCAT(a1.node_name_ja, ' > ', a0.node_name_ja) WHEN 3 THEN CONCAT(a2.node_name_ja, ' > ', a1.node_name_ja, ' > ', a0.node_name_ja) ELSE CONCAT(a3.node_name_ja, ' > ', a2.node_name_ja, ' > ', a1.node_name_ja, ' > ', a0.node_name_ja) END AS path_name
  FROM `stock-analyzer-edinet.taxonomy.taxonomy_node` AS a0
  LEFT JOIN `stock-analyzer-edinet.taxonomy.taxonomy_node` AS a1 ON a1.node_id = a0.parent_id
  LEFT JOIN `stock-analyzer-edinet.taxonomy.taxonomy_node` AS a2 ON a2.node_id = a1.parent_id
  LEFT JOIN `stock-analyzer-edinet.taxonomy.taxonomy_node` AS a3 ON a3.node_id = a2.parent_id
  WHERE a0.is_active IS NOT FALSE AND a0.effective_to IS NULL
)
SELECT
  mp.company_id,
  IF(STARTS_WITH(mp.company_id, 'sector:'), 'sector', 'company') AS entity_type,
  co.edinet_code,
  co.name,
  co.industry,
  mp.node_id,
  nd.node_name_ja,
  nd.node_level,
  nd.parent_id,
  nd.domain_id,
  nd.domain_name_ja,
  nd.path_name,
  mp.relevance_score,
  mp.confidence,
  mp.revenue_share_est,
  mp.confidence_type,
  mp.auto_derived,
  mp.verified_flag,
  fin.fiscal_year,
  fin.net_sales,
  fin.operating_income,
  fin.operating_income / NULLIF(fin.net_sales, 0) AS operating_margin,
  rnd.rnd_expenses,
  rnd.rnd_expenses / NULLIF(fin.net_sales, 0) AS rnd_ratio
FROM mp
LEFT JOIN co ON co.sec_code4 = mp.company_id AND co.rn = 1
LEFT JOIN nd ON nd.node_id = mp.node_id
LEFT JOIN fin ON fin.edinet_code = co.edinet_code AND fin.rn = 1
LEFT JOIN rnd ON rnd.edinet_code = fin.edinet_code AND rnd.fiscal_year = fin.fiscal_year
WHERE mp.rn = 1
