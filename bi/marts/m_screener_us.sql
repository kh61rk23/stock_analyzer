-- m_screener_us: 1銘柄1行（キー ticker）。米国株のスクリーナー用の表。通貨はすべて USD（列名の _usd は米ドル。円ではない）。
-- 日本版 m_screener と同じ考え方の列体系。財務は SEC の無料 API（companyfacts・submissions）から取った
-- us.company_profile（社名・業種・発行済株式数・最新の貸借・直近4四半期合計）と us.financials_annual（通期の損益・貸借・CF）を使う。
-- どちらも location=US のため asia の bi.src_us_company_profile / bi.src_us_financials_annual に写したもの（bi_export.py が list_rows→load で更新。クエリ課金なし）を読む。
-- 通期（revenue_usd など）: 10-K の通期のうち最新（決算期末が2年より新しいもの）。1年前は 330〜400日前の通期。会社ごとの XBRL タグの揺れは取得側で年度ごとに吸収済み。
-- 直近4四半期合計（ttm_*）: 通期 + 当期累計 - 前年同期累計。10-Q が通期より後に無ければ通期と同じ。
-- 貸借（total_assets_usd・equity_usd など）: 10-K / 10-Q の最新の期末（balance_date）。有利子負債は短期＋長期。
-- 時価総額: 終値 × 発行済株式数（SEC の表紙値 dei:EntityCommonStockSharesOutstanding。無い会社は貸借日の株数、それも無ければ加重平均株数。出どころは shares_source）。
-- PER: 終値 ÷ 直近4四半期の希薄化EPS（無ければ通期）。PBR: 時価総額 ÷ 最新の株主資本。ROE: 直近4四半期の純利益 ÷ 最新の株主資本。株主資本が0以下なら PBR・ROE は空。
-- 配当利回り: 通期の配当支払総額 ÷ 時価総額。FCF＝営業CF－設備投資（直近4四半期が揃えばそれ、無ければ通期）。
-- 株価系・騰落率・52週高安・平均売買代金は analytics.price_daily_latest の直近約400日から全銘柄（約5,800）について作る。
--   終値は分割調整済み・配当未調整。日々の前日比が 0.6倍未満／1.7倍超の日（分割の調整漏れ・異常値）があれば、その日以降の終値だけを使う
--   （price_break_date）。株数の基準日より後にその日があるときは時価総額が食い違うため、時価総額・PBR・PS・利回りを空にする。
--   EPS の期末より後にその日があるときは PER を空にする。どちらかに該当すれば valuation_flag='split_suspect'。
-- 騰落率は営業日ベース（return_1m＝21営業日前・return_3m＝63営業日前・return_1y＝252営業日前、足りなければ360日前以前で最も新しい終値、それも無ければ NULL）。
-- 52週高値・安値は終値ベースで直近252営業日、履歴が120営業日未満なら NULL。最新日が全体の最新日から14日以内の株価だけ使う。
-- スコア・RSI は analytics.stock_snapshot の market='US' の会社別最新行（14日以内）から（スナップショットは約400銘柄のみ）。
WITH ann AS (
  SELECT ticker, cik, period_end, revenue, operating_income, net_income, eps_diluted, buyback, dividends_paid,
         IF(operating_cf IS NOT NULL AND capex IS NOT NULL, operating_cf - ABS(capex), NULL) AS fcf
  FROM `stock-analyzer-edinet.bi.src_us_financials_annual`
  WHERE revenue IS NOT NULL OR operating_income IS NOT NULL OR net_income IS NOT NULL
),
fy_cur AS (
  SELECT * FROM ann
  WHERE period_end >= DATE_SUB(CURRENT_DATE('America/New_York'), INTERVAL 2 YEAR)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY period_end DESC) = 1
),
fund AS (
  SELECT c.ticker, c.cik, c.period_end AS fiscal_year_end, c.revenue, c.operating_income, c.net_income, c.eps_diluted AS eps,
         c.fcf, c.buyback, c.dividends_paid,
         IF(p.revenue > 0 AND c.revenue > 0, c.revenue / p.revenue - 1, NULL) AS sales_growth_1y,
         IF(p.operating_income > 0, c.operating_income / p.operating_income - 1, NULL) AS op_income_growth_1y,
         IF(p.net_income > 0, c.net_income / p.net_income - 1, NULL) AS net_income_growth_1y,
         IF(p.eps_diluted > 0, c.eps_diluted / p.eps_diluted - 1, NULL) AS eps_growth_1y
  FROM fy_cur AS c
  LEFT JOIN ann AS p ON p.ticker = c.ticker AND DATE_DIFF(c.period_end, p.period_end, DAY) BETWEEN 330 AND 400
),
prof AS (
  SELECT ticker, cik, name, exchange, sic, sic_description, shares_outstanding, shares_as_of, shares_source,
         IF(bs_period_end >= DATE_SUB(CURRENT_DATE('America/New_York'), INTERVAL 2 YEAR), bs_period_end, NULL) AS balance_date,
         bs_total_assets, bs_total_liabilities, bs_equity, bs_cash, bs_debt_short, bs_debt_long,
         IF(ttm_period_end >= DATE_SUB(CURRENT_DATE('America/New_York'), INTERVAL 2 YEAR), ttm_period_end, NULL) AS ttm_end,
         ttm_revenue, ttm_operating_income, ttm_net_income, ttm_eps_diluted, ttm_operating_cf, ttm_capex
  FROM `stock-analyzer-edinet.bi.src_us_company_profile`
),
snap AS (
  SELECT * FROM (
    SELECT ticker, snapshot_date, ingested_at, name, composite_score, value_score, quality_score,
           MAX(snapshot_date) OVER () AS max_d
    FROM `stock-analyzer-edinet.analytics.stock_snapshot`
    WHERE market = 'US'
  )
  WHERE snapshot_date >= DATE_SUB(max_d, INTERVAL 14 DAY)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY snapshot_date DESC, ingested_at DESC) = 1
),
tech AS (
  SELECT * FROM (
    SELECT ticker, snapshot_date, rsi14, above_ma200, MAX(snapshot_date) OVER () AS max_d
    FROM `stock-analyzer-edinet.analytics.stock_snapshot`
    WHERE market = 'US' AND rsi14 IS NOT NULL
  )
  WHERE snapshot_date >= DATE_SUB(max_d, INTERVAL 14 DAY)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY snapshot_date DESC) = 1
),
px_raw AS (
  SELECT ticker, trade_date, close, volume,
         LAG(close) OVER (PARTITION BY ticker ORDER BY trade_date) AS prev_close
  FROM `stock-analyzer-edinet.analytics.price_daily_latest`
  WHERE trade_date >= DATE_SUB(CURRENT_DATE('America/New_York'), INTERVAL 400 DAY)
    AND ticker NOT LIKE '%.T' AND close > 0 AND close < 10000000
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
tickers AS (
  SELECT ticker FROM px
  UNION DISTINCT
  SELECT ticker FROM fund
  UNION DISTINCT
  SELECT ticker FROM prof
),
j AS (
  SELECT
    t.ticker AS ticker,
    COALESCE(p.name, s.name) AS name,
    p.exchange AS exchange,
    p.sic AS sic,
    p.sic_description AS industry,
    COALESCE(f.cik, p.cik) AS cik,
    (f.ticker IS NOT NULL OR p.balance_date IS NOT NULL OR p.ttm_end IS NOT NULL) AS has_fundamentals,
    f.fiscal_year_end AS fiscal_year_end,
    f.revenue AS revenue_usd,
    f.operating_income AS operating_income_usd,
    f.net_income AS net_income_usd,
    f.fcf AS fcf_usd,
    f.buyback AS buyback_usd,
    f.dividends_paid AS dividends_paid_usd,
    f.eps AS eps_usd,
    f.operating_income / NULLIF(f.revenue, 0) AS operating_margin,
    f.net_income / NULLIF(f.revenue, 0) AS net_margin,
    f.fcf / NULLIF(f.revenue, 0) AS fcf_margin,
    f.sales_growth_1y AS sales_growth_1y,
    f.op_income_growth_1y AS op_income_growth_1y,
    f.net_income_growth_1y AS net_income_growth_1y,
    f.eps_growth_1y AS eps_growth_1y,
    p.ttm_end AS ttm_period_end,
    IF(p.ttm_end IS NOT NULL, p.ttm_revenue, NULL) AS ttm_revenue_usd,
    IF(p.ttm_end IS NOT NULL, p.ttm_operating_income, NULL) AS ttm_operating_income_usd,
    IF(p.ttm_end IS NOT NULL, p.ttm_net_income, NULL) AS ttm_net_income_usd,
    IF(p.ttm_end IS NOT NULL, p.ttm_eps_diluted, NULL) AS ttm_eps_usd,
    p.balance_date AS balance_date,
    IF(p.balance_date IS NOT NULL, p.bs_total_assets, NULL) AS total_assets_usd,
    IF(p.balance_date IS NOT NULL, p.bs_total_liabilities, NULL) AS total_liabilities_usd,
    IF(p.balance_date IS NOT NULL, p.bs_equity, NULL) AS equity_usd,
    IF(p.balance_date IS NOT NULL, p.bs_cash, NULL) AS cash_usd,
    IF(p.balance_date IS NOT NULL AND (p.bs_debt_short IS NOT NULL OR p.bs_debt_long IS NOT NULL),
       COALESCE(p.bs_debt_short, 0) + COALESCE(p.bs_debt_long, 0), NULL) AS interest_bearing_debt_usd,
    IF(p.shares_outstanding > 0, p.shares_outstanding, NULL) AS shares_outstanding,
    IF(p.shares_outstanding > 0, p.shares_as_of, NULL) AS shares_as_of,
    IF(p.shares_outstanding > 0, p.shares_source, NULL) AS shares_source,
    IF(x.ticker IS NOT NULL, 'price_daily', NULL) AS price_source,
    x.close AS current_price_usd,
    x.price_date AS price_date,
    s.snapshot_date AS snapshot_date,
    x.break_date AS price_break_date,
    x.close * IF(p.shares_outstanding > 0, p.shares_outstanding, NULL) AS mcap,
    COALESCE(IF(p.ttm_end IS NOT NULL, p.ttm_operating_cf - ABS(p.ttm_capex), NULL), f.fcf) AS fcf_basis,
    COALESCE(IF(p.ttm_end IS NOT NULL, p.ttm_eps_diluted, NULL), f.eps) AS eps_basis,
    COALESCE(IF(p.ttm_end IS NOT NULL, p.ttm_revenue, NULL), f.revenue) AS revenue_basis,
    COALESCE(IF(p.ttm_end IS NOT NULL, p.ttm_net_income, NULL), f.net_income) AS net_income_basis,
    COALESCE(p.ttm_end, f.fiscal_year_end) AS eps_basis_date,
    x.close / x.c_1m - 1 AS return_1m,
    x.close / x.c_3m - 1 AS return_3m,
    x.close / x.c_1y - 1 AS return_1y,
    IF(x.price_days >= 120, x.hi_52w, NULL) AS high_52w_usd,
    IF(x.price_days >= 120, x.lo_52w, NULL) AS low_52w_usd,
    IF(x.price_days >= 120 AND x.hi_52w > x.lo_52w, (x.close - x.lo_52w) / (x.hi_52w - x.lo_52w) * 100, NULL) AS week52_position_pct,
    x.turnover_20d AS avg_turnover_20d_usd,
    x.price_days AS price_days,
    s.composite_score AS composite_score,
    s.value_score AS value_score,
    s.quality_score AS quality_score,
    h.rsi14 AS rsi14,
    h.above_ma200 AS above_ma200,
    h.snapshot_date AS technical_date
  FROM tickers AS t
  LEFT JOIN fund AS f ON f.ticker = t.ticker
  LEFT JOIN prof AS p ON p.ticker = t.ticker
  LEFT JOIN px AS x ON x.ticker = t.ticker
  LEFT JOIN snap AS s ON s.ticker = t.ticker
  LEFT JOIN tech AS h ON h.ticker = t.ticker
),
v AS (
  SELECT *,
    (price_break_date IS NOT NULL AND shares_as_of IS NOT NULL AND price_break_date > shares_as_of) AS mcap_suspect,
    (price_break_date IS NOT NULL AND eps_basis_date IS NOT NULL AND price_break_date > eps_basis_date) AS eps_suspect
  FROM j
),
m AS (
  SELECT *,
    IF(mcap_suspect, NULL, mcap) AS mcap_ok,
    IF(mcap_suspect OR eps_suspect, 'split_suspect', NULL) AS valuation_flag
  FROM v
)
SELECT
  ticker,
  name,
  exchange,
  sic,
  industry,
  cik,
  has_fundamentals,
  fiscal_year_end,
  revenue_usd,
  operating_income_usd,
  net_income_usd,
  fcf_usd,
  buyback_usd,
  dividends_paid_usd,
  eps_usd,
  operating_margin,
  net_margin,
  fcf_margin,
  sales_growth_1y,
  op_income_growth_1y,
  net_income_growth_1y,
  eps_growth_1y,
  ttm_period_end,
  ttm_revenue_usd,
  ttm_operating_income_usd,
  ttm_net_income_usd,
  ttm_eps_usd,
  balance_date,
  total_assets_usd,
  total_liabilities_usd,
  equity_usd,
  cash_usd,
  interest_bearing_debt_usd,
  equity_usd / NULLIF(total_assets_usd, 0) AS equity_ratio,
  shares_outstanding,
  shares_as_of,
  shares_source,
  price_source,
  current_price_usd,
  price_date,
  snapshot_date,
  mcap_ok AS market_cap_usd,
  mcap_ok / 1e9 AS market_cap_bn_usd,
  IF(NOT eps_suspect AND eps_basis > 0, current_price_usd / eps_basis, NULL) AS per,
  IF(mcap_ok > 0 AND revenue_basis > 0, mcap_ok / revenue_basis, NULL) AS ps_ratio,
  IF(mcap_ok > 0, fcf_basis / mcap_ok, NULL) AS fcf_yield,
  IF(mcap_ok > 0, buyback_usd / mcap_ok, NULL) AS buyback_yield,
  valuation_flag,
  IF(mcap_ok > 0 AND equity_usd > 0, mcap_ok / equity_usd, NULL) AS pbr,
  IF(equity_usd > 0, net_income_basis / equity_usd, NULL) AS roe,
  IF(mcap_ok > 0, dividends_paid_usd / mcap_ok, NULL) AS dividend_yield,
  return_1m,
  return_3m,
  return_1y,
  high_52w_usd,
  low_52w_usd,
  week52_position_pct,
  avg_turnover_20d_usd,
  price_days,
  price_break_date,
  composite_score,
  value_score,
  quality_score,
  rsi14,
  above_ma200,
  technical_date
FROM m
