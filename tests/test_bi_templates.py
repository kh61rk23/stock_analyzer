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
    "price_source", "interest_bearing_debt_basis",
}
INT_COLS = {
    "fiscal_year", "employee_count", "foreign_ministry_level", "state_dept_level", "exposure_type_count",
    "latest_foreign_ministry_level", "latest_state_dept_level", "hospital_count", "facility_count",
    "municipality_count", "population_current", "population_2050", "est_excluded_count",
    "node_level", "child_count", "company_count", "company_count_rollup",
}
DATE_COLS = {"fiscal_year_end", "risk_date", "latest_risk_date", "price_date", "snapshot_date", "technical_date"}
BOOL_COLS = {"is_delisted", "sanction_flag", "is_landlocked", "review_flag", "auto_derived", "verified_flag", "above_ma200"}
ALL_MARTS = ("m_company", "m_fin_annual", "m_country_profile", "m_country_risk", "m_company_country",
             "m_re_area", "m_re_holdings", "m_re_holdings_pref", "m_tax_node", "m_company_tax", "m_screener")


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


# 1社1行。株価系は i%3==0 が snapshot、1 が price_daily、2 が株価なし。技術指標は snapshot の一部だけ
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
        src = ["snapshot", "price_daily", None][i % 3]
        price = 1000.0 + i * 50 if src else None
        cap_b = (100.0 + i * 40) if src else None
        d = dt.date(2026, 10, 9)
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
            "price_source": src, "current_price": price, "price_date": d if src else None,
            "snapshot_date": d if src == "snapshot" else None,
            "market_cap_b": cap_b, "market_cap": cap_b * 1e8 if src else None,
            "per": 6.0 + (i % 10) * 1.5 if src else None, "pbr": 0.4 + (i % 8) * 0.25 if src else None,
            "dividend_yield": 0.01 + (i % 9) * 0.008 if src else None,
            "net_cash_to_mcap": (cash - debt) / (cap_b * 1e8) if src else None,
            "composite_score": 30.0 + i if src == "snapshot" else None,
            "value_score": 40.0 + i % 50 if src == "snapshot" else None,
            "quality_score": 50.0 + i % 40 if src == "snapshot" else None,
            "rsi14": 40.0 + i % 40 if src == "snapshot" and i % 2 == 0 else None,
            "week52_position_pct": 50.0 + (i % 50) if src == "snapshot" and i % 2 == 0 else None,
            "above_ma200": True if src == "snapshot" and i % 2 == 0 else None,
            "technical_date": d if src == "snapshot" and i % 2 == 0 else None,
        }
        _insert(c, "m_screener", row)


MARTS = set(ALL_MARTS)
TEMPLATES = bt.load_templates()


def test_template_count_and_unique_ids():
    assert len(TEMPLATES) >= 15
    for domain in ("geo", "realestate", "taxonomy", "screener"):
        assert sum(1 for t in TEMPLATES if t["domain"] == domain) >= 8, domain
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
        assert "stock-analyzer-edinet.bi." not in body, mart


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


def test_taxonomy_templates_are_single_mart():
    """ダッシュボードは1テンプレ1マートで実体化表を読むので、テンプレSQLが他のマートを参照しない。"""
    for t in TEMPLATES:
        if t["domain"] != "taxonomy":
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
