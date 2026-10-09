-- m_re_holdings_pref: 1社×1都道府県×1年度につき1行（キー edinet_code, fiscal_year, prefecture）。
-- m_re_holdings をさらに都道府県で畳んだ集約マート（都道府県別・会社別の見通しを軽くするため）。
-- 単位・時価の目安の定義（簿価20倍超の除外を含む）は m_re_holdings と同じ（百万円・m2・円/m2。概算）。
-- population_change_pct は施設数で重み付けせず、市区町村値の単純平均（地域の傾向を見る目安）。
WITH h AS (
  SELECT
    edinet_code,
    fiscal_year,
    prefecture,
    municipality_code,
    book_value_land_m_yen,
    land_area_m2,
    COALESCE(industrial_land_price_per_m2, commercial_land_price_per_m2, residential_land_price_per_m2) AS ref_price,
    population_change_pct,
    flood_risk_area_pct
  FROM `stock-analyzer-edinet.real_estate.corporate_holdings_p1`
),
h1 AS (
  SELECT
    h.*,
    IF(h.land_area_m2 > 0 AND h.ref_price IS NOT NULL, h.land_area_m2 * h.ref_price / 1000000, NULL) AS est_raw
  FROM h
),
h2 AS (
  SELECT
    h1.*,
    IF(COALESCE(h1.book_value_land_m_yen, 0) > 0 AND h1.est_raw > 20 * h1.book_value_land_m_yen, NULL, h1.est_raw) AS est_m_yen,
    IF(COALESCE(h1.book_value_land_m_yen, 0) > 0 AND h1.est_raw > 20 * h1.book_value_land_m_yen, 1, 0) AS excl
  FROM h1
),
agg AS (
  SELECT
    edinet_code,
    fiscal_year,
    prefecture,
    COUNT(*) AS facility_count,
    COUNT(DISTINCT municipality_code) AS municipality_count,
    SUM(land_area_m2) AS land_area_m2,
    SUM(book_value_land_m_yen) AS book_value_land_m_yen,
    SUM(est_m_yen) AS land_value_est_m_yen,
    SUM(excl) AS est_excluded_count,
    SUM(IF(book_value_land_m_yen > 0 AND est_m_yen IS NOT NULL, est_m_yen - book_value_land_m_yen, NULL)) AS unrealized_gain_est_m_yen,
    AVG(population_change_pct) AS population_change_pct,
    AVG(flood_risk_area_pct) AS flood_risk_area_pct
  FROM h2
  WHERE prefecture IS NOT NULL
  GROUP BY edinet_code, fiscal_year, prefecture
)
SELECT
  g.edinet_code,
  c.name,
  SUBSTR(c.sec_code, 1, 4) AS sec_code4,
  c.industry,
  g.fiscal_year,
  g.prefecture,
  g.facility_count,
  g.municipality_count,
  g.land_area_m2,
  g.book_value_land_m_yen,
  g.land_value_est_m_yen,
  g.est_excluded_count,
  g.unrealized_gain_est_m_yen,
  g.population_change_pct,
  g.flood_risk_area_pct
FROM agg AS g
LEFT JOIN `stock-analyzer-edinet.edinet.company` AS c
  ON c.edinet_code = g.edinet_code
