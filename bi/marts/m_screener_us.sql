-- m_screener_us: 1銘柄1行（キー ticker）。米国株のスクリーナー用の表。通貨はすべて USD（列名の _usd は米ドル。円ではない）。
-- 日本版 m_screener と同じ考え方の列体系だが、財務は us.fundamentals（SEC XBRL の要約：売上・営業利益・純利益・希薄化EPS・FCF・自社株買い）しか無く、
-- 資産・負債・純資産・社名・業種は元表に無い（ROE・PBR・配当利回りは stock_snapshot の US 行がある約400銘柄だけ）。
-- us.fundamentals は location=US のため asia の bi.src_us_fundamentals に写したもの（bi_export.py が list_rows→load で更新。クエリ課金なし）を読む。
-- 通期の判定: us.fundamentals の period_end 行は 10-Q の期首からの累計値と 10-K の通期値が混ざり、期間の長さの列が無い。
--   決算月ごとに「次の行で値が小さくなる（累計がリセットされる）回数」を数え、最も多い月を通期の決算月とみなす
--   （リセットが2回未満なら12月決算とみなす）。その月の最新行が最新の通期。1年前の通期は 330〜400日前の同じ決算月の行。
--   最新の通期が2年より古い銘柄は財務を空にする（has_fundamentals＝FALSE）。
-- 時価総額は 終値 × 株数の推定（株数＝純利益÷希薄化EPS の絶対値。元表に発行済株式数が無いため。加重平均株数に近い値）。
-- 株価系・騰落率・52週高安・平均売買代金は analytics.price_daily_latest の直近約400日から全銘柄（約5,800）について作る。
--   終値は分割調整済み・配当未調整。日々の前日比が 0.6倍未満／1.7倍超の日（分割の調整漏れ・異常値）があれば、その日以降の終値だけを使う
--   （price_break_date）。決算期末より後にその日があるときは、EPS と株価の分割基準がずれるため valuation_flag='split_suspect' として
--   per・時価総額・FCF利回り・自社株買い利回りを空にする。
-- 騰落率は営業日ベース（return_1m＝21営業日前・return_3m＝63営業日前・return_1y＝252営業日前、足りなければ365日前以前で最も新しい終値、それも無ければ NULL）。
-- 52週高値・安値は終値ベースで直近252営業日、履歴が120営業日未満なら NULL。最新日が全体の最新日から14日以内の株価だけ使う。
-- name・スコア・RSI・PBR・ROE・配当利回りは analytics.stock_snapshot の market='US' の会社別最新行（14日以内）から。
WITH fx AS (
  SELECT ticker, cik, period_end, revenue, operating_income, net_income, eps_diluted, fcf, buyback,
         EXTRACT(MONTH FROM DATE_ADD(period_end, INTERVAL 15 DAY)) AS mk,
         MAX(period_end) OVER (PARTITION BY ticker) AS tmax,
         LEAD(net_income) OVER (PARTITION BY ticker ORDER BY period_end) AS next_ni,
         LEAD(period_end) OVER (PARTITION BY ticker ORDER BY period_end) AS next_pe
  FROM `stock-analyzer-edinet.bi.src_us_fundamentals`
  WHERE net_income IS NOT NULL AND period_end IS NOT NULL
),
fym AS (
  SELECT ticker, mk AS fy_mk FROM (
    SELECT ticker, mk,
           COUNTIF(next_pe IS NOT NULL AND DATE_DIFF(next_pe, period_end, DAY) BETWEEN 60 AND 130 AND ABS(next_ni) < ABS(net_income) * 0.9) AS resets,
           AVG(ABS(net_income)) AS a
    FROM fx
    WHERE period_end >= DATE_SUB(tmax, INTERVAL 4 YEAR)
    GROUP BY ticker, mk
  )
  QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY IF(resets >= 2, resets, 0) DESC, IF(mk = 1, 1, 0) DESC, a DESC) = 1
),
fy AS (
  SELECT fx.* FROM fx JOIN fym USING (ticker) WHERE fx.mk = fym.fy_mk
),
fy_cur AS (
  SELECT * FROM fy
  WHERE period_end >= DATE_SUB(CURRENT_DATE('America/New_York'), INTERVAL 2 YEAR)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY period_end DESC) = 1
),
fund AS (
  SELECT c.ticker, c.cik, c.period_end AS fiscal_year_end, c.revenue, c.operating_income, c.net_income, c.eps_diluted AS eps, c.fcf, c.buyback,
         IF(p.revenue > 0 AND c.revenue > 0, c.revenue / p.revenue - 1, NULL) AS sales_growth_1y,
         IF(p.operating_income > 0, c.operating_income / p.operating_income - 1, NULL) AS op_income_growth_1y,
         IF(p.net_income > 0, c.net_income / p.net_income - 1, NULL) AS net_income_growth_1y,
         IF(p.eps_diluted > 0, c.eps_diluted / p.eps_diluted - 1, NULL) AS eps_growth_1y
  FROM fy_cur AS c
  LEFT JOIN fy AS p ON p.ticker = c.ticker AND DATE_DIFF(c.period_end, p.period_end, DAY) BETWEEN 330 AND 400
),
snap AS (
  SELECT * FROM (
    SELECT ticker, snapshot_date, ingested_at, name, composite_score, value_score, quality_score, pbr, roe, dividend_yield,
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
             ARRAY_AGG(IF(trade_date <= DATE_SUB(tm, INTERVAL 365 DAY), close, NULL) IGNORE NULLS ORDER BY trade_date DESC LIMIT 1)[SAFE_OFFSET(0)]) AS c_1y,
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
),
j AS (
  SELECT
    t.ticker AS ticker,
    s.name AS name,
    f.cik AS cik,
    f.ticker IS NOT NULL AS has_fundamentals,
    f.fiscal_year_end AS fiscal_year_end,
    f.revenue AS revenue_usd,
    f.operating_income AS operating_income_usd,
    f.net_income AS net_income_usd,
    f.fcf AS fcf_usd,
    f.buyback AS buyback_usd,
    f.eps AS eps_usd,
    f.operating_income / NULLIF(f.revenue, 0) AS operating_margin,
    f.net_income / NULLIF(f.revenue, 0) AS net_margin,
    f.fcf / NULLIF(f.revenue, 0) AS fcf_margin,
    f.sales_growth_1y AS sales_growth_1y,
    f.op_income_growth_1y AS op_income_growth_1y,
    f.net_income_growth_1y AS net_income_growth_1y,
    f.eps_growth_1y AS eps_growth_1y,
    IF(f.eps != 0 AND f.net_income / f.eps > 0, f.net_income / f.eps, NULL) AS shares_est,
    IF(x.ticker IS NOT NULL, 'price_daily', NULL) AS price_source,
    x.close AS current_price_usd,
    x.price_date AS price_date,
    s.snapshot_date AS snapshot_date,
    x.break_date AS price_break_date,
    x.close * IF(f.eps != 0 AND f.net_income / f.eps > 0, f.net_income / f.eps, NULL) AS mcap,
    x.close / x.c_1m - 1 AS return_1m,
    x.close / x.c_3m - 1 AS return_3m,
    x.close / x.c_1y - 1 AS return_1y,
    IF(x.price_days >= 120, x.hi_52w, NULL) AS high_52w_usd,
    IF(x.price_days >= 120, x.lo_52w, NULL) AS low_52w_usd,
    IF(x.price_days >= 120 AND x.hi_52w > x.lo_52w, (x.close - x.lo_52w) / (x.hi_52w - x.lo_52w) * 100, NULL) AS week52_position_pct,
    x.turnover_20d AS avg_turnover_20d_usd,
    x.price_days AS price_days,
    s.pbr AS pbr,
    s.roe AS roe,
    s.dividend_yield AS dividend_yield,
    s.composite_score AS composite_score,
    s.value_score AS value_score,
    s.quality_score AS quality_score,
    h.rsi14 AS rsi14,
    h.above_ma200 AS above_ma200,
    h.snapshot_date AS technical_date
  FROM tickers AS t
  LEFT JOIN fund AS f ON f.ticker = t.ticker
  LEFT JOIN px AS x ON x.ticker = t.ticker
  LEFT JOIN snap AS s ON s.ticker = t.ticker
  LEFT JOIN tech AS h ON h.ticker = t.ticker
),
v AS (
  SELECT *,
    IF(price_break_date IS NOT NULL AND fiscal_year_end IS NOT NULL AND price_break_date > fiscal_year_end, 'split_suspect', NULL) AS valuation_flag
  FROM j
)
SELECT
  ticker,
  name,
  cik,
  has_fundamentals,
  fiscal_year_end,
  revenue_usd,
  operating_income_usd,
  net_income_usd,
  fcf_usd,
  buyback_usd,
  eps_usd,
  operating_margin,
  net_margin,
  fcf_margin,
  sales_growth_1y,
  op_income_growth_1y,
  net_income_growth_1y,
  eps_growth_1y,
  shares_est,
  price_source,
  current_price_usd,
  price_date,
  snapshot_date,
  IF(valuation_flag IS NULL, mcap, NULL) AS market_cap_usd,
  IF(valuation_flag IS NULL, mcap / 1e9, NULL) AS market_cap_bn_usd,
  IF(valuation_flag IS NULL AND eps_usd > 0, current_price_usd / eps_usd, NULL) AS per,
  IF(valuation_flag IS NULL AND revenue_usd > 0 AND mcap > 0, mcap / revenue_usd, NULL) AS ps_ratio,
  IF(valuation_flag IS NULL AND mcap > 0, fcf_usd / mcap, NULL) AS fcf_yield,
  IF(valuation_flag IS NULL AND mcap > 0, buyback_usd / mcap, NULL) AS buyback_yield,
  valuation_flag,
  pbr,
  roe,
  dividend_yield,
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
FROM v
