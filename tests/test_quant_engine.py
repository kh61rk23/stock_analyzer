# ==============================================================
# tests/test_quant_engine.py — quant/engine.py のテスト（合成データ・ネットワーク不使用）
# ==============================================================
# 先の日を使わない（重みは t+1 に効く・vol_target の倍率は当日まで）・資金調達5種の手計算・
# レバレッジ上限・cash の売り切り・NaN 収益・売買コスト・apply_band の逐次判定を確かめる。
# ==============================================================
from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from quant import engine
from quant.types import Instrument, MarketData

D = 252.0


def _idx(n):
    return pd.bdate_range("2024-01-01", periods=n)


def _inst(symbol, asset_class, funding, cost_bps=0.0, currency="USD", base=None, quote=None):
    return Instrument(symbol=symbol, asset_class=asset_class, name=symbol, funding=funding,
                      cost_bps=cost_bps, currency=currency, base=base, quote=quote)


def _data(returns: pd.DataFrame, insts, rates=None):
    idx = returns.index
    if rates is None:
        rates = pd.DataFrame({"USD": 0.0, "JPY": 0.0}, index=idx)
    close = (1 + returns.fillna(0)).cumprod() * 100
    return MarketData(close=close, returns=returns, rates=rates,
                      instruments={i.symbol: i for i in insts})


def _const_weight(idx, **cols):
    return pd.DataFrame({k: pd.Series(v, index=idx) for k, v in cols.items()})


CFG = SimpleNamespace(
    QUANT_FX_SWAP_MARKUP=0.0075,
    QUANT_MARGIN_JP_LONG_RATE=0.028,
    QUANT_MARGIN_JP_SHORT_RATE=0.0115,
    QUANT_MARGIN_JP_SHORT_PREMIUM=0.010,
    QUANT_MARGIN_US_LONG_RATE_SPREAD=0.025,
    QUANT_MARGIN_US_SHORT_RATE=0.010,
)


def _zero_ret_data(insts, n=6, rates=None):
    idx = _idx(n)
    ret = pd.DataFrame(0.0, index=idx, columns=[i.symbol for i in insts])
    return _data(ret, insts, rates)


# ---------- 先の日を使わない ----------

def test_weight_on_day_t_hits_t_plus_1():
    idx = _idx(6)
    ret = pd.DataFrame({"A": [0.0, 0.01, 0.02, 0.03, 0.04, 0.05]}, index=idx)
    data = _data(ret, [_inst("A", "index", "futures")])
    w = pd.DataFrame({"A": [0.0, 0.0, 1.0, 0.0, 0.0, 0.0]}, index=idx)
    r = engine.simulate(w, data, CFG)
    assert r.gross.iloc[2] == 0.0          # 当日の収益には効かない
    assert r.gross.iloc[3] == pytest.approx(0.03)   # 翌日に効く
    assert r.gross.iloc[4] == 0.0
    assert r.weights["A"].iloc[3] == 1.0


def test_sign_of_today_return_pnl_is_next_day():
    rng = np.random.default_rng(0)
    idx = _idx(60)
    ret = pd.DataFrame({"A": rng.normal(0, 0.01, 60)}, index=idx)
    data = _data(ret, [_inst("A", "index", "futures")])
    w = np.sign(ret)
    r = engine.simulate(w, data, CFG, max_leverage=10)
    expected = (ret["A"].shift(-1) * np.sign(ret["A"])).shift(1).fillna(0.0)
    # gross[t+1] = ret[t+1] * sign(ret[t])
    for t in range(len(idx) - 1):
        assert r.gross.iloc[t + 1] == pytest.approx(ret["A"].iloc[t + 1] * np.sign(ret["A"].iloc[t]))
    assert r.gross.iloc[0] == 0.0
    assert expected.shape == r.gross.shape


def test_vol_target_scale_uses_only_past_values():
    rng = np.random.default_rng(1)
    n, k = 120, 80
    idx = _idx(n)
    ret = pd.DataFrame({"A": rng.normal(0, 0.01, n)}, index=idx)
    w = pd.DataFrame({"A": np.ones(n)}, index=idx)
    insts = [_inst("A", "index", "futures")]
    base = engine.vol_target(w, _data(ret, insts), CFG, target=0.10, lookback=20, cap=5.0)
    ret2 = ret.copy()
    ret2.iloc[k:, 0] = rng.normal(0, 0.05, n - k)   # 先の日だけ変える
    alt = engine.vol_target(w, _data(ret2, insts), CFG, target=0.10, lookback=20, cap=5.0)
    pd.testing.assert_frame_equal(base.iloc[:k], alt.iloc[:k])
    assert not base.iloc[k:].equals(alt.iloc[k:])


