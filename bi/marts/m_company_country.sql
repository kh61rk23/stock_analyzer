-- m_company_country: 1社(ticker)×1国につき1行（キー ticker, country_code）。
-- company_country_exposure には同じ ticker×国×種別が重複して入ることがある（取り込みの再実行）ため、
-- exposure_strength / revenue_share_est は合算せず MAX を採る（足すと依存度が膨らむ）。
-- 種別が複数ある国は最も強い行の exposure_type を代表にし、種類数は exposure_type_count に持つ。
-- 期限切れ（valid_to が過去）の権益は除く。
-- ticker は日本株が '1605.T'、米国株が 'AAPL'。日本株だけ証券コード4桁で edinet.company と結合する
-- （edinet_code・社名・業種は米国株では空）。同じ sec_code4 が複数社に付く場合は上場中を優先して1社に絞る。
-- 国の最新リスクは country_risk_daily の最新日の値。
WITH e AS (
  SELECT
    ticker,
    market,
    country_code,
    exposure_type,
    exposure_strength,
    revenue_share_est,
    review_flag,
    ROW_NUMBER() OVER (PARTITION BY ticker, country_code ORDER BY exposure_strength DESC, exposure_type) AS rn,
    MAX(exposure_strength) OVER (PARTITION BY ticker, country_code) AS max_strength,
    MAX(revenue_share_est) OVER (PARTITION BY ticker, country_code) AS max_revenue_share,
    COUNT(DISTINCT exposure_type) OVER (PARTITION BY ticker, country_code) AS type_count,
    MAX(IF(review_flag, 1, 0)) OVER (PARTITION BY ticker, country_code) AS any_review
  FROM `stock-analyzer-edinet.geopolitics.company_country_exposure`
  WHERE valid_to IS NULL OR valid_to >= CURRENT_DATE()
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
lr AS (
  SELECT
    country_code,
    composite_risk_score,
    gdelt_instability,
    foreign_ministry_level,
    state_dept_level,
    risk_date,
    ROW_NUMBER() OVER (PARTITION BY country_code ORDER BY risk_date DESC, updated_at DESC) AS rn
  FROM `stock-analyzer-edinet.geopolitics.country_risk_daily`
)
SELECT
  e.ticker,
  e.market,
  IF(REGEXP_CONTAINS(e.ticker, r'^[0-9A-Z]{4}\.T$'), SUBSTR(e.ticker, 1, 4), NULL) AS sec_code4,
  co.edinet_code,
  co.name,
  co.industry,
  e.country_code,
  p.country_name_ja,
  p.region,
  p.sanction_flag,
  e.exposure_type,
  e.type_count AS exposure_type_count,
  e.max_strength AS exposure_strength,
  e.max_revenue_share AS revenue_share_est,
  e.any_review = 1 AS review_flag,
  lr.composite_risk_score AS latest_risk_score,
  lr.gdelt_instability AS latest_gdelt_instability,
  lr.foreign_ministry_level AS latest_foreign_ministry_level,
  lr.state_dept_level AS latest_state_dept_level,
  lr.risk_date AS latest_risk_date,
  e.max_strength * lr.composite_risk_score AS risk_weighted_score
FROM e
LEFT JOIN co
  ON co.sec_code4 = IF(REGEXP_CONTAINS(e.ticker, r'^[0-9A-Z]{4}\.T$'), SUBSTR(e.ticker, 1, 4), NULL) AND co.rn = 1
LEFT JOIN `stock-analyzer-edinet.geopolitics.country_profile` AS p
  ON p.country_code = e.country_code
LEFT JOIN lr
  ON lr.country_code = e.country_code AND lr.rn = 1
WHERE e.rn = 1
