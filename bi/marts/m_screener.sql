-- m_screener: 1社1行（キー edinet_code）。会社ごとに「売上がある最新年度」の財務と、株価系指標を横に並べたスクリーナー用の表（日本株・円）。
-- 金額は円、比率は小数（0.12＝12%）。market_cap は円。market_cap_b は「億円（1e8円）」（列名の b は十億ではない。トヨタ 7203 で約34.5兆円＝344,775）。
-- 株価系（current_price・price_date・market_cap・per・pbr・dividend_yield・net_cash_to_mcap）・騰落率・52週高安・平均売買代金は、
-- analytics.price_daily_latest（ticker×営業日で重複除去したビュー）の直近約400日から全上場について作る（price_source は株価があれば 'price_daily'、無ければ NULL）。
-- 終値は分割調整済み・配当未調整。EPS・BPS・1株配当・株数は決算期末の値で、期末後に株式分割があると終値と単位がずれる。
-- 細目の adjusted_eps / adjusted_bps / adjusted_dividend_per_share（分割調整後）があればそれを使い、
-- 調整前EPS÷調整後EPS を分割倍率として株数にも掛ける（東京海上 8766 は期末後の分割で EPS が15倍に見えていた）。
-- stock_snapshot はスコア（composite_score 等）とテクニカル（rsi14・above_ma200）にだけ使う。株価・時価総額・PER 等には使わない。
-- 騰落率は営業日ベース（return_1m＝21営業日前・return_3m＝63営業日前・return_1y＝252営業日前の終値との比。足りなければ 360日前以前で最も新しい終値。それも無ければ NULL）。
-- 52週高値・安値は終値ベースで直近252営業日、履歴が120営業日未満なら NULL。
-- 最新日が全体の最新日から14日以内の株価だけ使う（上場廃止・取込停止の古い株価を最新として出さないため）。
-- 成長率は「ちょうど1年前／3年前の年度」が財務表にあるときだけ計算し、無ければ NULL（年が欠けた期を跨がない）。
-- 営業利益・純利益の成長率は前年値が正のときだけ計算する（赤字からの回復を％で表すと意味が逆転するため）。
-- 有利子負債は financials の値を優先し、無ければ細目の合計（interest_bearing_debt_basis='reported'）、さらに無ければ
-- 借入金・社債・CPの合計（'estimated'。リース負債は含まない概算）。
-- 多面的な指標（式・単位・注意の正本は SPEC_bi_marts §11）:
--   資本効率 roic（NOPAT÷(有利子負債＋純資産)の期末ベース。税率は effective_tax_rate が 5〜50% なら報告値、無ければ法定実効税率の近似 30.6%）・営業CF率・FCF率・粗利率の前年差、
--   企業価値倍率 ev（時価総額＋有利子負債−現金）・ev_ebitda（EBITDA＝営業利益＋減価償却費）・ev_sales・psr・peg（PER÷純利益3年CAGR%）・fcf_yield・earnings_yield、
--   資産の裏付け re_*（bi.m_re_holdings_pref の最新年度を会社で合計した土地の含み益目安。補正係数と施設ごとの簿価20倍超除外はそちらで適用済み。
--   開示時価〔real_estate.rental_property_fv〕がある会社は賃貸分を開示の含み益に置き換える。re_unrealized_gain が置き換え後、re_unrealized_gain_est が推定だけ、根拠は re_gain_basis）・adj_pbr・asset_value_ratio、
--   安全性と質 interest_coverage・current_ratio・goodwill_to_equity・accrual_ratio・f_score（ピオトロスキー。前年が取れる項目だけ）、
--   還元 buyback_est（自己株式簿価の増加＋消却額。簿価は会社により負値（純資産の控除）で入るので絶対値で比べる。下限寄りの推定）・total_payout_yield・doe、成長の質 op_income_cagr_3y・net_income_cagr_3y・consec_up_years。
-- 含み益が時価総額の3倍超の会社は re_gain_flag='gt_3x_mcap' とし、含み益を使う派生列（re_gain_to_mcap・re_land_value_to_mcap・adj_pbr・asset_value_ratio・re_gain_after_tax）を空にする（再取得・単位違いの疑い）。re_unrealized_gain は元の値のまま残す。
-- bi.m_re_holdings_pref は bi_export が前回実体化した表を読む（数MB。マート名順で m_re_holdings_pref が先に作られるので通常は当日分）。
-- PER・PBR・配当利回りが意味を持たない会社は valuation_flag で理由を示し、per・pbr・dividend_yield を NULL にする（規則は SPEC_bi_marts §11）。
WITH items_raw AS (
  SELECT edinet_code, fiscal_year,
    MAX(IF(item = 'gross_profit', value, NULL)) AS gross_profit,
    MAX(IF(item = 'rnd_expenses', value, NULL)) AS rnd_expenses,
    MAX(IF(item = 'eps', value, NULL)) AS eps,
    MAX(IF(item = 'adjusted_eps', value, NULL)) AS adjusted_eps,
    MAX(IF(item = 'bps', value, NULL)) AS bps,
    MAX(IF(item = 'adjusted_bps', value, NULL)) AS adjusted_bps,
    MAX(IF(item = 'dividend_per_share', value, NULL)) AS dividend_per_share,
    MAX(IF(item = 'adjusted_dividend_per_share', value, NULL)) AS adjusted_dividend_per_share,
    MAX(IF(item = 'payout_ratio', value, NULL)) AS payout_ratio,
    MAX(IF(item = 'num_employees', value, NULL)) AS num_employees,
    MAX(IF(item = 'shares_issued', value, NULL)) AS shares_issued,
    MAX(IF(item = 'treasury_end_period_holding_count', value, NULL)) AS treasury_shares,
    MAX(IF(item = 'ibd_current', value, NULL)) AS ibd_current,
    MAX(IF(item = 'ibd_noncurrent', value, NULL)) AS ibd_noncurrent,
    MAX(IF(item = 'ibd_total_unsplit_ifrs', value, NULL)) AS ibd_total,
    MAX(IF(item = 'short_term_loans', value, NULL)) AS short_term_loans,
    MAX(IF(item = 'long_term_loans', value, NULL)) AS long_term_loans,
    MAX(IF(item = 'current_portion_lt_loans', value, NULL)) AS current_portion_lt_loans,
    MAX(IF(item = 'bonds_payable', value, NULL)) AS bonds_payable,
    MAX(IF(item = 'current_portion_bonds_payable', value, NULL)) AS current_portion_bonds_payable,
    MAX(IF(item = 'short_term_bonds_payable', value, NULL)) AS short_term_bonds_payable,
    MAX(IF(item = 'commercial_papers', value, NULL)) AS commercial_papers,
    MAX(IF(item = 'effective_tax_rate', value, NULL)) AS effective_tax_rate,
    MAX(IF(item = 'interest_expenses', value, NULL)) AS interest_expenses,
    MAX(IF(item = 'current_assets', value, NULL)) AS current_assets,
    MAX(IF(item = 'current_liabilities', value, NULL)) AS current_liabilities,
    MAX(IF(item = 'goodwill', value, NULL)) AS goodwill,
    MAX(IF(item = 'cash_dividends_paid', value, NULL)) AS cash_dividends_paid,
    MAX(IF(item = 'treasury_stock', value, NULL)) AS treasury_stock,
    MAX(IF(item = 'treasury_cancelled_amount', value, NULL)) AS treasury_cancelled
  FROM `stock-analyzer-edinet.edinet.financial_items`
  WHERE item IN ('gross_profit', 'rnd_expenses', 'eps', 'adjusted_eps', 'bps', 'adjusted_bps', 'dividend_per_share',
                 'adjusted_dividend_per_share', 'payout_ratio',
                 'num_employees', 'shares_issued', 'treasury_end_period_holding_count', 'ibd_current', 'ibd_noncurrent',
                 'ibd_total_unsplit_ifrs', 'short_term_loans', 'long_term_loans', 'current_portion_lt_loans', 'bonds_payable',
                 'current_portion_bonds_payable', 'short_term_bonds_payable', 'commercial_papers',
                 'effective_tax_rate', 'interest_expenses', 'current_assets', 'current_liabilities', 'goodwill',
                 'cash_dividends_paid', 'treasury_stock', 'treasury_cancelled_amount')
  GROUP BY edinet_code, fiscal_year
),
items AS (
  SELECT *,
    IF(COALESCE(short_term_loans, long_term_loans, current_portion_lt_loans, bonds_payable, current_portion_bonds_payable, short_term_bonds_payable, commercial_papers) IS NULL, NULL,
       COALESCE(short_term_loans, 0) + COALESCE(long_term_loans, 0) + COALESCE(current_portion_lt_loans, 0) + COALESCE(bonds_payable, 0) + COALESCE(current_portion_bonds_payable, 0) + COALESCE(short_term_bonds_payable, 0) + COALESCE(commercial_papers, 0)) AS loan_debt,
    -- 期末後の株式分割・併合の倍率（調整前EPS÷調整後EPS。BPSで代用。差が1%以内なら分割なし）
    COALESCE(
      IF(eps IS NOT NULL AND adjusted_eps IS NOT NULL AND eps != 0 AND adjusted_eps != 0 AND eps / adjusted_eps > 0 AND ABS(eps / adjusted_eps - 1) > 0.01, eps / adjusted_eps, NULL),
      IF(bps IS NOT NULL AND adjusted_bps IS NOT NULL AND bps != 0 AND adjusted_bps != 0 AND bps / adjusted_bps > 0 AND ABS(bps / adjusted_bps - 1) > 0.01, bps / adjusted_bps, NULL),
      1.0) AS split_factor
  FROM items_raw
),
fin AS (
  SELECT edinet_code, fiscal_year, fiscal_year_end, net_sales, operating_income, ordinary_income, net_income,
         total_assets, equity, interest_bearing_debt, cash_and_equiv, employee_count, avg_annual_salary, roe, roa, operating_margin,
         operating_cf, capex, depreciation
  FROM `stock-analyzer-edinet.edinet.financials`
),
-- 増収増益（売上・営業利益がともに前年超え。前年がちょうど1年前の年度にあるときだけ）が何期続いているか。途切れた期の翌期から数える
grow_flag AS (
  SELECT edinet_code, fiscal_year,
    COALESCE(fiscal_year - LAG(fiscal_year) OVER w = 1
             AND net_sales > LAG(net_sales) OVER w
             AND operating_income > LAG(operating_income) OVER w, FALSE) AS up
  FROM fin
  WINDOW w AS (PARTITION BY edinet_code ORDER BY fiscal_year)
),
grow AS (
  SELECT edinet_code, fiscal_year,
    IF(up, fiscal_year - MAX(IF(NOT up, fiscal_year, NULL)) OVER (PARTITION BY edinet_code ORDER BY fiscal_year ROWS UNBOUNDED PRECEDING), 0) AS streak
  FROM grow_flag
),
-- 土地の含み益目安は最新年度を会社で合計（円）。unrealized は土地簿価のある施設だけの（時価目安−簿価）なので、簿価の無い施設（不動産業の販売用不動産など）は入らない
re_est AS (
  SELECT edinet_code,
    fiscal_year AS re_fiscal_year,
    SUM(facility_count) AS re_facility_count,
    SUM(est_excluded_count) AS re_est_excluded_count,
    SUM(land_value_est_m_yen) * 1e6 AS re_land_value_est,
    SUM(unrealized_gain_est_m_yen) * 1e6 AS re_gain_est
  FROM `stock-analyzer-edinet.bi.m_re_holdings_pref`
  GROUP BY edinet_code, fiscal_year
  QUALIFY fiscal_year = MAX(fiscal_year) OVER (PARTITION BY edinet_code)
),
-- 保有不動産（主要設備）の最新年度の簿価（土地＋建物・円）。賃貸分の割合の分母
re_book AS (
  SELECT edinet_code, (SUM(IFNULL(book_value_land_m_yen, 0)) + SUM(IFNULL(book_value_buildings_m_yen, 0))) * 1e6 AS re_book_total
  FROM (
    SELECT edinet_code, book_value_land_m_yen, book_value_buildings_m_yen
    FROM `stock-analyzer-edinet.real_estate.corporate_holdings_p1`
    WHERE TRUE
    QUALIFY fiscal_year = MAX(fiscal_year) OVER (PARTITION BY edinet_code)
  )
  GROUP BY edinet_code
),
-- 開示された時価（rental_property_fv。EdinetDB の賃貸等不動産＝日本基準と、政府 EDINET の IFRS 投資不動産の公正価値）。
-- 年度：会社ごとに時価のある最新年度を1行。今年から2年より前の年度は、保有不動産の今の姿と離れるので使わない。
-- 単位：表は百万円で、ここで円にする。時価÷簿価が 0.1〜30 の外の行は抽出誤り・単位違いの疑いで使わない（EdinetDB 側の除外と同じ範囲）。
-- source：キー（edinet_code, fiscal_year）で1行なので通常は重ならない。重なったときは政府 EDINET を先にする
--   （有報の XBRL から直接読んだ値で、EdinetDB は IFRS の公正価値を抽出しないため）。
rental AS (
  SELECT edinet_code, fiscal_year AS re_rental_fv_year, source AS re_rental_fv_source,
    book_value_m_yen * 1e6 AS rental_book, fair_value_m_yen * 1e6 AS rental_fair
  FROM `stock-analyzer-edinet.real_estate.rental_property_fv`
  WHERE fair_value_m_yen > 0 AND book_value_m_yen > 0
    AND fair_value_m_yen / book_value_m_yen BETWEEN 0.1 AND 30
    AND fiscal_year >= EXTRACT(YEAR FROM CURRENT_DATE('Asia/Tokyo')) - 2
  QUALIFY ROW_NUMBER() OVER (PARTITION BY edinet_code ORDER BY fiscal_year DESC, IF(source = 'edinet_gov_xbrl', 0, 1)) = 1
),
-- 推定の含み益のうち賃貸分を開示の含み益（時価−簿価）に置き換える（real_estate_signal.blend_disclosed_gain と同じ規則）。
-- どの施設が賃貸用かは分からないので、賃貸分の簿価が保有不動産の簿価に占める割合（re_rental_share、上限1）だけ推定を減らす。
-- 賃貸分が主要設備に載っていなければ割合が大きめに出て推定を多めに外す（二重に数えない側）。保有不動産の簿価が無ければ割合1。
re_mix AS (
  SELECT COALESCE(e.edinet_code, r.edinet_code) AS edinet_code,
    COALESCE(e.re_fiscal_year, r.re_rental_fv_year) AS re_fiscal_year,
    e.re_facility_count, e.re_est_excluded_count, e.re_land_value_est, e.re_gain_est,
    r.re_rental_fv_year, r.re_rental_fv_source, r.rental_book, r.rental_fair,
    IF(r.edinet_code IS NULL, NULL, IF(b.re_book_total > 0, LEAST(1, r.rental_book / b.re_book_total), 1)) AS re_rental_share
  FROM re_est AS e
  FULL OUTER JOIN rental AS r ON r.edinet_code = e.edinet_code
  LEFT JOIN re_book AS b ON b.edinet_code = COALESCE(e.edinet_code, r.edinet_code)
),
re AS (
  SELECT edinet_code, re_fiscal_year, re_facility_count, re_est_excluded_count, re_land_value_est, re_gain_est,
    re_rental_fv_year, re_rental_fv_source, re_rental_share,
    rental_fair - rental_book AS re_rental_gain,
    CASE WHEN re_rental_share IS NULL THEN re_gain_est
         WHEN re_gain_est IS NULL OR re_rental_share >= 1 THEN rental_fair - rental_book
         ELSE re_gain_est * (1 - re_rental_share) + (rental_fair - rental_book) END AS re_gain_raw,
    CASE WHEN re_rental_share IS NULL THEN IF(re_gain_est IS NULL, NULL, 'estimate')
         WHEN re_gain_est IS NULL OR re_rental_share >= 1 THEN 'disclosed'
         ELSE 'estimate+disclosed' END AS re_gain_basis
  FROM re_mix
),
latest AS (
  SELECT * FROM fin WHERE net_sales IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY edinet_code ORDER BY fiscal_year DESC) = 1
),
snap AS (
  SELECT * FROM (
    SELECT ticker, snapshot_date, ingested_at, composite_score, value_score, quality_score,
           MAX(snapshot_date) OVER () AS max_d
    FROM `stock-analyzer-edinet.analytics.stock_snapshot`
    WHERE market = 'JP'
  )
  WHERE snapshot_date >= DATE_SUB(max_d, INTERVAL 14 DAY)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY snapshot_date DESC, ingested_at DESC) = 1
),
tech AS (
  SELECT * FROM (
    SELECT ticker, snapshot_date, rsi14, above_ma200, MAX(snapshot_date) OVER () AS max_d
    FROM `stock-analyzer-edinet.analytics.stock_snapshot`
    WHERE market = 'JP' AND rsi14 IS NOT NULL
  )
  WHERE snapshot_date >= DATE_SUB(max_d, INTERVAL 14 DAY)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY snapshot_date DESC) = 1
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
px_rows AS (
  SELECT ticker, trade_date, close, volume, break_date,
         ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY trade_date DESC) AS rn,
         MAX(trade_date) OVER (PARTITION BY ticker) AS tm,
         MAX(trade_date) OVER () AS max_d
  FROM px_brk
  WHERE break_date IS NULL OR trade_date >= break_date
),
px AS (
  SELECT ticker,
    MAX(IF(rn = 1, trade_date, NULL)) AS price_date,
    MAX(IF(rn = 1, close, NULL)) AS close,
    MAX(IF(rn = 22, close, NULL)) AS c_1m,
    MAX(IF(rn = 64, close, NULL)) AS c_3m,
    COALESCE(MAX(IF(rn = 253, close, NULL)),
             ARRAY_AGG(IF(trade_date <= DATE_SUB(tm, INTERVAL 360 DAY), close, NULL) IGNORE NULLS ORDER BY trade_date DESC LIMIT 1)[SAFE_OFFSET(0)]) AS c_1y,
    COUNT(*) AS price_days,
    MAX(break_date) AS break_date,
    MAX(IF(rn <= 252, close, NULL)) AS hi_52w,
    MIN(IF(rn <= 252, close, NULL)) AS lo_52w,
    IF(COUNTIF(rn <= 20) >= 15, AVG(IF(rn <= 20, close * volume, NULL)), NULL) AS turnover_20d
  FROM px_rows
  WHERE tm >= DATE_SUB(max_d, INTERVAL 14 DAY)
  GROUP BY ticker
),
j AS (
  SELECT
    c.edinet_code AS edinet_code,
    SUBSTR(c.sec_code, 1, 4) AS sec_code4,
    CASE WHEN c.sec_code IS NOT NULL THEN CONCAT(SUBSTR(c.sec_code, 1, 4), '.T') END AS ticker,
    c.name AS name,
    c.industry AS industry,
    c.accounting_standard AS accounting_standard,
    l.fiscal_year AS fiscal_year,
    l.fiscal_year_end AS fiscal_year_end,
    l.net_sales AS net_sales,
    l.operating_income AS operating_income,
    l.ordinary_income AS ordinary_income,
    l.net_income AS net_income,
    l.total_assets AS total_assets,
    l.equity AS equity,
    COALESCE(i.num_employees, l.employee_count) AS employees,
    l.avg_annual_salary AS avg_annual_salary,
    l.operating_income / NULLIF(l.net_sales, 0) AS operating_margin,
    l.net_income / NULLIF(l.net_sales, 0) AS net_margin,
    i.gross_profit / NULLIF(l.net_sales, 0) AS gross_margin,
    l.roe AS roe,
    l.roa AS roa,
    i.rnd_expenses / NULLIF(l.net_sales, 0) AS rnd_ratio,
    l.equity / NULLIF(l.total_assets, 0) AS equity_ratio,
    ibd.v AS interest_bearing_debt,
    ibd.basis AS interest_bearing_debt_basis,
    l.cash_and_equiv AS cash_and_equiv,
    l.cash_and_equiv - ibd.v AS net_cash,
    ibd.v / NULLIF(l.equity, 0) AS de_ratio,
    IF(p1.net_sales > 0, l.net_sales / p1.net_sales - 1, NULL) AS sales_growth_1y,
    IF(p1.operating_income > 0, l.operating_income / p1.operating_income - 1, NULL) AS op_income_growth_1y,
    IF(p1.net_income > 0, l.net_income / p1.net_income - 1, NULL) AS net_income_growth_1y,
    IF(p3.net_sales > 0 AND l.net_sales > 0, POWER(l.net_sales / p3.net_sales, 1.0 / 3) - 1, NULL) AS sales_cagr_3y,
    COALESCE(i.adjusted_eps, i.eps) AS eps,
    COALESCE(i.adjusted_bps, i.bps) AS bps,
    COALESCE(i.adjusted_dividend_per_share, i.dividend_per_share) AS dividend_per_share,
    i.payout_ratio AS payout_ratio,
    i.shares_issued AS shares_issued,
    (i.shares_issued - COALESCE(i.treasury_shares, 0)) * i.split_factor AS shares_out,
    IF(x.ticker IS NOT NULL, 'price_daily', NULL) AS price_source,
    x.close AS current_price,
    x.price_date AS price_date,
    s.snapshot_date AS snapshot_date,
    x.close * (i.shares_issued - COALESCE(i.treasury_shares, 0)) * i.split_factor AS mcap,
    x.close / x.c_1m - 1 AS return_1m,
    x.close / x.c_3m - 1 AS return_3m,
    x.close / x.c_1y - 1 AS return_1y,
    IF(x.price_days >= 120, x.hi_52w, NULL) AS high_52w,
    IF(x.price_days >= 120, x.lo_52w, NULL) AS low_52w,
    IF(x.price_days >= 120 AND x.hi_52w > x.lo_52w, (x.close - x.lo_52w) / (x.hi_52w - x.lo_52w) * 100, NULL) AS week52_position_pct,
    x.turnover_20d AS avg_turnover_20d,
    x.price_days AS price_days,
    x.break_date AS break_date,
    s.composite_score AS composite_score,
    s.value_score AS value_score,
    s.quality_score AS quality_score,
    t.rsi14 AS rsi14,
    t.above_ma200 AS above_ma200,
    t.snapshot_date AS technical_date,
    l.operating_cf AS operating_cf,
    l.capex AS capex,
    l.depreciation AS depreciation,
    IF(i.effective_tax_rate BETWEEN 0.05 AND 0.5, i.effective_tax_rate, NULL) AS tax_rate_reported,
    i.interest_expenses AS interest_expenses,
    i.current_assets AS current_assets,
    i.current_liabilities AS current_liabilities,
    i.goodwill AS goodwill,
    i.cash_dividends_paid AS cash_dividends_paid,
    i.treasury_stock AS treasury_stock,
    ip1.treasury_stock AS treasury_stock_p1,
    i.treasury_cancelled AS treasury_cancelled,
    i.gross_profit AS gp,
    ip1.gross_profit AS gp_p1,
    p1.net_sales AS sales_p1,
    p1.net_income AS ni_p1,
    p1.total_assets AS ta_p1,
    ip1.current_assets AS ca_p1,
    ip1.current_liabilities AS cl_p1,
    ip1.shares_issued AS shares_issued_p1,
    COALESCE(p1.interest_bearing_debt, ip1.ibd_current + ip1.ibd_noncurrent, ip1.ibd_total, ip1.loan_debt) AS ibd_p1,
    IF(p3.operating_income > 0 AND l.operating_income > 0, POWER(l.operating_income / p3.operating_income, 1.0 / 3) - 1, NULL) AS op_income_cagr_3y,
    IF(p3.net_income > 0 AND l.net_income > 0, POWER(l.net_income / p3.net_income, 1.0 / 3) - 1, NULL) AS net_income_cagr_3y,
    gr.streak AS consec_up_years,
    r.re_fiscal_year AS re_fiscal_year,
    r.re_facility_count AS re_facility_count,
    r.re_est_excluded_count AS re_est_excluded_count,
    r.re_land_value_est AS re_land_value_est,
    r.re_gain_est AS re_gain_est,
    r.re_rental_fv_year AS re_rental_fv_year,
    r.re_rental_fv_source AS re_rental_fv_source,
    r.re_rental_share AS re_rental_share,
    r.re_rental_gain AS re_rental_gain,
    r.re_gain_basis AS re_gain_basis,
    r.re_gain_raw AS re_gain_raw
  FROM latest AS l
  JOIN `stock-analyzer-edinet.edinet.company` AS c ON c.edinet_code = l.edinet_code
  LEFT JOIN items AS i ON i.edinet_code = l.edinet_code AND i.fiscal_year = l.fiscal_year
  CROSS JOIN UNNEST([STRUCT(COALESCE(l.interest_bearing_debt, i.ibd_current + i.ibd_noncurrent, i.ibd_total, i.loan_debt) AS v, CASE WHEN l.interest_bearing_debt IS NOT NULL OR i.ibd_current + i.ibd_noncurrent IS NOT NULL OR i.ibd_total IS NOT NULL THEN 'reported' WHEN i.loan_debt IS NOT NULL THEN 'estimated' END AS basis)]) AS ibd
  LEFT JOIN items AS ip1 ON ip1.edinet_code = l.edinet_code AND ip1.fiscal_year = l.fiscal_year - 1
  LEFT JOIN grow AS gr ON gr.edinet_code = l.edinet_code AND gr.fiscal_year = l.fiscal_year
  LEFT JOIN re AS r ON r.edinet_code = l.edinet_code
  LEFT JOIN fin AS p1 ON p1.edinet_code = l.edinet_code AND p1.fiscal_year = l.fiscal_year - 1
  LEFT JOIN fin AS p3 ON p3.edinet_code = l.edinet_code AND p3.fiscal_year = l.fiscal_year - 3
  LEFT JOIN snap AS s ON s.ticker = CONCAT(SUBSTR(c.sec_code, 1, 4), '.T')
  LEFT JOIN tech AS t ON t.ticker = CONCAT(SUBSTR(c.sec_code, 1, 4), '.T')
  LEFT JOIN px AS x ON x.ticker = CONCAT(SUBSTR(c.sec_code, 1, 4), '.T')
  WHERE COALESCE(c.is_delisted, FALSE) = FALSE
),
v AS (
  SELECT *,
    IF(current_price IS NOT NULL AND eps > 0, current_price / eps, NULL) AS per_raw,
    IF(current_price IS NOT NULL AND bps > 0, current_price / bps, NULL) AS pbr_raw,
    IF(current_price IS NOT NULL AND dividend_per_share IS NOT NULL, dividend_per_share / current_price, NULL) AS yield_raw
  FROM j
),
d1 AS (
  SELECT *,
    COALESCE(tax_rate_reported, 0.306) AS tax_rate,
    IF(tax_rate_reported IS NOT NULL, 'reported', 'assumed') AS roic_tax_basis,
    operating_income + depreciation AS ebitda,
    operating_cf - capex AS fcf,
    mcap + interest_bearing_debt - cash_and_equiv AS ev,
    COALESCE(cash_dividends_paid, dividend_per_share * shares_out) AS dividends_paid,
    IF(treasury_stock IS NOT NULL AND treasury_stock_p1 IS NOT NULL, GREATEST(0, ABS(treasury_stock) - ABS(treasury_stock_p1) + ABS(COALESCE(treasury_cancelled, 0))), NULL) AS buyback_est,
    IF(re_gain_raw IS NOT NULL AND mcap > 0 AND re_gain_raw > 3 * mcap, 'gt_3x_mcap', NULL) AS re_gain_flag,
    IF(re_gain_raw IS NOT NULL AND NOT (mcap > 0 AND re_gain_raw > 3 * mcap), re_gain_raw, NULL) AS re_gain_use,
    IF(interest_bearing_debt IS NOT NULL AND interest_bearing_debt + equity > 0, interest_bearing_debt + equity, NULL) AS invested_capital
  FROM v
),
d2 AS (
  SELECT *,
    operating_income * (1 - tax_rate) AS nopat,
    operating_income * (1 - tax_rate) / invested_capital AS roic,
    operating_cf / NULLIF(net_sales, 0) AS ocf_margin,
    fcf / NULLIF(net_sales, 0) AS fcf_margin,
    gp / NULLIF(net_sales, 0) - gp_p1 / NULLIF(sales_p1, 0) AS gross_margin_chg_1y,
    IF(ev > 0 AND ebitda > 0, ev / ebitda, NULL) AS ev_ebitda,
    IF(ev > 0, ev / NULLIF(net_sales, 0), NULL) AS ev_sales,
    IF(mcap > 0, mcap / NULLIF(net_sales, 0), NULL) AS psr,
    IF(mcap > 0, fcf / mcap, NULL) AS fcf_yield,
    IF(interest_expenses > 0, operating_income / interest_expenses, NULL) AS interest_coverage,
    current_assets / NULLIF(current_liabilities, 0) AS current_ratio,
    IF(equity > 0, goodwill / equity, NULL) AS goodwill_to_equity,
    (net_income - operating_cf) / NULLIF(total_assets, 0) AS accrual_ratio,
    IF(equity > 0, dividends_paid / equity, NULL) AS doe,
    IF(mcap > 0, buyback_est / mcap, NULL) AS buyback_yield,
    IF(mcap > 0 AND dividends_paid IS NOT NULL, (dividends_paid + COALESCE(buyback_est, 0)) / mcap, NULL) AS total_payout_yield,
    IF(mcap > 0, re_gain_use / mcap, NULL) AS re_gain_to_mcap,
    IF(mcap > 0 AND re_gain_use IS NOT NULL, re_land_value_est / mcap, NULL) AS re_land_value_to_mcap,
    re_gain_use * (1 - 0.306) AS re_gain_after_tax,
    IF(mcap > 0 AND equity + re_gain_use * (1 - 0.306) > 0, equity + re_gain_use * (1 - 0.306), NULL) AS adj_equity,
    IF(mcap > 0 AND re_gain_use IS NOT NULL AND net_cash IS NOT NULL, (net_cash + re_gain_use * (1 - 0.306)) / mcap, NULL) AS asset_value_ratio,
    CAST(net_income / NULLIF(total_assets, 0) > 0 AS INT64) AS f1,
    CAST(operating_cf > 0 AS INT64) AS f2,
    CAST(net_income / NULLIF(total_assets, 0) > ni_p1 / NULLIF(ta_p1, 0) AS INT64) AS f3,
    CAST(operating_cf > net_income AS INT64) AS f4,
    CAST(interest_bearing_debt / NULLIF(total_assets, 0) <= ibd_p1 / NULLIF(ta_p1, 0) AS INT64) AS f5,
    CAST(current_assets / NULLIF(current_liabilities, 0) > ca_p1 / NULLIF(cl_p1, 0) AS INT64) AS f6,
    CAST(shares_issued / NULLIF(shares_issued_p1, 0) <= 1.01 OR shares_issued / NULLIF(shares_issued_p1, 0) >= 1.2 AS INT64) AS f7,
    CAST(gp / NULLIF(net_sales, 0) > gp_p1 / NULLIF(sales_p1, 0) AS INT64) AS f8,
    CAST(net_sales / NULLIF(total_assets, 0) > sales_p1 / NULLIF(ta_p1, 0) AS INT64) AS f9
  FROM d1
),
f AS (
  SELECT *,
    CASE
      -- EPS×株数が純利益と合わない（株数・EPSの単位不整合）
      WHEN eps > 0 AND net_income > 0 AND shares_out > 0 AND (eps * shares_out / net_income < 0.5 OR eps * shares_out / net_income > 2) THEN 'eps_mismatch'
      -- 純利益が純資産の半分を超え PER が3倍未満＝負ののれん発生益など一過性の利益で EPS が膨らんでいる
      WHEN per_raw < 3 AND equity > 0 AND net_income / equity > 0.5 THEN 'one_off_profit'
      -- 配当利回り15%超は特別配当・単位違いの疑い
      WHEN yield_raw > 0.15 THEN 'yield_outlier'
    END AS valuation_flag
  FROM d2
),
g AS (
  SELECT *,
    IF(valuation_flag IN ('eps_mismatch', 'one_off_profit'), NULL, per_raw) AS per_final,
    IF(adj_equity > 0, mcap / adj_equity, NULL) AS adj_pbr,
    IF(f1 IS NULL, 0, 1) + IF(f2 IS NULL, 0, 1) + IF(f3 IS NULL, 0, 1) + IF(f4 IS NULL, 0, 1) + IF(f5 IS NULL, 0, 1) + IF(f6 IS NULL, 0, 1) + IF(f7 IS NULL, 0, 1) + IF(f8 IS NULL, 0, 1) + IF(f9 IS NULL, 0, 1) AS f_items,
    COALESCE(f1, 0) + COALESCE(f2, 0) + COALESCE(f3, 0) + COALESCE(f4, 0) + COALESCE(f5, 0) + COALESCE(f6, 0) + COALESCE(f7, 0) + COALESCE(f8, 0) + COALESCE(f9, 0) AS f_sum
  FROM f
)
SELECT
  edinet_code,
  sec_code4,
  ticker,
  name,
  industry,
  accounting_standard,
  fiscal_year,
  fiscal_year_end,
  net_sales,
  operating_income,
  ordinary_income,
  net_income,
  total_assets,
  equity,
  employees,
  avg_annual_salary,
  operating_margin,
  net_margin,
  gross_margin,
  roe,
  roa,
  rnd_ratio,
  equity_ratio,
  interest_bearing_debt,
  interest_bearing_debt_basis,
  cash_and_equiv,
  net_cash,
  de_ratio,
  sales_growth_1y,
  op_income_growth_1y,
  net_income_growth_1y,
  sales_cagr_3y,
  eps,
  bps,
  dividend_per_share,
  payout_ratio,
  shares_issued,
  shares_out,
  price_source,
  current_price,
  price_date,
  snapshot_date,
  mcap / 1e8 AS market_cap_b,
  mcap AS market_cap,
  IF(valuation_flag IN ('eps_mismatch', 'one_off_profit'), NULL, per_raw) AS per,
  pbr_raw AS pbr,
  IF(valuation_flag = 'yield_outlier', NULL, yield_raw) AS dividend_yield,
  valuation_flag,
  IF(net_cash IS NOT NULL AND mcap > 0, net_cash / mcap, NULL) AS net_cash_to_mcap,
  return_1m,
  return_3m,
  return_1y,
  high_52w,
  low_52w,
  week52_position_pct,
  avg_turnover_20d,
  price_days,
  break_date AS price_break_date,
  composite_score,
  value_score,
  quality_score,
  rsi14,
  above_ma200,
  technical_date,
  nopat,
  invested_capital,
  tax_rate,
  roic_tax_basis,
  roic,
  ocf_margin,
  fcf_margin,
  gross_margin_chg_1y,
  operating_cf,
  fcf,
  ebitda,
  ev,
  ev_ebitda,
  ev_sales,
  psr,
  IF(per_final > 0 AND net_income_cagr_3y > 0, per_final / (net_income_cagr_3y * 100), NULL) AS peg,
  fcf_yield,
  IF(valuation_flag IN ('eps_mismatch', 'one_off_profit') OR current_price IS NULL, NULL, eps / current_price) AS earnings_yield,
  re_fiscal_year,
  re_facility_count,
  re_est_excluded_count,
  re_land_value_est,
  re_gain_raw AS re_unrealized_gain,
  re_gain_est AS re_unrealized_gain_est,
  re_rental_gain,
  re_rental_share,
  re_rental_fv_year,
  re_rental_fv_source,
  re_gain_basis,
  re_gain_flag,
  re_gain_to_mcap,
  re_land_value_to_mcap,
  re_gain_after_tax,
  adj_equity,
  adj_pbr,
  asset_value_ratio,
  interest_coverage,
  current_ratio,
  goodwill_to_equity,
  accrual_ratio,
  IF(f_items >= 7, f_sum, NULL) AS f_score,
  f_items AS f_score_items,
  buyback_est,
  buyback_yield,
  total_payout_yield,
  doe,
  op_income_cagr_3y,
  net_income_cagr_3y,
  consec_up_years
FROM g
