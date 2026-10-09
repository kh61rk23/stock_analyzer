-- m_screener: 1社1行（キー edinet_code）。会社ごとに「売上がある最新年度」の財務と、株価系指標を横に並べたスクリーナー用の表。
-- 金額は円、比率は小数（0.12＝12%）。market_cap は円。market_cap_b は analytics.stock_snapshot の列名を引き継いだ値で
-- 単位は「億円（1e8円）」（列名の b は十億ではない。トヨタ 7203 で 344,775＝約34.5兆円）。
-- 株価系は、analytics.stock_snapshot の会社別最新日があればそれ（price_source='snapshot'）、無ければ analytics.price_daily の
-- 最新終値×（発行済株式数−自己株式数）で時価総額・PER・PBR・配当利回りを計算する（price_source='price_daily'）。
-- snapshot は market='JP' の行だけ使う（同じ ticker で market='US' の行があり、market_cap_b の単位と点数が別物のため）。
-- 有利子負債は financials の値を優先し、無ければ細目の合計（interest_bearing_debt_basis='reported'）、さらに無ければ
-- 借入金・社債・CPの合計（'estimated'。リース負債は含まない概算）。
-- RSI・52週位置・200日線は、スナップショットの日によって空の日があるため、会社ごとに「値のある最新日」（technical_date）から取る。
-- 株価系・テクニカルとも、最新日が全体の最新日から14日以内のものだけ使う（上場廃止・取込停止の古い株価を最新として出さないため）。
-- 騰落率（return_1m 等）は price_daily の履歴が約1か月しか無いため載せない。
-- 成長率は「ちょうど1年前／3年前の年度」が財務表にあるときだけ計算し、無ければ NULL（年が欠けた期を跨がない）。
-- 営業利益・純利益の成長率は前年値が正のときだけ計算する（赤字からの回復を％で表すと意味が逆転するため）。
WITH items_raw AS (
  SELECT edinet_code, fiscal_year,
    MAX(IF(item = 'gross_profit', value, NULL)) AS gross_profit,
    MAX(IF(item = 'rnd_expenses', value, NULL)) AS rnd_expenses,
    MAX(IF(item = 'eps', value, NULL)) AS eps,
    MAX(IF(item = 'bps', value, NULL)) AS bps,
    MAX(IF(item = 'dividend_per_share', value, NULL)) AS dividend_per_share,
    MAX(IF(item = 'payout_ratio', value, NULL)) AS payout_ratio,
    MAX(IF(item = 'num_employees', value, NULL)) AS num_employees,
    MAX(IF(item = 'shares_issued', value, NULL)) AS shares_issued,
    MAX(IF(item = 'treasury_end_period_holding_count', value, NULL)) AS treasury_shares,
    MAX(IF(item = 'ibd_current', value, NULL)) AS ibd_current,
    MAX(IF(item = 'ibd_noncurrent', value, NULL)) AS ibd_noncurrent,
    MAX(IF(item = 'ibd_total_unsplit_ifrs', value, NULL)) AS ibd_total,
    MAX(IF(item = 'short_term_loans', value, NULL)) AS short_term_loans,
    MAX(IF(item = 'long_term_loans', value, NULL)) AS long_term_loans,
    MAX(IF(item = 'current_portion_lt_loans', value, NULL)) AS current_portion_lt_loans,
    MAX(IF(item = 'bonds_payable', value, NULL)) AS bonds_payable,
    MAX(IF(item = 'current_portion_bonds_payable', value, NULL)) AS current_portion_bonds_payable,
    MAX(IF(item = 'short_term_bonds_payable', value, NULL)) AS short_term_bonds_payable,
    MAX(IF(item = 'commercial_papers', value, NULL)) AS commercial_papers
  FROM `stock-analyzer-edinet.edinet.financial_items`
  WHERE item IN ('gross_profit', 'rnd_expenses', 'eps', 'bps', 'dividend_per_share', 'payout_ratio',
                 'num_employees', 'shares_issued', 'treasury_end_period_holding_count', 'ibd_current', 'ibd_noncurrent',
                 'ibd_total_unsplit_ifrs', 'short_term_loans', 'long_term_loans', 'current_portion_lt_loans', 'bonds_payable',
                 'current_portion_bonds_payable', 'short_term_bonds_payable', 'commercial_papers')
  GROUP BY edinet_code, fiscal_year
),
items AS (
  SELECT *,
    IF(COALESCE(short_term_loans, long_term_loans, current_portion_lt_loans, bonds_payable, current_portion_bonds_payable, short_term_bonds_payable, commercial_papers) IS NULL, NULL,
       COALESCE(short_term_loans, 0) + COALESCE(long_term_loans, 0) + COALESCE(current_portion_lt_loans, 0) + COALESCE(bonds_payable, 0) + COALESCE(current_portion_bonds_payable, 0) + COALESCE(short_term_bonds_payable, 0) + COALESCE(commercial_papers, 0)) AS loan_debt
  FROM items_raw
),
fin AS (
  SELECT edinet_code, fiscal_year, fiscal_year_end, net_sales, operating_income, ordinary_income, net_income,
         total_assets, equity, interest_bearing_debt, cash_and_equiv, employee_count, avg_annual_salary, roe, roa, operating_margin
  FROM `stock-analyzer-edinet.edinet.financials`
),
latest AS (
  SELECT * FROM fin WHERE net_sales IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY edinet_code ORDER BY fiscal_year DESC) = 1
),
snap AS (
  SELECT * FROM (
    SELECT ticker, snapshot_date, ingested_at, current_price, market_cap_b, per, pbr, dividend_yield, composite_score, value_score, quality_score,
           MAX(snapshot_date) OVER () AS max_d
    FROM `stock-analyzer-edinet.analytics.stock_snapshot`
    WHERE market = 'JP'
  )
  WHERE snapshot_date >= DATE_SUB(max_d, INTERVAL 14 DAY)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY snapshot_date DESC, ingested_at DESC) = 1
),
tech AS (
  SELECT * FROM (
    SELECT ticker, snapshot_date, rsi14, week52_position_pct, above_ma200, MAX(snapshot_date) OVER () AS max_d
    FROM `stock-analyzer-edinet.analytics.stock_snapshot`
    WHERE market = 'JP' AND week52_position_pct IS NOT NULL
  )
  WHERE snapshot_date >= DATE_SUB(max_d, INTERVAL 14 DAY)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY snapshot_date DESC) = 1
),
px AS (
  SELECT * FROM (
    SELECT ticker, trade_date, close, MAX(trade_date) OVER () AS max_d, ingested_at
    FROM `stock-analyzer-edinet.analytics.price_daily`
    WHERE close IS NOT NULL
  )
  WHERE trade_date >= DATE_SUB(max_d, INTERVAL 14 DAY)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY trade_date DESC, ingested_at DESC) = 1
),
j AS (
  SELECT
    c.edinet_code AS edinet_code,
    SUBSTR(c.sec_code, 1, 4) AS sec_code4,
    CASE WHEN c.sec_code IS NOT NULL THEN CONCAT(SUBSTR(c.sec_code, 1, 4), '.T') END AS ticker,
    c.name AS name,
    c.industry AS industry,
    c.accounting_standard AS accounting_standard,
    l.fiscal_year AS fiscal_year,
    l.fiscal_year_end AS fiscal_year_end,
    l.net_sales AS net_sales,
    l.operating_income AS operating_income,
    l.ordinary_income AS ordinary_income,
    l.net_income AS net_income,
    l.total_assets AS total_assets,
    l.equity AS equity,
    COALESCE(i.num_employees, l.employee_count) AS employees,
    l.avg_annual_salary AS avg_annual_salary,
    l.operating_income / NULLIF(l.net_sales, 0) AS operating_margin,
    l.net_income / NULLIF(l.net_sales, 0) AS net_margin,
    i.gross_profit / NULLIF(l.net_sales, 0) AS gross_margin,
    l.roe AS roe,
    l.roa AS roa,
    i.rnd_expenses / NULLIF(l.net_sales, 0) AS rnd_ratio,
    l.equity / NULLIF(l.total_assets, 0) AS equity_ratio,
    ibd.v AS interest_bearing_debt,
    ibd.basis AS interest_bearing_debt_basis,
    l.cash_and_equiv AS cash_and_equiv,
    l.cash_and_equiv - ibd.v AS net_cash,
    ibd.v / NULLIF(l.equity, 0) AS de_ratio,
    IF(p1.net_sales > 0, l.net_sales / p1.net_sales - 1, NULL) AS sales_growth_1y,
    IF(p1.operating_income > 0, l.operating_income / p1.operating_income - 1, NULL) AS op_income_growth_1y,
    IF(p1.net_income > 0, l.net_income / p1.net_income - 1, NULL) AS net_income_growth_1y,
    IF(p3.net_sales > 0 AND l.net_sales > 0, POWER(l.net_sales / p3.net_sales, 1.0 / 3) - 1, NULL) AS sales_cagr_3y,
    i.eps AS eps,
    i.bps AS bps,
    i.dividend_per_share AS dividend_per_share,
    i.payout_ratio AS payout_ratio,
    i.shares_issued AS shares_issued,
    i.shares_issued - COALESCE(i.treasury_shares, 0) AS shares_out,
    CASE WHEN s.ticker IS NOT NULL THEN 'snapshot' WHEN x.ticker IS NOT NULL THEN 'price_daily' END AS price_source,
    COALESCE(s.current_price, x.close) AS current_price,
    s.snapshot_date AS snapshot_date,
    IF(s.ticker IS NOT NULL, s.snapshot_date, x.trade_date) AS price_date,
    s.market_cap_b AS snap_cap_b,
    s.per AS snap_per,
    s.pbr AS snap_pbr,
    s.dividend_yield AS snap_yield,
    x.close AS px_close,
    s.composite_score AS composite_score,
    s.value_score AS value_score,
    s.quality_score AS quality_score,
    t.rsi14 AS rsi14,
    t.week52_position_pct AS week52_position_pct,
    t.above_ma200 AS above_ma200,
    t.snapshot_date AS technical_date
  FROM latest AS l
  JOIN `stock-analyzer-edinet.edinet.company` AS c ON c.edinet_code = l.edinet_code
  LEFT JOIN items AS i ON i.edinet_code = l.edinet_code AND i.fiscal_year = l.fiscal_year
  CROSS JOIN UNNEST([STRUCT(COALESCE(l.interest_bearing_debt, i.ibd_current + i.ibd_noncurrent, i.ibd_total, i.loan_debt) AS v, CASE WHEN l.interest_bearing_debt IS NOT NULL OR i.ibd_current + i.ibd_noncurrent IS NOT NULL OR i.ibd_total IS NOT NULL THEN 'reported' WHEN i.loan_debt IS NOT NULL THEN 'estimated' END AS basis)]) AS ibd
  LEFT JOIN fin AS p1 ON p1.edinet_code = l.edinet_code AND p1.fiscal_year = l.fiscal_year - 1
  LEFT JOIN fin AS p3 ON p3.edinet_code = l.edinet_code AND p3.fiscal_year = l.fiscal_year - 3
  LEFT JOIN snap AS s ON s.ticker = CONCAT(SUBSTR(c.sec_code, 1, 4), '.T')
  LEFT JOIN tech AS t ON t.ticker = CONCAT(SUBSTR(c.sec_code, 1, 4), '.T')
  LEFT JOIN px AS x ON x.ticker = CONCAT(SUBSTR(c.sec_code, 1, 4), '.T')
  WHERE COALESCE(c.is_delisted, FALSE) = FALSE
)
SELECT
  edinet_code,
  sec_code4,
  ticker,
  name,
  industry,
  accounting_standard,
  fiscal_year,
  fiscal_year_end,
  net_sales,
  operating_income,
  ordinary_income,
  net_income,
  total_assets,
  equity,
  employees,
  avg_annual_salary,
  operating_margin,
  net_margin,
  gross_margin,
  roe,
  roa,
  rnd_ratio,
  equity_ratio,
  interest_bearing_debt,
  interest_bearing_debt_basis,
  cash_and_equiv,
  net_cash,
  de_ratio,
  sales_growth_1y,
  op_income_growth_1y,
  net_income_growth_1y,
  sales_cagr_3y,
  eps,
  bps,
  dividend_per_share,
  payout_ratio,
  shares_issued,
  price_source,
  current_price,
  price_date,
  snapshot_date,
  CASE price_source WHEN 'snapshot' THEN snap_cap_b WHEN 'price_daily' THEN px_close * shares_out / 1e8 END AS market_cap_b,
  CASE price_source WHEN 'snapshot' THEN snap_cap_b * 1e8 WHEN 'price_daily' THEN px_close * shares_out END AS market_cap,
  CASE price_source WHEN 'snapshot' THEN snap_per WHEN 'price_daily' THEN IF(eps > 0, px_close / eps, NULL) END AS per,
  CASE price_source WHEN 'snapshot' THEN snap_pbr WHEN 'price_daily' THEN IF(bps > 0, px_close / bps, NULL) END AS pbr,
  CASE price_source WHEN 'snapshot' THEN snap_yield WHEN 'price_daily' THEN IF(dividend_per_share IS NOT NULL, dividend_per_share / px_close, NULL) END AS dividend_yield,
  IF(net_cash IS NOT NULL AND price_source = 'snapshot' AND snap_cap_b > 0, net_cash / (snap_cap_b * 1e8), IF(net_cash IS NOT NULL AND price_source = 'price_daily' AND px_close * shares_out > 0, net_cash / (px_close * shares_out), NULL)) AS net_cash_to_mcap,
  composite_score,
  value_score,
  quality_score,
  rsi14,
  week52_position_pct,
  above_ma200,
  technical_date
FROM j
