# ==============================================================
# tests/test_quant_stats.py — quant/stats.py の数式を既知の値で確かめる
# ==============================================================
# 合成データだけを使う（ネットワークなし）。乱数は seed 固定。
# ==============================================================
from __future__ import annotations

import math
import time

import numpy as np
import pytest

from quant import stats as st


# ── 正規分布 ──

@pytest.mark.parametrize("p, x", [
    (0.5, 0.0),
    (0.975, 1.959963984540054),
    (0.95, 1.6448536269514722),
    (0.001, -3.090232306167813),
    (0.01, -2.3263478740408408),
    (1e-8, -5.612001244174789),
])
def test_norm_ppf_known_values(p, x):
    assert st.norm_ppf(p) == pytest.approx(x, abs=1e-9)


def test_norm_ppf_inverts_cdf():
    for p in np.linspace(1e-6, 1 - 1e-6, 201):
        assert st.norm_cdf(st.norm_ppf(float(p))) == pytest.approx(float(p), abs=1e-12)
    assert st.norm_ppf(0.0) == -math.inf and st.norm_ppf(1.0) == math.inf
    with pytest.raises(ValueError):
        st.norm_ppf(1.5)


# ── 指標 ──

def test_sharpe_basic_and_degenerate():
    r = np.array([0.01, -0.005, 0.02, 0.0, 0.003])
    exp = r.mean() / r.std(ddof=1) * math.sqrt(252)
    assert st.sharpe(r) == pytest.approx(exp)
    assert st.sharpe([0.01]) == 0.0
    assert st.sharpe([0.01, 0.01, 0.01]) == 0.0
    assert st.sharpe([0.01, np.nan, -0.01, 0.02]) == pytest.approx(st.sharpe([0.01, -0.01, 0.02]))


def test_max_drawdown_hand_example():
    # 1 → 1.1 → 0.88 → 0.968 → 1.1616：高値 1.1 から 0.88 で 20% の下げ
    r = [0.10, -0.20, 0.10, 0.20]
    assert st.max_drawdown(r) == pytest.approx(0.20)
    # 初日から下げる：1 → 0.9 → 0.45（55%）
    assert st.max_drawdown([-0.1, -0.5, 0.3]) == pytest.approx(0.55)
    assert st.max_drawdown([0.01, 0.02]) == 0.0


def test_ann_return_vol_calmar_sortino():
    r = np.full(252, 0.001)
    assert st.ann_return(r) == pytest.approx(1.001 ** 252 - 1)
    r2 = [0.01, -0.02, 0.015, 0.005]
    assert st.ann_vol(r2) == pytest.approx(np.std(r2, ddof=1) * math.sqrt(252))
    assert st.calmar(r2) == pytest.approx(st.ann_return(r2) / st.max_drawdown(r2))
    dd = math.sqrt(np.mean(np.minimum(r2, 0) ** 2))
    assert st.sortino(r2) == pytest.approx(np.mean(r2) / dd * math.sqrt(252))
    assert st.ann_return([-1.0, 0.1]) == -1.0


def test_skew_kurtosis_normal():
    r = np.random.default_rng(1).normal(0, 0.01, 200_000)
    assert st.skew(r) == pytest.approx(0.0, abs=0.02)
    assert st.kurtosis(r) == pytest.approx(3.0, abs=0.05)
    # 右に裾の長い系列は歪度が正
    assert st.skew(np.random.default_rng(2).exponential(1.0, 50_000)) == pytest.approx(2.0, abs=0.15)


def test_summary_keys():
    r = np.random.default_rng(3).normal(0.0005, 0.01, 500)
    s = st.summary(r)
    assert set(s) == {"days", "ann_return", "ann_vol", "sharpe", "sortino", "max_drawdown",
                      "calmar", "skew", "kurtosis", "hit_rate"}
    assert s["days"] == 500
    assert s["hit_rate"] == pytest.approx(np.mean(r > 0))


