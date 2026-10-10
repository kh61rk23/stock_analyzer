-- m_us_segments: 米国株のセグメント別の売上・営業利益。1社×軸×セグメント×通期1行（キー cik, axis, member, fiscal_year_end）。通貨は USD。
-- 元は bi.src_us_segments（us.segment_financials の写し。SEC Financial Statement Data Sets の num.txt のうち、
-- 軸1つ（BusinessSegments＝事業・ProductOrService＝製品/サービス・Geographical＝地域）の売上系タグと営業利益。us_segments.py が四半期ごとに追記）。
-- 通期（qtrs=4）だけを使う。同じ期の同じ値は翌年の 10-K に比較列として再掲されるので、(cik, 軸, セグメント, タグ, 期末) ごとに提出日が新しい行を採る（訂正後の値）。
-- 売上は会社ごとに使うタグが違うため、RevenueFromContractWithCustomerExcludingAssessedTax → Revenues → その他の順で1つ選ぶ。
-- 営業利益（OperatingIncomeLoss）をセグメント別に開示するのは主に事業セグメント。製品・地域の軸は売上だけの会社が多い。
-- ティッカーは CIK ごとに1つ（種類株などで複数あるときは辞書順で最初のもの。tickers_all に全部）。
-- revenue_share は全社売上（bi.src_us_financials_annual の同じ会社・期末±10日の通期売上）に対する比率。全社売上が無ければ空。
-- 軸のセグメント合計で割らない理由: 製品・サービスの軸には「Product（製品計）」「Service」のような合計の行と内訳の行が並ぶ会社が多く
-- （AAPL は Product＝iPhone＋Mac＋iPad＋Wearables）、合計で割ると比率が半分ほどに縮む。合計の行も残すので、足し上げると100%を超えることがある。
WITH s AS (
  SELECT cik, axis, member, tag, period_end, val
  FROM `stock-analyzer-edinet.bi.src_us_segments`
  WHERE qtrs = 4
  QUALIFY ROW_NUMBER() OVER (PARTITION BY cik, axis, member, tag, period_end ORDER BY filed DESC) = 1
),
rev AS (
  SELECT cik, axis, member, period_end,
    ARRAY_AGG(val ORDER BY CASE tag WHEN 'RevenueFromContractWithCustomerExcludingAssessedTax' THEN 1 WHEN 'Revenues' THEN 2 ELSE 3 END LIMIT 1)[OFFSET(0)] AS revenue
  FROM s WHERE tag != 'OperatingIncomeLoss'
  GROUP BY cik, axis, member, period_end
),
oi AS (
  SELECT cik, axis, member, period_end, val AS operating_income FROM s WHERE tag = 'OperatingIncomeLoss'
),
seg AS (
  SELECT COALESCE(r.cik, o.cik) AS cik, COALESCE(r.axis, o.axis) AS axis, COALESCE(r.member, o.member) AS member,
         COALESCE(r.period_end, o.period_end) AS period_end, r.revenue, o.operating_income
  FROM rev AS r
  FULL OUTER JOIN oi AS o ON o.cik = r.cik AND o.axis = r.axis AND o.member = r.member AND o.period_end = r.period_end
),
co AS (
  SELECT cik, period_end, ANY_VALUE(revenue) AS company_revenue
  FROM `stock-analyzer-edinet.bi.src_us_financials_annual`
  WHERE revenue > 0
  GROUP BY cik, period_end
),
tk AS (
  SELECT cik, MIN(ticker) AS ticker, STRING_AGG(ticker, ',' ORDER BY ticker) AS tickers_all,
         ANY_VALUE(name) AS name, ANY_VALUE(sic_description) AS industry
  FROM `stock-analyzer-edinet.bi.src_us_company_profile`
  WHERE cik IS NOT NULL
  GROUP BY cik
),
cr AS (
  SELECT seg.cik, seg.period_end, ARRAY_AGG(co.company_revenue ORDER BY ABS(DATE_DIFF(seg.period_end, co.period_end, DAY)) LIMIT 1)[OFFSET(0)] AS company_revenue
  FROM (SELECT DISTINCT cik, period_end FROM seg) AS seg
  JOIN co ON co.cik = seg.cik AND ABS(DATE_DIFF(seg.period_end, co.period_end, DAY)) <= 10
  GROUP BY seg.cik, seg.period_end
),
x AS (
  SELECT seg.*, cr.company_revenue,
    LAG(period_end) OVER (PARTITION BY cik, axis, member ORDER BY period_end) AS p_end,
    LAG(revenue) OVER (PARTITION BY cik, axis, member ORDER BY period_end) AS p_revenue,
    MAX(period_end) OVER (PARTITION BY cik) AS latest_end
  FROM seg
  LEFT JOIN cr USING (cik, period_end)
)
SELECT
  t.ticker AS ticker,
  x.cik AS cik,
  t.tickers_all AS tickers_all,
  t.name AS name,
  t.industry AS industry,
  x.axis AS axis,
  CASE x.axis WHEN 'BusinessSegments' THEN '事業' WHEN 'ProductOrService' THEN '製品・サービス' WHEN 'Geographical' THEN '地域' END AS axis_ja,
  x.member AS member,
  REGEXP_REPLACE(REGEXP_REPLACE(REGEXP_REPLACE(x.member, r'^.*:', ''), r'(Segment)?Member$', ''), r'([a-z])([A-Z])', r'\1 \2') AS segment_name,
  x.period_end AS fiscal_year_end,
  EXTRACT(YEAR FROM x.period_end) AS fiscal_year,
  (x.period_end = x.latest_end) AS is_latest,
  x.revenue AS revenue_usd,
  x.operating_income AS operating_income_usd,
  x.operating_income / NULLIF(x.revenue, 0) AS operating_margin,
  x.company_revenue AS company_revenue_usd,
  IF(x.company_revenue > 0 AND x.revenue >= 0, x.revenue / x.company_revenue, NULL) AS revenue_share,
  IF(DATE_DIFF(x.period_end, x.p_end, DAY) BETWEEN 330 AND 400 AND x.p_revenue > 0, x.revenue / x.p_revenue - 1, NULL) AS revenue_growth_1y
FROM x
LEFT JOIN tk AS t ON t.cik = x.cik
