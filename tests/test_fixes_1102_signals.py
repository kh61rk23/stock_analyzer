"""geo / stress / independence / US sector / funding / tracker / rule_optimizer / position_sizer の修正テスト。"""
import json
from types import SimpleNamespace

import pytest

import geo_signal as geo
import stress_tester as stt
import independence_score_calculator as isc
import us_sector_pipeline as usp
import public_funding_features as pff
import performance_tracker as pt
import rule_optimizer as ro
import position_sizer as ps


# ── geo_signal ──────────────────────────────────────────────
def test_geo_raw_impact_uses_risk_in_points():
    # 関与度 0.5 × リスク 40点 × 0.3 = 6.0pt（以前は 0.06pt）
    assert geo._raw_impact(0.5, 40.0, 0.3) == pytest.approx(6.0)


def test_geo_bonus_reaches_meaningful_range_and_clamps(monkeypatch):
    monkeypatch.setattr(geo, "_get_risk_scores_for_countries", lambda codes, *a, **k: {c: 36.0 for c in codes})
    cfg = SimpleNamespace(ENABLE_GEO_SIGNAL=True, GEO_IMPACT_SCALE=0.30, GEO_IMPACT_MAX=15.0,
                          GEO_IMPACT_MIN=-15.0,
                          GEO_COMMODITY_DIRECTION_JP={"crude_oil": {"1009": 1.0}})
    exposures = [{"country_code": "SAU", "exposure_strength": 0.6, "commodity_code": "crude_oil",
                  "composite_risk_score": 36.0}]
    b = geo.calc_geo_impact_bonus_jp("1605.T", "1009", cfg, prefetched_exposures=exposures)
    assert b == pytest.approx(0.6 * 36 * 0.3)             # 6.48
    big = exposures * 5
    assert geo.calc_geo_impact_bonus_jp("1605.T", "1009", cfg, prefetched_exposures=big) == 15.0


def test_geo_undefined_direction_threshold():
    cfg = SimpleNamespace(GEO_UNDEFINED_DIR_RISK_MIN=40.0)
    assert geo._undefined_direction(41.0, cfg) == -0.5
    assert geo._undefined_direction(35.0, cfg) == 0.0
    assert geo._undefined_direction(57.9, None) == 0.0      # 既定は 58（国別リスク上位約15%）
    assert geo._undefined_direction(58.0, None) == -0.5
    import config
    assert config.GEO_UNDEFINED_DIR_RISK_MIN == geo.UNDEFINED_DIRECTION_RISK_MIN


def test_geo_undefined_direction_penalty_in_breakdown(monkeypatch):
    monkeypatch.setattr(geo, "_get_risk_scores_for_countries", lambda codes, *a, **k: {"RUS": 62.0, "VNM": 12.0})
    cfg = SimpleNamespace(ENABLE_GEO_SIGNAL=True, GEO_IMPACT_SCALE=0.30, GEO_IMPACT_MAX=15.0,
                          GEO_IMPACT_MIN=-15.0, GEO_UNDEFINED_DIR_RISK_MIN=58.0)
    ex = [{"country_code": "RUS", "exposure_strength": 0.5, "commodity_code": None},
          {"country_code": "VNM", "exposure_strength": 0.5, "commodity_code": None}]
    b = geo.calc_geo_impact_bonus_jp("9999.T", "1009", cfg, prefetched_exposures=ex)
    assert b == pytest.approx(-0.5 * 0.5 * 62 * 0.3)         # 高リスク国だけ減点（VNM は 0）
    bd = geo.get_geo_breakdown("9999.T", "JP")
    assert bd["undefined_dir"] == pytest.approx(b) and bd["direction_raw"] == 0.0 and bd["bonus"] == b


def test_geo_sector_score_spreads():
    cfg = SimpleNamespace(ENABLE_GEO_SIGNAL=True, GEO_IMPACT_MAX=15.0, GEO_IMPACT_MIN=-15.0)
    def sec(bonus):
        return {"top_companies": [{"fetch_ok": True, "geo_impact_bonus": bonus}]}
    res = {"sectors": {"a": sec(6.5), "b": sec(-6.5), "c": sec(0.0)}}
    sc = {k: geo.aggregate_geo_signal(k, res, cfg) for k in "abc"}
    assert sc["c"] == 50.0 and sc["a"] > 70 and sc["b"] < 30


# ── stress_tester ───────────────────────────────────────────
def _series(rets, start=100.0):
    out, p = {}, start
    for i, r in enumerate(rets):
        p *= 1 + r
        out[f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}"] = p
    return out


def test_estimate_beta_recovers_known_beta():
    import random
    random.seed(1)
    m = [random.gauss(0, 0.01) for _ in range(120)]
    bench = _series(m)
    stock = _series([1.5 * x for x in m])
    assert stt.estimate_beta(stock, bench) == pytest.approx(1.5, abs=0.05)


def test_estimate_beta_none_when_too_short():
    assert stt.estimate_beta(_series([0.01] * 10), _series([0.01] * 10)) is None