def test_vol_target_value_and_cap():
    n = 12
    idx = _idx(n)
    ret = pd.DataFrame({"A": [0.01 if i % 2 == 0 else -0.01 for i in range(n)]}, index=idx)
    data = _data(ret, [_inst("A", "index", "futures")])
    w = pd.DataFrame({"A": np.ones(n)}, index=idx)
    out = engine.vol_target(w, data, CFG, target=0.10, lookback=4, cap=10.0)
    # 行 6 の窓は行 3..6（いずれも前日の重み 1 が効いている）。±0.01 が 2 個ずつ
    sd = np.std([0.01, -0.01, 0.01, -0.01], ddof=1)
    expected = 0.10 / (sd * np.sqrt(252))
    assert out["A"].iloc[6] == pytest.approx(expected)
    assert out["A"].iloc[0] == 0.0   # 変動を測れない間は建てない
    capped = engine.vol_target(w, data, CFG, target=0.10, lookback=4, cap=0.3)
    assert capped["A"].iloc[6] == pytest.approx(0.3)


def test_vol_target_default_cap_is_asset_class_limit():
    n = 12
    idx = _idx(n)
    ret = pd.DataFrame({"A": [0.0001 if i % 2 == 0 else -0.0001 for i in range(n)]}, index=idx)
    data = _data(ret, [_inst("A", "commodity", "futures")])
    w = pd.DataFrame({"A": np.ones(n)}, index=idx)
    out = engine.vol_target(w, data, None, target=0.10, lookback=4)
    assert out["A"].iloc[8] == pytest.approx(1.5)   # commodity の既定上限


def test_inverse_vol_is_causal_and_value():
    rng = np.random.default_rng(2)
    idx = _idx(50)
    ret = pd.DataFrame({"A": rng.normal(0, 0.01, 50), "B": rng.normal(0, 0.02, 50)}, index=idx)
    iv = engine.inverse_vol(ret, 10)
    assert iv.iloc[:9].isna().all().all()
    assert iv["A"].iloc[20] == pytest.approx(1.0 / ret["A"].iloc[11:21].std())
    ret2 = ret.copy()
    ret2.iloc[30:] = 0.5
    iv2 = engine.inverse_vol(ret2, 10)
    pd.testing.assert_frame_equal(iv.iloc[:30], iv2.iloc[:30])


# ---------- 資金調達 5 種（手計算） ----------

def _fin_last(funding, w, insts, rates, cost_mult=1.0, cfg=CFG):
    data = _zero_ret_data(insts, n=6, rates=rates)
    wdf = _const_weight(data.returns.index, **{insts[0].symbol: w})
    r = engine.simulate(wdf, data, cfg, cost_mult=cost_mult, max_leverage=10)
    return r


def _rates(idx, **kw):
    return pd.DataFrame({k: v for k, v in kw.items()}, index=idx)


def test_funding_fx():
    idx = _idx(6)
    rates = _rates(idx, USD=0.04, JPY=0.01)
    insts = [_inst("USDJPY=X", "fx", "fx", currency="JPY", base="USD", quote="JPY")]
    # 買い：受取 = 0.03 − 0.0075 → 払った額は負
    r = _fin_last("fx", 1.0, insts, rates)
    assert r.financing.iloc[0] == 0.0
    assert r.financing.iloc[-1] == pytest.approx(-(0.03 - 0.0075) / D)
    # 売り：受取 = −0.03 − 0.0075
    r = _fin_last("fx", -1.0, insts, rates)
    assert r.financing.iloc[-1] == pytest.approx((0.03 + 0.0075) / D)
    # cost_mult は業者の取り分だけ。金利差は倍にならない
    r = _fin_last("fx", 1.0, insts, rates, cost_mult=2.0)
    assert r.financing.iloc[-1] == pytest.approx(-(0.03 - 0.015) / D)
    # 重み 0.5 は比例
    r = _fin_last("fx", 0.5, insts, rates)
    assert r.financing.iloc[-1] == pytest.approx(-0.5 * (0.03 - 0.0075) / D)


