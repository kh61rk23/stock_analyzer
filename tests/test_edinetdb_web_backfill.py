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
