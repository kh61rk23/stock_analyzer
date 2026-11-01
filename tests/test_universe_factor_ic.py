"""universe_factor_ic：観点別 IC の集計・判定・メールの節（BQ は使わない）。"""
from datetime import date
from types import SimpleNamespace

import universe_factor_ic as u


def _rows(factor, ics, h=5, mkt="JP"):
    return [{"market": mkt, "horizon": h, "factor": factor, "score_date": date(2026, 9, 1 + i),
             "ic": v, "q5_q1": v / 10, "n": 2700} for i, v in enumerate(ics)]


def test_summarize_verdicts_with_overlap_discount():
    rows = (_rows("s_value", [0.05, 0.06, 0.04, 0.05] * 6)          # 24日・5日先 → 実効4.8日
            + _rows("s_sector", [-0.05, -0.06, -0.04, -0.05] * 6)
            + _rows("s_flow", [0.1, -0.1] * 12)
            + _rows("s_move", [0.05] * 10))                           # 実効2日 → 日数不足
    s = {x["factor"]: x for x in u.summarize(rows)}
    assert s["s_value"]["verdict"] == "効いている" and s["s_value"]["eff_days"] == 4.8
    assert s["s_sector"]["verdict"] == "逆向き"
    assert s["s_flow"]["verdict"] == "はっきりしない"
    assert s["s_move"]["verdict"] == "日数不足"
    assert s["s_value"]["hit"] == 1.0 and abs(s["s_value"]["q5_q1"] - 0.005) < 1e-9


def test_report_lines_and_alert():
    rows = _rows("s_value", [0.05, 0.06] * 12) + _rows("s_sector", [-0.05, -0.06] * 12)
    res = {"lines": u.report_lines(u.summarize(rows))}
    text = "\n".join(res["lines"])
    assert "🇯🇵 日本・5取引日後" in text and "割安" in text and "🔻" in text
    a = u.build_alert({"universe_factor_ic": res})
    assert a["trigger"] == "universe_factor_ic"
    assert u.build_alert({}) is None and u.build_alert({"universe_factor_ic": {"error": "x"}}) is None


def test_sql_ties_do_not_use_future_returns():
    sql = u.build_sql("p", "screener_universe_daily", "1306.T")
    # 5分位の同点は先のリターンで割らない（上位に勝った銘柄が寄るため）
    assert "ORDER BY val, FARM_FINGERPRINT(ticker)" in sql
    assert "ORDER BY val, ex" not in sql
    assert "s20.i = se.ei + 20" in sql and "'1306.T'" in sql


def test_run_disabled_and_empty():
    assert u.run(SimpleNamespace(ENABLE_UNIVERSE_FACTOR_IC=False))["skipped"]

    class _C:
        project = "p"
    import universe_factor_ic as m
    orig = m.fetch
    m.fetch = lambda c, since: ([], 0.0, ["US: 表が無い"])
    try:
        r = m.run(SimpleNamespace(), today=date(2026, 11, 2), client=_C())
        assert r["skipped"] and r["markets_skipped"] == ["US: 表が無い"]
    finally:
        m.fetch = orig
