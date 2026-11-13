# ==============================================================
# tests/test_quant_options.py — quant/options_sim.py の検査（合成の価格・ネットワークなし）
# ==============================================================
# 横ばいの受取額・急落の損の一致・スプレッドの損の上限・カバードコールの上値・コスト倍率・
# 予想変動率のフォールバック・未来を見ないこと・未知の kind・速度を確かめる。
# ==============================================================
from __future__ import annotations

import math
import time

import numpy as np
import pandas as pd
import pytest

from backtest.options_model import bs_price
from quant import settings as S
from quant.options_sim import simulate_options, targets_options
from quant.types import Instrument, MarketData

SYM = "TST"
IV = 0.20


def _data(prices, iv=IV, with_iv=True):
    idx = pd.bdate_range("2020-01-01", periods=len(prices))
    close = pd.DataFrame({SYM: np.asarray(prices, dtype=float)}, index=idx)
    aux = {}
    if with_iv:
        aux["iv"] = pd.DataFrame({SYM: iv if not hasattr(iv, "__len__") else np.asarray(iv)}, index=idx)
    return MarketData(
        close=close, returns=close.pct_change(), rates=pd.DataFrame({"USD": 0.0}, index=idx),
        instruments={SYM: Instrument(SYM, "options", "テスト", "cash", 0.0)}, quality={}, aux=aux,
    )


def _first_expiry_pos(data):
    start = data.close.index[0]
    return int(data.close.index.searchsorted(start + pd.Timedelta(days=S.get(None, "QUANT_OPT_DTE"))))


def _skewed(sigma, spot, k):
    """行使価格ごとの予想変動率（下は上げ・上は下げる。下げは現値の 0.6 倍まで）。"""
    sigma = sigma * S.get(None, "QUANT_OPT_IV_INDEX_RATIO")      # 引数は指数の値。現値の行使価格の予想変動率はその 0.88 倍
    if k <= spot:
        return sigma * (1 + S.get(None, "QUANT_OPT_SKEW_PER_10PCT") * ((spot - k) / spot) / 0.10)
    return sigma * max(0.6, 1 - S.get(None, "QUANT_OPT_CALL_SKEW_PER_10PCT") * ((k - spot) / spot) / 0.10)


def test_sideways_put_sale_earns_premium():
    d = _data([100.0] * 80)
    r = simulate_options("cash_secured_put", SYM, d, {"otm": 0.05}, None)
    e = _first_expiry_pos(d)
    k = 95.0
    prem = bs_price(100.0, k, S.get(None, "QUANT_OPT_DTE") / 365.0, 0.0, _skewed(IV, 100.0, k), "put")
    assert r.gross.iloc[0] == 0.0 and r.gross.iloc[1:e + 1].sum() == pytest.approx(prem / k, rel=1e-9)
    assert r.net.iloc[0] < 0  # 建てた日の損益はコストだけ
    assert r.net.iloc[1:e + 1].sum() > 0


def test_crash_loss_matches_strike_minus_value_minus_premium():
    p = [100.0] * 10 + [70.0] * 70
    d = _data(p)
    r = simulate_options("cash_secured_put", SYM, d, {"otm": 0.05}, None)
    e = _first_expiry_pos(d)
    k = 95.0
    prem = bs_price(100.0, k, S.get(None, "QUANT_OPT_DTE") / 365.0, 0.0, _skewed(IV, 100.0, k), "put")
    expected = -((k - 70.0) - prem) / k
    assert r.gross.iloc[:e + 1].sum() == pytest.approx(expected, rel=1e-9)


def test_put_credit_spread_max_loss_bounded():
    p = [100.0] * 10 + [50.0] * 70
    d = _data(p)
    r = simulate_options("put_credit_spread", SYM, d, {"otm": 0.03, "width": 0.05}, None)
    e = _first_expiry_pos(d)
    dte = S.get(None, "QUANT_OPT_DTE") / 365.0
    ks, kl = 97.0, 92.0
    credit = (bs_price(100.0, ks, dte, 0.0, _skewed(IV, 100.0, ks), "put")
              - bs_price(100.0, kl, dte, 0.0, _skewed(IV, 100.0, kl), "put"))
    width = ks - kl
    loss = r.gross.iloc[:e + 1].sum()
    assert loss == pytest.approx(-(width - credit) / width, rel=1e-9)
    assert loss >= -(width - credit) / width - 1e-12


