-- m_sector_perf: 業種×営業日（キー industry, trade_date）。日本株の業種ごとの日次騰落率・指数・上昇銘柄比率。約33業種×約240営業日＝約8千行（業種×日でも軽いので週にしない）。
-- 元は analytics.price_daily_latest の直近約400日（.T のみ）と bi.m_screener（業種 industry と時価総額 market_cap。bi_export が前回実体化した表を読む。小さい）。
-- 業種は EDINET の業種名（m_screener.industry）。株価が取れ、業種のある上場会社だけ。
-- 株価の取り込みが途中の日（日本株全体で前日比が付いた銘柄数が直近21営業日の最大の9割未満）は除く（業種の銘柄が欠けて偏るため）。
-- 日々の前日比が 0.6倍未満／1.7倍超の日（分割の調整漏れ・異常値）があれば、その日以降の価格だけ使う（m_screener と同じ規則）。
-- ret_ew＝業種内の銘柄の日次騰落率の単純平均（等加重）、ret_cw＝時価総額で加重した平均。重みは最新の時価総額（日々の時価総額の履歴は持たないため固定の近似。時価総額が無い銘柄は ret_cw から外す）。
-- idx_ew／idx_cw＝日次騰落率を掛け合わせた指数（取得期間の頭の前営業日＝100）。リバランス日次の近似で、実際の業種指数（TOPIX-17 等）とは一致しない。
-- ret_1m_*／ret_3m_*＝21／63営業日前の指数との比、ret_ytd_*＝前年最終営業日の指数との比（前年の履歴が無ければ NULL）。
-- up_ratio＝前日比が上がった銘柄 ÷（上がった＋下がった）。変わらずは除く。turnover＝業種内の 終値×出来高 の合計（円）。
WITH scr AS (
  SELECT ticker, ANY_VALUE(industry) AS industry, MAX(market_cap) AS market_cap
  FROM `stock-analyzer-edinet.bi.m_screener`
  WHERE ticker IS NOT NULL AND industry IS NOT NULL
  GROUP BY ticker
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
cal AS (
  SELECT trade_date FROM (
    SELECT trade_date, COUNT(*) AS n,
           MAX(COUNT(*)) OVER (ORDER BY trade_date ROWS BETWEEN 20 PRECEDING AND CURRENT ROW) AS n_ref
    FROM px_brk WHERE prev_close > 0 GROUP BY trade_date
  ) WHERE n >= 0.9 * n_ref
),
r AS (
  SELECT s.industry, p.trade_date, p.close / p.prev_close - 1 AS ret, p.close * p.volume AS turnover, s.market_cap
  FROM px_brk AS p JOIN scr AS s ON s.ticker = p.ticker
  WHERE (p.break_date IS NULL OR p.trade_date >= p.break_date) AND p.prev_close > 0
    AND p.trade_date IN (SELECT trade_date FROM cal)
),
d AS (
  SELECT industry, trade_date,
    COUNT(*) AS n_stocks,
    COUNTIF(ret > 0) AS n_up,
    COUNTIF(ret < 0) AS n_down,
    AVG(ret) AS ret_ew,
    SUM(IF(market_cap > 0, ret * market_cap, NULL)) / NULLIF(SUM(IF(market_cap > 0, market_cap, NULL)), 0) AS ret_cw,
    SUM(turnover) AS turnover
  FROM r
  GROUP BY industry, trade_date
),
i AS (
  SELECT *,
    100 * EXP(SUM(LN(1 + ret_ew)) OVER (PARTITION BY industry ORDER BY trade_date)) AS idx_ew,
    100 * EXP(SUM(LN(1 + COALESCE(ret_cw, ret_ew))) OVER (PARTITION BY industry ORDER BY trade_date)) AS idx_cw
  FROM d
),
ye AS (
  SELECT industry, EXTRACT(YEAR FROM trade_date) + 1 AS yr,
         ARRAY_AGG(idx_ew ORDER BY trade_date DESC LIMIT 1)[OFFSET(0)] AS base_ew,
         ARRAY_AGG(idx_cw ORDER BY trade_date DESC LIMIT 1)[OFFSET(0)] AS base_cw
  FROM i
  GROUP BY industry, yr
),
l AS (
  SELECT *,
    LAG(idx_ew, 21) OVER (PARTITION BY industry ORDER BY trade_date) AS ew_21,
    LAG(idx_ew, 63) OVER (PARTITION BY industry ORDER BY trade_date) AS ew_63,
    LAG(idx_cw, 21) OVER (PARTITION BY industry ORDER BY trade_date) AS cw_21,
    LAG(idx_cw, 63) OVER (PARTITION BY industry ORDER BY trade_date) AS cw_63
  FROM i
)
SELECT
  l.industry AS industry,
  l.trade_date AS trade_date,
  l.n_stocks AS n_stocks,
  l.n_up AS n_up,
  l.n_down AS n_down,
  ROUND(l.n_up / NULLIF(l.n_up + l.n_down, 0), 4) AS up_ratio,
  ROUND(l.ret_ew, 5) AS ret_ew,
  ROUND(l.ret_cw, 5) AS ret_cw,
  ROUND(l.idx_ew, 4) AS idx_ew,
  ROUND(l.idx_cw, 4) AS idx_cw,
  ROUND(l.idx_ew / l.ew_21 - 1, 5) AS ret_1m_ew,
  ROUND(l.idx_ew / l.ew_63 - 1, 5) AS ret_3m_ew,
  ROUND(l.idx_ew / ye.base_ew - 1, 5) AS ret_ytd_ew,
  ROUND(l.idx_cw / l.cw_21 - 1, 5) AS ret_1m_cw,
  ROUND(l.idx_cw / l.cw_63 - 1, 5) AS ret_3m_cw,
  ROUND(l.idx_cw / ye.base_cw - 1, 5) AS ret_ytd_cw,
  ROUND(l.turnover, 0) AS turnover
FROM l
LEFT JOIN ye ON ye.industry = l.industry AND ye.yr = EXTRACT(YEAR FROM l.trade_date)
