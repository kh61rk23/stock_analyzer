# ==============================================================
# us_screener_universe.py
# 米国株スクリーナーの一次採点（全上場を採点し、上位だけ二次評価＝us_stock_screener の詳細評価へ回す）
# ==============================================================
# 【なぜ】
#   二次評価（us_stock_screener）は銘柄ごとに yfinance を引くため、全上場（約5,000社）には回せない。
#   手で選んだテーマ別リスト（data/custom_universe/）だけを見ると、リストに無い会社は永久に候補にならない。
#   そこで日本株（screener_universe.py）と同じく、BQ の全上場を先に採点して上位だけを回す。
# 【原則】日本の一次採点と同じ11観点・同じ重み（config.SCREEN_UNIVERSE_WEIGHTS）・同じ順位化と平滑化の関数を使う。
#   違うのは材料だけ（日本は bi.m_screener、米国は SEC の XBRL から作った us.financials_annual 等）。
#   値の無い観点は中立50にして coverage に記録する。規模（時価総額）はどの観点にも入れず、足切りもしない。
# 【入力】BQ 2本（表の置き場所が東京と US に分かれており、1本の SQL では結合できないため）。ループ内で BQ を引かない。
#   東京：analytics.price_daily_latest（米国分の日足）・analytics.short_interest（FINRA の空売り残）・
#         geopolitics.company_country_exposure × country_risk_daily（国のリスクの重み付き平均）
#   US  ：us.company_profile（会社一覧・株数・直近12か月）・us.financials_annual（年次の財務）・
#         us.fundamentals（EPS の加速）・us.sec_13dg（13D）・us.sec_filings（8-K の項目）・us.company_taxonomy_map（技術マップ）
#   価格は trade_date < 実行日（前営業日の終値まで）。
# 【出力】run_us_universe() の dict、BQ analytics.screener_universe_us_daily（日付パーティション・同日は置き換え）。
# ==============================================================
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from pathlib import Path

import screener_universe as su

TABLE = "screener_universe_us_daily"
MARKET_TICKER = "SPY"
_GIB = 1024 ** 3

# 観点ごとの順位化する列（向き: True=大きいほど良い）。観点の名前と意味は日本（su.RANK_COLUMNS）と同じ。
RANK_COLUMNS = {
    "move":     [("ret_1d", True), ("vol_ratio", True), ("dist_high20", True)],
    "momentum": [("r1m", True), ("r3m", True), ("mom_12_1", True), ("near_high52", True)],
    "technical": [],
    # 日本の資産バリュー倍率（不動産の含み益込み）の代わりに、流動資産−負債合計（ネットネット）を使う
    "asset":    [("ncav_to_mcap", True), ("net_cash_to_mcap", True), ("pbr", False)],
    "quality":  [("roic", True), ("ocf_margin", True), ("fcf_margin", True), ("f_score", True),
                 ("interest_coverage", True)],
    "growth":   [("sales_growth_1y", True), ("op_income_growth_1y", True), ("op_income_cagr_3y", True),
                 ("consec_up_years", True)],
    "value":    [("earnings_yield", True), ("fcf_yield", True), ("total_payout_yield", True), ("ev_ebitda", False)],
    # 空売り残は FINRA が取引所の上場株をほぼ全部出すので順位化する（多い・増えた・買い戻しに日数がかかるほど悪い）
    "flow":     [("short_ratio", False), ("si_change_pct", False), ("days_to_cover", False)],
    "theme":    [("theme_value", True), ("theme_sales_g", True)],
    # 業種スコアに、事業のある国のリスク（重み付き平均・低いほど良い）を並べる。どちらも会社の外の環境
    "sector":   [("sector_score", True), ("geo_risk", False)],
    "idio":     [("idio_vol_60", False), ("vs_theme_idio", False)],
}

# 8-K の項目のうち、悪い知らせとして質の観点から引くもの（破産・減損・上場廃止の通知・監査人の交代・過去の決算の訂正）
K8_RED_ITEMS = {"1.03", "2.06", "3.01", "4.01", "4.02"}