def test_covered_call_upside_capped_at_strike():
    p = [100.0] * 10 + [140.0] * 70
    d = _data(p)
    r = simulate_options("covered_call", SYM, d, {"otm": 0.05}, None)
    e = _first_expiry_pos(d)
    k = 105.0
    prem = bs_price(100.0, k, S.get(None, "QUANT_OPT_DTE") / 365.0, 0.0, _skewed(IV, 100.0, k), "call")
    assert r.gross.iloc[:e + 1].sum() == pytest.approx((k - 100.0 + prem) / 100.0, rel=1e-9)


def test_collar_runs_and_bounds_loss():
    p = [100.0] * 10 + [60.0] * 70
    d = _data(p)
    r = simulate_options("collar", SYM, d, {"otm": 0.05}, None)
    e = _first_expiry_pos(d)
    dte = S.get(None, "QUANT_OPT_DTE") / 365.0
    pp = bs_price(100.0, 95.0, dte, 0.0, _skewed(IV, 100.0, 95.0), "put")
    pc = bs_price(100.0, 105.0, dte, 0.0, _skewed(IV, 100.0, 105.0), "call")
    # 下値は買いのプットで止まる：満期の価値＝現物 60＋プットの本質的価値 35＝95
    expected = (95.0 - (100.0 + pp - pc)) / 100.0
    assert r.gross.iloc[:e + 1].sum() == pytest.approx(expected, rel=1e-9)
    assert expected > -0.06


def test_entry_cost_and_roll_on_expiry_day():
    d = _data([100.0] * 80)
    r = simulate_options("cash_secured_put", SYM, d, {"otm": 0.05}, None)
    e = _first_expiry_pos(d)
    k = 95.0
    spread = S.get(None, "QUANT_OPT_SPREAD_PCT")
    min_cost = S.get(None, "QUANT_OPT_MIN_COST_PER_SHARE")
    prem = bs_price(100.0, k, S.get(None, "QUANT_OPT_DTE") / 365.0, 0.0, _skewed(IV, 100.0, k), "put")
    assert r.cost.iloc[0] == pytest.approx(max(prem * spread / 2, min_cost) / k, rel=1e-12)
    # 間の日はコストなし。満期日は価値 0 で閉じるコストなし＋同じ日に次を建てるコスト
    assert (r.cost.iloc[1:e] == 0).all()
    exp_date = d.close.index[e]
    nxt = d.close.index.searchsorted(exp_date + pd.Timedelta(days=30))
    t2 = (d.close.index[nxt] - exp_date).days / 365.0
    prem2 = bs_price(100.0, k, t2, 0.0, _skewed(IV, 100.0, k), "put")
    assert r.cost.iloc[e] == pytest.approx(max(prem2 * spread / 2, min_cost) / k, rel=1e-12)
    assert (r.leverage.iloc[e - 1:e + 2] == 1.0).all()
    # 満期は 30 暦日後以降の最初の営業日
    assert (exp_date - d.close.index[0]).days >= 30
    assert (d.close.index[e - 1] - d.close.index[0]).days < 30


def test_output_index_matches_data_with_nan_prefix():
    p = np.r_[np.full(10, np.nan), np.full(70, 100.0)]
    d = _data(p)
    r = simulate_options("cash_secured_put", SYM, d, {"otm": 0.05}, None)
    assert r.net.index.equals(d.close.index)
    assert (r.net.iloc[:10] == 0).all() and r.net.iloc[10] < 0
    assert not r.net.isna().any()


def test_rate_lowers_put_premium():
    d0 = _data([100.0] * 80)
    d1 = _data([100.0] * 80)
    d1.rates = pd.DataFrame({"USD": 0.05}, index=d1.close.index)
    a = simulate_options("cash_secured_put", SYM, d0, {"otm": 0.05}, None)
    b = simulate_options("cash_secured_put", SYM, d1, {"otm": 0.05}, None)
    e = _first_expiry_pos(d0)
    assert b.gross.iloc[:e + 1].sum() < a.gross.iloc[:e + 1].sum()
    assert (b.financing == 0).all()


def _walk(n=300, seed=1):
    rng = np.random.default_rng(seed)
    return 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, n)))


def test_cost_mult_lowers_result():
    d = _data(_walk())
    a = simulate_options("cash_secured_put", SYM, d, {"otm": 0.05}, None, cost_mult=1.0)
    b = simulate_options("cash_secured_put", SYM, d, {"otm": 0.05}, None, cost_mult=2.0)
    assert b.net.sum() < a.net.sum()
    assert b.cost.sum() > a.cost.sum() and a.gross.equals(b.gross)


