"""DCF・バックテスト・自己評価・シナリオ・長期スタンスの修正のテスト（ネットワーク・BQ なし）。"""
from types import SimpleNamespace

import pytest

import dcf_valuation as dcf
import backtester as bt
import self_evaluator as se
import scenario_analyzer as sa
import longterm_scenario as lt


# ── DCF ──────────────────────────────────────────────────────
def test_dcf_bonus_is_gradual_and_capped():
    assert dcf.calc_dcf_bonus(10) == pytest.approx(1.0)
    assert dcf.calc_dcf_bonus(20) == pytest.approx(2.0)
    assert dcf.calc_dcf_bonus(60) == pytest.approx(5.0)
    assert dcf.calc_dcf_bonus(100) == pytest.approx(8.0)
    assert dcf.calc_dcf_bonus(250) == pytest.approx(8.0)
    assert dcf.calc_dcf_bonus(5) == 0.0
    assert dcf.calc_dcf_bonus(-25) == -6.0


def test_dcf_bonus_invalid_over_300():
    assert dcf.calc_dcf_bonus(300.5) == 0.0
    assert not dcf.is_valid_discount(6823.1)
    assert dcf.is_valid_discount(299.0)
    assert not dcf.is_valid_discount(None)


def test_dcf_skip_financials_and_high_fcf_yield():
    assert dcf.dcf_skip_reason({"sector": "Financial Services", "industry": "Banks - Regional"})
    assert dcf.dcf_skip_reason({"industry": "Insurance - Life"})
    assert dcf.dcf_skip_reason({"sector_code": "1030"})
    assert dcf.dcf_skip_reason({"fcf_avg": 120.0, "market_cap": 100.0})   # FCF/時価総額 > 15%
    assert dcf.dcf_skip_reason({"sector": "Industrials", "industry": "Machinery",
                                "fcf_avg": 5.0, "market_cap": 100.0}) is None


def test_wacc_floor():
    assert dcf.calc_wacc(0.1, 0.015) == pytest.approx(dcf.DCF_WACC_FLOOR)   # 既定の下限
    assert dcf.calc_wacc(0.1, 0.015, floor=0.08) == pytest.approx(0.08)
    assert dcf.calc_wacc(1.0, 0.015, floor=0.0) == pytest.approx(0.015 + dcf.MARKET_RISK_PREM)


def test_dcf_run_skips_bank_and_excludes_from_top(tmp_path, monkeypatch):
    def fake(ticker):
        if ticker == "8303.T":
            return {"fcf_avg": 1e12, "beta": 1.0, "shares_outstanding": 1e9, "net_debt": 0,
                    "current_price": 1600.0, "market_cap": 1.6e12, "currency": "JPY",
                    "name": "bank", "sector": "Financial Services", "industry": "Banks - Regional"}
        return {"fcf_avg": 1e9, "beta": 1.0, "shares_outstanding": 1e8, "net_debt": 0,
                "current_price": 100.0, "market_cap": 1e10, "currency": "JPY", "name": "mfg",
                "sector": "Industrials", "industry": "Machinery"}
    monkeypatch.setattr(dcf, "_fetch_dcf_inputs", fake)
    monkeypatch.setattr(dcf.time, "sleep", lambda s: None)
    cfg = SimpleNamespace(DATA_DIR=str(tmp_path))
    out = dcf.run_dcf_valuation(cfg, ["8303.T", "7203.T"])
    assert out["results"]["8303.T"]["dcf_bonus"] == 0.0
    assert out["results"]["8303.T"]["dcf_skip"]
    assert all(t["ticker"] != "8303.T" for t in out["top_undervalued"])
    assert out["results"]["7203.T"]["schema"] == dcf.CACHE_SCHEMA


def test_dcf_old_cache_entries_are_recomputed(tmp_path):
    import json
    (tmp_path / dcf.CACHE_FILENAME).write_text(json.dumps({
        "cached_at": __import__("datetime").datetime.now().isoformat(),
        "results": {"A.T": {"discount_pct": 900, "dcf_bonus": 8.0}}}), encoding="utf-8")
    assert dcf._load_cache(SimpleNamespace(DATA_DIR=str(tmp_path))) == {}