def test_funding_futures():
    idx = _idx(6)
    rates = _rates(idx, USD=0.04, JPY=0.01)
    insts = [_inst("GLD", "commodity", "futures", currency="USD")]
    r = _fin_last("futures", 0.5, insts, rates)
    assert r.financing.iloc[-1] == pytest.approx(0.5 * 0.04 / D)
    r = _fin_last("futures", -0.5, insts, rates, cost_mult=2.0)
    assert r.financing.iloc[-1] == pytest.approx(-0.5 * 0.04 / D)   # 売りは受け取り・cost_mult 無関係
    insts_jp = [_inst("1321.T", "index", "futures", currency="JPY")]
    r = _fin_last("futures", 1.0, insts_jp, rates)
    assert r.financing.iloc[-1] == pytest.approx(0.01 / D)


def test_funding_margin_jp():
    idx = _idx(6)
    rates = _rates(idx, USD=0.04, JPY=0.01)
    insts = [_inst("1617.T", "jp_margin", "margin_jp", currency="JPY")]
    r = _fin_last("m", 1.0, insts, rates)
    assert r.financing.iloc[-1] == pytest.approx(0.028 / D)
    r = _fin_last("m", 1.0, insts, rates, cost_mult=2.0)
    assert r.financing.iloc[-1] == pytest.approx(0.028 / D)   # 買方金利は倍にならない
    r = _fin_last("m", -1.0, insts, rates)
    assert r.financing.iloc[-1] == pytest.approx((0.0115 + 0.010) / D)
    r = _fin_last("m", -1.0, insts, rates, cost_mult=2.0)
    assert r.financing.iloc[-1] == pytest.approx((0.0115 + 0.020) / D)   # 逆日歩の見込みだけ倍
    r = _fin_last("m", -0.5, insts, rates)
    assert r.financing.iloc[-1] == pytest.approx(0.5 * (0.0115 + 0.010) / D)


def test_funding_margin_us():
    idx = _idx(6)
    rates = _rates(idx, USD=0.04, JPY=0.01)
    insts = [_inst("XLK", "us_margin", "margin_us")]
    r = _fin_last("m", 1.0, insts, rates)
    assert r.financing.iloc[-1] == pytest.approx((0.04 + 0.025) / D)
    r = _fin_last("m", 1.0, insts, rates, cost_mult=2.0)
    assert r.financing.iloc[-1] == pytest.approx((0.04 + 0.050) / D)   # 上乗せだけ倍
    r = _fin_last("m", -1.0, insts, rates, cost_mult=2.0)
    assert r.financing.iloc[-1] == pytest.approx(0.010 / D)


def test_funding_cash_no_financing_and_short_clipped():
    idx = _idx(6)
    rates = _rates(idx, USD=0.04, JPY=0.01)
    insts = [_inst("SPY", "options", "cash")]
    ret = pd.DataFrame({"SPY": [0.0, 0.01, -0.02, 0.03, 0.01, -0.01]}, index=idx)
    data = _data(ret, insts, rates)
    # 売りの重みは 0 に切られる → 損益ゼロ・金利ゼロ・売買ゼロ
    r = engine.simulate(_const_weight(idx, SPY=-1.0), data, CFG)
    assert (r.net == 0).all() and (r.financing == 0).all() and (r.turnover == 0).all()
    assert (r.weights["SPY"] == 0).all()
    # 買いは金利なし
    r = engine.simulate(_const_weight(idx, SPY=1.0), data, CFG)
    assert (r.financing == 0).all()
    assert r.gross.iloc[1] == pytest.approx(0.01)


def test_missing_rate_currency_raises():
    idx = _idx(6)
    rates = _rates(idx, USD=0.04)
    insts = [_inst("USDJPY=X", "fx", "fx", currency="JPY", base="USD", quote="JPY")]
    data = _zero_ret_data(insts, rates=rates)
    with pytest.raises(KeyError):
        engine.simulate(_const_weight(data.returns.index, **{"USDJPY=X": 1.0}), data, CFG)


# ---------- レバレッジ・NaN・売買 ----------

def test_leverage_cap_scales_proportionally():
    idx = _idx(6)
    insts = [_inst("A", "fx", "futures"), _inst("B", "fx", "futures")]
    data = _zero_ret_data(insts)
    w = _const_weight(idx, A=2.0, B=-1.0)    # Σ|w| = 3
    r = engine.simulate(w, data, CFG, max_leverage=1.5)
    assert r.weights["A"].iloc[-1] == pytest.approx(1.0)
    assert r.weights["B"].iloc[-1] == pytest.approx(-0.5)
    assert r.leverage.iloc[-1] == pytest.approx(1.5)
    # 上限の内側なら縮めない
    r = engine.simulate(w, data, CFG, max_leverage=3.0)
    assert r.leverage.iloc[-1] == pytest.approx(3.0)
    assert r.weights["A"].iloc[-1] == pytest.approx(2.0)


