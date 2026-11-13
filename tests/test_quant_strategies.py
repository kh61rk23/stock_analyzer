# ==============================================================
# tests/test_quant_strategies.py — quant/strategies.py のテスト（合成データ・ネットワーク不使用）
# ==============================================================
# 設定の数・順張りの向き・金利差の選び方・業種の買い売りの本数と危険量の釣り合い・月末の再判定・
# 未来を見ないこと（全系統）・履歴不足の除外・targets の形・family_id の重複なしを確かめる。
# ==============================================================
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from quant import instruments
from quant import strategies as ST
from quant.types import Family, Instrument, MarketData

N = 600
CFG = SimpleNamespace(QUANT_MIN_HISTORY_DAYS=100)

RATES = {"USD": 0.04, "JPY": 0.0, "EUR": 0.02, "GBP": 0.05, "AUD": 0.06, "NZD": 0.045, "CAD": 0.03, "CHF": -0.01}


def _universe_data(seed=7, n=N, drift_sign=None, sigma=0.01, rates=None):
    """QUANT_UNIVERSE の全銘柄の合成の価格。drift_sign があれば銘柄ごとに ±0.002 の傾きを付ける。"""
    insts = instruments.load(CFG)
    idx = pd.bdate_range("2020-01-01", periods=n)
    rng = np.random.default_rng(seed)
    cols = {}
    for i, sym in enumerate(insts):
        mu = 0.0 if drift_sign is None else 0.002 * drift_sign(sym, i)
        r = mu + sigma * rng.standard_normal(n)
        cols[sym] = 100.0 * np.cumprod(1.0 + r)
    close = pd.DataFrame(cols, index=idx)
    rates = pd.DataFrame({c: v for c, v in (rates or RATES).items()}, index=idx)
    iv = pd.DataFrame({"SPY": 0.2, "QQQ": 0.25}, index=idx)
    return MarketData(close=close, returns=close.pct_change(fill_method=None), rates=rates,
                      instruments=insts, quality={"rates_source": "bis"}, aux={"iv": iv})


def _rebuild(data, close):
    return MarketData(close=close, returns=close.pct_change(fill_method=None), rates=data.rates,
                      instruments=data.instruments, quality=data.quality, aux=data.aux)


def _mini(prices: dict, idx=None):
    """銘柄を FX として持つだけの小さな MarketData（純関数の検査用）。"""
    n = len(next(iter(prices.values())))
    idx = idx if idx is not None else pd.bdate_range("2020-01-01", periods=n)
    close = pd.DataFrame({k: np.asarray(v, dtype=float) for k, v in prices.items()}, index=idx)
    insts = {k: Instrument(k, "fx", k, "fx", 1.0, base="USD", quote="JPY") for k in prices}
    return MarketData(close=close, returns=close.pct_change(fill_method=None),
                      rates=pd.DataFrame({"USD": 0.0, "JPY": 0.0}, index=idx), instruments=insts)


def _noisy(n, mu, sigma=0.005, seed=0, start=100.0):
    rng = np.random.default_rng(seed)
    return start * np.cumprod(1.0 + mu + sigma * rng.standard_normal(n))


FAMILIES = ST.all_families(CFG)
WEIGHT_FAMILIES = [f for f in FAMILIES if not f.model_priced]


# ---------- 一覧 ----------
def test_family_ids_unique_and_expected():
    ids = [f.family_id for f in FAMILIES]
    assert len(ids) == len(set(ids))
    for must in ("fx_tsmom", "fx_carry", "cmd_tsmom", "idx_trend", "multi_tsmom", "jpm_sector_mom",
                 "jpm_reversal", "usm_sector_mom", "opt_csp_SPY", "opt_pcs_QQQ", "opt_cc_1321_T"):
        assert must in ids
    assert len(ids) == 8 + 3 * 3


def test_all_isinstance_family():
    assert all(isinstance(f, Family) for f in FAMILIES)