# ── PSR・DSR・MinTRL ──

def test_psr_half_at_benchmark():
    r = np.random.default_rng(4).normal(0.0004, 0.01, 1000)
    sr_annual = st.sharpe(r)
    assert st.psr(r, sr_benchmark=sr_annual) == pytest.approx(0.5, abs=1e-12)


def test_psr_matches_formula_and_direction():
    r = np.random.default_rng(5).normal(0.001, 0.01, 750)
    a = r
    sr = a.mean() / a.std(ddof=1)
    d = a - a.mean()
    g3 = np.mean(d ** 3) / np.mean(d ** 2) ** 1.5
    g4 = np.mean(d ** 4) / np.mean(d ** 2) ** 2
    z = sr * math.sqrt(len(a) - 1) / math.sqrt(1 - g3 * sr + (g4 - 1) / 4 * sr ** 2)
    assert st.psr(r) == pytest.approx(st.norm_cdf(z))
    assert st.psr(r) > 0.99
    assert st.psr(-r) < 0.01


def test_expected_max_sharpe_matches_simulation():
    rng = np.random.default_rng(6)
    for n in (10, 50, 200):
        sim = rng.normal(0.0, 1.0, (20_000, n)).max(axis=1).mean()
        assert st.expected_max_sharpe(n, 1.0) == pytest.approx(sim, rel=0.03)
    # 期間あたりのシャープで：真の平均 0 の試行（各 T 日）の最大のシャープの平均
    T, n = 500, 30
    rets = rng.normal(0, 0.01, (400, T, n))
    srs = rets.mean(axis=1) / rets.std(axis=1, ddof=1)
    sim_max = srs.max(axis=1).mean()
    assert st.expected_max_sharpe(n, 1.0 / T) == pytest.approx(sim_max, rel=0.06)
    assert st.expected_max_sharpe(1, 1.0) == 0.0
    assert st.expected_max_sharpe(10, 0.0) == 0.0


def test_dsr_deflates_with_more_trials():
    r = np.random.default_rng(7).normal(0.0008, 0.01, 1000)
    one = st.dsr(r, [st.sharpe(r)])
    assert one["n_trials"] == 1 and one["sr_benchmark_annual"] == 0.0
    assert one["dsr"] == pytest.approx(st.psr(r))
    trials = np.random.default_rng(8).normal(0.0, 0.5, 100)
    many = st.dsr(r, trials)
    assert many["n_trials"] == 100
    assert many["sr_benchmark_annual"] > 0.5
    assert many["dsr"] < one["dsr"]
    v = np.var(trials / math.sqrt(252), ddof=1)
    assert many["sr_benchmark_annual"] == pytest.approx(st.expected_max_sharpe(100, v) * math.sqrt(252))


def test_min_track_record_length():
    r = np.random.default_rng(9).normal(0.001, 0.01, 2000)
    mtrl = st.min_track_record_length(r, conf=0.95)
    assert 1 < mtrl < 2000
    # 必要な日数ちょうどで PSR が conf になる（同じ SR・歪度・尖度のとき）
    sr = r.mean() / r.std(ddof=1)
    d = r - r.mean()
    g3 = np.mean(d ** 3) / np.mean(d ** 2) ** 1.5
    g4 = np.mean(d ** 4) / np.mean(d ** 2) ** 2
    z = sr * math.sqrt(mtrl - 1) / math.sqrt(1 - g3 * sr + (g4 - 1) / 4 * sr ** 2)
    assert st.norm_cdf(z) == pytest.approx(0.95, abs=1e-9)
    assert st.min_track_record_length(-r) == math.inf


# ── PBO ──

def test_pbo_noise_is_about_half():
    vals = []
    for seed in range(10):
        x = np.random.default_rng(seed).normal(0, 0.01, (1000, 30))
        res = st.pbo_cscv(x, n_blocks=10)
        assert res["n_combos"] == 252 and res["n_trials"] == 30
        vals.append(res["pbo"])
    assert np.mean(vals) == pytest.approx(0.5, abs=0.12)