def test_default_cap_is_min_over_asset_classes():
    idx = _idx(6)
    insts = [_inst("A", "fx", "futures"), _inst("B", "commodity", "futures")]  # 3.0 と 1.5
    data = _zero_ret_data(insts)
    r = engine.simulate(_const_weight(idx, A=1.0, B=1.0), data, None)
    assert r.leverage.iloc[-1] == pytest.approx(1.5)
    assert r.weights["A"].iloc[-1] == pytest.approx(0.75)


def test_nan_return_is_zero():
    idx = _idx(5)
    ret = pd.DataFrame({"A": [0.0, 0.01, np.nan, 0.02, 0.01]}, index=idx)
    data = _data(ret, [_inst("A", "index", "futures")])
    r = engine.simulate(_const_weight(idx, A=1.0), data, CFG)
    assert r.gross.notna().all()
    assert r.gross.iloc[2] == 0.0
    assert r.gross.iloc[3] == pytest.approx(0.02)


def test_turnover_and_cost_hand_calc():
    idx = _idx(6)
    ret = pd.DataFrame({"A": 0.0, "B": 0.0}, index=idx)
    insts = [_inst("A", "index", "futures", cost_bps=10.0), _inst("B", "index", "futures", cost_bps=20.0)]
    data = _data(ret, insts)
    w = pd.DataFrame({"A": [0.0, 1.0, 1.0, 0.5, 0.5, 0.5], "B": [0.0, 0.0, -0.5, -0.5, 0.0, 0.0]}, index=idx)
    r = engine.simulate(w, data, CFG)
    # 効く日は 1 日後。w_eff A: [0,0,1,1,.5,.5]  B: [0,0,0,-.5,-.5,0]
    assert list(r.turnover.round(10)) == [0.0, 0.0, 1.0, 0.5, 0.5, 0.5]
    assert r.cost.iloc[2] == pytest.approx(1.0 * 10 / 1e4)
    assert r.cost.iloc[3] == pytest.approx(0.5 * 20 / 1e4)
    assert r.cost.iloc[4] == pytest.approx(0.5 * 10 / 1e4)
    assert r.cost.iloc[5] == pytest.approx(0.5 * 20 / 1e4)
    r2 = engine.simulate(w, data, CFG, cost_mult=2.0)
    assert r2.cost.iloc[2] == pytest.approx(2 * 1.0 * 10 / 1e4)
    assert r2.net.iloc[2] == pytest.approx(-2 * 10 / 1e4)
    assert (r.net == r.gross - r.cost - r.financing).all()


def test_ffill_reindex_and_missing_columns():
    idx = _idx(8)
    ret = pd.DataFrame({"A": 0.01}, index=idx)
    data = _data(ret, [_inst("A", "index", "futures")])
    sparse = pd.DataFrame({"A": [1.0, 0.0], "ZZZ": [5.0, 5.0]}, index=[idx[1], idx[5]])
    r = engine.simulate(sparse, data, CFG, max_leverage=10)
    assert "ZZZ" not in r.weights.columns          # 価格の無い銘柄は 0 扱い
    assert list(r.weights["A"]) == [0, 0, 1, 1, 1, 1, 0, 0]
    assert r.gross.iloc[0] == 0.0 and r.gross.iloc[2] == pytest.approx(0.01)


def test_weight_is_zero_on_days_without_price():
    idx = _idx(6)
    ret = pd.DataFrame({"A": [np.nan, np.nan, 0.01, 0.02, np.nan, 0.03]}, index=idx)
    data = _data(ret, [_inst("A", "index", "futures", cost_bps=10.0)])
    data.close = pd.DataFrame({"A": [np.nan, np.nan, 100.0, 102.0, np.nan, 105.0]}, index=idx)
    r = engine.simulate(_const_weight(idx, A=1.0), data, CFG)
    # 終値の無い日 0・1・4 の目標は 0 → 効く重みは [0,0,0,1,1,0]
    assert list(r.weights["A"]) == [0.0, 0.0, 0.0, 1.0, 1.0, 0.0]
    assert r.gross.iloc[3] == pytest.approx(0.02)
    assert r.gross.iloc[4] == 0.0
    assert list(r.turnover) == [0.0, 0.0, 0.0, 1.0, 0.0, 1.0]


