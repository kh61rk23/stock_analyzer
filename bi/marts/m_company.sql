-- m_company: 1社1行（キー edinet_code）。edinet.company は edinet_code が一意（4,010社＝4,010キー）なので重複除去は不要。
-- sec_code は5桁（末尾に検査用の0）。先頭4桁が銘柄コード（英字入り '285A' 含む）。
SELECT
  c.edinet_code,
  SUBSTR(c.sec_code, 1, 4) AS sec_code4,
  c.name,
  c.industry,
  c.accounting_standard,
  c.listing_status,
  c.is_delisted
FROM `stock-analyzer-edinet.edinet.company` AS c
