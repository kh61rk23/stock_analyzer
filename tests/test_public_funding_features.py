"""public_funding_features の純関数と、BQ を差し替えた build_features のテスト。"""
import json
from datetime import date
from types import SimpleNamespace

import public_funding_features as pff

TODAY = date(2026, 10, 8)


def feat(**kw):
    f = pff.empty_feat()
    f.update(kw)
    return f


def test_bonus_empty_and_none():
    assert pff.public_funding_bonus(pff.empty_feat(), None, TODAY) == (0.0, [])
    assert pff.public_funding_bonus(None, None, TODAY) == (0.0, [])


def test_bonus_each_condition():
    pt, r = pff.public_funding_bonus(feat(econ_security_materials=["半導体"]), None, TODAY)
    assert pt == 1.5 and len(r) == 1
    pt, _ = pff.public_funding_bonus(feat(ratio_to_sales=0.05), None, TODAY)
    assert pt == 1.0
    pt, _ = pff.public_funding_bonus(feat(recent_1y_yen=1e8, last_award="2026-08-01"), None, TODAY)
    assert pt == 0.5


def test_bonus_ratio_bounds():
    assert pff.public_funding_bonus(feat(ratio_to_sales=0.009), None, TODAY)[0] == 0.0
    assert pff.public_funding_bonus(feat(ratio_to_sales=0.01), None, TODAY)[0] == 1.0
    assert pff.public_funding_bonus(feat(ratio_to_sales=0.30), None, TODAY)[0] == 1.0
    assert pff.public_funding_bonus(feat(ratio_to_sales=0.31), None, TODAY)[0] == 0.0


def test_bonus_recent_award_requires_recent_and_amount():
    assert pff.public_funding_bonus(feat(recent_1y_yen=1e8, last_award="2026-01-01"), None, TODAY)[0] == 0.0
    assert pff.public_funding_bonus(feat(recent_1y_yen=0, last_award="2026-09-01"), None, TODAY)[0] == 0.0


def test_bonus_cap_and_cfg_override():
    f = feat(econ_security_materials=["半導体"], ratio_to_sales=0.1, recent_1y_yen=1, last_award="2026-10-01")
    assert pff.public_funding_bonus(f, None, TODAY)[0] == 3.0
    cfg = SimpleNamespace(SCORE_MAX_PUBLIC_FUNDING_ADJ=2.0, SEISAKU_BONUS_ECON_SECURITY=5.0)
    assert pff.public_funding_bonus(f, cfg, TODAY)[0] == 2.0


def test_sector_aggregation():
    fs = {"A": feat(recent_1y_yen=10), "B": feat(recent_1y_yen=5), "C": feat(recent_1y_yen=7), "D": feat()}
    out = pff.sector_public_funding(fs, {"A": "x", "B": "x", "C": "y"})
    assert out == {"x": 15, "y": 7}
    assert pff.sector_public_funding({}, {}) == {}


def test_ticker_to_code():
    assert pff.ticker_to_code("6758.T") == "6758"
    assert pff.ticker_to_code("67580") == "6758"
    assert pff.ticker_to_code("624A.T") == "624A"
    assert pff.ticker_to_code("AAPL") is None


def test_build_features_fills_and_saves(tmp_path):
    cfg = SimpleNamespace(DATA_DIR=tmp_path)
    calls = []

    def fetcher(codes):
        calls.append(codes)
        return [{"company_code": "7011", "total_yen": 100.0, "subsidy_yen": 60.0, "procurement_yen": 40.0,
                 "recent_1y_yen": 20.0, "n_events": 3, "last_award": "2026-09-01",
                 "rank_total_oku": 12.5, "materials": ["半導体", "半導体"], "net_sales": 200.0}]

    out = pff.build_features(cfg, ["7011.T", "6758.T", "AAPL"], fetcher=fetcher, today=TODAY)
    assert len(calls) == 1 and calls[0] == ["6758", "7011"]
    assert out["7011.T"]["ratio_to_sales"] == 0.1
    assert out["7011.T"]["econ_security_materials"] == ["半導体"]
    assert out["6758.T"] == pff.empty_feat() and out["AAPL"] == pff.empty_feat()
    saved = json.loads((tmp_path / "public_funding_features.json").read_text(encoding="utf-8"))
    assert saved["date"] == "2026-10-08" and set(saved["features"]) == {"7011.T", "6758.T", "AAPL"}


def test_build_features_no_sales_gives_none(tmp_path):
    cfg = SimpleNamespace(DATA_DIR=tmp_path)
    out = pff.build_features(cfg, ["7011"], today=TODAY, fetcher=lambda c: [
        {"company_code": "7011", "recent_1y_yen": 5.0, "net_sales": None}])
    assert out["7011"]["ratio_to_sales"] is None


def test_build_features_falls_back_to_previous(tmp_path):
    cfg = SimpleNamespace(DATA_DIR=tmp_path)
    prev = feat(total_yen=9.0)
    (tmp_path / "public_funding_features.json").write_text(
        json.dumps({"date": "2026-10-07", "features": {"7011.T": prev}}), encoding="utf-8")

    def boom(codes):
        raise RuntimeError("bq down")

    out = pff.build_features(cfg, ["7011.T", "6758.T"], fetcher=boom, today=TODAY)
    assert out["7011.T"]["total_yen"] == 9.0
    assert out["6758.T"] == pff.empty_feat()
