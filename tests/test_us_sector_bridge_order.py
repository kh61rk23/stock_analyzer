"""米国 8-K の決算 → JP業種ブリッジ：業種の採点より前に入力が揃い、読み手の形に合うこと。

11/06 の形（決算76件・screener 照合3件）を模した入力で、業種に信号が出ることを確かめる。
SEC・BQ には触れない。
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

import signal_dedup
import us_8k_monitor as k8
import us_sector_bridge_enhanced as bridge

_REAL_FETCH = bridge._fetch_estimate_surprises   # autouse でスタブされる前の本物
MAIN = Path(__file__).resolve().parent.parent / "stock_analyzer_main.py"


@pytest.fixture(autouse=True)
def _no_bq_facts(monkeypatch):
    """EPS の引き先 us.company_facts（BQ）には触れない。無い銘柄はローカル DB にだけ落ちる。"""
    import us_facts_quarterly
    monkeypatch.setattr(us_facts_quarterly, "lookup", lambda cfg, t: None)
    monkeypatch.setattr("us_fundamental_rotator._lookup_us_fundamentals_bq", lambda t, table_name="fundamentals": None)
    # 予想比サプライズ（yfinance）にも触れない。既定は取得なし＝前年同期比に落ちる
    bridge._SURPRISE_CACHE.clear()
    monkeypatch.setattr(bridge, "_fetch_estimate_surprises", lambda tickers, cfg: 0)
    yield
    bridge._SURPRISE_CACHE.clear()


@pytest.fixture
def cfg(tmp_path):
    return SimpleNamespace(DATA_DIR=tmp_path, US_WATCHLIST_TICKERS=[], ENABLE_US_8K_MONITOR=True,
                           ENABLE_US_SECTOR_BRIDGE=True)


def _filings(n=76):
    out = []
    for i in range(n):
        out.append({"cik": 1000 + i, "company_name": f"Co{i}", "accession_number": f"0000000000-26-{i:06d}",
                    "filing_date": "2026-11-05", "doc_url": "u", "items": ["2.02", "9.01"]})
    return out


def _write_fundamentals(cfg, ticker, latest, year_ago):
    d = Path(cfg.DATA_DIR) / "us_fundamentals_db"
    d.mkdir(exist_ok=True)
    hist = [{"val": latest}, {"val": 1}, {"val": 1}, {"val": 1}, {"val": year_ago}]
    (d / f"{ticker}.json").write_text(json.dumps({"eps_history": hist}), encoding="utf-8")


@pytest.fixture
def day(cfg, monkeypatch):
    """11/06 の形：決算76件。T0=半導体 beat、T1=半導体 miss。"""
    calls = {"fetch": 0}

    def fake_fetch(target_date):
        calls["fetch"] += 1
        return _filings()

    monkeypatch.setattr(k8, "_fetch_8k_filings", fake_fetch)
    monkeypatch.setattr(k8, "_load_cik_to_ticker_map", lambda c: {1000 + i: f"T{i}" for i in range(76)})
    monkeypatch.setattr(k8, "_notify_fundamental_rotator", lambda c, t: None)
    monkeypatch.setattr(k8, "FETCH_DELAY", 0)
    _write_fundamentals(cfg, "T0", 2.0, 1.0)
    _write_fundamentals(cfg, "T1", 0.5, 1.0)
    mapping = {t: {"cik": 1, "sic": 3674, "sic_desc": "semi", "company_name": t, "jp_sectors": ["1016"]}
               for t in ("T0", "T1")}
    monkeypatch.setattr(bridge, "update_sector_mapping", lambda c, t: mapping)
    monkeypatch.setattr(bridge, "load_sector_mapping", lambda c: mapping)
    return calls


def test_early_fetch_has_earnings_without_screener(cfg, day):
    early = k8.fetch_earnings_early(cfg, {})
    assert len(early["earnings_tickers"]) == 76
    assert early["screener_hits"] == []
    assert early["partial"] is True
    assert not (Path(cfg.DATA_DIR) / k8.CACHE_FILE).exists()   # 後段が screener 照合なしのキャッシュを返さない


def test_later_run_reuses_filings_and_matches_screener(cfg, day):
    results = {}
    results["us_8k"] = k8.fetch_earnings_early(cfg, results)
    results["us_screener"] = {"candidates": [{"ticker": "T0"}, {"ticker": "T5"}, {"ticker": "T9"}]}
    full = k8.run_us_8k_monitor(cfg, results)
    assert day["fetch"] == 1                                   # EDGAR は1回だけ
    assert full["screener_hits"] == ["T0", "T5", "T9"]
    assert len(full["earnings_tickers"]) == 76
    assert "_filings" not in full and "partial" not in full


def test_bridge_makes_signals_in_reader_shape(cfg, day):
    results = {}
    results["us_8k"] = k8.fetch_earnings_early(cfg, results)
    out = bridge.run_us_sector_bridge_earnings(cfg, results)
    assert not out.get("skipped")
    assert out["beat_tickers"] == ["T0"] and out["miss_tickers"] == ["T1"]
    assert out["jp_affected"] == ["1016"]
    for s in out["cross_signals"]:
        # 読み手（業種の横断産業補正）は signal を dict として読む
        tw = s["signal"].get("tailwind", [])
        hw = s["signal"].get("headwind", [])
        assert isinstance(tw, list) and isinstance(hw, list)
        assert s["rule_id"]
    by_dir = {s["direction"]: s for s in out["cross_signals"]}
    assert by_dir["tailwind"]["signal"] == {"tailwind": ["1016"], "headwind": []}
    assert by_dir["headwind"]["signal"] == {"tailwind": [], "headwind": ["1016"]}


def test_signals_reach_the_sector_via_signal_dedup(cfg, day):
    results = {}
    results["us_8k"] = k8.fetch_earnings_early(cfg, results)
    out = bridge.run_us_sector_bridge_earnings(cfg, results)
    results["cross_sector_signals"] = out["cross_signals"]
    got = [(x["sector_code"], x["direction"]) for x in signal_dedup.collect_all_signals(results)
           if x["source"] == "cross_sector"]
    assert ("1016", "tailwind") in got and ("1016", "headwind") in got


def test_bridge_skips_with_reason_when_us_8k_missing_or_skipped(cfg):
    assert bridge.run_us_sector_bridge_earnings(cfg, {})["skipped"] is True
    out = bridge.run_us_sector_bridge_earnings(cfg, {"us_8k": {"skipped": True, "reason": "cik_map not found"}})
    assert out["skipped"] is True and "cik_map" in out["reason"]


def test_ism_part_is_separate_and_needs_us_macro(cfg, day):
    results = {"us_8k": k8.fetch_earnings_early(cfg, {})}
    assert bridge.run_us_sector_bridge_ism(cfg, results) == []     # us_macro が無い間は出ない
    results["us_macro"] = {"score": 1}
    results["macro_snapshot"] = {"us_mfg_survey": 12.0}
    sigs = bridge.run_us_sector_bridge_ism(cfg, results)
    assert sigs and sigs[0]["signal"]["tailwind"]
    results["macro_snapshot"] = {"us_mfg_survey": 7.9}              # 閾値（+8）未満なら出ない
    assert bridge.run_us_sector_bridge_ism(cfg, results) == []
    results["macro_snapshot"] = {"us_ism": 54.0}                    # 旧キー（有償の ISM）は読まない
    assert bridge.run_us_sector_bridge_ism(cfg, results) == []


def test_main_runs_bridge_before_sector_scoring_and_ism_after_us_macro():
    src = MAIN.read_text(encoding="utf-8")
    early = src.index("run_us_sector_bridge_earnings(")
    ism = src.index("run_us_sector_bridge_ism(")
    sector_loop = src.index("for code in cfg.IMPLEMENTED_AGENTS:\n        try:")
    dedup = src.index('"signal_dedup",\n                    "/content/signal_dedup.py"')
    us_macro = src.index("run_us_macro_pipeline(")
    us_8k_full = src.index("run_us_8k_monitor(")
    assert early < sector_loop < dedup
    assert us_macro < ism
    assert early < us_8k_full                                       # 8-K 本体は米国スクリーナーの後
    assert len(re.findall(r"fetch_earnings_early\(", src)) == 1


def test_eps_comes_from_company_facts_when_local_db_has_no_ticker(cfg, monkeypatch):
    """決算銘柄の大半はローカル DB（ローテーションの約900社）に無い。company_facts から引けて判定できること。"""
    import us_facts_quarterly
    hist = [{"val": 3.0}, {"val": 1}, {"val": 1}, {"val": 1}, {"val": 1.0}]
    monkeypatch.setattr(us_facts_quarterly, "lookup",
                        lambda c, t: {"eps_history": hist} if t == "NOLOCAL" else None)
    assert not (Path(cfg.DATA_DIR) / "us_fundamentals_db" / "NOLOCAL.json").exists()
    assert bridge._eval_earnings_strength("NOLOCAL", cfg) == "beat"
    assert bridge._eval_earnings_strength("NODATA", cfg) == "unknown"


def test_classify_eps_boundaries():
    h = lambda a, b: [{"val": a}, {"val": 0}, {"val": 0}, {"val": 0}, {"val": b}]
    assert bridge._classify_eps(h(1.2, 1.0)) == "beat"
    assert bridge._classify_eps(h(0.8, 1.0)) == "miss"
    assert bridge._classify_eps(h(1.05, 1.0)) == "neutral"
    assert bridge._classify_eps(h(1.0, 0.0)) == "neutral"          # 前年がほぼ0なら比率を作らない
    assert bridge._classify_eps(h(-0.5, -1.0)) == "beat"           # 赤字縮小は改善
    assert bridge._classify_eps([{"val": 1}] * 4) == "unknown"     # 5四半期に満たない


def test_bridge_reports_why_no_signal(cfg, day):
    results = {"us_8k": k8.fetch_earnings_early(cfg, {})}
    out = bridge.run_us_sector_bridge_earnings(cfg, results)
    st = out["eval_stats"]
    assert st["no_mapping"] == 74 and st["unknown"] == 0 and st["neutral"] == 0
    assert st["basis_yoy"] == 2 and st["basis_estimate"] == 0
    assert len(out["beat_tickers"]) + len(out["miss_tickers"]) == 2


# ── 予想比サプライズ（yfinance）による beat/miss ─────────────────────────

TODAY = date(2026, 11, 6)


def _rec(sp, quarter="2026-09-30"):
    return {"quarter": quarter, "eps_estimate": 1.0, "eps_actual": 1.0, "surprise_pct": sp}


def test_classify_surprise_boundaries_and_unusable():
    c = SimpleNamespace()   # 既定の閾値: beat >= +10 / miss <= -2
    f = lambda rec: bridge._classify_surprise(rec, c, TODAY)
    assert f(_rec(10.0)) == ("beat", 10.0)
    assert f(_rec(9.9)) == ("neutral", 9.9)
    assert f(_rec(-2.0)) == ("miss", -2.0)
    assert f(_rec(-1.9)) == ("neutral", -1.9)
    assert f(None) == (None, None)
    assert f(_rec(None)) == (None, None)                    # 予想がほぼ0で率が作れない
    assert f(_rec(50.0, quarter="2026-06-30")) == (None, None)   # 今回の決算が未反映（1つ前の四半期）
    assert f(_rec(50.0, quarter="bad")) == (None, None)


def test_threshold_comes_from_config():
    c = SimpleNamespace(US_8K_SURPRISE_BEAT_PCT=3.0, US_8K_SURPRISE_MISS_PCT=-3.0)
    assert bridge._classify_surprise(_rec(3.0), c, TODAY)[0] == "beat"
    assert bridge._classify_surprise(_rec(-3.0), c, TODAY)[0] == "miss"


def test_estimate_basis_wins_over_yoy_and_is_recorded(cfg, day, monkeypatch):
    """T0 は前年同期比だと beat だが、予想比では miss（予想が高かった）。予想比が優先され記録される。"""
    today = date.today().strftime("%Y-%m-%d")
    monkeypatch.setattr(bridge, "_fetch_estimate_surprises", lambda tickers, c: bridge._SURPRISE_CACHE.update(
        {"T0": _rec(-8.0, today), "T1": None}) or 1)
    results = {"us_8k": k8.fetch_earnings_early(cfg, {})}
    out = bridge.run_us_sector_bridge_earnings(cfg, results)
    by_t = {s["trigger_ticker"]: s for s in out["cross_signals"]}
    assert by_t["T0"]["direction"] == "headwind"
    assert by_t["T0"]["basis"] == "estimate" and by_t["T0"]["surprise_pct"] == -8.0
    assert "予想比-8.0%" in by_t["T0"]["label"]
    assert by_t["T1"]["basis"] == "yoy" and by_t["T1"]["surprise_pct"] is None   # 予想なし → 前年同期比
    st = out["eval_stats"]
    assert st["basis_estimate"] == 1 and st["basis_yoy"] == 1


def test_stale_estimate_falls_back_to_yoy(cfg, day):
    bridge._SURPRISE_CACHE["T0"] = _rec(-30.0, "2020-01-01")
    d = bridge._eval_earnings_detail("T0", cfg)
    assert d["basis"] == "yoy" and d["strength"] == "beat"


def test_fetch_estimate_surprises_is_bounded_and_survives_failures(monkeypatch):
    import earnings_surprise as es
    seen = []

    def fake(t):
        seen.append(t)
        if t == "BAD":
            raise RuntimeError("yfinance down")
        return [_rec(5.0)] if t != "NONE" else []

    monkeypatch.setattr(es, "_fetch_earnings_history", fake)
    monkeypatch.setattr(es, "FETCH_DELAY", 0)
    real = _REAL_FETCH
    bridge._SURPRISE_CACHE.clear()
    cfg = SimpleNamespace(US_8K_SURPRISE_MAX_TICKERS=3)
    got = real(["a", "BAD", "NONE", "d", "a"], cfg)
    assert seen == ["A", "BAD", "NONE"]                      # 重複除去・大文字化・上限3件
    assert got == 1                                           # 取れたのは A だけ
    assert bridge._SURPRISE_CACHE["BAD"] is None and bridge._SURPRISE_CACHE["NONE"] is None
    assert real(["A"], cfg) == 0 and seen == ["A", "BAD", "NONE"]   # 取得済みは取り直さない
    assert real(["z"], SimpleNamespace(US_8K_SURPRISE_ENABLED=False)) == 0


# ── ISM の代わりの製造業景況（地区連銀の調査） ───────────────────────────

def test_mfg_survey_composite_averages_fresh_series_only():
    import us_macro_pipeline as um
    t = date(2026, 11, 6)
    ser = {"us_phil_fed_mfg": [{"date": "2026-09-01", "value": 4.0}, {"date": "2026-10-01", "value": 12.0}],
           "us_empire_mfg": [{"date": "2026-10-01", "value": 6.0}]}
    out = um.mfg_survey_composite(ser, t, 75)
    assert out == {"us_phil_fed_mfg": 12.0, "us_empire_mfg": 6.0, "us_mfg_survey": 9.0}
    ser["us_empire_mfg"] = [{"date": "2026-06-01", "value": -30.0}]   # 止まった系列は混ぜない
    assert um.mfg_survey_composite(ser, t, 75) == {"us_phil_fed_mfg": 12.0, "us_mfg_survey": 12.0}
    assert um.mfg_survey_composite({"a": []}, t, 75) == {}
    assert um.mfg_survey_composite({}, t, 75) == {}


def test_fetch_mfg_survey_no_key_no_call_and_cache(tmp_path, monkeypatch):
    import policy_tracker
    import us_macro_pipeline as um
    calls = []
    monkeypatch.setattr(policy_tracker, "fetch_fred_data",
                        lambda k, sid, days=90: calls.append(sid) or [{"date": date.today().strftime("%Y-%m-%d"), "value": 10.0}])
    cfg = SimpleNamespace(CACHE_DIR=tmp_path, POLICY_FRED_API_KEY="",
                          US_MFG_SURVEY_SERIES={"us_phil_fed_mfg": "P", "us_empire_mfg": "E"})
    assert um.fetch_mfg_survey(cfg) == {} and calls == []        # キーが無ければ叩かない
    cfg.POLICY_FRED_API_KEY = "k"
    out = um.fetch_mfg_survey(cfg)
    assert out["us_mfg_survey"] == 10.0 and calls == ["P", "E"]  # 1系列1回
    assert um.fetch_mfg_survey(cfg)["us_mfg_survey"] == 10.0 and calls == ["P", "E"]   # 20時間はキャッシュ
    cfg.ENABLE_US_MFG_SURVEY = False
    assert um.fetch_mfg_survey(cfg) == {}


def test_cross_sector_ism_rules_read_us_mfg_survey_and_fire():
    import config
    import stock_analyzer_main as m
    up = config.CROSS_SECTOR_RULES["us_ism_manufacturing_up"]["trigger"]
    dn = config.CROSS_SECTOR_RULES["us_ism_manufacturing_down"]["trigger"]
    assert up["key"] == dn["key"] == "us_mfg_survey"
    assert up["val"] == config.US_MFG_SURVEY_UP and dn["val"] == config.US_MFG_SURVEY_DOWN
    fired = lambda v: {s["rule_id"] for s in m.evaluate_cross_sector_signals({"macro_snapshot": {"us_mfg_survey": v}})}
    assert "us_ism_manufacturing_up" in fired(config.US_MFG_SURVEY_UP)
    assert "us_ism_manufacturing_down" in fired(config.US_MFG_SURVEY_DOWN)
    assert not fired(0.0) & {"us_ism_manufacturing_up", "us_ism_manufacturing_down"}
    assert not fired(None) & {"us_ism_manufacturing_up", "us_ism_manufacturing_down"}


def test_main_adds_mfg_survey_before_cross_sector_evaluation():
    src = MAIN.read_text(encoding="utf-8")
    assert src.index("fetch_mfg_survey(") < src.index("evaluate_cross_sector_signals(results)")
