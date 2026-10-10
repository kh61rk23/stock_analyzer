-- m_market_breadth: 市場×営業日（キー market, trade_date）。market は 'JP'（.T）と 'US'（英字ティッカー。先物・指数は除く）。約2市場×約240営業日＝約500行。
-- 元は analytics.price_daily_latest の直近約400日だけ。全上場の日次終値から、その日の騰落の広がりを数える。
-- 日々の前日比が 0.6倍未満／1.7倍超の日（分割の調整漏れ・異常値）があれば、その日以降の価格だけ使う（m_screener と同じ規則）。
-- n_stocks＝その日に前日比が付いた銘柄数。is_complete＝n_stocks が直近21営業日の最大の9割以上（株価の取り込みが途中の日は FALSE。その日の比率・新高値数は母数が欠けるので、テンプレは is_complete の日だけ使う）。n_up／n_down／n_flat＝上がった／下がった／変わらず。
-- ad_ratio_25d＝騰落レシオ（直近25営業日のうち is_complete の日の上昇銘柄数の合計 ÷ 下落銘柄数の合計 × 100。そろった日が20日以上あるときだけ）。120以上が過熱、70以下が売られすぎの目安。
-- n_new_high／n_new_low＝終値が直前252営業日の終値の最高値を上回った／最安値を下回った銘柄数（直前の履歴が120営業日以上ある銘柄だけ。履歴が約1年しか無いため、
--   取得期間の序盤は母数 n_hl_base が少なく0に近い。比較するときは n_hl_base を見る）。
-- pct_above_ma50／pct_above_ma200＝終値が50／200営業日移動平均（当日を含む）より上の銘柄の比率（その日数ぶんの履歴がある銘柄だけが母数 n_ma50／n_ma200。
--   200日は取得開始から約200営業日後（2026年7月ごろ）から出る）。
-- ret_ew＝全銘柄の日次騰落率の単純平均、idx_ew＝それを掛け合わせた等加重指数（is_complete でない日は動かさない。取得期間の頭の前営業日＝100）。turnover＝終値×出来高の合計（現地通貨）。
WITH px_raw AS (
  SELECT ticker, trade_date, close, volume,
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
  SELECT ticker, trade_date, close, volume, prev_close FROM px_brk WHERE break_date IS NULL OR trade_date >= break_date
),
d AS (
  SELECT IF(ticker LIKE '%.T', 'JP', 'US') AS market, ticker, trade_date, close, volume, prev_close,
    MAX(close) OVER (PARTITION BY ticker ORDER BY trade_date ROWS BETWEEN 252 PRECEDING AND 1 PRECEDING) AS hi_prev,
    MIN(close) OVER (PARTITION BY ticker ORDER BY trade_date ROWS BETWEEN 252 PRECEDING AND 1 PRECEDING) AS lo_prev,
    COUNT(close) OVER (PARTITION BY ticker ORDER BY trade_date ROWS BETWEEN 252 PRECEDING AND 1 PRECEDING) AS n_prev,
    AVG(close) OVER (PARTITION BY ticker ORDER BY trade_date ROWS BETWEEN 49 PRECEDING AND CURRENT ROW) AS ma50,
    COUNT(close) OVER (PARTITION BY ticker ORDER BY trade_date ROWS BETWEEN 49 PRECEDING AND CURRENT ROW) AS c50,
    AVG(close) OVER (PARTITION BY ticker ORDER BY trade_date ROWS BETWEEN 199 PRECEDING AND CURRENT ROW) AS ma200,
    COUNT(close) OVER (PARTITION BY ticker ORDER BY trade_date ROWS BETWEEN 199 PRECEDING AND CURRENT ROW) AS c200
  FROM px
),
a AS (
  SELECT market, trade_date,
    COUNT(*) AS n_stocks,
    COUNTIF(close > prev_close) AS n_up,
    COUNTIF(close < prev_close) AS n_down,
    COUNTIF(close = prev_close) AS n_flat,
    COUNTIF(n_prev >= 120) AS n_hl_base,
    COUNTIF(n_prev >= 120 AND close > hi_prev) AS n_new_high,
    COUNTIF(n_prev >= 120 AND close < lo_prev) AS n_new_low,
    COUNTIF(c50 = 50) AS n_ma50,
    COUNTIF(c50 = 50 AND close > ma50) AS n_above_ma50,
    COUNTIF(c200 = 200) AS n_ma200,
    COUNTIF(c200 = 200 AND close > ma200) AS n_above_ma200,
    AVG(close / prev_close - 1) AS ret_ew,
    SUM(close * volume) AS turnover
  FROM d
  WHERE prev_close > 0
  GROUP BY market, trade_date
),
c AS (
  SELECT *,
    n_stocks >= 0.9 * MAX(n_stocks) OVER (PARTITION BY market ORDER BY trade_date ROWS BETWEEN 20 PRECEDING AND CURRENT ROW) AS is_complete
  FROM a
),
b AS (
  SELECT *,
    SUM(IF(is_complete, n_up, 0)) OVER w25 AS up25,
    SUM(IF(is_complete, n_down, 0)) OVER w25 AS down25,
    COUNTIF(is_complete) OVER w25 AS days25,
    100 * EXP(SUM(IF(is_complete, LN(1 + ret_ew), 0)) OVER (PARTITION BY market ORDER BY trade_date)) AS idx_ew
  FROM c
  WINDOW w25 AS (PARTITION BY market ORDER BY trade_date ROWS BETWEEN 24 PRECEDING AND CURRENT ROW)
)
SELECT
  market,
  trade_date,
  n_stocks,
  is_complete,
  n_up,
  n_down,
  n_flat,
  ROUND(n_up / NULLIF(n_up + n_down, 0), 4) AS up_ratio,
  IF(days25 >= 20, ROUND(up25 / NULLIF(down25, 0) * 100, 2), NULL) AS ad_ratio_25d,
  n_hl_base,
  n_new_high,
  n_new_low,
  n_ma50,
  ROUND(n_above_ma50 / NULLIF(n_ma50, 0), 4) AS pct_above_ma50,
  n_ma200,
  ROUND(n_above_ma200 / NULLIF(n_ma200, 0), 4) AS pct_above_ma200,
  ROUND(ret_ew, 5) AS ret_ew,
  ROUND(idx_ew, 4) AS idx_ew,
  ROUND(turnover, 0) AS turnover
FROM b
