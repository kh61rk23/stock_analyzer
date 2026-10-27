-- m_deliberations: 投資部の4エージェント審議の結果。1行＝1審議（キー case_id）。元は invest.deliberations。
-- パケット・レポートの全文は重いので出さず、ブル・ベア・デビルのメモは先頭600字だけ。承認欄は人間が書く。
SELECT
  case_id AS case_id,
  ticker AS ticker,
  company_name AS company_name,
  DATE(deliberated_at, 'Asia/Tokyo') AS deliberated_on,
  verdict AS verdict,
  conviction AS conviction,
  layer AS layer,
  bull_conviction AS bull_conviction,
  bear_conviction AS bear_conviction,
  devil_unresolved AS devil_unresolved,
  devil_reversal AS devil_reversal,
  LEFT(bull_memo, 600) AS bull_memo,
  LEFT(bear_memo, 600) AS bear_memo,
  LEFT(devil_memo, 600) AS devil_memo,
  approval_status AS approval_status,
  DATE(approved_at, 'Asia/Tokyo') AS approved_on,
  review_on AS review_on,
  holding_intent AS holding_intent
FROM `stock-analyzer-edinet.invest.deliberations`