PRICE_SQL = """
WITH px_raw AS (
  SELECT ticker, trade_date, close, volume,
         LAG(close) OVER (PARTITION BY ticker ORDER BY trade_date) AS prev_close
  FROM `{project}.analytics.price_daily_latest`
  WHERE trade_date >= DATE_SUB(@asof, INTERVAL @price_days DAY) AND trade_date < @asof
    AND REGEXP_CONTAINS(ticker, r'^[A-Z][A-Z0-9-]{{0,9}}$') AND close > 0 AND close < 10000000
),
px_brk AS (
  SELECT *, MAX(IF(prev_close > 0 AND (close / prev_close < 0.6 OR close / prev_close > 1.7), trade_date, NULL))
              OVER (PARTITION BY ticker) AS break_date
  FROM px_raw
),
px_rows AS (
  SELECT ticker, trade_date, close, volume, break_date,
         ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY trade_date DESC) AS rn,
         MAX(trade_date) OVER (PARTITION BY ticker) AS last_date,
         MAX(trade_date) OVER () AS max_d
  FROM px_brk
  WHERE break_date IS NULL OR trade_date >= break_date
),
px AS (
  SELECT ticker, ANY_VALUE(last_date) AS last_date, ANY_VALUE(max_d) AS max_d, COUNT(*) AS price_days,
         MAX(break_date) AS break_date,
         ARRAY_AGG(IF(rn <= 260, close, NULL) IGNORE NULLS ORDER BY trade_date) AS closes,
         ARRAY_AGG(IF(rn <= 260, trade_date, NULL) IGNORE NULLS ORDER BY trade_date) AS dates,
         ARRAY_AGG(IF(rn <= 21, IFNULL(volume, 0), NULL) IGNORE NULLS ORDER BY trade_date) AS vols,
         MAX(IF(rn = 1, volume, NULL)) AS vol_1d,
         AVG(IF(rn <= 20, volume, NULL)) AS vol_avg20,
         IF(COUNTIF(rn <= 20) >= 15, AVG(IF(rn <= 20, close * volume, NULL)), NULL) AS turnover_20d
  FROM px_rows
  GROUP BY ticker
),
si AS (
  SELECT REGEXP_REPLACE(symbol, r'[./]', '-') AS ticker, short_qty, change_pct AS si_change_pct, days_to_cover
  FROM `{project}.analytics.short_interest`
  WHERE settlement_date >= DATE_SUB(@asof, INTERVAL @si_days DAY) AND settlement_date < @asof
  QUALIFY ROW_NUMBER() OVER (PARTITION BY REGEXP_REPLACE(symbol, r'[./]', '-')
                             ORDER BY settlement_date DESC, short_qty DESC) = 1
),
risk AS (
  SELECT country_code, composite_risk_score
  FROM `{project}.geopolitics.country_risk_daily`
  WHERE risk_date >= DATE_SUB(@asof, INTERVAL 30 DAY)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY country_code ORDER BY risk_date DESC) = 1
),
geo AS (
  SELECT e.ticker,
         SAFE_DIVIDE(SUM(e.exposure_strength * IFNULL(r.composite_risk_score, 0)), SUM(e.exposure_strength)) AS geo_risk
  FROM `{project}.geopolitics.company_country_exposure` AS e
  LEFT JOIN risk AS r USING (country_code)
  WHERE e.market = 'US' AND e.ticker IS NOT NULL AND e.exposure_strength > 0
    AND (e.valid_to IS NULL OR e.valid_to > @asof)
  GROUP BY e.ticker
)
SELECT x.*, s.short_qty, s.si_change_pct, s.days_to_cover, g.geo_risk
FROM px AS x
LEFT JOIN si AS s USING (ticker)
LEFT JOIN geo AS g USING (ticker)
"""

