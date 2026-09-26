# 2026-09-26 セッション2: 配線漏れの修正
#   ・アクルーアル: 取り込みが常に None／スクリーナーは廃止済みローカルDBだけを参照
#   ・有報テキストのリスク減点を専用フラグで停止（text_extractor は生かす）
#   ・edinet_exposure_extractor: 本文列が NULL 固定・代替経路がメタ情報を本文扱い
import sys
import types

import pytest


def test_ingestor_fills_accrual_ratio():
    from edinetdb_ingestor import map_edinetdb_financials
    row = map_edinetdb_financials(
        {"fiscal_year": 2025, "net_income": 100.0, "cf_operating": 150.0,
         "total_assets": 1000.0}, edinet_code="E1", company_code="1234")
    assert row["accrual_ratio"] == pytest.approx(-0.05)
    row = map_edinetdb_financials({"fiscal_year": 2025, "net_income": 100.0},
                                  edinet_code="E1", company_code="1234")
    assert row["accrual_ratio"] is None


def test_screener_reads_accruals_from_bq_when_local_db_empty(monkeypatch):
    import stock_screener as ss
    erm = types.ModuleType("edinet_rotation_manager")
    erm.load_db = lambda cfg: {"meta": {}, "analysis": {}}
    bq = types.ModuleType("edinet_bq_client")

    class _C:
        def get_latest_accrual_ratios(self):
            return {"7203": -0.031, "6758": 0.06}
    bq.EDINETBQClient = _C
    monkeypatch.setitem(sys.modules, "edinet_rotation_manager", erm)
    monkeypatch.setitem(sys.modules, "edinet_bq_client", bq)
    assert ss._load_edinet_accruals(types.SimpleNamespace()) == {"7203.T": -0.031, "6758.T": 0.06}


def test_text_risk_bonus_is_off_by_default():
    import config
    assert config.ENABLE_TEXT_SIGNAL is True, "text_extractor は他モジュールも使う"
    assert config.ENABLE_TEXT_RISK_BONUS is False


def test_exposure_fallback_returns_section_bodies(monkeypatch):
    import edinet_exposure_extractor as m

    class _Row:
        section_type = "risk"
        text_body = None

    class _Client:
        def query(self, _q):
            return types.SimpleNamespace(result=lambda: [_Row()])
    bq = types.ModuleType("edinet_bq_client")
    bq.EDINETBQClient = lambda: types.SimpleNamespace(client=_Client())
    te = types.ModuleType("text_extractor")
    te.extract_sections = lambda cfg, code: {
        "securities_code": code, "company_name": "トヨタ",
        "sections": {"business": "海外売上の比率は…", "risk": ""}}
    monkeypatch.setitem(sys.modules, "edinet_bq_client", bq)
    monkeypatch.setitem(sys.modules, "text_extractor", te)
    out = m._get_edinet_text_sections("7203.T", None)
    assert out == {"business": "海外売上の比率は…"}, "メタ情報や空本文を本文として返している"