def test_flags_and_labels():
    by = {f.family_id: f for f in FAMILIES}
    assert by["fx_tsmom"].needs_rate_history and by["fx_carry"].needs_rate_history
    assert not any(f.needs_rate_history for f in FAMILIES if f.family_id not in ("fx_tsmom", "fx_carry"))
    assert by["multi_tsmom"].asset_class == "index" and by["multi_tsmom"].label_ja == "複数資産の順張り"
    assert by["opt_csp_SPY"].label_ja == "オプション：S&P500 の現金担保プット売り"
    assert by["jpm_sector_mom"].label_ja == "信用取引：日本の業種の強弱"
    assert all(f.model_priced == (f.asset_class == "options") for f in FAMILIES)


@pytest.mark.parametrize("fam", FAMILIES, ids=lambda f: f.family_id)
def test_param_grid_size(fam):
    g = fam.param_grid(CFG)
    # 表の設定どおり opt_csp・opt_cc は 3 個（他は 4〜12 個）
    assert (3 if fam.model_priced else 4) <= len(g) <= 12
    assert len({tuple(sorted(p.items())) for p in g}) == len(g)


# ---------- 順張り ----------
def test_tsmom_long_for_uptrend_short_for_downtrend():
    d = _mini({"UP": _noisy(400, 0.002, seed=1), "DN": _noisy(400, -0.002, seed=2)})
    w = ST._tsmom_weights(d, ["UP", "DN"], 126, 0, False, CFG)
    last = w.iloc[-1]
    assert last["UP"] > 0 and last["DN"] < 0
    assert last.abs().sum() == pytest.approx(1.0)


def test_tsmom_long_only_never_short():
    d = _mini({"UP": _noisy(400, 0.002, seed=1), "DN": _noisy(400, -0.002, seed=2)})
    w = ST._tsmom_weights(d, ["UP", "DN"], 126, 0, True, CFG)
    assert (w >= 0).all().all()
    assert w.iloc[-1]["UP"] > 0 and w.iloc[-1]["DN"] == 0


def test_tsmom_uses_only_past_return_window():
    """L 日の収益は close[t]/close[t-L]-1。窓の外（L 日より前）の値を変えても重みは変わらない。"""
    px = _noisy(400, 0.001, seed=3)
    d1 = _mini({"A": px, "B": _noisy(400, 0.0, seed=4)})
    w1 = ST._tsmom_weights(d1, ["A", "B"], 63, 0, False, CFG)
    r = ST._past_return(d1.close, 63)
    assert r["A"].iloc[100] == pytest.approx(px[100] / px[37] - 1.0)
    r5 = ST._past_return(d1.close, 63, 5)
    assert r5["A"].iloc[100] == pytest.approx(px[95] / px[32] - 1.0)
    assert w1.iloc[-1].abs().sum() == pytest.approx(1.0)


def test_family_targets_signs_match_drift():
    d = _universe_data(drift_sign=lambda s, i: 1 if i % 2 == 0 else -1, sigma=0.004)
    fam = {f.family_id: f for f in FAMILIES}["fx_tsmom"]
    tg = fam.targets(d, {"L": 126, "skip": 0}, CFG)
    assert tg
    order = {s: i for i, s in enumerate(d.instruments)}
    for t in tg:
        expect = 1 if order[t["symbol"]] % 2 == 0 else -1
        assert np.sign(t["weight"]) == expect


# ---------- 金利差 ----------
def test_carry_buys_high_yield_sells_low_yield():
    d = _universe_data()
    syms = d.symbols("fx")
    w = ST._carry_weights(d, syms, 2, False, CFG)
    last = w.iloc[-1]
    assert {s for s in syms if last[s] > 0} == {"AUDJPY=X", "GBPJPY=X"}
    assert {s for s in syms if last[s] < 0} == {"EURUSD=X", "CHFJPY=X"}
    assert last[last > 0].sum() == pytest.approx(-last[last < 0].sum())


def test_carry_drops_when_swap_markup_wipes_edge():
    flat = {c: 0.01 for c in RATES}
    d = _universe_data(rates=flat)
    w = ST._carry_weights(d, d.symbols("fx"), 2, False, CFG)
    assert (w.iloc[-1] == 0).all()