FUND_SQL = """
WITH pr AS (
  SELECT ticker, cik, name, exchange, sic, sic_description, shares_outstanding,
         ttm_period_end, ttm_revenue, ttm_net_income, ttm_operating_cf, ttm_capex,
         bs_period_end, bs_total_assets, bs_total_liabilities, bs_equity, bs_cash, bs_debt_short, bs_debt_long
  FROM `{project}.us.company_profile`
  WHERE ticker IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY fetched_at DESC) = 1
),
fa AS (
  SELECT ticker,
         ARRAY_AGG(STRUCT(period_end, revenue, operating_income, net_income, total_assets, total_liabilities, equity,
                          cash, debt_short, debt_long, operating_cf, capex, buyback, dividends_paid, gross_profit,
                          depreciation, income_tax, pretax_income, interest_expense, shares_diluted,
                          assets_current, liabilities_current)
                   ORDER BY period_end DESC LIMIT 5) AS fy
  FROM (
    SELECT ticker, period_end, revenue, operating_income, net_income, total_assets, total_liabilities, equity,
           cash, debt_short, debt_long, operating_cf, capex, buyback, dividends_paid, gross_profit,
           depreciation, income_tax, pretax_income, interest_expense, shares_diluted,
           assets_current, liabilities_current
    FROM `{project}.us.financials_annual`
    WHERE period_end >= DATE_SUB(@asof, INTERVAL 7 YEAR)
    QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker, period_end ORDER BY updated_at DESC) = 1
  )
  GROUP BY ticker
),
fu AS (
  SELECT ticker, eps_accel_score_xbrl
  FROM `{project}.us.fundamentals`
  WHERE eps_accel_score_xbrl IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY fetched_at DESC) = 1
),
nm AS (
  SELECT DISTINCT cik, UPPER(REGEXP_REPLACE(name, r'[^A-Za-z0-9]', '')) AS nm FROM pr WHERE cik IS NOT NULL
),
dgr AS (   -- 発行会社の CIK が空の行（13D の XML のタグ名の違いで取り損ねた分）は会社名で引く
  SELECT accession, issuer_cik, UPPER(REGEXP_REPLACE(issuer_name, r'[^A-Za-z0-9]', '')) AS nm, percent
  FROM `{project}.us.sec_13dg`
  WHERE filed_date >= DATE_SUB(@asof, INTERVAL @dg_days DAY) AND filed_date < @asof
    AND STARTS_WITH(form, 'SCHEDULE 13D')
),
dg AS (
  SELECT COALESCE(d.issuer_cik, n.cik) AS cik, COUNT(DISTINCT d.accession) AS n13d, MAX(d.percent) AS max_pct_13d
  FROM dgr AS d
  LEFT JOIN nm AS n ON d.issuer_cik IS NULL AND n.nm = d.nm
  GROUP BY cik
),
k8 AS (
  SELECT cik, STRING_AGG(items, ',') AS k8_items
  FROM `{project}.us.sec_filings`
  WHERE filed_date >= DATE_SUB(@asof, INTERVAL @k8_days DAY) AND filed_date < @asof
    AND form IN ('8-K', '8-K/A') AND items IS NOT NULL
  GROUP BY cik
),
tax AS (
  SELECT company_id AS ticker, ARRAY_AGG(STRUCT(node_id, relevance_score AS relevance)) AS themes
  FROM (
    SELECT company_id, node_id, relevance_score
    FROM `{project}.us.company_taxonomy_map`
    WHERE company_id IS NOT NULL AND node_id IS NOT NULL AND relevance_score IS NOT NULL
      AND (valid_to IS NULL OR valid_to >= @asof)
    QUALIFY ROW_NUMBER() OVER (PARTITION BY company_id, node_id
                               ORDER BY IF(verified_flag, 0, 1), updated_at DESC) = 1
  )
  GROUP BY ticker
)
SELECT p.*, f.fy, u.eps_accel_score_xbrl, d.n13d, d.max_pct_13d, k.k8_items, t.themes
FROM pr AS p
LEFT JOIN fa AS f USING (ticker)
LEFT JOIN fu AS u USING (ticker)
LEFT JOIN dg AS d ON d.cik = p.cik
LEFT JOIN k8 AS k ON k.cik = p.cik
LEFT JOIN tax AS t USING (ticker)
"""


# ── 純粋ロジック ──────────────────────────────────────────

_num = su._num


def sic_to_etf(sic) -> str | None:
    """SIC（4桁）→ 米国の業種 ETF（us_sector_pipeline の11業種）。対応の無いもの（空・SPAC 等）は None。
    SIC は GICS と区切りが違うので近いものに寄せた概算（業種スコアを1観点の材料として使うだけ）。"""
    try:
        s = int(str(sic).strip())
    except (TypeError, ValueError):
        return None
    rules = (
        (100, 999, "XLP"), (1000, 1299, "XLB"), (1300, 1399, "XLE"), (1400, 1499, "XLB"), (1500, 1799, "XLI"),
        (2000, 2199, "XLP"), (2200, 2399, "XLY"), (2400, 2499, "XLB"), (2500, 2599, "XLY"), (2600, 2699, "XLB"),
        (2700, 2799, "XLC"), (2800, 2829, "XLB"), (2830, 2836, "XLV"), (2840, 2844, "XLP"), (2845, 2899, "XLB"),
        (2900, 2999, "XLE"), (3000, 3399, "XLB"), (3400, 3569, "XLI"), (3570, 3579, "XLK"), (3580, 3629, "XLI"),
        (3630, 3652, "XLY"), (3653, 3689, "XLK"), (3690, 3699, "XLI"), (3711, 3716, "XLY"), (3751, 3751, "XLY"),
        (3700, 3799, "XLI"), (3810, 3839, "XLK"), (3840, 3859, "XLV"), (3860, 3869, "XLK"), (3870, 3999, "XLY"),
        (4000, 4799, "XLI"), (4800, 4899, "XLC"), (4950, 4959, "XLI"), (4900, 4999, "XLU"), (5122, 5122, "XLV"),
        (5140, 5149, "XLP"), (5000, 5199, "XLI"), (5400, 5499, "XLP"), (5912, 5912, "XLP"), (5200, 5999, "XLY"),
        (6500, 6553, "XLRE"), (6798, 6798, "XLRE"), (6000, 6799, "XLF"), (7000, 7299, "XLY"), (7310, 7319, "XLC"),
        (7370, 7379, "XLK"), (7300, 7399, "XLI"), (7990, 7999, "XLY"), (7800, 7999, "XLC"), (8000, 8099, "XLV"),
        (8731, 8731, "XLV"), (8200, 8299, "XLY"), (8000, 8999, "XLI"),
    )
    for lo, hi, etf in rules:   # 細かい範囲を先に置いている（先に当たったものを採る）
        if lo <= s <= hi:
            return etf
    return None