# ── バックテスト ────────────────────────────────────────────
def test_max_drawdown_not_compounded_across_tickers():
    # 同じ日に 3 銘柄 -10%、翌日に 3 銘柄 +10% : 直列複利だと -27% 級、日付平均なら -10%
    rets = [-10, -10, -10, 10, 10, 10]
    dates = ["2026-08-01"] * 3 + ["2026-08-02"] * 3
    st = bt.calc_performance_stats(rets, 0.0, dates)
    assert st["max_drawdown"] == pytest.approx(-10.0)
    assert st["worst_return"] == pytest.approx(-10.0)


def test_max_drawdown_many_records_not_minus_100():
    rets = [-5.0] * 900
    # 900 件が 2 日に分かれていれば、日付別平均 -5% の 2 日分(-9.75%)にしかならない
    dates = ["2026-08-01"] * 450 + ["2026-08-02"] * 450
    st = bt.calc_performance_stats(rets, 0.0, dates)
    assert st["max_drawdown"] > -15


def test_fill_prices_uses_adjusted_close_for_entry():
    # 保存された生の価格 1000 円は 1:10 分割前。調整済み終値 100 を entry に使う
    recs = [{"ticker": "A", "entry_date": "2026-08-03", "exit_date": "2026-08-31",
             "entry_price": 1000.0, "composite_score": 70.0, "sector_code": "1",
             "cluster_phase_bonus": 0.0}]
    closes = {"A": {"2026-08-03": 100.0, "2026-08-31": 105.0}}
    bt.fill_prices(recs, closes, {})
    r = recs[0]
    assert r["entry_price"] == 100.0 and r["entry_price_raw"] == 1000.0
    assert r["entry_price_source"] == "close"
    res = bt.run_backtest_analysis(recs, {})
    assert res["top_performers"][0]["return_pct"] == pytest.approx(5.0)


def test_fill_prices_falls_back_to_stored_price():
    recs = [{"ticker": "A", "entry_date": "2026-08-03", "exit_date": "2026-08-31",
             "entry_price": 1000.0, "composite_score": 70.0, "sector_code": "1",
             "cluster_phase_bonus": 0.0}]
    bt.fill_prices(recs, {"A": {"2026-08-31": 1100.0}}, {})
    assert recs[0]["entry_price"] == 1000.0
    assert recs[0]["entry_price_source"] == "screener_raw"


# ── 自己評価 ─────────────────────────────────────────────────
def test_weekly_summary_keeps_none_for_empty_band():
    ev = [{"composite_score": 70, "alpha": 1.0, "sector_code": "1"},
          {"composite_score": 72, "alpha": -1.0, "sector_code": "1"}]
    e = se.build_weekly_summary(ev, "2026-W45", 0.0)
    assert e["win_rate_score80+"] is None
    assert e["win_rate_60_80"] == 0.5
    assert e["band_counts"]["80+"] == 0
    assert se.fmt_win_rate(e["win_rate_score80+"], e["band_counts"]["80+"]) == "—(n=0)"
    assert se.fmt_win_rate(0.5) == "50.0%"


def test_report_renders_no_sample_band():
    ev = [{"composite_score": 70, "alpha": 1.0, "sector_code": "1"}]
    e = se.build_weekly_summary(ev, "2026-W45", 0.0)
    log = se.update_log_summary({"log_entries": [e]})
    md = se.format_self_eval_report(log, e)
    assert "—(n=0)" in md


def test_update_log_summary_handles_all_none():
    e = {"win_rate_overall": None, "avg_alpha": 0, "evaluated": 0}
    log = se.update_log_summary({"log_entries": [e]})
    assert log["summary"]["lifetime_win_rate"] is None


