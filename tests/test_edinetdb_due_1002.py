# 2026-10-02: 「財務が2025年度まで」の会社の扱い
#   ・12月/9月決算の会社は FY2025 が最新で正しい（例 E02062: 2025年12月期・2026-03-26提出）
#   ・ただし取得後180日は取り直さなかったため、新しい有報が出ても古いまま残り得た
from datetime import date

import edinetdb_ingestor as m

HTML = ('<div>12月期</div><h2>FY2025 開示財務データ</h2>'
        '<script>{"fiscalYear": 2024}{"fiscalYear": 2025}</script>')


def test_parse_company_page():
    assert m.parse_company_page(HTML) == {"has": True, "latest_fy": 2025, "fy_month": 12}
    assert m.parse_company_page("分析に必要なデータが不足")["has"] is False


def test_fiscal_year_end_date():
    assert m.fiscal_year_end_date(2025, 12) == "2025-12-31"
    assert m.fiscal_year_end_date(2026, 3) == "2026-03-31"
    assert m.fiscal_year_end_date(2026, 2) == "2026-02-28"
    assert m.fiscal_year_end_date(None, 3) is None


def test_report_due_by_fiscal_month():
    t = date(2026, 10, 2)
    # 12月決算: 2026年12月期はまだ → FY2025 で最新
    assert not m.report_due({"held_fy": 2025, "fy_month": 12, "last_fetched": "2026-05-01"}, t)
    # 3月決算: 2026年3月期の有報は6月に出ている → FY2025 なら取り直す
    assert m.report_due({"held_fy": 2025, "fy_month": 3, "last_fetched": "2026-05-01"}, t)
    # 6月決算: 期末+100日（10/8）前なので、まだ待つ
    assert not m.report_due({"held_fy": 2025, "fy_month": 6, "last_fetched": "2026-05-01"}, t)
    # Web に新しい年度が載っていれば決算月に関係なく取り直す
    assert m.report_due({"held_fy": 2025, "web_check": {"latest_fy": 2026},
                         "last_fetched": "2026-05-01"}, t)
    # 直近14日以内に取り直したばかりなら待つ（EdinetDB側の反映待ち）
    assert not m.report_due({"held_fy": 2025, "fy_month": 3, "last_fetched": "2026-09-25"}, t)
    # 手元の年度が不明なら判定しない
    assert not m.report_due({"fy_month": 3}, t)


def test_due_company_is_refetched_within_refresh_days():
    t = date(2026, 10, 2)
    meta = {"E1": {"last_fetched": "2026-06-01", "held_fy": 2025, "fy_month": 3},
            "E2": {"last_fetched": "2026-06-01", "held_fy": 2025, "fy_month": 12}}
    got = m.select_ingest_targets([], [], [], ["E2", "E1"], meta, 30, today=t)
    assert got == ["E1"]


def test_prefilter_records_fiscal_month_and_rechecks_old_entries():
    t = date(2026, 10, 2)
    meta = {"E1": {"web_check": {"date": "2026-09-30", "has": True}}}   # 決算月なしの旧形式
    calls = []

    def checker(code):
        calls.append(code)
        return {"has": True, "latest_fy": 2025, "fy_month": 12}
    sel, skipped = m.prefilter_targets(["E1"], 1, meta, object(), checker=checker,
                                       today=t, sleep=lambda s: None)
    assert sel == ["E1"] and calls == ["E1"]
    assert meta["E1"]["fy_month"] == 12 and meta["E1"]["web_check"]["latest_fy"] == 2025


def test_earnings_no_data_is_remembered(tmp_path, monkeypatch):
    import types
    import earnings_surprise as es
    cfg = types.SimpleNamespace(DATA_DIR=str(tmp_path))
    calls = []
    monkeypatch.setattr(es, "analyze_ticker", lambda t: calls.append(t) or None)
    monkeypatch.setattr(es, "sync_module_constants", lambda *a, **k: None)
    es.run_earnings_surprise(cfg, ["X.T"])
    es.run_earnings_surprise(cfg, ["X.T"])
    assert calls == ["X.T"], "データ無しの銘柄を翌日も取り直していた"