_US_SYMBOL = re.compile(r"^[A-Z][A-Z0-9-]{0,9}$")   # PRICE_SQL の REGEXP_CONTAINS と同じ


def is_us_symbol(ticker) -> bool:
    """米国の記号（価格の表の対象）か。'.' 付き・取引所接尾辞（.T / .KS / .L など）の銘柄は False。"""
    return bool(_US_SYMBOL.match(str(ticker or "").strip().upper()))


def k8_red_count(items) -> int:
    """8-K の items（"1.01,2.06,9.01" など・期間内の全件をつないだもの）に含まれる悪い知らせの項目の数（種類ごとに1）。"""
    codes = {x.strip() for x in str(items or "").replace(";", ",").split(",")}
    return len(codes & K8_RED_ITEMS)


def _f(d: dict, k: str):
    return _num((d or {}).get(k))


def piotroski(fy: list) -> int | None:
    """年次の直近2期から Piotroski の F スコア（0〜9）。総資産が無い・2期そろわないときは None。
    項目が欠けた判定は満たさない側に数える。"""
    if len(fy or []) < 2:
        return None
    a, b = fy[0], fy[1]
    ta_a, ta_b = _f(a, "total_assets"), _f(b, "total_assets")
    if not ta_a or not ta_b:
        return None
    ni_a, ni_b, ocf_a = _f(a, "net_income"), _f(b, "net_income"), _f(a, "operating_cf")

    def _gt(x, y):
        return x is not None and y is not None and x > y

    def _ratio(d, n, den):
        x, y = _f(d, n), _f(d, den)
        return x / y if x is not None and y else None

    s = 0
    s += ni_a is not None and ni_a > 0
    s += ocf_a is not None and ocf_a > 0
    s += _gt(ni_a / ta_a if ni_a is not None else None, ni_b / ta_b if ni_b is not None else None)
    s += _gt(ocf_a, ni_a)
    s += _gt((_f(b, "debt_long") or 0) / ta_b, (_f(a, "debt_long") or 0) / ta_a)   # 長期の借入の比率が下がった
    s += _gt(_ratio(a, "assets_current", "liabilities_current"), _ratio(b, "assets_current", "liabilities_current"))
    sa, sb = _f(a, "shares_diluted"), _f(b, "shares_diluted")
    s += sa is not None and sb is not None and sa <= sb
    s += _gt(_ratio(a, "gross_profit", "revenue"), _ratio(b, "gross_profit", "revenue"))
    s += _gt(_ratio(a, "revenue", "total_assets"), _ratio(b, "revenue", "total_assets"))
    return int(s)


