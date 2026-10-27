-- m_pick_outcomes: 推奨のその後。1行＝選び方（source）× 選ばれた日 × 銘柄（キー pick_date, source, ticker）。
-- 元は analytics.pick_outcomes（pick_outcomes.py が日次で直近130日を MERGE）。選ばれた日以降の最初の終値で買ったときの
-- 5・20・60 取引日後の騰落（ret_*）・指数の ETF（1306.T・SPY）の騰落（bench_*）・その差（ex_*）。まだ日数が経っていなければ空。
SELECT
  pick_date AS pick_date,
  source AS source,
  ticker AS ticker,
  name AS name,
  score AS score,
  rank AS rank,
  n_picks AS n_picks,
  is_new AS is_new,
  bench AS bench,
  entry_date AS entry_date,
  entry_close AS entry_close,
  ret_5d AS ret_5d,
  bench_5d AS bench_5d,
  ex_5d AS ex_5d,
  ret_20d AS ret_20d,
  bench_20d AS bench_20d,
  ex_20d AS ex_20d,
  ret_60d AS ret_60d,
  bench_60d AS bench_60d,
  ex_60d AS ex_60d
FROM `stock-analyzer-edinet.analytics.pick_outcomes`