def test_iv_fallback_to_realized_vol_times_ratio():
    p = _walk(250, seed=3)
    d_none = _data(p, with_iv=False)
    ratio = S.get(None, "QUANT_OPT_IV_HV_RATIO")
    hv = np.log(d_none.close[SYM]).diff().rolling(20).std() * math.sqrt(252) * ratio
    idx_ratio = S.get(None, "QUANT_OPT_IV_INDEX_RATIO")
    d_full = _data(p, iv=hv.to_numpy() / idx_ratio)     # 指数として渡すと 0.88 倍されるので、同じ値になるよう割っておく
    a = simulate_options("cash_secured_put", SYM, d_none, {"otm": 0.05}, None)
    b = simulate_options("cash_secured_put", SYM, d_full, {"otm": 0.05}, None)
    pd.testing.assert_series_equal(a.net, b.net)
    assert (a.leverage.iloc[:20] == 0).all()  # 変動率が出せない最初の20日は建てない
    # 一部の日だけ指数が欠けても、その日は実績×比率が入る
    iv_gap = np.full(250, IV)
    iv_gap[100:120] = np.nan
    filled = np.where(np.isnan(iv_gap), hv.to_numpy() / idx_ratio, iv_gap)
    c = simulate_options("cash_secured_put", SYM, _data(p, iv=iv_gap), {"otm": 0.05}, None)
    e = simulate_options("cash_secured_put", SYM, _data(p, iv=filled), {"otm": 0.05}, None)
    pd.testing.assert_series_equal(c.net, e.net)


def test_no_lookahead():
    p = _walk(300, seed=5)
    p2 = p.copy()
    p2[200:] *= 0.5
    a = simulate_options("put_credit_spread", SYM, _data(p, with_iv=False), {"otm": 0.03, "width": 0.05}, None)
    b = simulate_options("put_credit_spread", SYM, _data(p2, with_iv=False), {"otm": 0.03, "width": 0.05}, None)
    pd.testing.assert_series_equal(a.net.iloc[:200], b.net.iloc[:200])
    assert not a.net.iloc[200:].equals(b.net.iloc[200:])


def test_unknown_kind_raises():
    d = _data([100.0] * 40)
    with pytest.raises(ValueError):
        simulate_options("naked_call", SYM, d, {}, None)
    with pytest.raises(ValueError):
        targets_options("naked_call", SYM, d, {}, None)


def test_targets_options_strike_expiry_credit():
    d = _data([100.0] * 40)
    t = targets_options("put_credit_spread", SYM, d, {"otm": 0.03, "width": 0.05}, None)
    assert [x["strike"] for x in t] == [pytest.approx(97.0), pytest.approx(92.0)]
    exp = pd.Timestamp(t[0]["expiry"])
    assert exp.weekday() < 5 and 30 <= (exp - d.close.index[-1]).days <= 32
    assert t[0]["dte"] == (exp - d.close.index[-1]).days
    assert t[0]["net_credit"] > 0 and t[0]["capital_per_share"] == pytest.approx(5.0)
    assert targets_options("cash_secured_put", SYM, _data([100.0] * 15, with_iv=False), {"otm": 0.05}, None) == []


def test_speed_20_years():
    d = _data(_walk(5000, seed=7), with_iv=False)
    t0 = time.time()
    simulate_options("collar", SYM, d, {"otm": 0.05}, None)
    assert time.time() - t0 < 2.0


def test_call_iv_is_lower_above_spot_and_floored():
    """上の行使価格のコールは、現値と同じ予想変動率で値を付けたときより安い（受取額を多く見積もらない）。"""
    from quant.options_sim import _leg_prices
    skew = (S.get(None, "QUANT_OPT_SKEW_PER_10PCT"), S.get(None, "QUANT_OPT_CALL_SKEW_PER_10PCT"))
    t = 30 / 365.0
    got = _leg_prices([(-1, "call", 105.0)], 100.0, t, 0.0, 0.20, skew)[0]
    assert got == pytest.approx(bs_price(100.0, 105.0, t, 0.0, 0.20 * (1 - 0.40 * 0.5), "call"))
    assert got < bs_price(100.0, 105.0, t, 0.0, 0.20, "call")
    far = _leg_prices([(-1, "call", 200.0)], 100.0, t, 0.0, 0.20, skew)[0]
    assert far == pytest.approx(bs_price(100.0, 200.0, t, 0.0, 0.20 * 0.6, "call"))
