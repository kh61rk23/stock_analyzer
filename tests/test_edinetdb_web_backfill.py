# 2026-10-03: EdinetDB の会社ページ埋め込みデータから財務を補完する
import json

import edinetdb_web_backfill as w

REC = {"fiscalYear": 2025, "revenue": 4639402000.0, "costOfSales": 2.0e9, "sga": 1.5e9,
       "rndExpenses": 6.0e8, "cfOperating": 3.0e8, "cfInvesting": -1.0e8,
       "crossShareholdingBookValueNonListed": 5.0e6, "otherIbdJp": 1.0, "docID": "S100XTHX",
       "submitDateTime": "2026-03-26 15:00", "financialStatementBasis": "consolidated",
       "filerName": "テスト株式会社", "isRestatedEps": False}
HTML = ("<div>12月期</div><h2>FY2025 開示財務データ</h2><script>"
        + json.dumps({**REC, "fiscalYear": 2024, "revenue": 1.0}) + json.dumps(REC)
        + json.dumps({"fiscalYear": 2025, "rndExpenses": None}) + "</script>")


def test_snake_case_matches_api_keys():
    assert w.snake("rndExpenses") == "rnd_expenses"
    assert w.snake("ghgScope1And2") == "ghg_scope1_and2"
    assert w.snake("crossShareholdingBookValueNonListed") == "cross_shareholding_book_value_nonlisted"
    assert w.snake("docID") == "doc_id"
    assert w.snake("otherIbdJp") == "other_interest_bearing_debt_jp"


def test_extract_records_keeps_fullest_per_year():
    recs = w.extract_financial_records(HTML)
    assert [r["fiscal_year"] for r in recs] == [2025, 2024]
    assert recs[0]["cost_of_sales"] == 2.0e9 and recs[0]["doc_id"] == "S100XTHX"


def test_build_rows_fills_fye_items_and_docs():
    rows = w.build_rows("E1", "6769", w.extract_financial_records(HTML), 12, 10, "t")
    f = rows["financials"][0]
    assert f["fiscal_year_end"] == "2025-12-31" and f["net_sales"] == 4639402000.0
    items = {i["item"]: i for i in rows["financial_items"] if i["fiscal_year"] == 2025}
    assert items["sga"]["statement"] == "PL" and items["rnd_expenses"]["value"] == 6.0e8
    assert items["cross_shareholding_book_value_nonlisted"]["statement"] == "株式"
    assert rows["doc_index"][0]["disclosure_date"] == "2026-03-26"


def test_infer_fy_month_from_submissions():
    import edinetdb_ingestor as ing
    assert ing.infer_fy_month(["2026-06-25 15:00", "2025-06-26", "2024-06-27"]) == 3
    assert ing.infer_fy_month(["2026-03-26", "2025-03-27"]) == 12
    assert ing.infer_fy_month(["2025-08-28", "2024-08-29", "2023-08-30"]) == 5   # E00004 カネコ種苗
    assert ing.infer_fy_month([None, "x"]) is None


def test_exposure_merge_query_is_valid_struct():
    """2026-10-03: STRUCT の「<」欠落で全MERGEが構文エラー（権益テーブルは導入以来0行）。"""
    import re
    import jogmec_scraper as j
    q = j._build_exposure_merge_query("p", [{"ticker": "1605.T", "country_code": "AUS",
                                              "rationale": "Ichthys's LNG"}])
    assert re.search(r"STRUCT<\s*\n\s*exposure_id STRING", q)
    assert "Ichthys\\'s" in q


def test_country_profile_merge_query_types():
    """2026-10-03: STRUCT の「<」欠落と、JSON列への文字列リテラルで country_profile は0行だった。"""
    import country_profile_builder as c
    q = c._build_merge_query("p", c.COUNTRY_PROFILES[:1])
    assert "STRUCT<" in q and "JSON '[" in q


def test_due_targets_order_and_exclusions():
    from datetime import date
    meta = {"E1": {"items_v1": "2026-05-01", "held_fy": 2025, "fy_month": 3},   # 3月決算・新年度が出ている
            "E2": {"items_v1": "2026-09-30", "held_fy": 2025, "fy_month": 12},  # 12月決算・最新
            "E4": {"web_check": {"has": False}}}                               # 財務なし
    companies = [{"edinet_code": c} for c in ("E1", "E2", "E3", "E4")] + [
        {"edinet_code": "E5", "is_delisted": True}]
    assert w.due_targets(meta, companies, date(2026, 10, 3)) == ["E1", "E3"]


def test_quarterly_items_keyed_by_quarter():
    import edinetdb_ingestor as ing
    rows = ing.map_edinetdb_quarterly_items(
        {"fiscal_year": 2026, "quarter": "Q1", "net_income": 100, "revenue": 1000,
         "derivation_method": "direct"}, edinet_code="E1", company_code="1301", now="t")
    by = {r["item"]: r for r in rows}
    assert by["net_income"]["item_id"] == "E1_2026_Q1_net_income" and by["revenue"]["quarter"] == "Q1"
    assert ing.map_edinetdb_quarterly_items({"fiscal_year": 2026, "net_income": 1},
                                            edinet_code="E1") == []


def test_accel_from_edinetdb_quarters():
    import earnings_surprise as es
    q = [(2026, "Q1", 150.0), (2025, "Q1", 100.0), (2025, "Q4", 80.0)]
    a = [(2026, 1100.0), (2025, 1000.0)]
    r = es.calc_accel_from_quarters(q, a)
    assert (r["q_yoy"], r["annual_yoy"], r["accel"], r["quarter"]) == (0.5, 0.1, 0.4, "FY2026Q1")
    assert es.calc_accel_from_quarters([(2026, "Q1", 150.0)], a) is None          # 前年同期なし
    assert es.calc_accel_from_quarters(q, [(2026, 1100.0), (2024, 900.0)]) is None  # 年次が連続しない