def fundamental_features(p: dict, fy: list, last_close, asof: date,
                         annual_stale_days: int = 550, ttm_stale_days: int = 270) -> dict:
    """会社の行（us.company_profile ＋ 年次 fy・新しい順）と前営業日の終値 → 日本の bi.m_screener に相当する指標。

    年次の直近期が annual_stale_days より古い会社は年次を使わない（提出が止まった会社の古い数字で順位を付けないため）。
    直近12か月（TTM）と貸借対照表は company_profile の値を優先し、古ければ年次で代える。求まらない指標は None。
    """
    fy = [dict(x) for x in (fy or [])]
    if fy and (fy[0].get("period_end") is None or (asof - fy[0]["period_end"]).days > annual_stale_days):
        fy = []
    a0 = fy[0] if fy else {}
    out: dict = {}
    close = _num(last_close)
    shares = _f(p, "shares_outstanding") or _f(a0, "shares_diluted")
    mcap = shares * close if shares and close else None
    out["market_cap"] = mcap

    def _fresh(key, days):
        d = (p or {}).get(key)
        return d is not None and (asof - d).days <= days

    ttm = _fresh("ttm_period_end", ttm_stale_days)
    rev = _f(p, "ttm_revenue") if ttm and _f(p, "ttm_revenue") is not None else _f(a0, "revenue")
    ni = _f(p, "ttm_net_income") if ttm and _f(p, "ttm_net_income") is not None else _f(a0, "net_income")
    ocf = _f(p, "ttm_operating_cf") if ttm and _f(p, "ttm_operating_cf") is not None else _f(a0, "operating_cf")
    capex = _f(p, "ttm_capex") if ttm and _f(p, "ttm_capex") is not None else _f(a0, "capex")
    fcf = ocf - abs(capex or 0.0) if ocf is not None else None

    bs = _fresh("bs_period_end", annual_stale_days)
    equity = _f(p, "bs_equity") if bs and _f(p, "bs_equity") is not None else _f(a0, "equity")
    cash = _f(p, "bs_cash") if bs and _f(p, "bs_cash") is not None else _f(a0, "cash")
    if bs and (_f(p, "bs_debt_short") is not None or _f(p, "bs_debt_long") is not None):
        debt = (_f(p, "bs_debt_short") or 0.0) + (_f(p, "bs_debt_long") or 0.0)
    elif a0:
        debt = (_f(a0, "debt_short") or 0.0) + (_f(a0, "debt_long") or 0.0)
    else:
        debt = None
    liab = _f(p, "bs_total_liabilities") if bs and _f(p, "bs_total_liabilities") is not None else _f(a0, "total_liabilities")

    # 割安
    out["earnings_yield"] = ni / mcap if mcap and ni is not None else None
    out["fcf_yield"] = fcf / mcap if mcap and fcf is not None else None
    payout = (abs(_f(a0, "buyback") or 0.0) + abs(_f(a0, "dividends_paid") or 0.0)) if a0 else None
    out["total_payout_yield"] = payout / mcap if mcap and payout is not None else None
    oi0 = _f(a0, "operating_income")
    ebitda = oi0 + (_f(a0, "depreciation") or 0.0) if oi0 is not None else None
    ev = mcap + (debt or 0.0) - (cash or 0.0) if mcap else None
    out["ev_ebitda"] = ev / ebitda if ev is not None and ebitda and ebitda > 0 else None
    # 資産の裏付け
    out["net_cash_to_mcap"] = ((cash or 0.0) - (debt or 0.0)) / mcap if mcap and (cash is not None or debt is not None) else None
    out["pbr"] = mcap / equity if mcap and equity and equity > 0 else None
    ac = _f(a0, "assets_current")
    out["ncav_to_mcap"] = (ac - liab) / mcap if mcap and ac is not None and liab is not None else None
    # 質
    pt, tx = _f(a0, "pretax_income"), _f(a0, "income_tax")
    tax = min(0.5, max(0.0, tx / pt)) if pt and pt > 0 and tx is not None else 0.21
    ic = (equity or 0.0) + (debt or 0.0) - (cash or 0.0)
    out["roic"] = oi0 * (1 - tax) / ic if oi0 is not None and equity is not None and ic > 0 else None
    out["ocf_margin"] = ocf / rev if ocf is not None and rev and rev > 0 else None
    out["fcf_margin"] = fcf / rev if fcf is not None and rev and rev > 0 else None
    out["f_score"] = piotroski(fy)
    ie = _f(a0, "interest_expense")
    out["interest_coverage"] = oi0 / abs(ie) if oi0 is not None and ie else None
    ta, ni_a, ocf_a = _f(a0, "total_assets"), _f(a0, "net_income"), _f(a0, "operating_cf")
    out["accrual_ratio"] = (ni_a - ocf_a) / ta if ta and ni_a is not None and ocf_a is not None else None
    # 成長
    revs = [_f(x, "revenue") for x in fy]
    ois = [_f(x, "operating_income") for x in fy]
    out["sales_growth_1y"] = revs[0] / revs[1] - 1 if len(revs) > 1 and revs[0] is not None and revs[1] and revs[1] > 0 else None
    out["op_income_growth_1y"] = (ois[0] / ois[1] - 1 if len(ois) > 1 and ois[0] is not None and ois[1] and ois[1] > 0
                                  else None)
    out["op_income_cagr_3y"] = ((ois[0] / ois[3]) ** (1 / 3) - 1
                                if len(ois) > 3 and ois[0] and ois[0] > 0 and ois[3] and ois[3] > 0 else None)
    up = None
    if len(ois) > 1 and ois[0] is not None and ois[1] is not None:
        up = 0
        for x, y in zip(ois, ois[1:]):
            if x is None or y is None or x <= y:
                break
            up += 1
    out["consec_up_years"] = up
    return out