def test_carry_trend_filter_removes_opposite_direction():
    d = _universe_data()
    syms = d.symbols("fx")
    # AUDJPY を下げ続けにする → 買い候補だが 63 日の向きが逆なので外れる
    close = d.close.copy()
    close["AUDJPY=X"] = _noisy(N, -0.003, seed=9)
    d2 = _rebuild(d, close)
    plain = ST._carry_weights(d2, syms, 2, False, CFG).iloc[-1]
    trend = ST._carry_weights(d2, syms, 2, True, CFG).iloc[-1]
    assert plain["AUDJPY=X"] > 0
    assert trend["AUDJPY=X"] == 0


def test_carry_equal_leg_counts_when_one_side_short_of_candidates():
    """売りの候補が 2 本しかなければ、k=3 でも買いも 2 本にそろえる。"""
    rates = dict(RATES, EUR=0.04, CHF=0.0, USD=0.06)
    d = _universe_data(rates=rates)
    syms = d.symbols("fx")
    last = ST._carry_weights(d, syms, 3, False, CFG).iloc[-1]
    assert (last > 0).sum() == (last < 0).sum()
    # 売りの候補は base−quote が −取り分 より小さい USD 建ての 2 本（EURUSD −2%・GBPUSD −1%）
    assert {s for s in syms if last[s] < 0} == {"EURUSD=X", "GBPUSD=X"}
    assert {s for s in syms if last[s] > 0} == {"USDJPY=X", "AUDJPY=X"}
    assert last[last > 0].sum() == pytest.approx(0.5) and last[last < 0].sum() == pytest.approx(-0.5)


def test_carry_no_position_without_short_candidates():
    rates = {c: 0.05 for c in RATES}
    rates["JPY"] = 0.0
    rates["USD"] = 0.05
    d = _universe_data(rates=rates)
    w = ST._carry_weights(d, d.symbols("fx"), 2, False, CFG)
    assert (w.iloc[-1] == 0).all()


def test_select_legs_ties_never_overlap():
    """同点が並んでも、買いと売りに同じ銘柄が入らず両側 k 本。"""
    idx = pd.bdate_range("2021-01-01", periods=3)
    score = pd.DataFrame(0.0, index=idx, columns=list("ABCD"))
    inv = pd.DataFrame(1.0, index=idx, columns=list("ABCD"))
    w = ST._select_legs(score, None, inv, score.notna().sum(axis=1), 2, False)
    assert ((w > 0).sum(axis=1) == 2).all() and ((w < 0).sum(axis=1) == 2).all()
    assert np.allclose(w.abs().to_numpy(), 0.25)


def test_tsmom_weights_proportional_to_inverse_vol_hand_calc():
    """両方上向き・変動が 2 倍違う 2 銘柄 → 重みは 1/σ の比（約 2:1）で Σ=1。"""
    n = 300
    rng = np.random.default_rng(0)
    z = rng.standard_normal(n)
    a = 100 * np.cumprod(1 + 0.003 + 0.005 * z)
    b = 100 * np.cumprod(1 + 0.006 + 0.010 * z)
    d = _mini({"A": a, "B": b})
    last = ST._tsmom_weights(d, ["A", "B"], 63, 0, False, CFG).iloc[-1]
    sa = pd.Series(a).pct_change().iloc[-60:].std()
    sb = pd.Series(b).pct_change().iloc[-60:].std()
    expect_a = (1 / sa) / (1 / sa + 1 / sb)
    assert last["A"] == pytest.approx(expect_a) and last["B"] == pytest.approx(1 - expect_a)


# ---------- 業種の強弱 ----------
def test_sector_mom_leg_counts_and_risk_balance():
    d = _universe_data(drift_sign=lambda s, i: (i % 5) - 2, sigma=0.01)
    syms = d.symbols("jp_margin")
    w = ST._sector_mom_weights(d, syms, 126, 21, 3, False, CFG).iloc[-1]
    assert (w > 0).sum() == 3 and (w < 0).sum() == 3
    assert w[w > 0].sum() == pytest.approx(-w[w < 0].sum())


