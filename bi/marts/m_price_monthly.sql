-- m_price_monthly: 銘柄×月（キー ticker, month）。日本株（.T）と米国株（英字ティッカー）の全上場について、月末終値・騰落率・月間売買代金・月中の高安を持つ。
-- 日次の全件ではなく月に畳むので行数が小さい（約1.2万銘柄×最大14か月）。元は analytics.price_daily_latest の直近約400日だけ（日次パーティションで読み取りを絞る）。
-- market は 'JP'/'US'（ティッカーが .T で終わるか、英字・ピリオド・ハイフンだけ。先物 CC=F や指数 ^ は除く）。市場区分（プライム等）は元表に無いので持たない。
-- 通貨は日本株が円、米国株が米ドル（turnover・close とも現地通貨）。終値は分割調整済み・配当未調整。
-- 日々の前日比が 0.6倍未満／1.7倍超の日（分割の調整漏れ・異常値）があれば、その日以降の価格だけ使う（m_screener と同じ規則）。
-- month は月初日。close は月内で最後の営業日の終値、month_end_date はその日。
--   return_1m／3m／6m は「ちょうど1／3／6か月前の月」の月末終値との比（その月が無ければ NULL。欠けた月を跨がない）。
--   is_partial は月がまだ終わっていない（当月）か、取得期間の頭で月の途中から始まる月。TRUE の月の騰落率・売買代金は他の月と比べない。
--   turnover＝月内の 終値×出来高 の合計。turnover_ratio_3m＝その月の turnover ÷ 直前3か月の turnover の平均（3か月そろうときだけ。当月は日数が少ないので小さく出る）。
--   month_high／month_low は日足の高値・安値（無ければ終値）。range_pct＝month_high÷month_low－1。
-- name・industry は日本株が edinet.company、米国株は stock_snapshot の最新行（約400銘柄だけ。無ければ NULL）。
WITH px_raw AS (
  SELECT ticker, trade_date, high, low, close, volume,
         LAG(close) OVER (PARTITION BY ticker ORDER BY trade_date) AS prev_close
  FROM `stock-analyzer-edinet.analytics.price_daily_latest`
  WHERE trade_date >= DATE_SUB(CURRENT_DATE('Asia/Tokyo'), INTERVAL 400 DAY)
    AND close > 0 AND close < 10000000
    AND (ticker LIKE '%.T' OR REGEXP_CONTAINS(ticker, r'^[A-Z][A-Z.\-]*$'))
),
px_brk AS (
  SELECT *,
         MAX(IF(prev_close > 0 AND (close / prev_close < 0.6 OR close / prev_close > 1.7), trade_date, NULL)) OVER (PARTITION BY ticker) AS break_date
  FROM px_raw
),
px AS (
  SELECT * FROM px_brk WHERE break_date IS NULL OR trade_date >= break_date
),
mon AS (
  SELECT ticker, DATE_TRUNC(trade_date, MONTH) AS month,
         ARRAY_AGG(close ORDER BY trade_date DESC LIMIT 1)[OFFSET(0)] AS close,
         MAX(trade_date) AS month_end_date,
         COUNT(*) AS trading_days,
         MAX(IF(high > 0, high, close)) AS month_high,
         MIN(IF(low > 0, low, close)) AS month_low,
         SUM(close * volume) AS turnover,
         SUM(volume) AS volume_sum,
         MIN(trade_date) AS first_date
  FROM px
  GROUP BY ticker, month
),
w AS (
  SELECT *,
    LAG(month, 1) OVER o AS m1, LAG(close, 1) OVER o AS c1,
    LAG(month, 3) OVER o AS m3, LAG(close, 3) OVER o AS c3,
    LAG(month, 6) OVER o AS m6, LAG(close, 6) OVER o AS c6,
    AVG(turnover) OVER (PARTITION BY ticker ORDER BY month ROWS BETWEEN 3 PRECEDING AND 1 PRECEDING) AS t_avg3,
    COUNT(turnover) OVER (PARTITION BY ticker ORDER BY month ROWS BETWEEN 3 PRECEDING AND 1 PRECEDING) AS t_n3,
    MIN(first_date) OVER () AS data_start
  FROM mon
  WINDOW o AS (PARTITION BY ticker ORDER BY month)
),
jp AS (
  SELECT CONCAT(SUBSTR(sec_code, 1, 4), '.T') AS ticker, ANY_VALUE(name) AS name, ANY_VALUE(industry) AS industry
  FROM `stock-analyzer-edinet.edinet.company`
  WHERE sec_code IS NOT NULL AND COALESCE(is_delisted, FALSE) = FALSE
  GROUP BY ticker
),
us AS (
  SELECT ticker, name FROM `stock-analyzer-edinet.analytics.stock_snapshot`
  WHERE market = 'US'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY snapshot_date DESC, ingested_at DESC) = 1
)
SELECT
  w.ticker AS ticker,
  IF(w.ticker LIKE '%.T', 'JP', 'US') AS market,
  COALESCE(jp.name, us.name) AS name,
  jp.industry AS industry,
  w.month AS month,
  w.month_end_date AS month_end_date,
  w.trading_days AS trading_days,
  (w.month = DATE_TRUNC(CURRENT_DATE('Asia/Tokyo'), MONTH) OR w.month < DATE_TRUNC(w.data_start, MONTH) OR (w.month = DATE_TRUNC(w.data_start, MONTH) AND EXTRACT(DAY FROM w.data_start) > 5)) AS is_partial,
  w.close AS close,
  w.c1 AS prev_month_close,
  IF(w.m1 = DATE_SUB(w.month, INTERVAL 1 MONTH), w.close / w.c1 - 1, NULL) AS return_1m,
  IF(w.m3 = DATE_SUB(w.month, INTERVAL 3 MONTH), w.close / w.c3 - 1, NULL) AS return_3m,
  IF(w.m6 = DATE_SUB(w.month, INTERVAL 6 MONTH), w.close / w.c6 - 1, NULL) AS return_6m,
  w.month_high AS month_high,
  w.month_low AS month_low,
  w.month_high / NULLIF(w.month_low, 0) - 1 AS range_pct,
  w.turnover AS turnover,
  w.volume_sum AS volume,
  IF(w.t_n3 = 3 AND w.m3 = DATE_SUB(w.month, INTERVAL 3 MONTH), w.turnover / NULLIF(w.t_avg3, 0), NULL) AS turnover_ratio_3m
FROM w
LEFT JOIN jp ON jp.ticker = w.ticker
LEFT JOIN us ON us.ticker = w.ticker