def build_records(price_rows: list, fund_rows: list, cfg, ctx: dict, asof: date) -> tuple:
    """BQ の2本の行 → 採点対象の records と除外件数（日本の su.build_records と同じ形）。

    ctx: sector_scores（{業種ETF: 点}）, hotness, leading_only, market（{日付: SPY の終値}）,
         accrual_fn（stock_screener.accrual_adjustment）, tech_fn / weekly_fn（technical_scorer）
    """
    caps = getattr(cfg, "US_SCREEN_UNIVERSE_BONUS_CAPS", {}) or {}
    pts = getattr(cfg, "US_SCREEN_UNIVERSE_EVENT_PTS", {}) or {}
    min_turn = float(getattr(cfg, "US_SCREEN_UNIVERSE_MIN_TURNOVER", 500_000.0))
    stale = int(getattr(cfg, "SCREEN_UNIVERSE_STALE_DAYS", 14))
    lead_bonus = float(getattr(cfg, "SCREEN_UNIVERSE_LEADING_BONUS", 15.0))
    hotness, leading = ctx.get("hotness") or {}, ctx.get("leading_only") or []
    sector_scores = ctx.get("sector_scores") or {}
    prof = {r["ticker"]: r for r in fund_rows if r.get("ticker")}

    excluded: dict = {}
    kept = []
    for r in price_rows:
        p = prof.get(r["ticker"])
        if p is None:   # ETF・優先株・信託など、SEC の会社一覧（company_profile）に無いもの
            excluded["not_company"] = excluded.get("not_company", 0) + 1
            continue
        why = su.classify(r, min_turn, stale)
        if why:
            excluded[why] = excluded.get(why, 0) + 1
            continue
        kept.append((r, p))

    feats = {r["ticker"]: fundamental_features(p, p.get("fy"), (r.get("closes") or [None])[-1], asof) for r, p in kept}
    # 業種ごとのアクルーアル平均（日本の二次評価と同じく、業種内の平均と比べる）
    by_sector: dict = {}
    for r, p in kept:
        acr = feats[r["ticker"]].get("accrual_ratio")
        if acr is not None:
            by_sector.setdefault(sic_to_etf(p.get("sic")), []).append(acr)
    sector_mean = {c: sum(v) / len(v) for c, v in by_sector.items() if v}

    records = []
    for r, p in kept:
        t = r["ticker"]
        etf = sic_to_etf(p.get("sic"))
        f = feats[t]
        cols = dict(f)
        cols.update(su.price_features(r["closes"]))
        v20 = _num(r.get("vol_avg20"))
        cols["vol_ratio"] = (_num(r.get("vol_1d")) or 0) / v20 if v20 else None
        cols["ud_vol"] = su.updown_volume(r["closes"], r.get("vols"))
        cols["theme_value"] = su.theme_value(r.get("themes") or p.get("themes"), hotness, leading, lead_bonus)
        cols["sector_score"] = sector_scores.get(etf)
        cols["geo_risk"] = _num(r.get("geo_risk"))
        sq, sh = _num(r.get("short_qty")), _num(p.get("shares_outstanding"))
        cols["short_ratio"] = sq / sh if sq is not None and sh else None
        cols["si_change_pct"] = _num(r.get("si_change_pct"))
        cols["days_to_cover"] = _num(r.get("days_to_cover"))
        sc: dict = {a: [] for a in su.ASPECTS}
        tech = ctx["tech_fn"](r["closes"], ctx["weekly_fn"](r["closes"]), [], [])
        sc["technical"].append(_num(tech.get("score")))
        # 出来事の加点は、出来事のあった会社だけ（無い会社を 0pt にして不利にしない。日本のキャッシュの加点と同じ扱い）
        acc = _num(p.get("eps_accel_score_xbrl"))
        if acc is not None:
            sc["growth"].append(su.bonus_to_score(acc, caps.get("eps_accel", 10.0)))
        n13d = int(_num(p.get("n13d")) or 0)
        if n13d > 0:
            sc["flow"].append(su.bonus_to_score(n13d * float(pts.get("13d", 5.0)), caps.get("13d", 10.0)))
        red = k8_red_count(p.get("k8_items"))
        if red > 0:
            sc["quality"].append(su.bonus_to_score(-red * float(pts.get("k8_red", 5.0)), caps.get("k8_red", 10.0)))
        acr = f.get("accrual_ratio")
        if acr is not None and ctx.get("accrual_fn"):
            sc["quality"].append(su.bonus_to_score(ctx["accrual_fn"](acr, sector_mean.get(etf))[1],
                                                   caps.get("accrual", 20.0)))
        mc = _num(f.get("market_cap"))
        records.append({
            "ticker": t, "cik": p.get("cik"), "name": p.get("name"), "market": "US", "exchange": p.get("exchange"),
            "sector_code": etf, "sector_name": p.get("sic_description"),
            "market_cap_usd_b": round(mc / 1e9, 4) if mc else None,
            "current_price": _num((r.get("closes") or [None])[-1]),
            "turnover_20d": _num(r.get("turnover_20d")), "n13d": n13d, "k8_red": red, "cols": cols,
            "scores": {a: [v for v in vs if v is not None] for a, vs in sc.items()},
        })
    by_t = {r["ticker"]: (r, p) for r, p in kept}

    def _themes(t):
        r, p = by_t[t]
        return [(dict(x).get("node_id"), dict(x).get("relevance")) for x in (r.get("themes") or p.get("themes") or [])]

    idio = su.idio_features(
        {rec["ticker"]: (by_t[rec["ticker"]][0].get("dates"), by_t[rec["ticker"]][0].get("closes")) for rec in records},
        ctx.get("market") or {}, {rec["ticker"]: _themes(rec["ticker"]) for rec in records},
        {rec["ticker"]: rec["cols"].get("sales_growth_1y") for rec in records})
    for rec in records:
        g = idio.get(rec["ticker"]) or {}
        for k in su.IDIO_COLS:
            rec["cols"][k] = g.get(k)
    return records, excluded


