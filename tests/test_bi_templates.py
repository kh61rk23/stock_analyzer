"""BI基盤のテンプレート（bi/templates/*.json）の検査。

- 全テンプレのSQLが、マートの列定義どおりの合成データ（DuckDB）で実行できる
- パラメータ置換が型検査される（引用符・桁あふれ・型違いは拒否）
- DuckDB と BigQuery の片方にしか無い関数・構文を含まない
- マートSQLの出力列が、このテストの列定義と一致している（マートを直したらここも直す）
"""
import re
import sys
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import bi_templates as bt  # noqa: E402

STRING_COLS = {
    "edinet_code", "name", "sec_code4", "industry", "accounting_standard", "listing_status",
    "country_code", "country_name_ja", "region", "ticker", "market", "exposure_type",
    "prefecture_name", "prefecture", "municipality_code", "municipality_name",
    "top_export_commodities", "sealane_adjacency", "military_alliances",
    "company_id", "entity_type", "node_id", "node_name_ja", "node_name_en", "node_type", "supply_chain_pos",
    "parent_id", "domain_id", "domain_name_ja", "path_name", "confidence_type",
    "price_source", "interest_bearing_debt_basis", "valuation_flag", "roic_tax_basis", "re_gain_flag", "exchange", "sic", "shares_source",
    "theme_name", "sector_code", "sector_name_ja", "supply_chain_position", "source", "signal_category", "direction",
    "tickers_all", "axis", "axis_ja", "member", "segment_name",
    "sector_name", "accrual_tier", "size_tier", "strategy_label", "analyst_reco", "signal_text",
    "paper_id", "primary_category", "categories", "title", "title_ja", "summary", "authors", "url", "keywords", "keyword",
    "risk_level", "signals", "indicator", "signal", "trigger_value", "judgement", "judgement_ja", "reason",
    "report_type", "report_key", "body_head", "source_path",
    "summary_head", "priority", "lang", "pub_date", "matched_sectors", "sentiment_label", "etfs",
    "indicator_code", "indicator_name", "stat_name", "unit", "period", "country", "score_path", "reasons",
}
INT_COLS = {
    "fiscal_year", "employee_count", "foreign_ministry_level", "state_dept_level", "exposure_type_count",
    "latest_foreign_ministry_level", "latest_state_dept_level", "hospital_count", "facility_count",
    "municipality_count", "population_current", "population_2050", "est_excluded_count",
    "node_level", "child_count", "company_count", "company_count_rollup",
    "theme_order", "days_before_latest", "price_days", "cik", "signal_count", "ticker_signal_count", "ticker_count",
    "investment_score", "score_rank", "chars", "heat_rank", "year", "re_fiscal_year", "re_facility_count", "re_est_excluded_count", "f_score", "f_score_items", "consec_up_years",
    "trading_days", "n_stocks", "n_up", "n_down", "n_flat", "n_hl_base", "n_new_high", "n_new_low", "n_ma50", "n_ma200",
}
DATE_COLS = {"fiscal_year_end", "risk_date", "latest_risk_date", "price_date", "price_break_date", "snapshot_date", "technical_date", "fired_at",
             "month", "month_end_date", "trade_date", "ttm_period_end", "balance_date", "shares_as_of", "base_trade_date", "fetched_date", "fetched_month", "published_date", "report_date", "first_seen", "last_seen"}
BOOL_COLS = {"is_delisted", "sanction_flag", "is_landlocked", "review_flag", "auto_derived", "verified_flag", "above_ma200", "has_fundamentals", "is_partial", "is_complete", "is_latest", "macd_crossup", "above_vwap", "is_idea", "investment_relevant", "passed"}
ALL_MARTS = ("m_company", "m_fin_annual", "m_country_profile", "m_country_risk", "m_company_country",
             "m_re_area", "m_re_holdings", "m_re_holdings_pref", "m_tax_node", "m_company_tax", "m_screener", "m_screener_us",
             "m_theme", "m_theme_def", "m_theme_signal", "m_price_monthly", "m_sector_perf", "m_market_breadth",
             "m_us_fin_annual", "m_us_segments", "m_stock_snapshot", "m_arxiv_papers", "m_arxiv_keywords",
             "m_sector_scores", "m_macro_indicators", "m_macro_signals", "m_reports",
             "m_news", "m_etf_themes", "m_etf_daily", "m_imf_forecasts", "m_gov_stats", "m_screener_trace")


def mart_columns(mart: str) -> list[str]:
    """マートSQLの最終SELECTの出力列名を取り出す。"""
    sql = "\n".join(l for l in (REPO / "bi" / "marts" / f"{mart}.sql").read_text(encoding="utf-8").splitlines()
                    if not l.strip().startswith("--"))
    last_select = sql.rsplit("\nSELECT\n", 1)[-1] if "\nSELECT\n" in sql else sql.split("SELECT", 1)[1]
    head = re.split(r"\nFROM\b", last_select, maxsplit=1)[0]
    cols = []
    for line in head.splitlines():
        line = line.strip().rstrip(",")
        if not line:
            continue
        m = re.search(r"\bAS\s+(\w+)$", line)
        cols.append(m.group(1) if m else line.split(".")[-1])
    return cols


def sql_type(col: str) -> str:
    if col in STRING_COLS:
        return "VARCHAR"
    if col in INT_COLS:
        return "BIGINT"
    if col in DATE_COLS:
        return "DATE"
    if col in BOOL_COLS:
        return "BOOLEAN"
    return "DOUBLE"


