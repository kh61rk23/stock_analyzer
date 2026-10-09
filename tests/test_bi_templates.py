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

STRING_COLS = {"edinet_code", "name", "sec_code4", "industry", "accounting_standard", "listing_status"}
INT_COLS = {"fiscal_year", "employee_count"}
DATE_COLS = {"fiscal_year_end"}
BOOL_COLS = {"is_delisted"}


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
    for mart in ("m_company", "m_fin_annual"):
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
    return c


MARTS = {"m_company", "m_fin_annual"}
TEMPLATES = bt.load_templates()


def test_template_count_and_unique_ids():
    assert len(TEMPLATES) >= 15
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
