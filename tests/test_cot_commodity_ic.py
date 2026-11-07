"""商品の加点の効き: 順位相関・週ごとの IC・的中率・判定・メール・過去の週の点の再現。"""
from datetime import date, timedelta

import pytest

import cot_commodity_ic as ci
import cot_signal as cs
from test_cot_commodity import CFG, IMPACT, _rows, UP, FLAT


def test_spearman_basic_and_degenerate():
    assert ci.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert ci.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert ci.spearman([0, 0, 0, 0], [1, 2, 3, 4]) is None      # 片方が全部同値
    assert ci.spearman([1, 2], [1, 2]) is None                  # 点が少なすぎる


def _returns(d, rets, h=5):
    return {(d, c): {h: r} for c, r in rets.items()}


def test_weekly_ic_positive_when_adjusted_sectors_move_with_points():
    d = date(2026, 10, 6)
    codes = [f"{1000 + i}" for i in range(20)]
    rets = {c: 0.001 * i for i, c in enumerate(codes)}
    deltas = {d: {codes[19]: 1.0, codes[18]: 0.5, codes[0]: -1.0}}   # 上位2つに加点・最下位に減点
    out = ci.weekly_ic(deltas, _returns(d, rets), 5)
    assert len(out) == 1 and out[0]["ic"] > 0.3
    assert out[0]["n_adj"] == 3 and out[0]["hit"] == 1.0


def test_weekly_ic_skips_weeks_with_few_adjustments_or_sectors():
    d = date(2026, 10, 6)
    rets = {f"{1000 + i}": 0.001 * i for i in range(20)}
    assert ci.weekly_ic({d: {"1019": 1.0}}, _returns(d, rets), 5) == []             # 加点が1業種
    assert ci.weekly_ic({d: {"1019": 1.0, "1018": 1.0, "1017": 1.0}},
                        _returns(d, {k: v for k, v in list(rets.items())[:5]}), 5) == []   # 成績のある業種が少ない


def test_hit_counts_sign_against_cross_section_median():
    d = date(2026, 10, 6)
    codes = [f"{1000 + i}" for i in range(20)]
    rets = {c: 0.001 * i for i, c in enumerate(codes)}
    deltas = {d: {codes[19]: -1.0, codes[18]: -1.0, codes[17]: -1.0}}   # 上位に減点＝外れ
    out = ci.weekly_ic(deltas, _returns(d, rets), 5)
    assert out[0]["hit"] == 0.0 and out[0]["ic"] < 0


def _rows_ic(vals, h=5):
    return [{"report_date": date(2026, 1, 6) + timedelta(days=7 * i), "horizon": h, "ic": v, "hit": 0.5}
            for i, v in enumerate(vals)]


def test_summarize_verdicts():
    rev = ci.summarize(_rows_ic([-0.2, -0.15, -0.25, -0.18, -0.22, -0.2, -0.17, -0.21, -0.19, -0.23]))
    assert rev[0]["verdict"] == "逆向き" and rev[0]["t"] < -2
    eff = ci.summarize(_rows_ic([0.2, 0.15, 0.25, 0.18, 0.22, 0.2, 0.17, 0.21, 0.19, 0.23]))
    assert eff[0]["verdict"] == "効いている"
    few = ci.summarize(_rows_ic([-0.2, -0.3, -0.25]))
    assert few[0]["verdict"] == "週数不足"
    flat = ci.summarize(_rows_ic([0.2, -0.2, 0.1, -0.1, 0.15, -0.15, 0.05, -0.05, 0.0, 0.01]))
    assert flat[0]["verdict"] == "はっきりしない"


def test_horizon_20_is_discounted_for_overlap():
    vals = [-0.2, -0.15, -0.25, -0.18, -0.22, -0.2, -0.17, -0.21, -0.19, -0.23]
    s5 = ci.summarize(_rows_ic(vals, 5))[0]
    s20 = ci.summarize(_rows_ic(vals, 20))[0]
    assert s20["eff_weeks"] == pytest.approx(s5["eff_weeks"] / 4)
    assert s20["verdict"] == "週数不足"


def test_alert_warns_only_when_reversed():
    ok = {"cot_commodity_ic": {"lines": ["x"], "reversed": []}}
    a = ci.build_alert(ok)
    assert a["trigger"] == "cot_commodity_ic" and a["severity"] == "info"
    bad = ci.build_alert({"cot_commodity_ic": {"lines": ["x"], "reversed": ["5d"]}})
    assert bad["severity"] == "warning" and "逆向き" in bad["title"]
    assert ci.build_alert({"cot_commodity_ic": {"skipped": True}}) is None
    assert ci.build_alert({}) is None


def test_report_lines_mention_not_auto_stopped():
    s = ci.summarize(_rows_ic([-0.2, -0.15, -0.25, -0.18, -0.22, -0.2, -0.17, -0.21, -0.19, -0.23]))
    assert any("自動では変えていません" in ln for ln in ci.report_lines(s))


def test_weekly_deltas_replays_each_week_with_that_weeks_data(monkeypatch):
    monkeypatch.setattr(cs, "_impact_map", lambda: IMPACT)
    rows = _rows("067651", UP) + _rows("002602", FLAT)     # WTI は直近で買い過ぎ → 逆張りで石油の影響先に点
    last = max(r["report_date"] for r in rows)
    d = ci.weekly_deltas(rows, CFG, weeks=3)
    assert sorted(d) == [last - timedelta(days=14), last - timedelta(days=7), last]
    assert d[last]                                          # 最新週は極端あり
    # 最新週までの建玉を切った版と同じ点になる（未来の建玉を使わない）
    cut = [r for r in rows if r["report_date"] <= last - timedelta(days=7)]
    sig = cs.build_signals(cut, CFG, today=last - timedelta(days=4))
    want = {c: v["delta"] for c, v in cs.sector_adjustments(sig, CFG, today=last - timedelta(days=4))["jp"].items() if v["delta"]}
    assert d[last - timedelta(days=7)] == pytest.approx(want) or d[last - timedelta(days=7)] == want


def test_sector_adjustments_today_controls_freshness():
    rows = _rows("067651", UP)
    sig = cs.build_signals(rows, CFG, today=max(r["report_date"] for r in rows))
    far = max(r["report_date"] for r in rows) + timedelta(days=60)
    assert cs.sector_adjustments(sig, CFG, IMPACT, today=far)["jp"] == {}
    assert cs.sector_adjustments(sig, CFG, IMPACT, today=max(r["report_date"] for r in rows))["jp"]


def test_returns_sql_is_parameterised_and_jp_only():
    sql = ci.build_returns_sql("proj")
    assert "@dates" in sql and "@since" in sql and "screener_universe_daily" in sql
    assert "price_daily_latest" in sql
