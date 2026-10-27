-- m_large_holdings: 日本の大量保有報告書（直近1年）。1行＝1報告書 × 保有者（キー doc_id, holder_name）。
-- 元は edinet.large_holdings（政府 EDINET の大量保有報告書の XBRL・edinet_gov_docs.py）。保有割合は小数（0.052＝5.2%）。
SELECT
  doc_id AS doc_id,
  submit_date AS submit_date,
  issuer_edinet_code AS issuer_edinet_code,
  issuer_sec_code AS issuer_sec_code,
  issuer_name AS issuer_name,
  holder_name AS holder_name,
  holder_kind AS holder_kind,
  prior_ratio AS prior_ratio,
  holding_ratio AS holding_ratio,
  shares_held AS shares_held,
  LEFT(purpose, 200) AS purpose,
  change_reason AS change_reason
FROM `stock-analyzer-edinet.edinet.large_holdings`
WHERE submit_date >= DATE_SUB(CURRENT_DATE('Asia/Tokyo'), INTERVAL 365 DAY)
QUALIFY ROW_NUMBER() OVER (PARTITION BY doc_id, holder_name ORDER BY base_date DESC) = 1