@pytest.fixture(scope="module")
def con():
    c = duckdb.connect(":memory:")
    for mart in ALL_MARTS:
        cols = mart_columns(mart)
        c.execute(f"CREATE TABLE {mart} (" + ", ".join(f"{col} {sql_type(col)}" for col in cols) + ")")
    industries = ["電気機器", "小売業", None]
    for i in range(40):
        code = "7203" if i == 0 else f"{7000 + i}"
        ind = industries[i % 3]
        c.execute("INSERT INTO m_company VALUES (?,?,?,?,?,?,?)",
                  [f"E{i:05d}", code, f"会社{i}", ind, "日本基準", "上場", False])
        for k, fy in enumerate(range(2016, 2027)):
            sales = (100 + i * 10) * 1e9 * (1 + 0.05 * k)
            op = sales * (0.02 + (i % 7) * 0.02) * (1 if (i + k) % 5 else -0.5)
            ni = op * 0.7
            assets = sales * 1.2
            eq = assets * (0.1 + (i % 9) * 0.1)
            row = {
                "edinet_code": f"E{i:05d}", "fiscal_year": fy, "fiscal_year_end": f"{fy}-03-31",
                "name": f"会社{i}", "sec_code4": code, "industry": ind,
                "net_sales": sales, "operating_income": op, "ordinary_income": op, "net_income": ni,
                "total_assets": assets, "equity": eq, "interest_bearing_debt": sales * 0.1,
                "cash_and_equiv": sales * 0.2, "operating_cf": op, "investing_cf": -op * (0.5 + (i % 3)),
                "free_cf": op * 0.5 - (i % 3) * op * 0.4, "employee_count": 500 + i * 100 + k * (10 if i % 2 else -10),
                "avg_annual_salary": 6e6 + i * 1e5, "capex": sales * 0.03, "depreciation": sales * 0.02,
                "roe": 0.02 + (i % 10) * 0.02, "roa": 0.03, "operating_margin": op / sales,
                "gross_profit": sales * 0.3, "sga": sales * 0.2, "rnd_expenses": sales * (i % 4) * 0.01,
                "cost_of_sales": sales * 0.7, "goodwill": eq * (i % 5) * 0.2, "inventories": sales * 0.1,
                "trade_receivables": sales * 0.15, "total_liabilities": assets - eq, "net_assets": eq,
                "shareholders_equity": eq, "eps": 50.0, "bps": 800.0, "dividend_per_share": 20.0,
                "payout_ratio": (i % 12) * 0.12, "per": 15.0, "pbr": 0.5 + (i % 8) * 0.4,
                "num_employees": 500 + i * 100, "female_manager_ratio": (i % 10) * 0.03,
                "avg_tenure_years": 12.0, "cash_dividends_paid": 1e9,
                "gross_margin": 0.3, "sga_ratio": 0.2, "rnd_ratio": (i % 4) * 0.01,
                "equity_ratio": eq / assets, "net_margin": ni / sales,
            }
            cols = mart_columns("m_fin_annual")
            assert set(row) == set(cols), (set(row) ^ set(cols))
            c.execute(f"INSERT INTO m_fin_annual ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                      [row[col] for col in cols])
    _load_geo_and_realestate(c)
    _load_taxonomy(c)
    _load_screener(c)
    _load_theme(c)
    _load_market(c)
    return c


def _insert(c, mart: str, row: dict) -> None:
    cols = mart_columns(mart)
    assert set(row) == set(cols), (mart, set(row) ^ set(cols))
    c.execute(f"INSERT INTO {mart} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
              [row[col] for col in cols])


COUNTRIES = [("CHN", "中国", "東アジア", False), ("USA", "アメリカ合衆国", "北米", False),
             ("AUS", "オーストラリア", "オセアニア", False), ("RUS", "ロシア", "欧州・ロシア", True),
             ("IRN", "イラン", "中東", True), ("SAU", "サウジアラビア", "中東", False),
             ("JPN", "日本", "東アジア", False), ("IDN", "インドネシア", "東南アジア", False)]
PREFS = ["東京都", "愛知県", "北海道", "宮崎県"]


def _load_geo_and_realestate(c) -> None:
    import datetime as dt
    for k, (code, nm, region, sanc) in enumerate(COUNTRIES):
        _insert(c, "m_country_profile", {
            "country_code": code, "country_name_ja": nm, "region": region, "democracy_index": 1.0 + k,
            "corruption_index": 3.0 + k, "rule_of_law": 2.0 + k, "gdp_usd_bn": 100.0 * (k + 1),
            "export_concentration": 0.1 * k, "is_landlocked": False, "sanction_flag": sanc,
            "trade_dependency_jp": 0.01 * k, "top_export_commodities": "[]", "sealane_adjacency": "[]",
            "military_alliances": "[]"})
        for d in range(20):
            _insert(c, "m_country_risk", {
                "country_code": code, "risk_date": dt.date(2026, 9, 20) + dt.timedelta(days=d),
                "country_name_ja": nm, "region": region, "sanction_flag": sanc,
                "gdelt_instability": 10.0 + k, "foreign_ministry_level": k % 4, "state_dept_level": (k + 1) % 4,
                "composite_risk_score": 20.0 + k * 3 + d * (0.5 if k % 2 else -0.2)})
    pairs = [("8058.T", "8058", "三菱商事", "総合商社", "AUS", "RESOURCE_CONCESSION", 0.18),
             ("8058.T", "8058", "三菱商事", "総合商社", "RUS", "RESOURCE_CONCESSION", 0.10),
             ("7203.T", "7203", "トヨタ自動車", "輸送用機器", "CHN", "PRODUCTION_FACILITY", 0.22),
             ("7203.T", "7203", "トヨタ自動車", "輸送用機器", "USA", "PRODUCTION_FACILITY", 0.18),
             ("5019.T", "5019", "出光興産", "石油", "IRN", "PROCUREMENT", 0.25),
             ("LRCX", None, None, None, "USA", "SALES_NETWORK", 0.56)]
    for tk, s4, nm, ind, cc, et, st in pairs:
        k = [x[0] for x in COUNTRIES].index(cc)
        _insert(c, "m_company_country", {
            "ticker": tk, "market": "JP" if s4 else "US", "sec_code4": s4, "edinet_code": f"E{tk[:4]}" if s4 else None,
            "name": nm, "industry": ind, "country_code": cc, "country_name_ja": COUNTRIES[k][1],
            "region": COUNTRIES[k][2], "sanction_flag": COUNTRIES[k][3], "exposure_type": et,
            "exposure_type_count": 1, "exposure_strength": st, "revenue_share_est": None, "review_flag": False,
            "latest_risk_score": 20.0 + k * 5, "latest_gdelt_instability": 10.0 + k,
            "latest_foreign_ministry_level": k % 4, "latest_state_dept_level": (k + 1) % 4,
            "latest_risk_date": dt.date(2026, 10, 9), "risk_weighted_score": st * (20.0 + k * 5)})
    for m in range(30):
        _insert(c, "m_re_area", {
            "municipality_code": f"{13100 + m:05d}", "prefecture_name": PREFS[m % 4],
            "municipality_name": f"市{m}", "median_price": 3e7 + m * 1e5,
            "avg_land_price_commercial": 50000.0 + m * 20000, "avg_land_price_residential": 30000.0 + m * 5000,
            "land_price_yoy_change": -5.0 + m * 0.4, "flood_risk_area_pct": (m % 10) * 0.1,
            "latest_avg_price_condo": 4e7, "hospital_count": m % 7})
    for i in range(12):
        # 会社別の保有不動産テンプレは既定がイオン（8267）なので、その会社を必ず含める
        code = "7203" if i == 0 else ("8267" if i == 1 else f"{7000 + i}")
        for fy in (2024, 2025, 2026):
            for j in range(3):
                m = (i * 3 + j) % 30
                area = 1000.0 * (j + 1)
                book = 100.0 * (i + 1)
                ref = 50000.0 + m * 20000
                est = area * ref / 1e6
                pop = -60.0 + m * 4
                flood = (m % 10) * 0.1
                _insert(c, "m_re_holdings", {
                    "edinet_code": f"E{i:05d}", "name": f"会社{i}", "sec_code4": code, "industry": "電気機器",
                    "fiscal_year": fy, "prefecture": PREFS[m % 4], "municipality_code": f"{13100 + m:05d}",
                    "municipality_name": f"市{m}", "facility_count": j + 1, "land_area_m2": area,
                    "book_value_land_m_yen": book, "book_value_buildings_m_yen": book / 2,
                    "land_price_ref_per_m2": ref, "land_value_est_m_yen": est, "est_excluded_count": 0, "unrealized_gain_est_m_yen": est - book,
                    "population_current": 100000, "population_2050": 60000, "population_change_pct": pop,
                    "flood_risk_area_pct": flood, "landslide_risk_area_pct": 0.1,
                    "area_land_price_yoy_change": 1.0})
            for pi, pref in enumerate(PREFS[:3]):
                est = 50.0 * (pi + 1) * (i + 1)
                book = 30.0 * (i + 1)
                _insert(c, "m_re_holdings_pref", {
                    "edinet_code": f"E{i:05d}", "name": f"会社{i}", "sec_code4": code, "industry": "電気機器",
                    "fiscal_year": fy, "prefecture": pref, "facility_count": pi + 1, "municipality_count": 1,
                    "land_area_m2": 5000.0, "book_value_land_m_yen": book, "land_value_est_m_yen": est,
                    "est_excluded_count": 0,
                    "unrealized_gain_est_m_yen": est - book, "population_change_pct": -35.0 + pi * 10,
                    "flood_risk_area_pct": 0.2 * pi})


# 階層: 領域3つ（2階層目 <領域>_a/_b、3階層目 <領域>_a1）。会社は 20 社、各社は 1〜3 ノードに紐づく。
def _load_taxonomy(c) -> None:
    nodes = []
    for d, did in enumerate(["semiconductor", "ai_software", "robotics_automation"]):
        nodes.append((did, None, f"領域{d}", 1, "domain"))
        for k in "ab":
            nodes.append((f"{did}_{k}", did, f"分野{d}{k}", 2, "category"))
        nodes.append((f"{did}_a1", f"{did}_a", f"細分{d}a1", 3, "leaf"))
    by_id = {n[0]: n for n in nodes}

    def chain(nid):
        out = []
        while nid:
            out.append(by_id[nid])
            nid = by_id[nid][1]
        return list(reversed(out))

    direct = {n[0]: set() for n in nodes}
    members = []
    for i in range(20):
        for j in range(1 + i % 3):
            members.append((i, nodes[(i * 2 + j * 3) % len(nodes)][0]))
    members += [(1, "ai_software"), (1, "semiconductor_a1")]  # テンプレ既定値（ai_software・6869・semiconductor）に行を持たせる
    for i, nid in members:
        direct[nid].add(i)
    for nid, parent, nm, lv, tp in nodes:
        ch = chain(nid)
        sub = {x for n2 in nodes if nid in [a[0] for a in chain(n2[0])] for x in direct[n2[0]]}
        _insert(c, "m_tax_node", {
            "node_id": nid, "parent_id": parent, "node_name_ja": nm, "node_name_en": nm, "node_level": lv,
            "node_type": tp, "supply_chain_pos": None, "domain_id": ch[0][0], "domain_name_ja": ch[0][2],
            "path_name": " > ".join(a[2] for a in ch), "child_count": sum(1 for n2 in nodes if n2[1] == nid),
            "company_count": len(direct[nid]), "company_count_rollup": len(sub)})
    for i, nid in members:
        ch = chain(nid)
        code = "7203" if i == 0 else ("6869" if i == 1 else f"{7000 + i}")
        _insert(c, "m_company_tax", {
            "company_id": code, "entity_type": "company", "edinet_code": f"E{i:05d}", "name": f"会社{i}",
            "industry": ["電気機器", "小売業", None][i % 3], "node_id": nid, "node_name_ja": by_id[nid][2],
            "node_level": by_id[nid][3], "parent_id": by_id[nid][1], "domain_id": ch[0][0],
            "domain_name_ja": ch[0][2], "path_name": " > ".join(a[2] for a in ch),
            "relevance_score": 0.3 + 0.1 * (i % 5), "confidence": 0.7, "revenue_share_est": 0.2,
            "confidence_type": "edinet", "auto_derived": True, "verified_flag": False, "fiscal_year": 2026,
            "net_sales": (100 + i * 10) * 1e9, "operating_income": (100 + i * 10) * 1e9 * 0.05,
            "operating_margin": 0.05 + (i % 4) * 0.02, "rnd_expenses": (100 + i * 10) * 1e9 * 0.03,
            "rnd_ratio": 0.01 + (i % 5) * 0.01})
    _insert(c, "m_company_tax", {
        "company_id": "sector:JP:1011", "entity_type": "sector", "edinet_code": None, "name": None,
        "industry": None, "node_id": "semiconductor_a", "node_name_ja": "分野0a", "node_level": 2, "parent_id": "semiconductor",
        "domain_id": "semiconductor", "domain_name_ja": "領域0", "path_name": "領域0 > 分野0a", "relevance_score": 0.2,
        "confidence": 0.7, "revenue_share_est": None, "confidence_type": "edinet", "auto_derived": True,
        "verified_flag": False, "fiscal_year": None, "net_sales": None, "operating_income": None,
        "operating_margin": None, "rnd_expenses": None, "rnd_ratio": None})


# 1社1行。株価系は i%3==2 が株価なし、他は price_daily。valuation_flag は i%10==7 が eps_mismatch。
# スコアは i%3==0、技術指標は i%6==0 の会社だけ
def _load_screener(c) -> None:
    import datetime as dt
    for i in range(60):
        code = "7203" if i == 0 else f"{7000 + i}"
        sales = (100 + i * 10) * 1e9
        op = sales * (0.02 + (i % 7) * 0.03)
        ni = op * 0.7
        assets = sales * 1.2
        eq = assets * (0.2 + (i % 6) * 0.1)
        cash = sales * 0.3
        debt = sales * (0.05 if i % 4 else 0.0)
        px = i % 3 != 2
        price = 1000.0 + i * 50 if px else None
        cap_b = (100.0 + i * 40) if px else None
        d = dt.date(2026, 10, 9)
        flag = "eps_mismatch" if i % 10 == 7 else None
        row = {
            "edinet_code": f"E{i:05d}", "sec_code4": code, "ticker": f"{code}.T", "name": f"会社{i}",
            "industry": ["電気機器", "小売業", None][i % 3], "accounting_standard": "日本基準",
            "fiscal_year": 2026, "fiscal_year_end": dt.date(2026, 3, 31),
            "net_sales": sales, "operating_income": op, "ordinary_income": op, "net_income": ni,
            "total_assets": assets, "equity": eq, "employees": 500.0 + i * 100, "avg_annual_salary": 6e6 + i * 1e5,
            "operating_margin": op / sales, "net_margin": ni / sales, "gross_margin": 0.3,
            "roe": 0.04 + (i % 10) * 0.03, "roa": 0.03, "rnd_ratio": (i % 5) * 0.03,
            "equity_ratio": eq / assets, "interest_bearing_debt": debt, "interest_bearing_debt_basis": "reported" if i % 2 else "estimated",
            "cash_and_equiv": cash, "net_cash": cash - debt, "de_ratio": debt / eq,
            "sales_growth_1y": -0.05 + (i % 9) * 0.04, "op_income_growth_1y": -0.1 + (i % 7) * 0.06,
            "net_income_growth_1y": -0.1 + (i % 5) * 0.08, "sales_cagr_3y": -0.02 + (i % 11) * 0.03,
            "eps": 50.0, "bps": 800.0, "dividend_per_share": 20.0, "payout_ratio": (i % 8) * 0.12, "shares_issued": 1e8,
            "shares_out": 9e7,
            "price_source": "price_daily" if px else None, "current_price": price, "price_date": d if px else None,
            "snapshot_date": d if i % 3 == 0 else None,
            "market_cap_b": cap_b, "market_cap": cap_b * 1e8 if px else None,
            "per": None if (flag or not px) else 6.0 + (i % 10) * 1.5, "pbr": 0.4 + (i % 8) * 0.25 if px else None,
            "dividend_yield": 0.01 + (i % 9) * 0.008 if px else None, "valuation_flag": flag,
            "net_cash_to_mcap": (cash - debt) / (cap_b * 1e8) if px else None,
            "return_1m": -0.1 + (i % 10) * 0.03 if px else None, "return_3m": -0.2 + (i % 12) * 0.05 if px else None,
            "return_1y": -0.3 + (i % 15) * 0.07 if px and i % 5 else None,
            "high_52w": price * 1.2 if px else None, "low_52w": price * 0.7 if px else None,
            "week52_position_pct": 41.0 + (i % 60) if px else None,
            "avg_turnover_20d": 1e8 * (1 + i) if px else None, "price_days": 250 if px else None,
            "price_break_date": None,
            "composite_score": 30.0 + i if i % 3 == 0 else None,
            "value_score": 40.0 + i % 50 if i % 3 == 0 else None,
            "quality_score": 50.0 + i % 40 if i % 3 == 0 else None,
            "rsi14": 40.0 + i % 40 if i % 6 == 0 else None,
            "above_ma200": True if i % 6 == 0 else None,
            "technical_date": d if i % 6 == 0 else None,
        }
        mcap = cap_b * 1e8 if px else None
        ocf = op * 1.1
        capex = sales * 0.03
        dep = sales * 0.02
        ev = mcap + debt - cash if px else None
        has_re = i % 4 == 1
        gain = sales * 0.2 * (i % 5) if has_re else None
        fcf = ocf - capex
        divs = 20.0 * 9e7
        bb = sales * 0.005 * (i % 3)
        row.update({
            "nopat": op * 0.7, "invested_capital": debt + eq, "tax_rate": 0.3, "roic_tax_basis": "reported" if i % 2 else "assumed",
            "roic": op * 0.7 / (debt + eq), "ocf_margin": ocf / sales, "fcf_margin": fcf / sales, "gross_margin_chg_1y": 0.01 * ((i % 5) - 2),
            "operating_cf": ocf, "fcf": fcf, "ebitda": op + dep, "ev": ev,
            "ev_ebitda": ev / (op + dep) if px and ev > 0 else None, "ev_sales": ev / sales if px and ev > 0 else None,
            "psr": mcap / sales if px else None,
            "peg": (6.0 + (i % 10) * 1.5) / (5 + i % 20) if px and not flag else None,
            "fcf_yield": fcf / mcap if px else None, "earnings_yield": 50.0 / price if px and not flag else None,
            "re_fiscal_year": 2025 if has_re else None, "re_facility_count": 3 + i % 7 if has_re else None,
            "re_est_excluded_count": 0 if has_re else None, "re_land_value_est": gain * 1.5 if has_re else None,
            "re_unrealized_gain": gain, "re_gain_flag": None, "re_gain_to_mcap": gain / mcap if has_re and px else None,
            "re_land_value_to_mcap": gain * 1.5 / mcap if has_re and px else None,
            "re_gain_after_tax": gain * 0.694 if has_re else None, "adj_equity": eq + gain * 0.694 if has_re else None,
            "adj_pbr": mcap / (eq + gain * 0.694) if has_re and px else None,
            "asset_value_ratio": (cash - debt + gain * 0.694) / mcap if has_re and px else None,
            "interest_coverage": op / (debt * 0.01) if debt else None, "current_ratio": 1.0 + (i % 6) * 0.4,
            "goodwill_to_equity": (i % 5) * 0.05, "accrual_ratio": (ni - ocf) / assets,
            "f_score": 3 + i % 7 if i % 7 != 6 else None, "f_score_items": 9 if i % 7 != 6 else 5,
            "buyback_est": bb, "buyback_yield": bb / mcap if px else None,
            "total_payout_yield": (divs + bb) / mcap if px else None, "doe": divs / eq,
            "op_income_cagr_3y": -0.05 + (i % 9) * 0.04, "net_income_cagr_3y": 0.05 + (i % 20) * 0.01,
            "consec_up_years": i % 8,
        })
        _insert(c, "m_screener", row)
    _load_screener_us(c)
    _load_us(c)
    _load_snapshot(c)
    _load_arxiv(c)
    _load_reports(c)
    _load_news_etf(c)
    _load_imf_gov(c)
    _load_trace(c)


# スクリーナーの全候補：10日×（通過6・落選4）。6501.T は日によって通過と落選が入れ替わる
def _load_trace(c) -> None:
    import datetime as dt
    for k in range(10):
        d = dt.date(2026, 9, 30) + dt.timedelta(days=k)
        for i in range(10):
            tk = "6501.T" if i == 0 else f"{7000 + i}.T"
            ok = (i < 6) if i else (k % 2 == 0)
            for mk in ("JP", "US"):
              _insert(c, "m_screener_trace", {
                "report_date": d, "market": mk, "ticker": tk, "name": f"銘柄{i}", "sector_code": "1016", "sector_name": "電気機器",
                "passed": ok, "composite_score": 60.0 + i * 3, "base_score": 55.0 + i * 2 if ok else None,
                "bonus": 5.0 + i if ok else None, "score_path": "55.0 →政策+5.0→ 60.0" if ok else None,
                "reasons": None if ok else ["PER 31.1（条件 0超〜30倍）", "ROE 5.0%（条件 8%以上）"][i % 2],
                "kpi_pbr": 1.0 if ok else None, "kpi_per": 12.0 if ok else None, "kpi_fcf_yield": 0.05 if ok else None,
                "kpi_ev_ebitda": 7.0 if ok else None, "kpi_roic": 0.1 if ok else None, "kpi_net_cash_ratio": 0.2 if ok else None,
                "kpi_irr": 0.08 if ok else None})


# IMF（3取得日×2指標×2国×3年・2回目に改定）と政府統計（5取得日×2統計）
def _load_imf_gov(c) -> None:
    import datetime as dt
    for k in range(3):
        d = dt.date(2026, 7, 23) + dt.timedelta(days=30 * k)
        for code, name in (("NGDP_RPCH", "実質GDP成長率(%)"), ("PCPIPCH", "消費者物価上昇率(%)")):
            for country in ("日本(JPN)", "米国(USA)"):
                for y in (2025, 2026, 2027):
                    _insert(c, "m_imf_forecasts", {"report_date": d, "indicator_code": code, "indicator_name": name, "country": country,
                                                   "year": y, "value": 1.0 + (0.2 if k >= 1 else 0.0), "revision": (0.2 if k == 1 else 0.0) if k else None})
    for k in range(5):
        d = dt.date(2026, 9, 1) + dt.timedelta(days=7 * k)
        for name, v in (("消費者物価指数（CPI）", 102.5), ("機械受注統計（民需）", 4195744.3)):
            _insert(c, "m_gov_stats", {"report_date": d, "stat_name": name, "value": v, "unit": "（2020年=100）", "period": f"2026-0{6 + k % 3}",
                                       "mom_pct": 0.2, "yoy_pct": 2.7 + k * 0.1, "signal": "tailwind"})


# ニュース（14日×10本・業種 1016 と macro）とテーマETF（20日×3テーマ・各1ETF）
def _load_news_etf(c) -> None:
    import datetime as dt
    for k in range(14):
        d = dt.date(2026, 9, 26) + dt.timedelta(days=k)
        for i in range(10):
            _insert(c, "m_news", {"url": f"https://ex.com/{k}/{i}", "first_seen": d, "last_seen": d, "title": f"半導体ニュース{i}",
                                  "summary_head": "半導体の需要が拡大", "source": ["NHK ビジネス", "CNBC Finance"][i % 2], "priority": "A",
                                  "lang": "ja", "pub_date": f"{d} 10:00", "matched_sectors": "1016,macro" if i % 2 else "macro",
                                  "importance_score": float(i % 4), "investment_relevant": i % 3 != 0,
                                  "sentiment_label": "NEUTRAL", "sentiment_score": 0.4 + (i % 3) * 0.1})
    for k in range(20):
        d = dt.date(2026, 9, 20) + dt.timedelta(days=k)
        for i, (th, etf) in enumerate((("サイバーセキュリティ", "CIBR"), ("量子コンピュータ", "QTUM"), ("水素・クリーンエネルギー", "ICLN"))):
            _insert(c, "m_etf_themes", {"report_date": d, "theme_name": th, "etfs": etf, "heat_score": 60.0 - i * 12 + k % 3,
                                        "etf_momentum_score": 70.0 - i * 15, "arxiv_trend_score": 55.0, "arxiv_growth_rate": 30.0 - i * 20,
                                        "arxiv_recent_4w": 100.0, "arxiv_prev_4w": 80.0, "signal": "warming",
                                        "heat_chg_5d": 1.0 if k >= 5 else None, "heat_rank": i + 1})
            _insert(c, "m_etf_daily", {"report_date": d, "theme_name": th, "ticker": etf, "price": 100.0 + k, "ret_1m": 5.0 - i * 3,
                                       "ret_3m": 8.0 - i * 4, "ma50_ratio": 2.0, "vol_ratio": 10.0})


# マクロ・業種レポート：20記録日。日本3業種・米国2セクター、マクロ指標3つ・米国債3年限、シグナル2つ
def _load_reports(c) -> None:
    import datetime as dt
    for k in range(20):
        d = dt.date(2026, 9, 10) + dt.timedelta(days=k)
        for mk, secs in (("JP", [("1001", "水産・農林業"), ("1029", "銀行業"), ("1033", "サービス業")]), ("US", [("XLK", "情報技術"), ("XLE", "エネルギー")])):
            for i, (code, name) in enumerate(secs):
                sc = 10.0 + i * 7 + (k % 5) * (1 if i % 2 else -1)
                _insert(c, "m_sector_scores", {"report_date": d, "market": mk, "sector_code": code, "sector_name": name, "score": sc,
                        "risk_level": "medium", "ret_1m": 1.0 if mk == "US" else None, "ret_3m": None, "ret_12m": 5.0 if mk == "US" else None,
                        "signals": "金利上昇: 追い風", "score_chg_1d": 1.0 if k else None, "score_chg_5d": 2.0 if k >= 5 else None, "score_rank": i + 1})
        for ind, v in (("USD/JPY", 150 + k * 0.3), ("VIX", 15 + k % 4), ("日経平均", 69000 + k * 50)):
            _insert(c, "m_macro_indicators", {"report_date": d, "source": "macro_report", "indicator": ind, "value": v, "change_1m": None, "change_1d": 0.1})
        for ind, v in (("米2年", 4.7), ("米10年", 5.2), ("米30年", 5.6)):
            _insert(c, "m_macro_indicators", {"report_date": d, "source": "bond_commodity", "indicator": ind, "value": v, "change_1m": 0.3, "change_1d": 0.0})
        for sig, sec, j, ja in (("金利上昇", "銀行業", "tailwind", "追い風"), ("金利上昇", "不動産業", "headwind", "逆風"), ("円安進行", "輸送用機器", "tailwind", "追い風")):
            _insert(c, "m_macro_signals", {"report_date": d, "signal": sig, "trigger_value": "米10年債: 5.23%", "sector_name": sec,
                                           "judgement": j, "judgement_ja": ja, "reason": "理由"})
        for rt, key in (("macro_report", ""), ("sector", "1029"), ("bond_commodity", "")):
            _insert(c, "m_reports", {"report_date": d, "report_type": rt, "report_key": key, "title": f"{rt} レポート",
                                     "body_head": "円安進行と金利上昇", "chars": 3000, "source_path": f"macro/{rt}.md"})


# arXiv：3回の取得×各20本。2610.03369 は詳細を引くテンプレの既定値
def _load_arxiv(c) -> None:
    import datetime as dt
    cats = ["q-fin.PM", "q-fin.TR", "q-fin.ST", "q-fin.RM", "econ.EM"]
    kws = ["tail risk", "volatility", "portfolio", "news"]
    for w in range(3):
        d = dt.date(2026, 9, 21) + dt.timedelta(days=7 * w)
        for i in range(20):
            pid = "2610.03369" if (w, i) == (2, 0) else f"26{w}9.{i:05d}"
            score = (i * 3 + w) % 7
            kk = [kws[i % 4], kws[(i + 1) % 4]] if i % 3 else []
            _insert(c, "m_arxiv_papers", {
                "paper_id": pid, "fetched_date": d, "fetched_month": d.replace(day=1), "published_date": d - dt.timedelta(days=2),
                "primary_category": cats[i % 5], "categories": cats[i % 5], "title": f"Tail risk and volatility {i}",
                "title_ja": f"テールリスクとボラティリティ{i}" if i % 2 else None, "summary": "We study volatility of portfolios.",
                "authors": "A, B", "url": f"https://arxiv.org/abs/{pid}", "investment_score": score, "is_idea": score >= 3,
                "keywords": "|".join(kk) or None, "source": "monitor"})
            for k in kk:
                _insert(c, "m_arxiv_keywords", {"paper_id": pid, "fetched_date": d, "fetched_month": d.replace(day=1),
                                                "primary_category": cats[i % 5], "keyword": k, "investment_score": score, "is_idea": score >= 3})


# スクリーナー通過の日次記録（JP/US 各8銘柄×30日。最新日だけ新規の銘柄 NEW.T を足す）
def _load_snapshot(c) -> None:
    import datetime as dt
    d0 = dt.date(2026, 9, 9)
    for mk, tks in (("JP", ["1418.T"] + [f"{7000 + i}.T" for i in range(7)]), ("US", ["AAPL"] + [f"U{i}" for i in range(7)])):
        for k in range(30):
            d = d0 + dt.timedelta(days=k)
            for i, tk in enumerate(tks):
                _insert(c, "m_stock_snapshot", {
                    "snapshot_date": d, "ticker": tk, "market": mk, "name": f"銘柄{i}", "sector_code": "3050", "sector_name": "建設業",
                    "composite_score": 45.0 + i * 5 + k % 3, "sector_score": 40.0, "value_score": 60.0, "quality_score": 70.0,
                    "technical_score": 55.0, "discovery_score": 10.0, "per": 12.0, "pbr": 1.1, "roe": 0.12, "roic": 0.08,
                    "fcf_margin": 0.1, "debt_equity": 0.3, "equity_ratio": 0.5, "dividend_yield": 0.03, "market_cap_b": 100.0,
                    "current_price": 1000.0 + k, "rsi14": 20.0 + (i * 9 + k) % 70, "macd_crossup": k % 2 == 0, "above_ma200": True,
                    "above_vwap": True, "week52_position_pct": 60.0, "accrual_tier": "A", "size_tier": "small", "strategy_label": "value",
                    "analyst_reco": "buy", "signal_text": "MACDクロスアップ:+20", "base_trade_date": d, "base_close": 1000.0 + k,
                    "fwd_return_5d": 0.01 * (i - 3) if k < 25 else None, "fwd_return_20d": 0.02 * (i - 3) if k < 10 else None})
    last = d0 + dt.timedelta(days=29)
    _insert(c, "m_stock_snapshot", {k: None for k in mart_columns("m_stock_snapshot")} | {
        "snapshot_date": last, "ticker": "NEW.T", "market": "JP", "name": "新顔", "composite_score": 80.0, "rsi14": 50.0})


# 米国株の通期財務（20銘柄×5期）とセグメント（AAPL・MSFT の事業／製品／地域）
def _load_us(c) -> None:
    import datetime as dt
    inds = ["Electronic Computers", "Services-Prepackaged Software", None]
    for i in range(20):
        tk = ["AAPL", "MSFT"][i] if i < 2 else f"U{i:02d}"
        for k, fy in enumerate(range(2021, 2026)):
            rev = (50 + i * 20) * 1e9 * (1 + 0.08 * k)
            op = rev * (0.05 + i * 0.03)
            ni = op * 0.8
            eq = rev * 0.5
            ocf = op * 1.1
            capex = rev * 0.04
            row = {
                "ticker": tk, "cik": 100 + i, "name": f"Corp {i}", "exchange": "Nasdaq", "sic": "3571", "industry": inds[i % 3],
                "fiscal_year_end": dt.date(fy, 9, 30), "fiscal_year": fy, "revenue_usd": rev, "gross_profit_usd": rev * 0.4,
                "operating_income_usd": op, "net_income_usd": ni, "eps_usd": ni / 1e9, "operating_cf_usd": ocf,
                "capex_usd": capex, "fcf_usd": ocf - capex, "depreciation_usd": rev * 0.03, "buyback_usd": ni * 0.5,
                "dividends_paid_usd": ni * 0.2 if i % 2 else None, "total_assets_usd": rev * 1.5, "total_liabilities_usd": rev,
                "equity_usd": eq, "cash_usd": rev * 0.2, "debt_usd": rev * 0.3, "gross_margin": 0.4, "operating_margin": op / rev,
                "net_margin": ni / rev, "fcf_margin": (ocf - capex) / rev, "roe": ni / eq, "equity_ratio": eq / (rev * 1.5),
                "sales_growth_1y": 0.08 if k else None, "op_income_growth_1y": 0.08 if k else None,
                "net_income_growth_1y": 0.08 if k else None, "eps_growth_1y": 0.08 if k else None,
            }
            _insert(c, "m_us_fin_annual", row)
    segs = {
        "AAPL": [("ProductOrService", "製品・サービス", ["IPhoneMember", "MacMember", "ServicesMember"]),
                 ("Geographical", "地域", ["AmericasSegmentMember", "GreaterChinaSegmentMember", "EuropeSegmentMember"])],
        "MSFT": [("BusinessSegments", "事業", ["IntelligentCloudMember", "ProductivityAndBusinessProcessesMember"])],
    }
    for ci, (tk, axes) in enumerate(segs.items()):
        for axis, axis_ja, members in axes:
            for k, fy in enumerate(range(2022, 2026)):
                for m, mem in enumerate(members):
                    rev = (30 + m * 10) * 1e9 * (1 + 0.1 * k)
                    op = rev * 0.3 if axis == "BusinessSegments" else None
                    _insert(c, "m_us_segments", {
                        "ticker": tk, "cik": 100 + ci, "tickers_all": tk, "name": f"Corp {ci}", "industry": "Electronic Computers",
                        "axis": axis, "axis_ja": axis_ja, "member": mem, "segment_name": mem.replace("Member", ""),
                        "fiscal_year_end": dt.date(fy, 9, 30), "fiscal_year": fy, "is_latest": fy == 2025,
                        "revenue_usd": rev, "operating_income_usd": op, "operating_margin": 0.3 if op else None,
                        "company_revenue_usd": rev * len(members), "revenue_share": 1 / len(members), "revenue_growth_1y": 0.1 if k else None})


# 米国株: 1銘柄1行。i%4==3 は財務なし（株価のみ）、i%5==4 は株価なし
def _load_screener_us(c) -> None:
    import datetime as dt
    for i in range(60):
        fund = i % 4 != 3
        px = i % 5 != 4
        rev = (10 + i) * 1e9 if fund else None
        op = rev * (0.05 + (i % 7) * 0.03) if fund else None
        ni = op * 0.7 if fund else None
        eps = 5.0 + (i % 6) * 3 if fund else None
        price = 20.0 + i * 5 if px else None
        mcap = price * 1e9 / 10 if (px and fund) else None
        d = dt.date(2026, 10, 9)
        row = {
            "ticker": "AAPL" if i == 0 else f"T{i:03d}", "name": f"Corp {i}" if i % 3 != 2 else None,
            "exchange": "Nasdaq" if i % 2 else "NYSE", "sic": "3571" if fund else None,
            "industry": "Electronic Computers" if fund else None,
            "cik": 1000 + i, "has_fundamentals": fund, "fiscal_year_end": dt.date(2026, 6, 30) if fund else None,
            "revenue_usd": rev, "operating_income_usd": op, "net_income_usd": ni,
            "fcf_usd": ni * 0.8 if fund else None, "buyback_usd": ni * 0.3 if fund else None, "dividends_paid_usd": ni * 0.2 if fund else None,
            "eps_usd": eps,
            "operating_margin": op / rev if fund else None, "net_margin": ni / rev if fund else None,
            "fcf_margin": ni * 0.8 / rev if fund else None,
            "sales_growth_1y": -0.05 + (i % 9) * 0.04 if fund else None,
            "op_income_growth_1y": -0.1 + (i % 7) * 0.06 if fund else None,
            "net_income_growth_1y": -0.1 + (i % 5) * 0.08 if fund else None,
            "eps_growth_1y": -0.1 + (i % 5) * 0.08 if fund else None,
            "ttm_period_end": dt.date(2026, 9, 30) if fund else None,
            "ttm_revenue_usd": rev * 1.05 if fund else None, "ttm_operating_income_usd": op * 1.05 if fund else None,
            "ttm_net_income_usd": ni * 1.05 if fund else None, "ttm_eps_usd": eps * 1.05 if fund else None,
            "balance_date": dt.date(2026, 9, 30) if fund else None,
            "total_assets_usd": rev * 2 if fund else None, "total_liabilities_usd": rev * 1.2 if fund else None,
            "equity_usd": rev * 0.8 if fund else None, "cash_usd": rev * 0.3 if fund else None,
            "interest_bearing_debt_usd": rev * 0.4 if fund else None, "equity_ratio": 0.4 if fund else None,
            "shares_outstanding": 1e8 if fund else None, "shares_as_of": dt.date(2026, 9, 20) if fund else None,
            "shares_source": "dei_cover" if fund else None,
            "price_source": "price_daily" if px else None, "current_price_usd": price, "price_date": d if px else None,
            "snapshot_date": d if i % 3 == 0 else None,
            "market_cap_usd": mcap, "market_cap_bn_usd": mcap / 1e9 if mcap else None,
            "per": price / eps if (px and fund) else None,
            "ps_ratio": mcap / rev if mcap else None, "fcf_yield": ni * 0.8 / mcap if mcap else None,
            "buyback_yield": ni * 0.3 / mcap if mcap else None, "valuation_flag": None,
            "pbr": 1.0 + (i % 8) * 0.8 if (px and fund) else None, "roe": 0.05 + (i % 10) * 0.03 if fund else None,
            "dividend_yield": (i % 6) * 0.007 if (px and fund) else None,
            "return_1m": -0.1 + (i % 10) * 0.03 if px else None, "return_3m": -0.2 + (i % 12) * 0.05 if px else None,
            "return_1y": -0.3 + (i % 15) * 0.07 if px else None,
            "high_52w_usd": price * 1.2 if px else None, "low_52w_usd": price * 0.7 if px else None,
            "week52_position_pct": 41.0 + (i % 60) if px else None,
            "avg_turnover_20d_usd": 1e8 * (1 + i) if px else None, "price_days": 250 if px else None,
            "price_break_date": None,
            "composite_score": 30.0 + i if i % 3 == 0 else None, "value_score": 40.0 + i % 50 if i % 3 == 0 else None,
            "quality_score": 50.0 + i % 40 if i % 3 == 0 else None,
            "rsi14": 40.0 + i % 40 if i % 6 == 0 else None, "above_ma200": True if i % 6 == 0 else None,
            "technical_date": d if i % 6 == 0 else None,
        }
        ev = mcap + rev * 0.4 - rev * 0.3 if mcap else None
        ebitda = op * 1.2 if fund else None
        row.update({
            "ebitda_usd": ebitda, "ev_usd": ev, "ev_ebitda": ev / ebitda if ev and ebitda and ebitda > 0 else None,
            "ev_sales": ev / rev if ev else None, "tax_rate": 0.21 if fund else None,
            "roic_tax_basis": ("reported" if i % 2 else "assumed") if fund else None,
            "invested_capital_usd": rev * 1.2 if fund else None, "roic": op * 0.79 / (rev * 1.2) if fund else None,
            "gross_margin": 0.4 if fund else None, "interest_coverage": 5.0 + i % 20 if fund else None,
            "current_ratio": 1.0 + (i % 5) * 0.3 if fund else None, "accrual_ratio": -0.02 + (i % 4) * 0.01 if fund else None,
            "total_payout_yield": ni * 0.5 / mcap if mcap else None, "earnings_yield": eps / price if (px and fund) else None,
            "peg": 0.5 + (i % 6) * 0.3 if (px and fund) else None,
            "sales_cagr_3y": 0.03 + (i % 7) * 0.03 if fund else None, "op_income_cagr_3y": 0.02 + (i % 6) * 0.04 if fund else None,
            "eps_cagr_3y": 0.05 + (i % 5) * 0.04 if fund else None,
            "f_score": 3 + i % 7 if fund else None, "f_score_items": 9 if fund else 0,
        })
        _insert(c, "m_screener_us", row)


# 株価・スクリーニング: 日本株25銘柄（7001〜7025）・米国株25銘柄、2026-01〜10の月次、業種×営業日、市場×営業日
def _load_market(c) -> None:
    import datetime as dt
    months = [dt.date(2026, m, 1) for m in range(1, 11)]
    for i in range(1, 26):
        for market, ticker in (("JP", f"{7000 + i}.T"), ("US", f"T{i:03d}")):
            rising = i % 3 == 0
            base = 100.0 * (1 + i / 10)
            closes = []
            for k, m in enumerate(months):
                closes.append(base * (1.06 ** k) if rising else base * (1 + 0.04 * ((k * i) % 5 - 2)))
            for k, m in enumerate(months):
                close = closes[k]
                partial = k == len(months) - 1
                surge = i % 7 == 0 and k == len(months) - 2
                row = {
                    "ticker": ticker, "market": market,
                    "name": f"会社{i}" if market == "JP" else (f"Corp {i}" if i % 3 == 0 else None),
                    "industry": ["電気機器", "小売業", None][i % 3] if market == "JP" else None,
                    "month": m, "month_end_date": m.replace(day=28), "trading_days": 5 if partial else 20,
                    "is_partial": partial, "close": close,
                    "prev_month_close": closes[k - 1] if k else None,
                    "return_1m": close / closes[k - 1] - 1 if k else None,
                    "return_3m": close / closes[k - 3] - 1 if k >= 3 else None,
                    "return_6m": close / closes[k - 6] - 1 if k >= 6 else None,
                    "month_high": close * 1.03, "month_low": close * (0.9 + (i % 5) * 0.015),
                    "range_pct": 1.03 / (0.9 + (i % 5) * 0.015) - 1,
                    "turnover": 5e9 * i * (3.5 if surge else 1.0), "volume": 1e6 * i,
                    "turnover_ratio_3m": (3.5 if surge else 1.0) if k >= 3 else None,
                }
                _insert(c, "m_price_monthly", row)
    days = [dt.date(2026, 8, 1) + dt.timedelta(days=n) for n in range(60)]
    for si, ind in enumerate(["電気機器", "小売業", "銀行業"]):
        idx_ew = idx_cw = 100.0
        for n, d in enumerate(days):
            r_ew = 0.002 * (si - 1) + 0.001 * ((n % 5) - 2)
            r_cw = r_ew * 1.2
            idx_ew *= 1 + r_ew
            idx_cw *= 1 + r_cw
            _insert(c, "m_sector_perf", {
                "industry": ind, "trade_date": d, "n_stocks": 10 + si, "n_up": 6, "n_down": 4, "up_ratio": 0.6,
                "ret_ew": r_ew, "ret_cw": r_cw, "idx_ew": idx_ew, "idx_cw": idx_cw,
                "ret_1m_ew": idx_ew / 100 - 1 if n >= 21 else None, "ret_3m_ew": idx_ew / 100 - 1 if n >= 59 else None,
                "ret_ytd_ew": idx_ew / 100 - 1, "ret_1m_cw": idx_cw / 100 - 1 if n >= 21 else None,
                "ret_3m_cw": idx_cw / 100 - 1 if n >= 59 else None, "ret_ytd_cw": idx_cw / 100 - 1, "turnover": 1e10 * (si + 1)})
    for market in ("JP", "US"):
        idx = 100.0
        for n, d in enumerate(days):
            full = n != len(days) - 1
            idx *= 1.001
            _insert(c, "m_market_breadth", {
                "market": market, "trade_date": d, "n_stocks": 3800 if full else 300, "is_complete": full,
                "n_up": 2000, "n_down": 1500, "n_flat": 300, "up_ratio": 2000 / 3500,
                "ad_ratio_25d": 100.0 + n if n >= 25 else None, "n_hl_base": 3500, "n_new_high": 50 + n, "n_new_low": 40,
                "n_ma50": 3500, "pct_above_ma50": 0.45, "n_ma200": 3000 if n >= 30 else 0,
                "pct_above_ma200": 0.5 if n >= 30 else None, "ret_ew": 0.001, "idx_ew": idx, "turnover": 8e12})


# 業種2つ（電気機器1016・医薬品1008）×テーマ各3つ。実在銘柄は業種ごとに3社（うち1社は 6501）。
THEME_SECTORS = [("1016", "電気機器", "midstream", ["AI半導体", "5G通信機器", "スマートグリッド"]),
                 ("1008", "医薬品", "downstream", ["後発薬", "AI創薬", "がん免疫療法"])]


def _load_theme(c) -> None:
    import datetime as dt
    for si, (sec, sname, pos, themes) in enumerate(THEME_SECTORS):
        for ti, th in enumerate(themes):
            _insert(c, "m_theme_def", {"theme_name": th, "market": "JP", "sector_code": sec, "sector_name_ja": sname,
                                       "supply_chain_position": pos, "theme_order": ti, "company_count": 3})
            for k in range(3):
                code = "6501" if (si == 0 and k == 0) else f"{7100 + si * 10 + k}"
                n = si * 3 + k
                sales = (100 + n * 20) * 1e9
                _insert(c, "m_theme", {
                    "theme_name": th, "market": "JP", "sector_code": sec, "sector_name_ja": sname,
                    "supply_chain_position": pos, "ticker": f"{code}.T", "sec_code4": code,
                    "edinet_code": f"E9{code}", "name": f"会社{code}", "industry": ["電気機器", "精密機器"][k % 2],
                    "relevance": 1.0, "source": "config_representative", "fiscal_year": 2026, "net_sales": sales,
                    "operating_income": sales * (0.04 + 0.02 * k), "net_income": sales * 0.03,
                    "operating_margin": 0.04 + 0.02 * k, "sales_growth": 0.02 + 0.03 * n})
            for d in range(0, 40, 5):
                for cat, dr in (("screener_pass", "bullish"), ("drift_spike", "bearish")):
                    _insert(c, "m_theme_signal", {
                        "theme_name": th, "sector_code": sec, "sector_name_ja": sname,
                        "fired_at": dt.date(2026, 10, 5) - dt.timedelta(days=d), "days_before_latest": d,
                        "signal_category": cat, "direction": dr, "signal_count": 3 + d % 4,
                        "ticker_signal_count": 3 if cat == "screener_pass" else 0, "ticker_count": 2,
                        "avg_signal_value": 1.0 if cat == "screener_pass" else None})
    _insert(c, "m_theme_def", {"theme_name": "AIインフラ", "market": "US", "sector_code": "45",
                               "sector_name_ja": "情報技術", "supply_chain_position": None, "theme_order": 0,
                               "company_count": 0})


MARTS = set(ALL_MARTS)
# 実体化済みの別マートを入力にするマート（業種の時価総額加重の重み・土地の含み益）。逆向きの依存は作らない
MART_DEPENDS = {"m_sector_perf": ("m_screener",), "m_screener": ("m_re_holdings_pref",)}
TEMPLATES = bt.load_templates()


def test_template_count_and_unique_ids():
    assert len(TEMPLATES) >= 15
    for domain in ("geo", "realestate", "taxonomy", "screener"):
        assert sum(1 for t in TEMPLATES if t["domain"] == domain) >= 8, domain
    assert sum(1 for t in TEMPLATES if t["domain"] == "screener_us") >= 6
    assert sum(1 for t in TEMPLATES if t["domain"] == "market") >= 10
    assert sum(1 for t in TEMPLATES if t["domain"] == "us") >= 10
    assert sum(1 for t in TEMPLATES if t["domain"] == "research") >= 5
    ids = [t["id"] for t in TEMPLATES]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("t", TEMPLATES, ids=[t["id"] for t in TEMPLATES])
def test_template_shape_and_dialect(t):
    assert bt.validate_template(t, MARTS) == []


@pytest.mark.parametrize("t", TEMPLATES, ids=[t["id"] for t in TEMPLATES])
def test_template_runs_on_duckdb(con, t):
    sql = bt.render_sql(t)
    cur = con.execute(sql)
    names = [d[0] for d in cur.description]
    rows = cur.fetchall()
    assert rows, f"{t['id']}: 既定値で0行（合成データでも空なら条件か列名の誤り）"
    chart = t["chart"]
    wanted = [chart["x"]] + (chart["y"] if isinstance(chart["y"], list) else [chart["y"]])
    if chart.get("series"):
        wanted.append(chart["series"])
    for col in wanted:
        assert col in names, f"{t['id']}: chart が参照する列 {col} が結果に無い"


def test_mart_columns_in_sync_with_test_definition():
    cols = mart_columns("m_fin_annual")
    assert "edinet_code" in cols and "fiscal_year" in cols and len(cols) == len(set(cols))
    assert mart_columns("m_company") == [
        "edinet_code", "sec_code4", "name", "industry", "accounting_standard", "listing_status", "is_delisted"]
    for mart in ALL_MARTS:
        cols = mart_columns(mart)
        assert cols and len(cols) == len(set(cols)), mart


def test_every_mart_file_is_covered_by_this_test():
    files = {p.stem for p in (REPO / "bi" / "marts").glob("*.sql")}
    assert files == set(ALL_MARTS), files ^ set(ALL_MARTS)


def test_mart_sql_does_not_read_its_own_output_tables():
    """マートSQLが実体化後の表（bi データセット）を読むと、自分自身の出力に依存して循環する。"""
    for mart in ALL_MARTS:
        sql = (REPO / "bi" / "marts" / f"{mart}.sql").read_text(encoding="utf-8")
        body = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
        # bi.src_*（location が違う元表の写し）は元表と同じ扱いで読んでよい。MART_DEPENDS に宣言した別マートの読み取りも許す（循環しない向きだけ）
        allowed = body.replace("stock-analyzer-edinet.bi.src_", "")
        for dep in MART_DEPENDS.get(mart, ()):
            allowed = allowed.replace(f"stock-analyzer-edinet.bi.{dep}`", "")
        assert "stock-analyzer-edinet.bi." not in allowed, mart


def test_param_type_checks():
    t = next(x for x in TEMPLATES if x["id"] == "fin_company_10y")
    assert "'7203'" in bt.render_sql(t, {"sec_code4": "7203", "years": 5})
    for bad in ({"sec_code4": "7203' OR '1'='1"}, {"sec_code4": "a;b"}, {"sec_code4": ""},
                {"sec_code4": 7203}, {"years": "5; DROP"}, {"years": 99}, {"years": 2.5},
                {"years": True}, {"nope": 1}):
        with pytest.raises(bt.TemplateError):
            bt.render_sql(t, bad)
    t2 = next(x for x in TEMPLATES if x["id"] == "fin_rnd_ranking")
    assert "1e+" not in bt.render_sql(t2, {"min_sales": 5000000000})
    with pytest.raises(bt.TemplateError):
        bt.render_sql(t2, {"min_sales": float("inf")})
    with pytest.raises(bt.TemplateError):
        bt.render_sql(t2, {"min_sales": "abc"})


def test_defaults_satisfy_own_constraints():
    for t in TEMPLATES:
        bt.render_sql(t)


@pytest.mark.parametrize("bad", [
    "SELECT MEDIAN(net_sales) FROM m_fin_annual",
    "SELECT net_sales FROM m_fin_annual QUALIFY ROW_NUMBER() OVER (ORDER BY net_sales) = 1",
    "SELECT SAFE_DIVIDE(net_sales, 2) FROM m_fin_annual",
    "SELECT CAST(net_sales AS DOUBLE) FROM m_fin_annual",
    "SELECT APPROX_QUANTILES(net_sales, 2) FROM m_fin_annual",
    "SELECT STRING_AGG(name) FROM m_fin_annual",
    "SELECT UPPER(name) FROM m_fin_annual",
    "SELECT * FROM edinet.financials",
    "SELECT * FROM `stock-analyzer-edinet.edinet.company`",
])
def test_forbidden_sql_detected(bad):
    assert bt.check_dialect(bad, MARTS), bad


def test_good_sql_passes():
    assert bt.check_dialect(
        "WITH d AS (SELECT industry, COUNT(*) AS n FROM m_fin_annual GROUP BY industry) SELECT * FROM d", MARTS) == []


def test_compose_bigquery_sql_merges_with():
    marts = {"m_fin_annual": "SELECT 1 AS fiscal_year"}
    out = bt.compose_bigquery_sql("WITH d AS (SELECT * FROM m_fin_annual) SELECT * FROM d", marts, ["m_fin_annual"])
    assert out.count("WITH ") == 1 and out.startswith("WITH m_fin_annual AS (")
    assert "),\nd AS (SELECT" in out
    out2 = bt.compose_bigquery_sql("SELECT * FROM m_fin_annual", marts, ["m_fin_annual"])
    assert out2.count("WITH ") == 1
    # 組み立て結果は DuckDB でも通る（構文の共通部分の確認）
    assert duckdb.connect(":memory:").execute(out).fetchall() == [(1,)]


def test_taxonomy_mart_guards():
    """技術分類マートの重複・欠損対策が外れていない（週次 map_id の重複、company_id='nan'、期限切れ）。"""
    sql = (REPO / "bi" / "marts" / "m_company_tax.sql").read_text(encoding="utf-8")
    assert "PARTITION BY company_id, node_id ORDER BY updated_at DESC" in sql
    assert "company_id <> 'nan'" in sql and "valid_to" in sql
    assert mart_columns("m_company_tax")[:2] == ["company_id", "entity_type"]
    node_sql = (REPO / "bi" / "marts" / "m_tax_node.sql").read_text(encoding="utf-8")
    assert "company_id <> 'nan'" in node_sql and "STARTS_WITH(company_id, 'sector:')" in node_sql


def test_taxonomy_and_theme_templates_are_single_mart():
    """ダッシュボードは1テンプレ1マートで実体化表を読むので、テンプレSQLが他のマートを参照しない。"""
    for t in TEMPLATES:
        if t["domain"] not in ("taxonomy", "theme"):
            continue
        refs = {m for m in ALL_MARTS if re.search(rf"\b{m}\b", t["sql"])}
        assert refs == {t["mart"]}, (t["id"], refs)


def test_screener_mart_shape():
    """スクリーナーは1社1行。単位（比率は小数・時価総額は円と億円）と株価の出どころの列が外れていない。"""
    cols = mart_columns("m_screener")
    assert cols[0] == "edinet_code" and len(cols) == len(set(cols))
    for need in ("ticker", "price_source", "market_cap", "market_cap_b", "net_cash_to_mcap", "sales_cagr_3y",
                 "interest_bearing_debt_basis", "technical_date"):
        assert need in cols, need
    sql = (REPO / "bi" / "marts" / "m_screener.sql").read_text(encoding="utf-8")
    assert "market = 'JP'" in sql and "ingested_at DESC" in sql
    assert "ROW_NUMBER() OVER (PARTITION BY edinet_code ORDER BY fiscal_year DESC)" in sql
    assert "p1.fiscal_year = l.fiscal_year - 1" in sql and "p3.fiscal_year = l.fiscal_year - 3" in sql


def test_screener_templates_are_single_mart():
    for t in TEMPLATES:
        if t["domain"] == "screener":
            assert t["mart"] == "m_screener" and not re.search(r"\bm_(?!screener)\w+", t["sql"]), t["id"]


def test_screener_us_mart_shape():
    """米国スクリーナーは1銘柄1行・通貨は米ドル（列名の _usd）。元表は us.company_profile / us.financials_annual の写し。株数は実数で、推定株数の列は無い。"""
    cols = mart_columns("m_screener_us")
    assert cols[0] == "ticker" and len(cols) == len(set(cols))
    for need in ("revenue_usd", "net_income_usd", "eps_usd", "current_price_usd", "market_cap_usd", "has_fundamentals",
                 "return_3m", "week52_position_pct", "valuation_flag", "price_break_date",
                 "total_assets_usd", "equity_usd", "shares_outstanding", "shares_source", "industry", "ttm_revenue_usd",
                 "pbr", "roe"):
        assert need in cols, need
    assert "shares_est" not in cols
    sql = (REPO / "bi" / "marts" / "m_screener_us.sql").read_text(encoding="utf-8")
    assert "bi.src_us_company_profile" in sql and "bi.src_us_financials_annual" in sql and "bi.src_us_fundamentals" not in sql
    assert "market = 'US'" in sql and "price_daily_latest" in sql


def test_screener_us_templates_are_single_mart():
    for t in TEMPLATES:
        if t["domain"] == "screener_us":
            assert t["mart"] == "m_screener_us" and not re.search(r"\bm_(?!screener_us)\w+", t["sql"]), t["id"]


def test_theme_mart_guards():
    """テーマ株マートの重複・プレースホルダ対策が外れていない。"""
    sql = (REPO / "bi" / "marts" / "m_theme.sql").read_text(encoding="utf-8")
    assert "PARTITION BY ticker, subtheme, sector_code ORDER BY updated_at DESC" in sql
    assert "NOT STARTS_WITH(ticker, 'SECTOR_')" in sql
    assert "prev.fiscal_year = fin.fiscal_year - 1" in sql
    sig = (REPO / "bi" / "marts" / "m_theme_signal.sql").read_text(encoding="utf-8")
    assert "COUNT(DISTINCT event_id)" in sig and "bi.src_signal_event" in sig
    assert "market = 'JP'" in sig


def test_theme_templates_cover_required_questions():
    ids = {t["id"] for t in TEMPLATES if t["domain"] == "theme"}
    assert len(ids) >= 8
    assert {"theme_company_stats", "theme_companies", "theme_company_themes", "theme_signal_recent",
            "theme_sales_growth", "theme_industry_cross"} <= ids


def test_market_marts_shape():
    """株価マート3本：日次の全件ではなく月・業種×日・市場×日の粒度で、取り込み途中の日と未完了の月が見分けられる。"""
    assert mart_columns("m_price_monthly")[:2] == ["ticker", "market"]
    for need in ("month", "is_partial", "return_1m", "return_6m", "turnover", "turnover_ratio_3m", "month_high", "range_pct"):
        assert need in mart_columns("m_price_monthly"), need
    assert mart_columns("m_sector_perf")[:2] == ["industry", "trade_date"]
    for need in ("idx_cw", "idx_ew", "ret_ytd_cw", "up_ratio"):
        assert need in mart_columns("m_sector_perf"), need
    assert mart_columns("m_market_breadth")[:2] == ["market", "trade_date"]
    for need in ("is_complete", "ad_ratio_25d", "n_new_high", "n_new_low", "pct_above_ma200", "n_hl_base"):
        assert need in mart_columns("m_market_breadth"), need
    for mart in ("m_price_monthly", "m_sector_perf", "m_market_breadth"):
        sql = (REPO / "bi" / "marts" / f"{mart}.sql").read_text(encoding="utf-8")
        assert "price_daily_latest" in sql and "INTERVAL 400 DAY" in sql, mart
        assert "break_date" in sql, mart
    assert "bi.m_screener" in (REPO / "bi" / "marts" / "m_sector_perf.sql").read_text(encoding="utf-8")


def test_market_templates_use_complete_data_only():
    """取り込み途中の日・未完了の月を使うテンプレは誤読のもと。市場広がり系は is_complete、月次系は is_partial で絞る。"""
    for t in TEMPLATES:
        if t["domain"] != "market":
            continue
        if t["mart"] == "m_market_breadth":
            assert "is_complete = TRUE" in t["sql"], t["id"]
        if t["mart"] == "m_price_monthly":
            assert "is_partial = FALSE" in t["sql"], t["id"]
