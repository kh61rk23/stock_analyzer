-- m_tax_node: 技術分類ノード1行（キー node_id）。taxonomy.taxonomy_node は node_id が一意（293ノード・孤児親なし）。
-- 階層は node_level 1（領域）〜4。親をたどって最上位（領域）と「領域 > … > 自ノード」のパス名を持つ。
-- company_count は直接このノードに紐づく会社数（上場企業のみ・sector: 始まりの業種単位の行と company_id='nan' は除く）、
-- company_count_rollup は自ノード以下（子孫）に紐づく会社の重複なしの数。
-- 親子の鎖は最大4段なので、親を3回自己結合して祖先を引く（再帰クエリは使わない）。
WITH n AS (
  SELECT node_id, parent_id, node_name_ja, node_name_en, node_level, node_type, supply_chain_pos
  FROM `stock-analyzer-edinet.taxonomy.taxonomy_node`
  WHERE is_active IS NOT FALSE AND effective_to IS NULL
),
mp AS (
  SELECT company_id, node_id
  FROM `stock-analyzer-edinet.taxonomy.company_taxonomy_map`
  WHERE company_id <> 'nan' AND NOT STARTS_WITH(company_id, 'sector:') AND (valid_to IS NULL OR valid_to >= CURRENT_DATE())
  GROUP BY company_id, node_id
),
up AS (
  SELECT mp.company_id, mp.node_id AS leaf_id, mp.node_id AS anc_id FROM mp
  UNION ALL
  SELECT mp.company_id, mp.node_id, n1.parent_id FROM mp JOIN n AS n1 ON n1.node_id = mp.node_id WHERE n1.parent_id IS NOT NULL
  UNION ALL
  SELECT mp.company_id, mp.node_id, n2.parent_id FROM mp JOIN n AS n1 ON n1.node_id = mp.node_id JOIN n AS n2 ON n2.node_id = n1.parent_id WHERE n2.parent_id IS NOT NULL
  UNION ALL
  SELECT mp.company_id, mp.node_id, n3.parent_id FROM mp JOIN n AS n1 ON n1.node_id = mp.node_id JOIN n AS n2 ON n2.node_id = n1.parent_id JOIN n AS n3 ON n3.node_id = n2.parent_id WHERE n3.parent_id IS NOT NULL
),
cnt AS (
  SELECT anc_id AS node_id, COUNT(DISTINCT company_id) AS company_count_rollup
  FROM up
  GROUP BY anc_id
),
direct AS (
  SELECT node_id, COUNT(DISTINCT company_id) AS company_count
  FROM mp
  GROUP BY node_id
),
kids AS (
  SELECT parent_id AS node_id, COUNT(*) AS child_count
  FROM n
  WHERE parent_id IS NOT NULL
  GROUP BY parent_id
)
SELECT
  a0.node_id,
  a0.parent_id,
  a0.node_name_ja,
  a0.node_name_en,
  a0.node_level,
  a0.node_type,
  a0.supply_chain_pos,
  CASE a0.node_level WHEN 1 THEN a0.node_id WHEN 2 THEN a1.node_id WHEN 3 THEN a2.node_id ELSE a3.node_id END AS domain_id,
  CASE a0.node_level WHEN 1 THEN a0.node_name_ja WHEN 2 THEN a1.node_name_ja WHEN 3 THEN a2.node_name_ja ELSE a3.node_name_ja END AS domain_name_ja,
  CASE a0.node_level WHEN 1 THEN a0.node_name_ja WHEN 2 THEN CONCAT(a1.node_name_ja, ' > ', a0.node_name_ja) WHEN 3 THEN CONCAT(a2.node_name_ja, ' > ', a1.node_name_ja, ' > ', a0.node_name_ja) ELSE CONCAT(a3.node_name_ja, ' > ', a2.node_name_ja, ' > ', a1.node_name_ja, ' > ', a0.node_name_ja) END AS path_name,
  COALESCE(k.child_count, 0) AS child_count,
  COALESCE(d.company_count, 0) AS company_count,
  COALESCE(c.company_count_rollup, 0) AS company_count_rollup
FROM n AS a0
LEFT JOIN n AS a1 ON a1.node_id = a0.parent_id
LEFT JOIN n AS a2 ON a2.node_id = a1.parent_id
LEFT JOIN n AS a3 ON a3.node_id = a2.parent_id
LEFT JOIN kids AS k ON k.node_id = a0.node_id
LEFT JOIN direct AS d ON d.node_id = a0.node_id
LEFT JOIN cnt AS c ON c.node_id = a0.node_id