def test_zero_beta_is_kept_in_scenario():
    h = {"ticker": "A", "name": "a", "market": "JP"}
    items = [{"holding": h, "stock_data": {"beta": 0.0, "beta_source": "info"},
              "position_value_jpy": 1_000_000.0}]
    r = stt.calc_stress_scenario(items, {"nikkei_drawdown": -0.3, "sp500_drawdown": -0.3}, 150.0)
    assert r["positions"][0]["beta"] == 0.0
    assert r["positions"][0]["loss_jpy"] == 0.0
    assert r["positions"][0]["beta_assumed"] is False


def test_missing_beta_flagged_assumed():
    h = {"ticker": "A", "name": "a", "market": "JP"}
    items = [{"holding": h, "stock_data": None, "position_value_jpy": 1_000_000.0}]
    r = stt.calc_stress_scenario(items, {"nikkei_drawdown": -0.3, "sp500_drawdown": -0.3}, 150.0)
    assert r["positions"][0]["beta"] == 1.0 and r["positions"][0]["beta_assumed"] is True


# ── independence ────────────────────────────────────────────
def _fake_returns(monkeypatch, stock, index):
    monkeypatch.setattr(isc, "_fetch_price_returns",
                        lambda *a, **k: (stock, index, len(stock)))
    monkeypatch.setattr(isc, "_load_edinet_text_for_ticker", lambda *a, **k: "")


def test_low_r2_is_undetermined(monkeypatch):
    import random
    random.seed(3)
    idx = [random.gauss(0, 0.01) for _ in range(60)]
    stock = [random.gauss(0.002, 0.02) for _ in range(60)]      # 指数と無関係なノイズ
    _fake_returns(monkeypatch, stock, idx)
    d = isc.calc_independence_score_for_ticker("X.T", "JP", None)
    assert d["r_squared"] < 0.05
    assert d["independence_tier"] == isc.TIER_UNDETERMINED
    assert d["independence_determined"] is False
    assert d["independence_score"] == 0.0
    assert d["independence_score_raw"] >= 0
    assert isc.get_latent_pearl_candidates({"X.T": d}, threshold=0) == []


def test_alpha_is_capped(monkeypatch):
    import random
    random.seed(4)
    idx = [random.gauss(0, 0.01) for _ in range(60)]
    stock = [0.5 * x + 0.004 for x in idx]          # 日次 +0.4% → 年率 +100%
    _fake_returns(monkeypatch, stock, idx)
    d = isc.calc_independence_score_for_ticker("X.T", "JP", None)
    assert d["alpha_annual"] == pytest.approx(isc.ALPHA_ANNUAL_CAP)
    assert d["independence_determined"] is True


def test_is_determined_old_cache_entries():
    assert isc.is_determined({"r_squared": 0.01}) is False
    assert isc.is_determined({"r_squared": 0.2}) is True


def test_normalize_index_aligns_dates():
    pd = pytest.importorskip("pandas")
    a = pd.Series([1.0, 2.0], index=pd.to_datetime(["2026-08-03 09:00", "2026-08-04 09:00"]).tz_localize("Asia/Tokyo"))
    b = pd.Series([1.0, 2.0], index=pd.to_datetime(["2026-08-03", "2026-08-04"]))
    na, nb = isc._normalize_index(a), isc._normalize_index(b)
    assert len(na.to_frame("s").join(nb.to_frame("i"), how="inner")) == 2


# ── US sector ───────────────────────────────────────────────
def _etf(r1, r3, r12, ratio=1.0):
    return {"fetch_ok": True, "current": 100.0 * ratio, "high_52w": 100.0, "ret_1m": r1,
            "ret_3m": r3, "ret_12m": r12, "vol_trend": 1.0}


SEC = {"etf": "XLE", "macro_sensitivity": {}}


def test_score_is_normalized_0_100():
    s = usp.score_sector(SEC, {"XLE": _etf(11, 20, 60)}, {})
    assert 0 <= s["score"] <= 100
    assert s["raw_score"] == 50.0
    assert s["score"] == pytest.approx(50 + 50 * 50 / 75, abs=0.1)
    bad = usp.score_sector(SEC, {"XLE": _etf(-9, -10, -13, ratio=0.8)}, {})
    assert 0 <= bad["score"] < 50 and bad["raw_score"] < 0


def test_saturated_sectors_get_distinct_scores():
    data = {"A": _etf(11, 20, 64), "B": _etf(11, 14, 30), "C": _etf(11, 6, 20),
            "D": _etf(2, 1, 5), "SPY": _etf(3, 5, 15)}
    rs = []
    for k in "ABCD":
        sr = usp.score_sector({"etf": k, "macro_sensitivity": {}}, data, {})
        rs.append({"sector": {"etf": k}, "score_result": sr})
    assert len({r["score_result"]["score"] for r in rs[:3]}) == 1           # 絶対点は同点
    usp.apply_relative_strength(rs, data["SPY"])
    sc = [r["score_result"]["score"] for r in rs]
    assert sc[0] > sc[1] > sc[2] > sc[3]


