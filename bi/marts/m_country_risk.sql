-- m_country_risk: 1国1日1行（キー country_code, risk_date）。country_risk_daily は国×日で一意のはずだが、
-- 取り込みの再実行で重複しても1行に落とすよう updated_at が新しい方を採る。
-- composite_risk_score は大きいほど高リスク。foreign_ministry_level / state_dept_level は外務省・米国務省の渡航レベル（1〜4）。
WITH r AS (
  SELECT
    country_code,
    risk_date,
    gdelt_instability,
    foreign_ministry_level,
    state_dept_level,
    composite_risk_score,
    ROW_NUMBER() OVER (PARTITION BY country_code, risk_date ORDER BY updated_at DESC) AS rn
  FROM `stock-analyzer-edinet.geopolitics.country_risk_daily`
)
SELECT
  r.country_code,
  r.risk_date,
  p.country_name_ja,
  p.region,
  p.sanction_flag,
  r.gdelt_instability,
  r.foreign_ministry_level,
  r.state_dept_level,
  r.composite_risk_score
FROM r
LEFT JOIN `stock-analyzer-edinet.geopolitics.country_profile` AS p
  ON p.country_code = r.country_code
WHERE r.rn = 1
