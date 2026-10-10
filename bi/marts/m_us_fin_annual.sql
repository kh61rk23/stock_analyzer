-- m_us_fin_annual: 米国株の通期財務。1銘柄1通期1行（キー ticker, fiscal_year_end）。通貨は USD（列名の _usd）。
-- 元は bi.src_us_financials_annual（us.financials_annual の写し。SEC companyfacts の 10-K 通期を、会社ごとのタグの揺れを吸収して1列にしたもの）と
-- bi.src_us_company_profile（社名・取引所・SIC 業種）。どちらも location=US の表を bi_export が list_rows→load で写したもの（クエリ課金なし）。
-- 直近6年の期末だけ（前年比を出すため5年＋1年）。fiscal_year は期末日の年（例: 2025-09-27 期末の Apple は 2025）。
-- 設備投資・自社株買い・配当は会社によって符号が逆に入るので絶対値にそろえる。FCF＝営業CF−設備投資。
-- 前年比は、1つ前の通期の期末が 330〜400日前にあるときだけ（決算期変更の年は空）。
WITH a AS (
  SELECT ticker, cik, period_end, revenue, operating_income, net_income, eps_diluted, operating_cf,
         ABS(capex) AS capex, ABS(buyback) AS buyback, ABS(dividends_paid) AS dividends_paid,
         total_assets, total_liabilities, equity, cash, gross_profit, depreciation,
         IF(debt_short IS NOT NULL OR debt_long IS NOT NULL, COALESCE(debt_short, 0) + COALESCE(debt_long, 0), NULL) AS debt
  FROM `stock-analyzer-edinet.bi.src_us_financials_annual`
  WHERE period_end >= DATE_SUB(CURRENT_DATE('America/New_York'), INTERVAL 6 YEAR)
    AND (revenue IS NOT NULL OR operating_income IS NOT NULL OR net_income IS NOT NULL)
),
l AS (
  SELECT *,
    LAG(period_end) OVER w AS p_end,
    LAG(revenue) OVER w AS p_revenue,
    LAG(operating_income) OVER w AS p_op,
    LAG(net_income) OVER w AS p_ni,
    LAG(eps_diluted) OVER w AS p_eps
  FROM a
  WINDOW w AS (PARTITION BY ticker ORDER BY period_end)
),
p AS (
  SELECT ticker, name, exchange, sic, sic_description FROM `stock-analyzer-edinet.bi.src_us_company_profile`
)
SELECT
  l.ticker AS ticker,
  l.cik AS cik,
  p.name AS name,
  p.exchange AS exchange,
  p.sic AS sic,
  p.sic_description AS industry,
  l.period_end AS fiscal_year_end,
  EXTRACT(YEAR FROM l.period_end) AS fiscal_year,
  l.revenue AS revenue_usd,
  l.gross_profit AS gross_profit_usd,
  l.operating_income AS operating_income_usd,
  l.net_income AS net_income_usd,
  l.eps_diluted AS eps_usd,
  l.operating_cf AS operating_cf_usd,
  l.capex AS capex_usd,
  IF(l.operating_cf IS NOT NULL AND l.capex IS NOT NULL, l.operating_cf - l.capex, NULL) AS fcf_usd,
  l.depreciation AS depreciation_usd,
  l.buyback AS buyback_usd,
  l.dividends_paid AS dividends_paid_usd,
  l.total_assets AS total_assets_usd,
  l.total_liabilities AS total_liabilities_usd,
  l.equity AS equity_usd,
  l.cash AS cash_usd,
  l.debt AS debt_usd,
  l.gross_profit / NULLIF(l.revenue, 0) AS gross_margin,
  l.operating_income / NULLIF(l.revenue, 0) AS operating_margin,
  l.net_income / NULLIF(l.revenue, 0) AS net_margin,
  IF(l.operating_cf IS NOT NULL AND l.capex IS NOT NULL, (l.operating_cf - l.capex) / NULLIF(l.revenue, 0), NULL) AS fcf_margin,
  IF(l.equity > 0, l.net_income / l.equity, NULL) AS roe,
  l.equity / NULLIF(l.total_assets, 0) AS equity_ratio,
  IF(DATE_DIFF(l.period_end, l.p_end, DAY) BETWEEN 330 AND 400 AND l.p_revenue > 0, l.revenue / l.p_revenue - 1, NULL) AS sales_growth_1y,
  IF(DATE_DIFF(l.period_end, l.p_end, DAY) BETWEEN 330 AND 400 AND l.p_op > 0, l.operating_income / l.p_op - 1, NULL) AS op_income_growth_1y,
  IF(DATE_DIFF(l.period_end, l.p_end, DAY) BETWEEN 330 AND 400 AND l.p_ni > 0, l.net_income / l.p_ni - 1, NULL) AS net_income_growth_1y,
  IF(DATE_DIFF(l.period_end, l.p_end, DAY) BETWEEN 330 AND 400 AND l.p_eps > 0, l.eps_diluted / l.p_eps - 1, NULL) AS eps_growth_1y
FROM l
LEFT JOIN p ON p.ticker = l.ticker
WHERE l.period_end >= DATE_SUB(CURRENT_DATE('America/New_York'), INTERVAL 5 YEAR)