def test_risk_level_branches_are_distinct():
    lo = usp.score_sector(SEC, {"XLE": _etf(11, 20, 64)}, {})
    assert lo["risk_level"] == "low"
    mid = usp.score_sector(SEC, {"XLE": _etf(0, 0, 0, ratio=0.9)}, {})
    assert mid["risk_level"] == "medium"
    hi = usp.score_sector(SEC, {"XLE": _etf(-9, -10, -13, ratio=0.8)}, {})
    assert hi["risk_level"] == "high"


# ── public funding ──────────────────────────────────────────
def test_ratio_none_when_recent_zero():
    f = pff._row_to_feat({"recent_1y_yen": 0, "net_sales": 1e9, "total_yen": 5e8})
    assert f["ratio_to_sales"] is None
    f = pff._row_to_feat({"recent_1y_yen": 1e8, "net_sales": 1e9})
    assert f["ratio_to_sales"] == pytest.approx(0.1)


# ── performance_tracker / rule_optimizer ────────────────────
def test_screener_history_keeps_pearl_fields(tmp_path):
    cfg = SimpleNamespace(DATA_DIR=str(tmp_path), SCREENER_HISTORY_MAX_ROWS=80)
    cand = {"ticker": "1.T", "pass_filters": True, "composite_score": 70, "is_latent_pearl": True,
            "pearl_tier": "S", "pearl_score": 81.0, "latent_pearl_score": 66.0,
            "independence_score": 70.0, "catalyst_flags": ["上方修正"], "midterm_trajectory": "ahead",
            "midterm_overall_score": 77.0, "technical_score": 55.0,
            "macro_signals": [{"factor": "金利", "signal": "tailwind", "extra": "x"}]}
    pt.append_screener_history(cfg, {"candidates": [cand]})
    hist = pt.load_screener_history(cfg)
    row = list(hist.values())[0][0]
    assert row["is_latent_pearl"] is True and row["pearl_tier"] == "S"
    assert row["pearl_score"] == 81.0 and row["latent_pearl_score"] == 66.0
    assert row["independence_score"] == 70.0 and row["catalyst_flags"] == ["上方修正"]
    assert row["midterm_trajectory"] == "ahead" and row["midterm_overall_score"] == 77.0
    assert row["technical_score"] == 55.0
    assert row["macro_signals"] == [{"factor": "金利", "signal": "tailwind"}]


def test_contraction_history_replaces_same_date(tmp_path):
    cfg = SimpleNamespace(CACHE_DIR=str(tmp_path))
    ro.save_contraction_history(cfg, {"mode": "Normal", "triggered_count": 0})
    ro.save_contraction_history(cfg, {"mode": "Warning", "triggered_count": 2,
                                      "triggered_flags": ["a"], "coefficient": 0.8})
    p = tmp_path / "contraction" / "contraction_history.jsonl"
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["mode"] == "Warning"
    # 別日の行は残る
    p.write_text(json.dumps({"date": "2020-01-01", "mode": "Normal"}) + "\n" + p.read_text(encoding="utf-8"),
                 encoding="utf-8")
    ro.save_contraction_history(cfg, {"mode": "Normal"})
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    assert [r["date"] for r in rows][0] == "2020-01-01" and len(rows) == 2 and rows[1]["mode"] == "Normal"


# ── position_sizer ──────────────────────────────────────────
ATR = {"current_price": 1000.0, "atr14": 10.0}


def test_unrated_conviction_is_l3_and_flagged():
    r = ps.calc_position_size(ATR, 10_000_000.0)
    assert r["conviction_level"] == 3 and r["conviction_score"] == 50.0
    assert r["conviction_unrated"] is True
    assert r["recommended_pct"] == pytest.approx(0.06)


def test_position_capped_by_recommended_pct():
    cv = {"conviction_level": 4, "conviction_score": 80.0, "recommended_pct": 0.10}
    r = ps.calc_position_size(ATR, 10_000_000.0, conviction_data=cv)
    # 基礎サイズ: リスク 10 万円 / (2×10) = 5000 株 = 500 万円(50%) → 上限 10%
    assert r["position_pct"] <= 10.0 and r["capped"] is True
    assert r["conviction_unrated"] is False


def test_level1_gets_zero_shares_not_min_lot():
    cv = {"conviction_level": 1, "conviction_score": 30.0, "recommended_pct": 0.0}
    r = ps.calc_position_size(ATR, 10_000_000.0, conviction_data=cv)
    assert r["suggested_shares"] == 0 and r["capped"] is False


def test_max_pct_never_exceeds_global_max():
    cv = {"conviction_level": 5, "conviction_score": 95.0, "recommended_pct": 0.15}
    r = ps.calc_position_size(ATR, 10_000_000.0, conviction_data=cv)
    assert r["position_pct"] <= 15.0
