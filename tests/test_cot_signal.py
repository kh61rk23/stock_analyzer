"""cot_signal: 買い越しの割合・パーセンタイル・極端の判定・欠けの扱い・レジーム/シナリオ/メールへの渡し方。"""
import json
from datetime import date, timedelta
from types import SimpleNamespace

import cot_signal as cs

CFG = SimpleNamespace(
    COT_SIGNAL_YEARS=3, COT_SIGNAL_MIN_WEEKS=52, COT_SIGNAL_PCT_HI=90.0, COT_SIGNAL_PCT_LO=10.0,
    COT_SIGNAL_CHANGE_WEEKS=4, COT_SIGNAL_STALE_DAYS=21, COT_REGIME_MAX_PT=4.0, COT_SCENARIO_SHOCK_WEIGHT=0.15,
    COT_MAIL_MAX_LINES=3,
    COT_SIGNAL_MARKETS={
        "sp500": {"report": "tff", "code": "13874A", "name": "S&P500先物", "risk_sign": 1, "w": 2.0},
        "vix": {"report": "tff", "code": "1170E1", "name": "VIX先物", "risk_sign": -1, "w": 1.0},
        "gold": {"report": "legacy", "code": "088691", "name": "金先物", "risk_sign": -1, "w": 1.0},
    })
LAST = date(2026, 10, 27)


def _rows(code, report, nets, last=LAST, oi=1000.0):
    """nets の最後が last の週。net は買い越し枚数（oi=1000 なので net/10 が割合%）。"""
    n = len(nets)
    return [{"report_date": last - timedelta(days=7 * (n - 1 - i)), "report": report, "market_code": code,
             "open_interest": oi, "spec_long": 500.0 + v, "spec_short": 500.0} for i, v in enumerate(nets)]


def test_net_pct_and_missing():
    assert cs.net_pct(600, 400, 1000) == 20.0
    assert cs.net_pct(None, 400, 1000) is None
    assert cs.net_pct(600, 400, 0) is None


def test_percentile_rank_ties_count_half():
    assert cs.percentile_rank([1, 2, 3, 4], 4) == 87.5
    assert cs.percentile_rank([5, 5, 5, 5], 5) == 50.0
    assert cs.percentile_rank([], 1) == 50.0


def test_extreme_and_tilt_are_contrarian():
    assert cs.extreme_of(95, 90, 10) == "long" and cs.extreme_of(5, 90, 10) == "short" and cs.extreme_of(50, 90, 10) is None
    assert cs.risk_tilt("long", 1) == -1      # 株の買い過ぎ → 弱気
    assert cs.risk_tilt("short", 1) == 1
    assert cs.risk_tilt("short", -1) == -1    # VIX の売り過ぎ → VIX 上昇 → リスクオフ
    assert cs.risk_tilt(None, 1) == 0


def test_market_signal_percentile_and_change():
    nets = list(range(0, 100))               # 単調増加 → 最新が最高位
    s = cs.market_signal([(LAST - timedelta(days=7 * (99 - i)), v / 10, v) for i, v in enumerate(nets)], CFG, 1)
    assert s["extreme"] == "long" and s["percentile"] > 99 and s["risk_tilt"] == -1
    assert s["chg_net"] == 4 * 1 * 1 and abs(s["chg_pct"] - 0.4) < 1e-9     # 4週前は 95 → 99


def test_short_history_is_skipped():
    assert cs.market_signal([(LAST - timedelta(days=7 * i), 1.0, 1) for i in range(30)], CFG) is None
    assert cs.market_signal([], CFG) is None


def test_build_signals_composite_and_missing_markets():
    rows = (_rows("13874A", "tff", list(range(100))) +                 # 買い過ぎ（w=2, 弱気）
            _rows("1170E1", "tff", list(range(100, 0, -1))))           # VIX 売り過ぎ → リスクオフ（弱気）
    sig = cs.build_signals(rows, CFG, today=LAST + timedelta(days=3))
    assert set(sig["markets"]) == {"sp500", "vix"}                      # gold は行が無く除外・分母にも入らない
    assert sig["composite"] == -1.0 and sig["extremes"][0] in ("sp500", "vix")
    assert sig["as_of"] == LAST.isoformat()