def test_weights_on_non_trading_dates_carry_to_next_day():
    idx = _idx(5)   # 2024-01-01(月)〜01-05(金)
    ret = pd.DataFrame({"A": [0.0, 0.01, 0.02, 0.03, 0.04]}, index=idx)
    data = _data(ret, [_inst("A", "index", "futures")])
    # 価格の index に無い日時（2023-12-31 と 2024-01-03 12:00）に置いた重み
    w = pd.DataFrame({"A": [1.0, 0.5]}, index=pd.DatetimeIndex([pd.Timestamp("2023-12-31"), pd.Timestamp("2024-01-03 12:00")]))
    r = engine.simulate(w, data, CFG, max_leverage=10)
    # 12-31 の 1.0 は 01-01 の目標に、01-03 12:00 の 0.5 は 01-04 の目標になる（前の日へは運ばない）
    assert list(r.weights["A"]) == [0.0, 1.0, 1.0, 1.0, 0.5]


def test_monthly_rates_are_carried_to_price_dates():
    idx = _idx(6)   # 2024-01-01〜01-08
    insts = [_inst("GLD", "commodity", "futures")]
    rates = pd.DataFrame({"USD": [0.04]}, index=pd.to_datetime(["2023-12-01"]))
    data = _zero_ret_data(insts, n=6, rates=rates)
    r = engine.simulate(_const_weight(idx, GLD=1.0), data, CFG)
    assert r.financing.iloc[-1] == pytest.approx(0.04 / D)


def test_unknown_instrument_raises():
    idx = _idx(4)
    ret = pd.DataFrame({"A": 0.0}, index=idx)
    data = MarketData(close=ret, returns=ret, rates=pd.DataFrame(index=idx), instruments={})
    with pytest.raises(KeyError):
        engine.simulate(_const_weight(idx, A=1.0), data, CFG)


# ---------- apply_band ----------

def test_apply_band_sequential_against_held_weight():
    idx = _idx(6)
    w = pd.DataFrame({"A": [1.0, 1.05, 1.09, 1.12, 1.13, 0.0]}, index=idx)
    out = engine.apply_band(w, 0.10)
    # 保有 1.0 との相対差: 1.05→4.8%・1.09→8.3% は据え置き、1.12→10.7% で動く。
    # 直前の目標（1.09）との差で見ると 2.7% で動かないはずなので、保有との差で判定していることの確認
    assert list(out["A"]) == [1.0, 1.0, 1.0, 1.12, 1.12, 0.0]


def test_apply_band_open_close_and_columns_independent():
    idx = _idx(4)
    w = pd.DataFrame({"A": [0.0, 0.01, 0.01, 0.0], "B": [1.0, 1.0, 0.95, 0.95]}, index=idx)
    out = engine.apply_band(w, 0.10)
    assert list(out["A"]) == [0.0, 0.01, 0.01, 0.0]   # 0 → 正は相対差 100% で建てる・閉じも動く
    assert list(out["B"]) == [1.0, 1.0, 1.0, 1.0]     # 5% は据え置き


def test_apply_band_zero_band_is_identity_and_nan_is_zero():
    idx = _idx(3)
    w = pd.DataFrame({"A": [0.3, np.nan, -0.2]}, index=idx)
    out = engine.apply_band(w, 0.0)
    assert list(out["A"]) == [0.3, 0.0, -0.2]


def test_apply_band_speed():
    rng = np.random.default_rng(3)
    idx = pd.bdate_range("2000-01-03", periods=5000)
    w = pd.DataFrame(rng.normal(0, 1, (5000, 40)), index=idx)
    t0 = time.time()
    out = engine.apply_band(w, 0.1)
    assert time.time() - t0 < 3.0
    assert out.shape == w.shape


# ---------- vol_target（今日の束を過去の収益に当てはめた変動） ----------

def _alt_ret(n, a=0.01):
    return [a if i % 2 == 0 else -a for i in range(n)]


def test_vol_target_hand_calc_two_assets():
    n = 10
    idx = _idx(n)
    ret = pd.DataFrame({"A": _alt_ret(n, 0.01), "B": _alt_ret(n, 0.02)}, index=idx)
    insts = [_inst("A", "index", "futures"), _inst("B", "index", "futures")]
    w = pd.DataFrame({"A": np.full(n, 0.5), "B": np.full(n, -0.1)}, index=idx)
    out = engine.vol_target(w, _data(ret, insts), CFG, target=0.10, lookback=4, cap=10.0)
    pr = np.array([0.5 * 0.01 - 0.1 * 0.02, -(0.5 * 0.01 - 0.1 * 0.02)] * 2)
    vol = pr.std(ddof=1) * np.sqrt(252)
    # 窓（行 3..6）の束は 0.5·A − 0.1·B
    assert vol > 0
    assert out["A"].iloc[6] == pytest.approx(0.5 * 0.10 / vol)
    assert out["B"].iloc[6] == pytest.approx(-0.1 * 0.10 / vol)