def test_pbo_one_strong_trial_is_near_zero():
    x = np.random.default_rng(11).normal(0, 0.01, (1500, 30))
    x[:, 7] += 0.003
    assert st.pbo_cscv(x)["pbo"] < 0.05


def test_pbo_insufficient_and_speed():
    assert st.pbo_cscv(np.zeros((500, 1)))["pbo"] is None
    assert st.pbo_cscv(np.random.default_rng(0).normal(size=(15, 5)))["pbo"] is None
    with pytest.raises(ValueError):
        st.pbo_cscv(np.zeros(100))
    x = np.random.default_rng(12).normal(0, 0.01, (2500, 60))
    t0 = time.perf_counter()
    st.pbo_cscv(x, n_blocks=10)
    assert time.perf_counter() - t0 < 1.0


# ── 再標本 ──

def test_stationary_bootstrap_shape_and_seed():
    r = np.arange(100, dtype=float)
    a = st.stationary_bootstrap(r, 50, 10, seed=1)
    assert a.shape == (50, 100)
    b = st.stationary_bootstrap(r, 50, 10, seed=1)
    assert np.array_equal(a, b)
    c = st.stationary_bootstrap(r, 50, 10, seed=2)
    assert not np.array_equal(a, c)
    assert st.stationary_bootstrap(r, 7, 10, seed=1, length=30).shape == (7, 30)
    assert set(np.unique(a)) <= set(r)


def test_stationary_bootstrap_block_length():
    # 値＝位置の系列で、隣が「前の位置の次」になる割合は 1 − 1/mean_block 前後
    r = np.arange(1000, dtype=float)
    a = st.stationary_bootstrap(r, 200, 20, seed=3, length=1000)
    cont = np.mean((a[:, 1:] - a[:, :-1]) % 1000 == 1)
    assert cont == pytest.approx(1 - 1 / 20, abs=0.01)
    with pytest.raises(ValueError):
        st.stationary_bootstrap([np.nan], 10, 5, seed=0)


def test_prob_profit_extremes():
    rng = np.random.default_rng(13)
    up = rng.normal(0.002, 0.005, 500)
    res = st.prob_profit(up, 252, 500, 20, seed=1)
    assert res["p_profit"] > 0.99
    assert res["q05"] < res["q50"] < res["q95"]
    down = rng.normal(-0.002, 0.005, 500)
    assert st.prob_profit(down, 252, 500, 20, seed=1)["p_profit"] < 0.01
    assert st.prob_profit([0.01], 252, 100, 20, seed=1)["p_profit"] is None


def test_bootstrap_pvalue_mean():
    rng = np.random.default_rng(14)
    assert st.bootstrap_pvalue_mean(rng.normal(0.003, 0.01, 1000), 1000, 20, seed=1) < 0.01
    assert st.bootstrap_pvalue_mean(rng.normal(-0.003, 0.01, 1000), 1000, 20, seed=1) > 0.99
    p = st.bootstrap_pvalue_mean(rng.normal(0.0, 0.01, 1000), 1000, 20, seed=1)
    assert 0.0 <= p <= 1.0


# ── 多重検定 ──

def test_holm_textbook():
    assert st.holm([0.01, 0.04, 0.03, 0.005]) == pytest.approx([0.03, 0.06, 0.06, 0.02])
    assert st.holm([0.5, 0.9]) == pytest.approx([1.0, 1.0])


def test_bh_textbook():
    assert st.benjamini_hochberg([0.01, 0.04, 0.03, 0.005]) == pytest.approx([0.02, 0.04, 0.04, 0.02])
    p = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, 0.212, 0.216]
    exp = [0.01, 0.04, 0.084, 0.084, 0.084, 0.1, 0.10571428571428572, 0.216, 0.216, 0.216]
    assert st.benjamini_hochberg(p) == pytest.approx(exp)