def market_series(price_rows: list, ticker: str = MARKET_TICKER) -> dict:
    """東京の1本に含まれる SPY の行 → {日付: 終値}（無ければ空）。"""
    for r in price_rows:
        if r.get("ticker") == ticker:
            return {d: float(c) for d, c in zip(r.get("dates") or [], r.get("closes") or []) if _num(c)}
    return {}


def stage2_entries(stage2: list, meta: dict, keep: list) -> list:
    """二次評価に回す銘柄の一覧（us_stock_screener.load_universe と同じ形）。

    stage2: 一次上位の ticker、meta: テーマ別リストの {ticker: entry}（カテゴリ・ビジネスモデル・S&P500 の印を引き継ぐ）、
    keep: 一次採点の対象外だが二次評価に残す ticker（テーマ別リストの米国以外の銘柄。価格の表に無いので一次では採点できない）。
    """
    out, seen = [], set()
    for t in list(stage2) + list(keep):
        if t in seen:
            continue
        seen.add(t)
        out.append(dict(meta[t]) if t in meta else
                   {"ticker": t, "category": "universe", "business_model": "mixed",
                    "description": "全上場の一次採点の上位", "in_sp500": False})
    return out


def summarize(scored: list, excluded: dict, top_n: int, stage2_n: int, total_rows: int, asof: date) -> dict:
    """results["us_screener"]["universe"] に載せる要約（日本の su.summarize と同じ項目。小型の目安は時価総額20億ドル未満）。"""
    top = [{"ticker": r["ticker"], "name": r.get("name"), "sector_code": r.get("sector_code"),
            "universe_score": r["universe_score"], "universe_rank": r["universe_rank"],
            "rank_score": r.get("rank_score"), "market_cap_usd_b": r.get("market_cap_usd_b"),
            "coverage": r["coverage"], "aspects": r["aspects"]}
           for r in scored[:top_n]]
    small = sum(1 for r in scored[:top_n] if r.get("market_cap_usd_b") is not None and r["market_cap_usd_b"] < 2)
    return {
        "date": asof.isoformat(), "total_rows": total_rows, "universe_count": len(scored),
        "excluded": dict(excluded), "excluded_count": sum(excluded.values()), "stage2_n": stage2_n,
        "top": top, f"small_cap_under_2usd_b_in_top{top_n}": small,
        "aspect_distribution": su.distribution(scored),
        "coverage_mean": round(sum(r["coverage"] for r in scored) / len(scored), 3) if scored else None,
    }


def output_rows(scored: list, score_date: str, stage2: set) -> list:
    """BQ 書き込み用の行（analytics.screener_universe_us_daily）。列は日本の表と同じで、edinet_code の代わりに cik、
    時価総額は十億ドル（market_cap_usd_b）。"""
    rows = su.output_rows(scored, score_date, stage2)
    for row, r in zip(rows, scored):
        row.pop("edinet_code", None)
        row.pop("market_cap_b", None)
        row["cik"] = str(r["cik"]) if r.get("cik") is not None else None
        row["market_cap_usd_b"] = r.get("market_cap_usd_b")
    return rows


def _schema():
    from google.cloud.bigquery import SchemaField as F
    out = []
    for f in su._schema():
        if f.name == "edinet_code":
            out.append(F("cik", "STRING"))
        elif f.name == "market_cap_b":
            out.append(F("market_cap_usd_b", "FLOAT64"))
        else:
            out.append(f)
    return out


# ── 入出力 ────────────────────────────────────────────────

def _job_config(cfg, params: list):
    from google.cloud import bigquery
    jc = bigquery.QueryJobConfig(query_parameters=params)
    cap = int(getattr(cfg, "US_SCREEN_UNIVERSE_MAX_BYTES", _GIB))
    base = getattr(cfg, "BQ_MAX_BYTES_BILLED", None)
    jc.maximum_bytes_billed = min(cap, int(base)) if base else cap
    return jc


