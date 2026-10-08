# 財務三表の細目を edinet.financial_items へ縦持ちで格納する
from types import SimpleNamespace

import edinetdb_fields as F
import edinetdb_ingestor as m


def test_item_definitions_cover_three_statements():
    meta = F.ITEM_META
    for k in ("cost_of_sales", "sga", "rnd_expenses", "interest_expenses", "impairment_loss"):
        assert meta[k][0] == "PL"
    for k in ("inventories", "goodwill", "trade_receivables", "short_term_loans", "treasury_stock"):
        assert meta[k][0] == "BS"
    for k in ("cf_operating", "capex", "cash_dividends_paid"):
        assert meta[k][0] == "CF"
    assert meta["sga"][1] == "販管費" and meta["sga"][2] == "円"


def test_map_items_keeps_all_numbers_and_unknown_fields():
    row = {"fiscal_year": 2026, "doc_id": "S100ABCD", "accounting_standard": "JP",
           "basis": "consolidated", "cost_of_sales": 700, "sga": 200, "rnd_expenses": 30.0,
           "brand_new_metric": 1.5, "is_restated_eps": True, "edinet_filing_url": "https://x",
           "goodwill": None}
    rows = m.map_edinetdb_financial_items(row, edinet_code="E1", company_code="1301",
                                          fiscal_year_end="2026-03-31", now="t")
    by = {r["item"]: r for r in rows}
    assert set(by) == {"cost_of_sales", "sga", "rnd_expenses", "brand_new_metric"}
    assert by["sga"]["statement"] == "PL" and by["sga"]["label_ja"] == "販管費"
    assert by["brand_new_metric"]["statement"] == "その他"   # API側の追加項目も捨てない
    assert by["cost_of_sales"]["item_id"] == "E1_2026_cost_of_sales"
    assert by["cost_of_sales"]["fiscal_year_end"] == "2026-03-31"
    assert by["cost_of_sales"]["doc_id"] == "S100ABCD"


def test_backfill_uses_leftover_calls_for_already_fetched(tmp_path, monkeypatch):
    companies = [{"edinet_code": f"E{i}", "sec_code": f"{1300 + i}0", "name": f"社{i}"}
                 for i in range(5)]
    merges = []

    class FakeRest:
        def __init__(self, *_a, **_k):
            self.calls, self.daily_budget, self.last_error = 0, 6, None

        def usage(self):
            return {"daily_remaining": 100}

        def get(self, path, params=None):
            self.calls += 1
            if path == "/companies":
                return companies
            if path.endswith("/financials"):
                return [{"fiscal_year": 2026, "revenue": 100, "sga": 20}]
            raise AssertionError(path)   # 補完は financials だけ（1社1回）

    class FakeBQ:
        def merge_rows(self, table, rows, keys, dataset="edinet"):
            merges.append((table, len(rows)))
            return True

    # 全社が取得済み（180日以内）＝通常の取込対象なし。細目だけ未格納
    meta = {c["edinet_code"]: {"last_fetched": "2026-09-30", "fetch_count": 1, "held_fy": 2026,
                               "fy_month": 3} for c in companies}
    m._save_json(tmp_path / "rotation.json", meta)
    monkeypatch.setattr(m, "DBRestClient", FakeRest)
    monkeypatch.setattr("edinet_bq_client.EDINETBQClient", FakeBQ)
    monkeypatch.setattr(m, "_bq_ingested_latest", lambda cfg: {})
    cfg = SimpleNamespace(ENABLE_EDINETDB_INGEST=True, EDINETDB_API_KEY="k",
                          EDINETDB_DAILY_BUDGET=6, EDINETDB_DOC_INDEX_RESERVE=1,
                          EDINETDB_ROTATION_META="rotation.json", EDINETDB_WEB_PRECHECK=False,
                          DATA_DIR=tmp_path, SECTOR_REPRESENTATIVE_TICKERS={})
    r = m.run_edinetdb_ingest(cfg)
    # 6回 − 会社一覧1回 − doc_index予約1回 = 4社を補完
    assert r["items_backfilled"] == 4 and r["items_companies"] == 4
    assert ("financial_items", 8) in merges
    saved = m._load_json(tmp_path / "rotation.json", {})
    assert sum(1 for v in saved.values() if v.get("items_v1")) == 4


def test_monthly_runner_skips_completed_steps(tmp_path, monkeypatch):
    """月初の複数日実行で、成功済みの月次ステップをやり直さないこと。"""
    import json
    import monthly_runner as mr
    st = tmp_path / "monthly_loop_status.json"
    monkeypatch.setattr(mr, "STATUS_PATH", st)
    monkeypatch.setattr(mr, "REPO", tmp_path)
    monkeypatch.setattr(mr, "_load_config", lambda: None)
    ran = []

    class R:
        returncode, stdout, stderr = 0, "", ""
    monkeypatch.setattr(mr.subprocess, "run", lambda cmd, **k: ran.append(cmd[1]) or R())
    from datetime import date as _date

    class _D(_date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 2)
    monkeypatch.setattr(mr, "date", _D)
    steps = mr.build_steps(None, "202609", _D.today())
    names = [n for n, *_ in steps]
    # geo_exposure だけ失敗していた日の翌日
    st.write_text(json.dumps({"target_month": "202609", "ok": False, "steps": {
        n: ("error: timeout" if n == "geo_exposure" else "success") for n in names}}))
    mr.main(["--month", "202609"])
    assert ran == ["geo_exposure_runner.py"]
    saved = json.loads(st.read_text())
    assert saved["ok"] is True and all(v == "success" for v in saved["steps"].values())
    ran.clear()
    mr.main(["--month", "202609"])      # 全完了済みなら何も走らない
    assert ran == []