def test_multiple_testing_nan_and_range():
    out = st.holm([0.01, float("nan"), 0.02])
    assert math.isnan(out[1]) and out[0] == pytest.approx(0.02) and out[2] == pytest.approx(0.02)
    with pytest.raises(ValueError):
        st.benjamini_hochberg([1.2])


# ── 端の条件・追加の確認 ──

def test_pbo_constant_column_does_not_win():
    # 定数の列（丸めで分散が極小の正になる値）が学習で最良になってはならない
    x = np.random.default_rng(20).normal(0, 0.01, (1000, 5))
    x[:, 2] = 0.0123
    res = st.pbo_cscv(x)
    assert res["pbo"] is not None and 0.0 <= res["pbo"] <= 1.0
    # 定数列は常に 0 扱い：他の列に本当に強いものがあれば、そちらが最良になり PBO は小さい
    x[:, 4] += 0.004
    assert st.pbo_cscv(x)["pbo"] < 0.05


def test_pbo_nan_rows_and_args():
    x = np.random.default_rng(21).normal(0, 0.01, (600, 6))
    y = x.copy()
    y[5, 3] = np.nan
    y[100, 0] = np.nan
    assert st.pbo_cscv(y)["pbo"] == pytest.approx(st.pbo_cscv(np.delete(x, [5, 100], axis=0))["pbo"])
    for bad in (3, 1, 0):
        with pytest.raises(ValueError):
            st.pbo_cscv(x, n_blocks=bad)
    assert st.pbo_cscv(x, n_blocks=4)["n_combos"] == 6


def test_degenerate_inputs():
    assert st.psr([0.01]) == 0.0
    assert st.psr([0.01, 0.01, 0.01]) == 0.0
    assert st.min_track_record_length([0.01, 0.01]) == math.inf
    assert st.sortino([0.01, 0.02, 0.03]) == 0.0
    assert st.skew([0.01, 0.01, 0.01]) == 0.0 and st.kurtosis([0.01, 0.01, 0.01]) == 3.0
    assert st.max_drawdown([]) == 0.0 and st.ann_return([]) == 0.0 and st.calmar([0.01, 0.02]) == 0.0
    assert st.summary([])["days"] == 0 and st.summary([])["hit_rate"] == 0.0
    assert st.bootstrap_pvalue_mean([0.01], 100, 5, seed=1) == 1.0
    assert st.dsr([0.01, -0.01, 0.02], [float("nan"), 0.5])["n_trials"] == 1


def test_mtrl_with_benchmark_and_periods():
    r = np.random.default_rng(22).normal(0.001, 0.01, 2000)
    base = st.min_track_record_length(r)
    assert st.min_track_record_length(r, sr_benchmark=1.0) > base
    # 基準が自分のシャープ以上なら判断不能（inf）
    assert st.min_track_record_length(r, sr_benchmark=st.sharpe(r) + 0.1) == math.inf


def test_psr_hand_value_normal_case():
    # 歪度 0・尖度 3 の理想形（SR=0.1・n=101）：分母 = 1 + 0.5·SR²、z = 0.1·10/√1.005
    sr, n = 0.1, 101
    z = sr * math.sqrt(n - 1) / math.sqrt(1 + 0.5 * sr * sr)
    assert st.norm_cdf(z) == pytest.approx(0.8413447460685429, rel=2e-3)


def test_holm_bh_order_preserved_with_ties_and_dominance():
    p = [0.2, 0.001, 0.2, 0.03]
    h, b = st.holm(p), st.benjamini_hochberg(p)
    assert h == pytest.approx([0.4, 0.004, 0.4, 0.09])
    assert all(bb <= hh + 1e-12 for bb, hh in zip(b, h))
    assert st.holm([]) == [] and st.benjamini_hochberg([]) == []