def fetch_rows(client, cfg, asof: date) -> tuple:
    """一次採点の入力を2本で取る（東京の価格系・US の会社系）。どちらかが失敗したら例外（1社ずつ引き直さない）。"""
    from google.cloud import bigquery
    P = bigquery.ScalarQueryParameter
    days = int(getattr(cfg, "SCREEN_UNIVERSE_PRICE_DAYS", 440))
    price = client.query(PRICE_SQL.format(project=client.project), location=su.LOCATION, job_config=_job_config(cfg, [
        P("asof", "DATE", asof), P("price_days", "INT64", days),
        P("si_days", "INT64", int(getattr(cfg, "US_SCREEN_UNIVERSE_SI_DAYS", 45)))])).result()
    price_rows = [dict(r.items()) for r in price]
    fund = client.query(FUND_SQL.format(project=client.project), location="US", job_config=_job_config(cfg, [
        P("asof", "DATE", asof),
        P("dg_days", "INT64", int(getattr(cfg, "US_SCREEN_UNIVERSE_13D_DAYS", 90))),
        P("k8_days", "INT64", int(getattr(cfg, "US_SCREEN_UNIVERSE_8K_DAYS", 90)))])).result()
    return price_rows, [dict(r.items()) for r in fund]


def run_us_universe(cfg, results: dict | None = None, client=None, asof: date | None = None) -> dict:
    """米国の一次採点を実行する。失敗は例外で返す（呼び出し側がテーマ別リストの従来経路へ縮退し、結果に理由を残す）。

    Returns: {"records": 採点済み全件, "stage2": [ticker...], "summary": dict}
    """
    import technical_scorer
    from stock_screener import accrual_adjustment
    asof = asof or datetime.now().date()
    if client is None:
        from edinet_bq_client import EDINETBQClient
        client = EDINETBQClient().client
    price_rows, fund_rows = fetch_rows(client, cfg, asof)
    if not price_rows or not fund_rows:
        raise RuntimeError(f"米国の一次採点の入力が空（価格 {len(price_rows)}行・会社 {len(fund_rows)}行）")
    hotness, leading = {}, []
    try:
        hotness, leading = su.load_hotness(cfg)
    except Exception as e:
        print(f"    ⚠️  米国の一次採点: テーマ熱度を読めず中立で続行: {str(e)[:80]}")
    market = market_series(price_rows)
    if not market:
        try:
            market = su.fetch_market(client, cfg, asof, MARKET_TICKER)
        except Exception as e:
            print(f"    ⚠️  米国の一次採点: SPY の株価を読めず、銘柄独自の観点は中立で続行: {str(e)[:80]}")
    us_sec = ((results or {}).get("us_sectors") or {}).get("sectors") or {}
    ctx = {
        "market": market, "hotness": hotness, "leading_only": leading,
        "sector_scores": {etf: _num(v.get("score")) for etf, v in us_sec.items() if isinstance(v, dict)},
        "accrual_fn": accrual_adjustment, "tech_fn": technical_scorer.calc_technical_score,
        "weekly_fn": technical_scorer.weekly_closes,
    }
    records, excluded = build_records(price_rows, fund_rows, cfg, ctx, asof)
    if not records:
        raise RuntimeError("米国の一次採点の対象が0件（全件が除外）")
    # 重ねがけは需給（上げ日／下げ日の出来高）だけ。地政学は sector 観点に入っており、13D は検証で無作為より悪く向きが定まらない
    extra = {k: w for k, w in (getattr(cfg, "SCREEN_UNIVERSE_EXTRA_WEIGHTS", {}) or {}).items() if k == "supply"}
    scored = su.score_universe(records, getattr(cfg, "SCREEN_UNIVERSE_WEIGHTS", {}), RANK_COLUMNS, extra or None)
    smooth = int(getattr(cfg, "SCREEN_UNIVERSE_RANK_SMOOTH_DAYS", 1))
    if smooth > 1:
        try:
            scored = su.smooth_rank(scored, su.fetch_recent_scores(client, asof, smooth - 1, TABLE), smooth)
        except Exception as e:
            print(f"    ⚠️  米国の一次採点: 前の回の点を読めず、その日の点で順位を付ける: {str(e)[:80]}")
    top_n = int(getattr(cfg, "US_SCREEN_STAGE2_TOP_N", 200))
    stage2 = [r["ticker"] for r in scored[:top_n]]
    summary = summarize(scored, excluded, int(getattr(cfg, "SCREEN_UNIVERSE_TOP_REPORT_N", 50)), len(stage2),
                        len(price_rows), asof)
    day = asof.isoformat()
    try:
        summary["bq_rows_written"] = su.write_daily(
            client, output_rows(scored, day, set(stage2)), day,
            Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "tmp", table=TABLE, schema=_schema())
    except Exception as e:
        summary["bq_write_error"] = str(e)[:150]
        print(f"    ⚠️  {TABLE} の書き込み失敗（採点は続行）: {str(e)[:100]}")
    summary["created_at"] = datetime.now(timezone.utc).isoformat()
    return {"records": scored, "stage2": stage2, "summary": summary}
