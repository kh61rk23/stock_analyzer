-- m_re_holdings: 1社×1市区町村×1年度につき1行（キー edinet_code, fiscal_year, municipality_code）。
-- 元の corporate_holdings_p1 は施設単位で 14.9 万行あり、parquet が大きくなるため市区町村単位に集約する
-- （施設名は持たない。同じ市区町村の複数施設は合算し facility_count に数を入れる）。
-- 単位：簿価は百万円、面積は m2、地価は円/m2、population_change_pct は％（2050年までの増減）、
--       flood_risk_area_pct・landslide_risk_area_pct は割合（0.3＝30%）。
-- 時価の目安：土地面積 × 参照地価（工業地→商業地→住宅地の順で最初にある値）。
--   land_value_est_m_yen は面積と参照地価がある施設の合計、
--   unrealized_gain_est_m_yen は上に加えて土地簿価がある施設だけの（時価目安 − 簿価）の合計。
--   本社欄に会社全体の面積が入っている施設（出光興産の本社欄 569万m2 を千代田区の地価で評価すると 22兆円になる等）が実在するため、
--   土地簿価がある施設で時価の目安が簿価の20倍を超えるものは紐づけ違いの疑いとして時価・含み益から除き、
--   その施設数を est_excluded_count に持つ（簿価が無い施設は検証できないので除かない）。あくまで概算で、投資判断の根拠にしない。
-- 市区町村名と地価の上昇率は area_profile_p1 から付ける。
WITH h AS (
  SELECT
    edinet_code,
    fiscal_year,
    prefecture,
    municipality_code,
    book_value_land_m_yen,
    book_value_buildings_m_yen,
    land_area_m2,
    COALESCE(industrial_land_price_per_m2, commercial_land_price_per_m2, residential_land_price_per_m2) AS ref_price,
    population_current,
    population_2050,
    population_change_pct,
    flood_risk_area_pct,
    landslide_risk_area_pct
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
    municipality_code,
    MAX(prefecture) AS prefecture,
    COUNT(*) AS facility_count,
    SUM(land_area_m2) AS land_area_m2,
    SUM(book_value_land_m_yen) AS book_value_land_m_yen,
    SUM(book_value_buildings_m_yen) AS book_value_buildings_m_yen,
    SUM(est_m_yen) AS land_value_est_m_yen,
    SUM(excl) AS est_excluded_count,
    SUM(IF(book_value_land_m_yen > 0 AND est_m_yen IS NOT NULL, est_m_yen - book_value_land_m_yen, NULL)) AS unrealized_gain_est_m_yen,
    AVG(ref_price) AS land_price_ref_per_m2,
    MAX(population_current) AS population_current,
    MAX(population_2050) AS population_2050,
    AVG(population_change_pct) AS population_change_pct,
    AVG(flood_risk_area_pct) AS flood_risk_area_pct,
    AVG(landslide_risk_area_pct) AS landslide_risk_area_pct
  FROM h2
  GROUP BY edinet_code, fiscal_year, municipality_code
)
SELECT
  g.edinet_code,
  c.name,
  SUBSTR(c.sec_code, 1, 4) AS sec_code4,
  c.industry,
  g.fiscal_year,
  g.prefecture,
  g.municipality_code,
  a.municipality_name,
  g.facility_count,
  g.land_area_m2,
  g.book_value_land_m_yen,
  g.book_value_buildings_m_yen,
  g.land_price_ref_per_m2,
  g.land_value_est_m_yen,
  g.est_excluded_count,
  g.unrealized_gain_est_m_yen,
  g.population_current,
  g.population_2050,
  g.population_change_pct,
  g.flood_risk_area_pct,
  g.landslide_risk_area_pct,
  a.land_price_yoy_change AS area_land_price_yoy_change
FROM agg AS g
LEFT JOIN `stock-analyzer-edinet.edinet.company` AS c
  ON c.edinet_code = g.edinet_code
LEFT JOIN (
  SELECT municipality_code, municipality_name, land_price_yoy_change,
         ROW_NUMBER() OVER (PARTITION BY municipality_code ORDER BY updated_at DESC) AS rn
  FROM `stock-analyzer-edinet.real_estate.area_profile_p1`
) AS a
  ON a.municipality_code = g.municipality_code AND a.rn = 1