# ── シナリオ ─────────────────────────────────────────────────
def _scen(shock, delta=0.0, markets=None):
    return {"market_shock_pct": shock, "shock_markets": markets,
            "sector_scores": {"1016": {"delta": delta}}}


def test_bear_case_portfolio_is_negative_with_beta():
    hold = [{"ticker": "A", "quantity": 100, "current_price": 1000.0, "avg_cost_jpy": 900,
             "market": "JP", "sector_code": "1016", "beta": 1.2}]
    imp = sa.calc_portfolio_impact(hold, {"bear_case": _scen(-15.0, delta=-2.0)})
    pct = imp["scenario_pnl"]["bear_case"]["pnl_pct"]
    assert pct == pytest.approx(-15.0 * 1.2 + (-2.0) * 0.3, abs=0.01)
    assert imp["beta_assumed_count"] == 0


def test_beta_assumed_when_missing_and_flagged():
    hold = [{"ticker": "A", "quantity": 100, "current_price": 1000.0, "avg_cost_jpy": 900,
             "market": "JP", "sector_code": "9999"}]
    imp = sa.calc_portfolio_impact(hold, {"bear_case": _scen(-15.0)})
    assert imp["holdings"][0]["beta_assumed"] is True
    assert imp["scenario_pnl"]["bear_case"]["pnl_pct"] == pytest.approx(-15.0)
    assert imp["beta_assumed_count"] == 1


def test_weak_yen_shock_applies_to_jp_only():
    hold = [{"ticker": "A", "quantity": 100, "current_price": 1000.0, "avg_cost_jpy": 900,
             "market": "JP", "sector_code": "1", "beta": 1.0},
            {"ticker": "B", "quantity": 10, "current_price": 100.0, "avg_cost_jpy": 90,
             "market": "US", "sector_code": "1", "beta": 1.0}]
    imp = sa.calc_portfolio_impact(hold, {"weak_yen": _scen(3.0, markets=("JP",))}, usdjpy=150.0)
    by = {h["ticker"]: h["scenario_impacts"]["weak_yen"]["pnl_pct"] for h in imp["holdings"]}
    assert by["A"] == pytest.approx(3.0) and by["B"] == pytest.approx(0.0)


def test_presets_have_market_shock():
    assert sa.PRESET_SCENARIOS["bear_case"]["market_shock_pct"] < 0
    assert sa.PRESET_SCENARIOS["bull_case"]["market_shock_pct"] > 0
    assert sa.PRESET_SCENARIOS["base_case"]["market_shock_pct"] == 0
    assert sa.PRESET_SCENARIOS["weak_yen"]["shock_markets"] == ("JP",)


# ── 長期スタンス ─────────────────────────────────────────────
def test_relative_long_high_reachable_with_real_range():
    scores = {str(i): 43.5 + i * 0.5 for i in range(33)}      # 43.5〜59.5
    hi = lt.relative_long_high(scores)
    assert lt.LONG_HIGH_FLOOR <= hi < 65.0
    st = lt.determine_investment_stance("x", 59.0, 70.0, long_high_base=hi)
    assert st["stance"] == "core_hold"


def test_relative_long_high_floor_and_few_sectors():
    assert lt.relative_long_high({str(i): 50.0 for i in range(33)}) == lt.LONG_HIGH_FLOOR
    assert lt.relative_long_high({"a": 60.0}) == 65.0


def test_recompute_stances_uses_sector_composite():
    scores = {str(1000 + i): 43.5 + i * 0.5 for i in range(33)}
    latest = {"sector_longterm_scores": scores, "investment_stances": {}}
    top = max(scores, key=scores.get)
    results = {"sectors": {top: {"composite_score": 80.0}}, "longterm_scenario": latest}
    out = lt.recompute_stances(results, latest)
    assert out["investment_stances"][top]["stance"] == "core_hold"
    assert out["investment_stances"][top]["composite_missing"] is False
    other = min(scores, key=scores.get)
    assert out["investment_stances"][other]["composite_missing"] is True
    assert results["longterm_scenario"] is latest
