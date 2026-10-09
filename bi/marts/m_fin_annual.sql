-- m_fin_annual: 1社1年度1行（キー edinet_code, fiscal_year。fiscal_year は決算期末の年）。
-- 基準表は edinet.financials。実測（2026-10）で financial_items の (edinet_code, fiscal_year) 36,654 組は
-- すべて financials（36,656 組）に含まれ、両表とも同キーの重複は0。items だけにある年度は無いため、
-- financials を基準に LEFT JOIN で細目を横持ちする。items 側が将来 financials に無い年度を持った場合は、
-- 主要約列（売上・利益など）が空のままその年度が落ちる点に注意（落とさない方が分析上の欠けに気づける）。
-- 細目は1表1スキャンで済むよう、対象項目だけに絞って先に横持ちする。
WITH items AS (
  SELECT
    edinet_code,
    fiscal_year,
    MAX(IF(item = 'gross_profit', value, NULL)) AS gross_profit,
    MAX(IF(item = 'sga', value, NULL)) AS sga,
    MAX(IF(item = 'rnd_expenses', value, NULL)) AS rnd_expenses,
    MAX(IF(item = 'cost_of_sales', value, NULL)) AS cost_of_sales,
    MAX(IF(item = 'goodwill', value, NULL)) AS goodwill,
    MAX(IF(item = 'inventories', value, NULL)) AS inventories,
    MAX(IF(item = 'trade_receivables', value, NULL)) AS trade_receivables,
    MAX(IF(item = 'total_liabilities', value, NULL)) AS total_liabilities,
    MAX(IF(item = 'net_assets', value, NULL)) AS net_assets,
    MAX(IF(item = 'shareholders_equity', value, NULL)) AS shareholders_equity,
    MAX(IF(item = 'eps', value, NULL)) AS eps,
    MAX(IF(item = 'bps', value, NULL)) AS bps,
    MAX(IF(item = 'dividend_per_share', value, NULL)) AS dividend_per_share,
    MAX(IF(item = 'payout_ratio', value, NULL)) AS payout_ratio,
    MAX(IF(item = 'per', value, NULL)) AS per,
    MAX(IF(item = 'pbr', value, NULL)) AS pbr,
    MAX(IF(item = 'num_employees', value, NULL)) AS num_employees,
    MAX(IF(item = 'female_manager_ratio', value, NULL)) AS female_manager_ratio,
    MAX(IF(item = 'avg_tenure_years', value, NULL)) AS avg_tenure_years,
    MAX(IF(item = 'cash_dividends_paid', value, NULL)) AS cash_dividends_paid
  FROM `stock-analyzer-edinet.edinet.financial_items`
  WHERE item IN (
    'gross_profit', 'sga', 'rnd_expenses', 'cost_of_sales', 'goodwill', 'inventories',
    'trade_receivables', 'total_liabilities', 'net_assets', 'shareholders_equity', 'eps', 'bps',
    'dividend_per_share', 'payout_ratio', 'per', 'pbr', 'num_employees', 'female_manager_ratio',
    'avg_tenure_years', 'cash_dividends_paid')
  GROUP BY edinet_code, fiscal_year
),
base AS (
  SELECT
    f.edinet_code,
    f.fiscal_year,
    f.fiscal_year_end,
    f.net_sales,
    f.operating_income,
    f.ordinary_income,
    f.net_income,
    f.total_assets,
    f.equity,
    f.interest_bearing_debt,
    f.cash_and_equiv,
    f.operating_cf,
    f.investing_cf,
    f.free_cf,
    f.employee_count,
    f.avg_annual_salary,
    f.capex,
    f.depreciation,
    f.roe,
    f.roa,
    f.operating_margin
  FROM `stock-analyzer-edinet.edinet.financials` AS f
)
SELECT
  b.edinet_code,
  b.fiscal_year,
  b.fiscal_year_end,
  m.name,
  SUBSTR(m.sec_code, 1, 4) AS sec_code4,
  m.industry,
  b.net_sales,
  b.operating_income,
  b.ordinary_income,
  b.net_income,
  b.total_assets,
  b.equity,
  b.interest_bearing_debt,
  b.cash_and_equiv,
  b.operating_cf,
  b.investing_cf,
  b.free_cf,
  b.employee_count,
  b.avg_annual_salary,
  b.capex,
  b.depreciation,
  b.roe,
  b.roa,
  b.operating_margin,
  i.gross_profit,
  i.sga,
  i.rnd_expenses,
  i.cost_of_sales,
  i.goodwill,
  i.inventories,
  i.trade_receivables,
  i.total_liabilities,
  i.net_assets,
  i.shareholders_equity,
  i.eps,
  i.bps,
  i.dividend_per_share,
  i.payout_ratio,
  i.per,
  i.pbr,
  i.num_employees,
  i.female_manager_ratio,
  i.avg_tenure_years,
  i.cash_dividends_paid,
  i.gross_profit / NULLIF(b.net_sales, 0) AS gross_margin,
  i.sga / NULLIF(b.net_sales, 0) AS sga_ratio,
  i.rnd_expenses / NULLIF(b.net_sales, 0) AS rnd_ratio,
  b.equity / NULLIF(b.total_assets, 0) AS equity_ratio,
  b.net_income / NULLIF(b.net_sales, 0) AS net_margin
FROM base AS b
LEFT JOIN items AS i
  ON i.edinet_code = b.edinet_code AND i.fiscal_year = b.fiscal_year
LEFT JOIN `stock-analyzer-edinet.edinet.company` AS m
  ON m.edinet_code = b.edinet_code
