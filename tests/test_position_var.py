"""paper/position_var: 保有銘柄×過去の騰落による VaR95%（純関数部分）。"""
from datetime import date, timedelta

from paper import position_var as pv


def _series(rets, start=100.0):
    d0, out, c = date(2025, 10, 1), [], start
    out.append((d0.isoformat(), c))
    for i, r in enumerate(rets, 1):
        c *= 1 + r
        out.append(((d0 + timedelta(days=i)).isoformat(), c))
    return out


def test_daily_returns_drop_split_jumps():
    r = pv.daily_returns([("2026-01-01", 100.0), ("2026-01-02", 101.0), ("2026-01-03", 50.0), ("2026-01-04", 51.0)])
    assert set(r) == {"2026-01-02", "2026-01-04"}


def test_var_matches_hand_quantile_and_scales_with_cash():
    rets = [0.01] * 95 + [-0.05] * 5          # 100日のうち下位5日が −5%
    closes = {"A.T": _series(rets)}
    full = pv.portfolio_var({"A.T": 100}, 0.0, closes)
    assert full["n_days"] == 100 and abs(full["var95"] - 0.05) < 1e-9
    half_cash = pv.portfolio_var({"A.T": 100}, full["gross"], closes)   # 現金が評価額と同額なら損失率は半分
    assert abs(half_cash["var95"] - 0.025) < 1e-9 and abs(half_cash["exposure"] - 0.5) < 1e-9


def test_cash_only_and_short_history():
    assert pv.portfolio_var({}, 2_000_000, {})["var95"] == 0.0
    short = pv.portfolio_var({"A.T": 10}, 0.0, {"A.T": _series([0.01] * 10)})
    assert short["var95"] is None and short["n_days"] == 10
    missing = pv.portfolio_var({"Z.T": 10}, 100.0, {})
    assert missing["var95"] is None and missing["priced"] == 0


def test_run_writes_json(tmp_path):
    accounts = {"s1": {"cash": 0.0, "positions": {"A.T": {"qty": 100}}}, "s2": {"cash": 5.0, "positions": {}}, "_pending": {}}
    doc = pv.run(None, accounts, tmp_path, closes={"A.T": _series([0.01] * 95 + [-0.05] * 5)})
    assert set(doc["strategies"]) == {"s1", "s2"} and (tmp_path / "var_positions.json").exists()
    assert doc["strategies"]["s2"]["method"] == "cash_only"
