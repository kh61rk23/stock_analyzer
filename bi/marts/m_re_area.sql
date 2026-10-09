-- m_re_area: 1市区町村1行（キー municipality_code＝5桁）。real_estate.area_profile_p1 の実測（2026-10）で
-- latest_land_price・land_price_yoy・total_transactions は全行 NULL のため持たない（入るようになったら足す）。
-- 単位：地価は円/m2、land_price_yoy_change は％（5.2＝+5.2%）、flood_risk_area_pct は割合（0.3＝30%）。
-- 取り込みの再実行で同じ市区町村が重複しても updated_at が新しい1行に絞る。
WITH a AS (
  SELECT
    municipality_code,
    prefecture_name,
    municipality_name,
    median_price,
    avg_land_price_commercial,
    avg_land_price_residential,
    land_price_yoy_change,
    flood_risk_area_pct,
    latest_avg_price_condo,
    hospital_count,
    ROW_NUMBER() OVER (PARTITION BY municipality_code ORDER BY updated_at DESC) AS rn
  FROM `stock-analyzer-edinet.real_estate.area_profile_p1`
)
SELECT
  a.municipality_code,
  a.prefecture_name,
  a.municipality_name,
  a.median_price,
  a.avg_land_price_commercial,
  a.avg_land_price_residential,
  a.land_price_yoy_change,
  a.flood_risk_area_pct,
  a.latest_avg_price_condo,
  a.hospital_count
FROM a
WHERE a.rn = 1
