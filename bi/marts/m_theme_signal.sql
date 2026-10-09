-- m_theme_signal: テーマ×日×シグナル種別×方向1行（キー theme_name, sector_code, fired_at, signal_category, direction）。
-- 元は ontology.signal_event（location=US）。asia-northeast1 のテーマ表と1本のクエリで結合できないため、
-- bi_export.py が list_rows → load job（どちらも無料）で asia の bi.src_signal_event へ写してからここで読む。
-- 結合は業種コードで行う（signal_event.sector_code ＝ sub_theme_master.sector_code）。テーマは業種ごとに定義され
-- 銘柄ごとの割当ではないため、同じ業種のテーマは同じシグナル件数になる点に注意。日本の業種だけ（米国のテーマに対応するシグナルは無い）。
-- signal_event は同じ event_id が再取り込みで重複して入るので event_id の重複を除いて数える。
-- ticker が空のシグナルは業種単位。ticker_signal_count は銘柄つきの件数、ticker_count はその銘柄数。
-- days_before_latest は表内の最新シグナル日からの日数（日付関数を使わず「直近N日」を絞れる。更新日に依存しない）。
WITH th AS (
  SELECT m.sector_code, m.name_ja AS sector_name_ja, theme_name
  FROM `stock-analyzer-edinet.edinet.sub_theme_master` AS m, UNNEST(JSON_VALUE_ARRAY(m.subthemes)) AS theme_name
  WHERE m.market = 'JP'
),
sig AS (
  SELECT
    sector_code,
    fired_at,
    signal_category,
    direction,
    COUNT(DISTINCT event_id) AS signal_count,
    COUNT(DISTINCT IF(ticker IS NOT NULL, event_id, NULL)) AS ticker_signal_count,
    COUNT(DISTINCT ticker) AS ticker_count,
    AVG(signal_value) AS avg_signal_value
  FROM `stock-analyzer-edinet.bi.src_signal_event`
  WHERE sector_code IS NOT NULL
  GROUP BY sector_code, fired_at, signal_category, direction
),
latest AS (
  SELECT MAX(fired_at) AS latest_date
  FROM `stock-analyzer-edinet.bi.src_signal_event`
)
SELECT
  th.theme_name,
  th.sector_code,
  th.sector_name_ja,
  sig.fired_at,
  DATE_DIFF(latest.latest_date, sig.fired_at, DAY) AS days_before_latest,
  sig.signal_category,
  sig.direction,
  sig.signal_count,
  sig.ticker_signal_count,
  sig.ticker_count,
  sig.avg_signal_value
FROM sig
INNER JOIN th ON th.sector_code = sig.sector_code
CROSS JOIN latest
