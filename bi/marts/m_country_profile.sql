-- m_country_profile: 1か国1行（キー country_code＝ISO3）。geopolitics.country_profile は 59 か国。
-- JSON 列は BI の軸にしにくいので文字列化して持つ（軸・集計には使わず、参照用）。
SELECT
  p.country_code,
  p.country_name_ja,
  p.region,
  p.democracy_index,
  p.corruption_index,
  p.rule_of_law,
  p.gdp_usd_bn,
  p.export_concentration,
  p.is_landlocked,
  p.sanction_flag,
  p.trade_dependency_jp,
  IF(p.top_export_commodities IS NULL, NULL, TO_JSON_STRING(p.top_export_commodities)) AS top_export_commodities,
  IF(p.sealane_adjacency IS NULL, NULL, TO_JSON_STRING(p.sealane_adjacency)) AS sealane_adjacency,
  IF(p.military_alliances IS NULL, NULL, TO_JSON_STRING(p.military_alliances)) AS military_alliances
FROM `stock-analyzer-edinet.geopolitics.country_profile` AS p