def test_vol_target_after_long_cash_equals_always_held():
    rng = np.random.default_rng(7)
    n = 200
    idx = _idx(n)
    ret = pd.DataFrame({"A": rng.normal(0, 0.01, n)}, index=idx)
    insts = [_inst("A", "index", "futures")]
    data = _data(ret, insts)
    always = pd.DataFrame({"A": np.ones(n)}, index=idx)
    cash_then = always.copy()
    cash_then.iloc[:150] = 0.0          # 長く現金 → 150 日目に建てる
    a = engine.vol_target(always, data, CFG, target=0.10, lookback=60, cap=5.0)
    b = engine.vol_target(cash_then, data, CFG, target=0.10, lookback=60, cap=5.0)
    assert b["A"].iloc[150] == pytest.approx(a["A"].iloc[150])
    assert b["A"].iloc[150] > 0
    assert (b["A"].iloc[:150] == 0).all()


def test_vol_target_short_window_is_zero():
    rng = np.random.default_rng(3)
    n = 60
    idx = _idx(n)
    ret = pd.DataFrame({"A": rng.normal(0, 0.01, n)}, index=idx)
    data = _data(ret, [_inst("A", "index", "futures")])
    w = pd.DataFrame({"A": np.ones(n)}, index=idx)
    out = engine.vol_target(w, data, CFG, target=0.10, lookback=20, cap=5.0)
    assert (out["A"].iloc[:9] == 0).all()      # 有効な日 < 10
    assert out["A"].iloc[9] > 0


def test_vol_target_nan_returns_drop_days_not_zero_fill():
    rng = np.random.default_rng(4)
    n = 40
    idx = _idx(n)
    a = rng.normal(0, 0.01, n)
    b = rng.normal(0, 0.01, n)
    b[:20] = np.nan                       # B は 20 日目まで存在しない
    ret = pd.DataFrame({"A": a, "B": b}, index=idx)
    insts = [_inst("A", "index", "futures"), _inst("B", "index", "futures")]
    w = pd.DataFrame({"A": np.full(n, 0.5), "B": np.full(n, 0.5)}, index=idx)
    data = _data(ret, insts)
    out = engine.vol_target(w, data, CFG, target=0.10, lookback=10, cap=10.0)
    # 日 25 の窓は行 16..25。B が NaN の行 16..19 は外し、有効な行 20..25（6 日 ≥ 5）で測る
    pr = 0.5 * a[20:26] + 0.5 * b[20:26]
    assert out["A"].iloc[25] == pytest.approx(0.5 * 0.10 / (pr.std(ddof=1) * np.sqrt(252)))
    # 日 21 は有効な行が 20・21 の 2 日（< 5）なので建てない
    assert out["A"].iloc[21] == 0.0


def test_vol_target_cap_limits_gross_exposure():
    n = 12
    idx = _idx(n)
    ret = pd.DataFrame({"A": _alt_ret(n, 0.0001), "B": _alt_ret(n, 0.0002)}, index=idx)
    insts = [_inst("A", "index", "futures"), _inst("B", "index", "futures")]
    w = pd.DataFrame({"A": np.full(n, 1.0), "B": np.full(n, 1.0)}, index=idx)
    out = engine.vol_target(w, _data(ret, insts), CFG, target=0.10, lookback=4, cap=1.5)
    assert out.abs().sum(axis=1).iloc[8] == pytest.approx(1.5)


def test_vol_target_speed():
    rng = np.random.default_rng(5)
    n, k = 5000, 40
    idx = _idx(n)
    cols = [f"S{i}" for i in range(k)]
    ret = pd.DataFrame(rng.normal(0, 0.01, (n, k)), index=idx, columns=cols)
    insts = [_inst(c, "index", "futures") for c in cols]
    w = pd.DataFrame(rng.normal(0, 0.1, (n, k)), index=idx, columns=cols)
    data = _data(ret, insts)
    t0 = time.perf_counter()
    engine.vol_target(w, data, CFG, target=0.10, lookback=60, cap=1.5)
    assert time.perf_counter() - t0 < 1.0