def test_composite_zero_without_extremes_and_empty():
    mid = [0, 5] * 50                                                    # 最新は中位
    sig = cs.build_signals(_rows("13874A", "tff", mid + [2]), CFG, today=LAST)
    assert sig["markets"]["sp500"]["extreme"] is None and sig["composite"] == 0.0 and sig["extremes"] == []
    empty = cs.build_signals([], CFG, today=LAST)
    assert empty["markets"] == {} and empty["composite"] == 0.0 and empty["as_of"] is None


def test_null_positions_rows_ignored():
    rows = _rows("13874A", "tff", list(range(100)))
    rows[-1]["spec_long"] = None
    sig = cs.build_signals(rows, CFG, today=LAST)
    assert sig["markets"]["sp500"]["date"] == (LAST - timedelta(days=7)).isoformat()


def test_stale_or_missing_gives_no_effect():
    sig = {"as_of": "2026-09-01", "composite": -1.0, "markets": {}, "extremes": []}
    today = date(2026, 10, 27)
    assert not cs.is_fresh(sig, CFG, today) and not cs.is_fresh(None, CFG, today)
    fresh = dict(sig, as_of=today.isoformat())
    assert cs.is_fresh(fresh, CFG, today)


def test_regime_adj_and_scenario_factor(monkeypatch):
    today = date.today().isoformat()
    sig = {"as_of": today, "composite": -0.5, "markets": {}, "extremes": []}
    assert cs.regime_adj(sig, CFG) == -2.0
    assert cs.regime_adj(None, CFG) == 0.0
    assert abs(cs.scenario_factor(-15.0, sig, CFG) - 1.075) < 1e-9      # 買い過ぎ → 下げ想定を増幅
    assert abs(cs.scenario_factor(8.0, sig, CFG) - 0.925) < 1e-9        # 上げ想定は縮む
    assert cs.scenario_factor(0.0, sig, CFG) == 1.0 and cs.scenario_factor(-15.0, None, CFG) == 1.0


def test_detect_market_regime_uses_adj():
    import signal_aggregator as sa
    res = {"options_monitor": {"vix_data": {"vix": {"current": 20}}}}      # (35-20)/25*100 = 60
    assert sa.detect_market_regime(res) == "リスクオン"
    assert sa.detect_market_regime(res, -4.0) == "中立"                    # 56 → 中立


def test_mail_lines_only_extremes_and_capped():
    today = date.today().isoformat()
    mk = {k: {"name": k, "extreme": "long", "percentile": 95.0 - i, "chg_pct": 1.5} for i, k in enumerate("abcd")}
    mk["z"] = {"name": "z", "extreme": None, "percentile": 50.0, "chg_pct": 0.0}
    sig = {"as_of": today, "markets": mk, "extremes": ["a", "b", "c", "d"], "composite": -1}
    lines = cs.mail_lines(sig, CFG)
    assert len(lines) == 4 and "95%点" in lines[0] and "下げやすい" in lines[0] and today in lines[-1]   # 3行＋出典行
    assert cs.mail_lines(dict(sig, extremes=[]), CFG) == []
    assert cs.mail_lines(dict(sig, as_of="2020-01-01"), CFG) == []


def test_load_signals_roundtrip(tmp_path):
    cfg = SimpleNamespace(**vars(CFG), DATA_DIR=tmp_path)
    assert cs.load_signals(cfg) is None
    (tmp_path / cs.SIGNAL_FILE).write_text(json.dumps({"as_of": date.today().isoformat(), "composite": 0.0}))
    assert cs.load_signals(cfg)["composite"] == 0.0
    (tmp_path / cs.SIGNAL_FILE).write_text("{broken")
    assert cs.load_signals(cfg) is None


def test_build_query_groups_codes_by_report():
    q = cs.build_query(CFG)
    assert "(report = 'legacy' AND market_code IN ('088691'))" in q
    assert "(report = 'tff' AND market_code IN ('13874A', '1170E1'))" in q and "{project}" in q
