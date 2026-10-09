-- m_screener: 1社1行（キー edinet_code）。会社ごとに「売上がある最新年度」の財務と、株価系指標を横に並べたスクリーナー用の表（日本株・円）。
-- 金額は円、比率は小数（0.12＝12%）。market_cap は円。market_cap_b は「億円（1e8円）」（列名の b は十億ではない。トヨタ 7203 で約34.5兆円＝344,775）。
-- 株価系（current_price・price_date・market_cap・per・pbr・dividend_yield・net_cash_to_mcap）・騰落率・52週高安・平均売買代金は、
-- analytics.price_daily_latest（ticker×営業日で重複除去したビュー）の直近約400日から全上場について作る（price_source は株価があれば 'price_daily'、無ければ NULL）。
-- 終値は分割調整済み・配当未調整。EPS・BPS・1株配当・株数は決算期末の値で、期末後に株式分割があると終値と単位がずれる。
-- 細目の adjusted_eps / adjusted_bps / adjusted_dividend_per_share（分割調整後）があればそれを使い、
-- 調整前EPS÷調整後EPS を分割倍率として株数にも掛ける（東京海上 8766 は期末後の分割で EPS が15倍に見えていた）。
-- stock_snapshot はスコア（composite_score 等）とテクニカル（rsi14・above_ma200）にだけ使う。株価・時価総額・PER 等には使わない。
-- 騰落率は営業日ベース（return_1m＝21営業日前・return_3m＝63営業日前・return_1y＝252営業日前の終値との比。足りなければ 360日前以前で最も新しい終値。それも無ければ NULL）。
-- 52週高値・安値は終値ベースで直近252営業日、履歴が120営業日未満なら NULL。
-- 最新日が全体の最新日から14日以内の株価だけ使う（上場廃止・取込停止の古い株価を最新として出さないため）。
-- 成長率は「ちょうど1年前／3年前の年度」が財務表にあるときだけ計算し、無ければ NULL（年が欠けた期を跨がない）。
-- 営業利益・純利益の成長率は前年値が正のときだけ計算する（赤字からの回復を％で表すと意味が逆転するため）。
-- 有利子負債は financials の値を優先し、無ければ細目の合計（interest_bearing_debt_basis='reported'）、さらに無ければ
-- 借入金・社債・CPの合計（'estimated'。リース負債は含まない概算）。
-- PER・PBR・配当利回りが意味を持たない会社は valuation_flag で理由を示し、per・pbr・dividend_yield を NULL にする（規則は SPEC_bi_marts §11）。
WITH items_raw AS (
  SELECT edinet_code, fiscal_year,
    MAX(IF(item = 'gross_profit', value, NULL)) AS gross_profit,
    MAX(IF(item = 'rnd_expenses', value, NULL)) AS rnd_expenses,
    MAX(IF(item = 'eps', value, NULL)) AS eps,
    MAX(IF(item = 'adjusted_eps', value, NULL)) AS adjusted_eps,
    MAX(IF(item = 'bps', value, NULL)) AS bps,
    MAX(IF(item = 'adjusted_bps', value, NULL)) AS adjusted_bps,
    MAX(IF(item = 'dividend_per_share', value, NULL)) AS dividend_per_share,
    MAX(IF(item = 'adjusted_dividend_per_share', value, NULL)) AS adjusted_dividend_per_share,
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
  WHERE item IN ('gross_profit', 'rnd_expenses', 'eps', 'adjusted_eps', 'bps', 'adjusted_bps', 'dividend_per_share',
                 'adjusted_dividend_per_share', 'payout_ratio',
                 'num_employees', 'shares_issued', 'treasury_end_period_holding_count', 'ibd_current', 'ibd_noncurrent',
                 'ibd_total_unsplit_ifrs', 'short_term_loans', 'long_term_loans', 'current_portion_lt_loans', 'bonds_payable',
                 'current_portion_bonds_payable', 'short_term_bonds_payable', 'commercial_papers')
  GROUP BY edinet_code, fiscal_year
),
items AS (
  SELECT *,
    IF(COALESCE(short_term_loans, long_term_loans, current_portion_lt_loans, bonds_payable, current_portion_bonds_payable, short_term_bonds_payable, commercial_papers) IS NULL, NULL,
       COALESCE(short_term_loans, 0) + COALESCE(long_term_loans, 0) + COALESCE(current_portion_lt_loans, 0) + COALESCE(bonds_payable, 0) + COALESCE(current_portion_bonds_payable, 0) + COALESCE(short_term_bonds_payable, 0) + COALESCE(commercial_papers, 0)) AS loan_debt,
    -- 期末後の株式分割・併合の倍率（調整前EPS÷調整後EPS。BPSで代用。差が1%以内なら分割なし）
    COALESCE(
      IF(eps IS NOT NULL AND adjusted_eps IS NOT NULL AND eps != 0 AND adjusted_eps != 0 AND eps / adjusted_eps > 0 AND ABS(eps / adjusted_eps - 1) > 0.01, eps / adjusted_eps, NULL),
      IF(bps IS NOT NULL AND adjusted_bps IS NOT NULL AND bps != 0 AND adjusted_bps != 0 AND bps / adjusted_bps > 0 AND ABS(bps / adjusted_bps - 1) > 0.01, bps / adjusted_bps, NULL),
      1.0) AS split_factor
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
    SELECT ticker, snapshot_date, ingested_at, composite_score, value_score, quality_score,
           MAX(snapshot_date) OVER () AS max_d
    FROM `stock-analyzer-edinet.analytics.stock_snapshot`
    WHERE market = 'JP'
  )
  WHERE snapshot_date >= DATE_SUB(max_d, INTERVAL 14 DAY)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY snapshot_date DESC, ingested_at DESC) = 1
),
tech AS (
  SELECT * FROM (
    SELECT ticker, snapshot_date, rsi14, above_ma200, MAX(snapshot_date) OVER () AS max_d
    FROM `stock-analyzer-edinet.analytics.stock_snapshot`
    WHERE market = 'JP' AND rsi14 IS NOT NULL
  )
  WHERE snapshot_date >= DATE_SUB(max_d, INTERVAL 14 DAY)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY snapshot_date DESC) = 1
),
px_raw AS (
  SELECT ticker, trade_date, close, volume,
         LAG(close) OVER (PARTITION BY ticker ORDER BY trade_date) AS prev_close
  FROM `stock-analyzer-edinet.analytics.price_daily_latest`
  WHERE trade_date >= DATE_SUB(CURRENT_DATE('Asia/Tokyo'), INTERVAL 400 DAY)
    AND ticker LIKE '%.T' AND close > 0 AND close < 10000000
),
px_brk AS (
  SELECT *,
         MAX(IF(prev_close > 0 AND (close / prev_close < 0.6 OR close / prev_close > 1.7), trade_date, NULL)) OVER (PARTITION BY ticker) AS break_date
  FROM px_raw
),
px_rows AS (
  SELECT ticker, trade_date, close, volume, break_date,
         ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY trade_date DESC) AS rn,
         MAX(trade_date) OVER (PARTITION BY ticker) AS tm,
         MAX(trade_date) OVER () AS max_d
  FROM px_brk
  WHERE break_date IS NULL OR trade_date >= break_date
),
px AS (
  SELECT ticker,
    MAX(IF(rn = 1, trade_date, NULL)) AS price_date,
    MAX(IF(rn = 1, close, NULL)) AS close,
    MAX(IF(rn = 22, close, NULL)) AS c_1m,
    MAX(IF(rn = 64, close, NULL)) AS c_3m,
    COALESCE(MAX(IF(rn = 253, close, NULL)),
             ARRAY_AGG(IF(trade_date <= DATE_SUB(tm, INTERVAL 360 DAY), close, NULL) IGNORE NULLS ORDER BY trade_date DESC LIMIT 1)[SAFE_OFFSET(0)]) AS c_1y,
    COUNT(*) AS price_days,
    MAX(break_date) AS break_date,
    MAX(IF(rn <= 252, close, NULL)) AS hi_52w,
    MIN(IF(rn <= 252, close, NULL)) AS lo_52w,
    IF(COUNTIF(rn <= 20) >= 15, AVG(IF(rn <= 20, close * volume, NULL)), NULL) AS turnover_20d
  FROM px_rows
  WHERE tm >= DATE_SUB(max_d, INTERVAL 14 DAY)
  GROUP BY ticker
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
    COALESCE(i.adjusted_eps, i.eps) AS eps,
    COALESCE(i.adjusted_bps, i.bps) AS bps,
    COALESCE(i.adjusted_dividend_per_share, i.dividend_per_share) AS dividend_per_share,
    i.payout_ratio AS payout_ratio,
    i.shares_issued AS shares_issued,
    (i.shares_issued - COALESCE(i.treasury_shares, 0)) * i.split_factor AS shares_out,
    IF(x.ticker IS NOT NULL, 'price_daily', NULL) AS price_source,
    x.close AS current_price,
    x.price_date AS price_date,
    s.snapshot_date AS snapshot_date,
    x.close * (i.shares_issued - COALESCE(i.treasury_shares, 0)) * i.split_factor AS mcap,
    x.close / x.c_1m - 1 AS return_1m,
    x.close / x.c_3m - 1 AS return_3m,
    x.close / x.c_1y - 1 AS return_1y,
    IF(x.price_days >= 120, x.hi_52w, NULL) AS high_52w,
    IF(x.price_days >= 120, x.lo_52w, NULL) AS low_52w,
    IF(x.price_days >= 120 AND x.hi_52w > x.lo_52w, (x.close - x.lo_52w) / (x.hi_52w - x.lo_52w) * 100, NULL) AS week52_position_pct,
    x.turnover_20d AS avg_turnover_20d,
    x.price_days AS price_days,
    x.break_date AS break_date,
    s.composite_score AS composite_score,
    s.value_score AS value_score,
    s.quality_score AS quality_score,
    t.rsi14 AS rsi14,
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
),
v AS (
  SELECT *,
    IF(current_price IS NOT NULL AND eps > 0, current_price / eps, NULL) AS per_raw,
    IF(current_price IS NOT NULL AND bps > 0, current_price / bps, NULL) AS pbr_raw,
    IF(current_price IS NOT NULL AND dividend_per_share IS NOT NULL, dividend_per_share / current_price, NULL) AS yield_raw
  FROM j
),
f AS (
  SELECT *,
    CASE
      -- EPS×株数が純利益と合わない（株数・EPSの単位不整合）
      WHEN eps > 0 AND net_income > 0 AND shares_out > 0 AND (eps * shares_out / net_income < 0.5 OR eps * shares_out / net_income > 2) THEN 'eps_mismatch'
      -- 純利益が純資産の半分を超え PER が3倍未満＝負ののれん発生益など一過性の利益で EPS が膨らんでいる
      WHEN per_raw < 3 AND equity > 0 AND net_income / equity > 0.5 THEN 'one_off_profit'
      -- 配当利回り15%超は特別配当・単位違いの疑い
      WHEN yield_raw > 0.15 THEN 'yield_outlier'
    END AS valuation_flag
  FROM v
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
  shares_out,
  price_source,
  current_price,
  price_date,
  snapshot_date,
  mcap / 1e8 AS market_cap_b,
  mcap AS market_cap,
  IF(valuation_flag IN ('eps_mismatch', 'one_off_profit'), NULL, per_raw) AS per,
  pbr_raw AS pbr,
  IF(valuation_flag = 'yield_outlier', NULL, yield_raw) AS dividend_yield,
  valuation_flag,
  IF(net_cash IS NOT NULL AND mcap > 0, net_cash / mcap, NULL) AS net_cash_to_mcap,
  return_1m,
  return_3m,
  return_1y,
  high_52w,
  low_52w,
  week52_position_pct,
  avg_turnover_20d,
  price_days,
  break_date AS price_break_date,
  composite_score,
  value_score,
  quality_score,
  rsi14,
  above_ma200,
  technical_date
FROM f