def test_sector_mom_long_only_top_k_only():
    d = _universe_data(drift_sign=lambda s, i: (i % 5) - 2)
    w = ST._sector_mom_weights(d, d.symbols("jp_margin"), 126, 21, 3, True, CFG).iloc[-1]
    assert (w > 0).sum() == 3 and (w < 0).sum() == 0
    assert w.sum() == pytest.approx(1.0)


def test_cross_section_needs_2k_names():
    d = _universe_data()
    three = d.symbols("us_margin")[:3]
    w = ST._sector_mom_weights(d, three, 126, 21, 2, False, CFG)
    assert (w == 0).all().all()
    w3 = ST._sector_mom_weights(d, three, 126, 21, 1, False, CFG)
    assert (w3.iloc[-1] != 0).sum() == 2


def test_reversal_buys_losers_and_holds_between_rebalances():
    d = _universe_data(seed=11)
    syms = d.symbols("jp_margin")
    w = ST._reversal_weights(d, syms, 3, 5, CFG)
    last = w.iloc[-1]
    assert (last > 0).sum() == 3 and (last < 0).sum() == 3
    ret5 = d.close[syms].pct_change(5)
    reb = (np.arange(len(w)) // 5) * 5          # 直近の判定の行
    row = reb[-1]
    losers = set(ret5.iloc[row].nsmallest(3).index)
    assert {s for s in syms if last[s] > 0} == losers
    changed = (w.diff().abs().sum(axis=1) > 0).to_numpy()
    assert not changed[(np.arange(len(w)) % 5) != 0].any()


# ---------- 月末の再判定 ----------
def test_month_end_flags_last_row_is_not_month_end():
    idx = pd.bdate_range("2024-01-29", "2024-03-12")
    f = ST._month_end_flags(idx)
    days = list(idx[f])
    assert pd.Timestamp("2024-01-31") in days and pd.Timestamp("2024-02-29") in days
    assert not f[-1]
    assert f.sum() == 2


def test_month_end_flag_on_last_row_of_month_data():
    idx = pd.bdate_range("2024-01-02", "2024-01-31")
    assert not ST._month_end_flags(idx)[-1]      # 月末が来たかは次の行がないと分からない


def test_trend_monthly_weights_change_only_on_month_end():
    d = _mini({"A": _noisy(500, 0.0005, 0.01, seed=5), "B": _noisy(500, 0.0, 0.01, seed=6)})
    w = ST._trend_weights(d, ["A", "B"], 50, True, CFG)
    flags = ST._month_end_flags(w.index)
    changed = (w.diff().abs().sum(axis=1) > 1e-12).to_numpy()
    assert changed.any()
    assert not changed[~flags].any()


def test_trend_buys_only_above_moving_average():
    up = _noisy(400, 0.002, seed=1)
    dn = _noisy(400, -0.002, seed=2)
    d = _mini({"UP": up, "DN": dn})
    w = ST._trend_weights(d, ["UP", "DN"], 100, False, CFG).iloc[-1]
    assert w["UP"] == pytest.approx(1.0) and w["DN"] == 0
    sma = pd.Series(up).rolling(100).mean().iloc[-1]
    assert up[-1] > sma


# ---------- 履歴不足 ----------
def test_short_history_symbol_excluded_until_enough_days():
    n = 500
    a = _noisy(n, 0.001, seed=1)
    b = _noisy(n, 0.001, seed=2)
    b[:300] = np.nan
    d = _mini({"A": a, "B": b})
    cfg = SimpleNamespace(QUANT_MIN_HISTORY_DAYS=150)
    w = ST._tsmom_weights(d, ["A", "B"], 63, 0, False, cfg)
    # B の有効な終値が 150 本になるのは行 449 以降
    assert (w["B"].iloc[:449] == 0).all()
    assert w["B"].iloc[-1] != 0
    assert w["A"].iloc[200] != 0


# ---------- targets ----------
@pytest.mark.parametrize("fam", WEIGHT_FAMILIES, ids=lambda f: f.family_id)
def test_targets_no_zero_and_sign_consistent(fam):
    d = _universe_data(drift_sign=lambda s, i: 1 if i % 2 == 0 else -1, sigma=0.006)
    final = fam.final_weights(d, fam.param_grid(CFG)[0], CFG)
    tg = fam.targets(d, fam.param_grid(CFG)[0], CFG)
    last = final.iloc[-1]
    assert {t["symbol"] for t in tg} == set(last[last.abs() > 1e-12].index)
    for t in tg:
        assert t["weight"] != 0
        assert np.sign(t["weight"]) == np.sign(last[t["symbol"]])
        assert t["note"] and t["name"]
    assert any(t["weight"] > 0 for t in tg) or fam.family_id in ("fx_carry",)


def test_targets_short_is_negative_and_long_only_has_none():
    d = _universe_data(drift_sign=lambda s, i: -1, sigma=0.004)
    fams = {f.family_id: f for f in FAMILIES}
    tg = fams["cmd_tsmom"].targets(d, {"L": 126, "long_only": False}, CFG)
    assert tg and all(t["weight"] < 0 for t in tg)
    assert fams["cmd_tsmom"].targets(d, {"L": 126, "long_only": True}, CFG) == []


def test_multi_tsmom_has_three_classes_and_leverage_within_cap():
    d = _universe_data(drift_sign=lambda s, i: 1 if i % 2 == 0 else -1, sigma=0.004)
    fam = {f.family_id: f for f in FAMILIES}["multi_tsmom"]
    final = fam.final_weights(d, {"L": 126, "monthly": False}, CFG)
    last = final.iloc[-1]
    classes = {d.instruments[s].asset_class for s in last[last.abs() > 0].index}
    assert classes == {"fx", "commodity", "index"}
    assert last.abs().sum() <= 1.5 + 1e-9            # 3 クラスを足した全体は最小の上限（1.5）以内
    for ac in ("fx", "commodity", "index"):
        part = last[[s for s in last.index if d.instruments[s].asset_class == ac]]
        assert part.abs().sum() <= 0.5 + 1e-9        # クラスごとは上限の 1/3


def test_run_returns_simresult_and_option_family_runs():
    d = _universe_data(sigma=0.008)
    fams = {f.family_id: f for f in FAMILIES}
    r = fams["idx_trend"].run(d, {"M": 100, "monthly": False}, CFG)
    assert len(r.net) == len(d.close)
    ro = fams["opt_csp_SPY"].run(d, {"otm": 0.05}, CFG)
    assert len(ro.net) == len(d.close) and ro.cost.sum() > 0
    tg = fams["opt_csp_SPY"].targets(d, {"otm": 0.05}, CFG)
    assert len(tg) == 1 and tg[0]["symbol"] == "SPY" and tg[0]["weight"] == 1.0 and tg[0]["legs"]


def test_missing_asset_class_fails_with_reason():
    d = _mini({"X": _noisy(300, 0.0)})
    fam = {f.family_id: f for f in FAMILIES}["jpm_sector_mom"]
    with pytest.raises(ValueError, match="jp_margin"):
        fam.run(d, fam.param_grid(CFG)[0], CFG)


# ---------- 未来を見ない（オプション以外の全系統） ----------
def _future_perturbed(d, t, seed=99):
    rng = np.random.default_rng(seed)
    close = d.close.copy()
    shock = np.cumprod(1.0 + 0.08 * rng.standard_normal((len(close) - t - 1, close.shape[1])), axis=0)
    close.iloc[t + 1:] = close.iloc[t + 1:].to_numpy() * shock
    return _rebuild(d, close)


@pytest.mark.parametrize("fam", WEIGHT_FAMILIES, ids=lambda f: f.family_id)
def test_no_lookahead_weights_before_change_are_identical(fam):
    d = _universe_data(seed=21)
    t = 450
    d2 = _future_perturbed(d, t)
    for params in fam.param_grid(CFG):
        w1 = fam.run(d, params, CFG).weights
        w2 = fam.run(d2, params, CFG).weights
        # run().weights は 1 日ずらした後。行 t+1 の重みは日 t の重みで、日 t までの情報しか使えない
        pd.testing.assert_frame_equal(w1.iloc[: t + 2], w2.iloc[: t + 2], check_exact=False, atol=1e-12)
