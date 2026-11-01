-- m_llm_news: S8 LLMニュース・アルファ（週次手動・参考情報）の履歴。1行＝1週の1シグナル（キー as_of, ticker）。
-- 元は invest.llm_news_runs と invest.llm_news_signals。該当なしの週は ticker が空の1行になる（signal_count=0）。
-- signal_aggregator の点数には入っていない。配線の要否は D-19 で、記入4週分（約20件）がたまってから決める。
SELECT
  r.as_of AS as_of,
  s.ticker AS ticker,
  s.market AS market,
  s.direction AS direction,
  s.strength AS strength,
  s.thesis AS thesis,
  s.source_url AS source_url,
  s.expire_date AS expire_date,
  r.signal_count AS signal_count,
  DATE(r.recorded_at, 'Asia/Tokyo') AS recorded_on
FROM `stock-analyzer-edinet.invest.llm_news_runs` r
LEFT JOIN `stock-analyzer-edinet.invest.llm_news_signals` s ON s.as_of = r.as_of
