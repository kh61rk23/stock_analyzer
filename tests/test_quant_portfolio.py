# ==============================================================
# tests/test_quant_portfolio.py — quant/portfolio.py の配分・上限・倍率・ブレーキを確かめる
# ==============================================================
# 合成の日次損益だけを使う（ネットワークなし）。乱数は seed 固定。再標本の回数は設定で減らして速くする。
# ==============================================================
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant import portfolio


class Cfg:
    QUANT_BOOT_N = 200
    QUANT_PORT_MAX_STRATEGY_WEIGHT = 1.0
    QUANT_PORT_MAX_CLASS_WEIGHT = 1.0


def mk(**over):
    c = Cfg()
    for k, v in over.items():
        setattr(c, k, v)
    return c


def rets(mean, sd, n=600, seed=0, start="2020-01-01", base=None):
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, 1, n) if base is None else base
    return pd.Series(mean + sd * noise, index=pd.bdate_range(start, periods=n))


def item(r, cls="fx"):
    return {"asset_class": cls, "returns": r}


def test_zero_adopted():
    res = portfolio.build({}, None)
    assert res["weights"] == {} and res["cash"] == 1.0 and res["note"] == "採用した戦略なし"


def test_single_strategy_is_capped_and_rest_is_cash():
    res = portfolio.build({"a": item(rets(0.0005, 0.005))}, None)
    assert res["weights"]["a"] == pytest.approx(0.35)
    assert res["cash"] == pytest.approx(0.65)


def test_single_strategy_without_caps_is_fully_invested():
    res = portfolio.build({"a": item(rets(0.0005, 0.005))}, mk())
    assert res["weights"]["a"] == pytest.approx(1.0)
    assert res["cash"] == pytest.approx(0.0)


def test_output_shape():
    res = portfolio.build({"a": item(rets(0.0005, 0.005))}, mk())
    assert set(res) >= {"weights", "cash", "scale", "metrics", "p_profit", "returns", "brake_now"}
    assert set(res["p_profit"]) == {"63", "126", "252"}
    assert all(0.0 <= v <= 1.0 for v in res["p_profit"].values())
    d, v = res["returns"][0]
    assert isinstance(d, str) and len(d) == 10 and isinstance(v, float)
    assert len(res["returns"]) == 600
    assert res["metrics"]["days"] == 600


def test_equal_risk_weights_when_uncorrelated():
    a = rets(0.0005, 0.005, seed=1)
    b = rets(0.0005, 0.010, seed=2)
    res = portfolio.build({"a": item(a), "b": item(b)}, mk())
    assert res["weights"]["a"] > res["weights"]["b"]            # 変動の小さい方に多く配る
    assert res["weights"]["a"] / res["weights"]["b"] == pytest.approx(2.0, rel=0.25)
    assert sum(res["weights"].values()) == pytest.approx(1.0)


def test_highly_correlated_pair_is_discounted():
    base = np.random.default_rng(10).normal(0, 1, 600)
    a = rets(0.0005, 0.005, base=base)
    b = rets(0.0005, 0.005, base=base + np.random.default_rng(11).normal(0, 0.05, 600))
    c = rets(0.0005, 0.005, seed=12)
    res = portfolio.build({"a": item(a), "b": item(b), "c": item(c)}, mk())
    w = res["weights"]
    assert w["c"] > w["a"] and w["c"] > w["b"]
    assert w["c"] == pytest.approx(0.5, abs=0.05)


def test_class_cap_leaves_cash_without_redistribution():
    rs = {k: item(rets(0.0005, 0.005, seed=s), "fx") for k, s in (("a", 1), ("b", 2), ("c", 3))}
    res = portfolio.build(rs, mk(QUANT_PORT_MAX_CLASS_WEIGHT=0.5))
    assert sum(res["weights"].values()) == pytest.approx(0.5)
    assert res["cash"] == pytest.approx(0.5)


def test_class_cap_does_not_touch_other_class():
    rs = {"a": item(rets(0.0005, 0.005, seed=1), "fx"), "b": item(rets(0.0005, 0.005, seed=2), "fx"),
          "c": item(rets(0.0005, 0.005, seed=3), "index")}
    res = portfolio.build(rs, mk(QUANT_PORT_MAX_CLASS_WEIGHT=0.4))
    w = res["weights"]
    assert w["a"] + w["b"] == pytest.approx(0.4)
    assert w["c"] <= 0.4 + 1e-9


def test_strategy_cap_applies():
    rs = {"a": item(rets(0.0005, 0.001, seed=1)), "b": item(rets(0.0005, 0.02, seed=2))}
    res = portfolio.build(rs, mk(QUANT_PORT_MAX_STRATEGY_WEIGHT=0.6))
    assert res["weights"]["a"] == pytest.approx(0.6)
    assert res["cash"] == pytest.approx(1.0 - sum(res["weights"].values()))


def test_nonpositive_mean_gives_zero_scale():
    res = portfolio.build({"a": item(rets(-0.001, 0.005))}, mk())
    assert res["scale"] == 0.0
    assert all(v == 0.0 for _, v in res["returns"])


def test_kelly_caps_scale():
    r = rets(0.0003, 0.01)
    loose = portfolio.build({"a": item(r)}, mk(QUANT_PORT_KELLY_FRACTION=100.0, QUANT_PORT_MAX_SCALE=100.0,
                                               QUANT_PORT_TARGET_VOL=0.05))
    tight = portfolio.build({"a": item(r)}, mk(QUANT_PORT_KELLY_FRACTION=0.01, QUANT_PORT_MAX_SCALE=100.0,
                                               QUANT_PORT_TARGET_VOL=0.05))
    assert tight["scale"] < loose["scale"]
    mu = float(r.mean()) * 252
    vol = float(r.std(ddof=1)) * np.sqrt(252)
    assert tight["scale"] == pytest.approx(0.01 * mu / vol ** 2, rel=1e-6)


def test_vol_target_and_max_scale():
    r = rets(0.001, 0.01)
    vol = float(r.std(ddof=1)) * np.sqrt(252)
    res = portfolio.build({"a": item(r)}, mk(QUANT_PORT_TARGET_VOL=0.08, QUANT_PORT_KELLY_FRACTION=100.0))
    assert res["scale"] == pytest.approx(0.08 / vol, rel=1e-6)
    capped = portfolio.build({"a": item(r)}, mk(QUANT_PORT_TARGET_VOL=10.0, QUANT_PORT_KELLY_FRACTION=100.0,
                                                QUANT_PORT_MAX_SCALE=1.5))
    assert capped["scale"] == pytest.approx(1.5)


def test_union_of_dates_with_zero_fill():
    a = rets(0.0005, 0.005, n=400, seed=1, start="2020-01-01")
    b = rets(0.0005, 0.005, n=400, seed=2, start="2020-06-01")
    res = portfolio.build({"a": item(a), "b": item(b)}, mk())
    dates = {d for d, _ in res["returns"]}
    assert dates == {pd.Timestamp(d).strftime("%Y-%m-%d") for d in a.index.union(b.index)}


def test_too_little_common_period_raises():
    a = rets(0.0005, 0.005, n=200, seed=1, start="2020-01-01")
    b = rets(0.0005, 0.005, n=200, seed=2, start="2022-01-01")
    with pytest.raises(ValueError):
        portfolio.build({"a": item(a), "b": item(b)}, mk())


def test_zero_vol_strategy_raises():
    flat = pd.Series(0.001, index=pd.bdate_range("2020-01-01", periods=300))
    with pytest.raises(ValueError):
        portfolio.build({"a": item(flat)}, mk())


def test_brake_acts_from_next_day_not_same_day():
    n = 300
    r = rets(0.002, 0.002, n=n, seed=5)
    crash = 200
    r.iloc[crash] = -0.1
    cfg = mk(QUANT_PORT_KELLY_FRACTION=1000.0, QUANT_PORT_MAX_SCALE=3.0, QUANT_PORT_TARGET_VOL=5.0)
    res = portfolio.build({"a": item(r)}, cfg)
    w, s = res["weights"]["a"], res["scale"]
    out = pd.Series([v for _, v in res["returns"]])
    unbraked = r.reset_index(drop=True) * w * s
    assert unbraked.iloc[crash] < -0.12                          # 下げはブレーキ 0 の水準に達している
    assert out.iloc[crash] == pytest.approx(unbraked.iloc[crash])   # 当日は避けられない
    assert out.iloc[crash + 1] == pytest.approx(0.0)             # 翌日から建玉が 0


def test_brake_now_reflects_latest_drawdown():
    n = 300
    r = rets(0.002, 0.002, n=n, seed=5)
    r.iloc[-1] = -0.1                                            # 最後の日に大きく下げる
    cfg = mk(QUANT_PORT_KELLY_FRACTION=1000.0, QUANT_PORT_MAX_SCALE=3.0, QUANT_PORT_TARGET_VOL=5.0)
    res = portfolio.build({"a": item(r)}, cfg)
    assert res["brake_now"] == 0.0                               # 次の日から建てない
    out = [v for _, v in res["returns"]]
    assert out[-1] < -0.1                                        # 最後の日の下げ自体は受ける


def test_correlation_discount_matches_hand_calculation():
    # 同じ変動の2本（相関 ρ）と無相関の1本：割引は 1/(1+ρ)・1/(1+ρ)・1 になり、正規化後の重みはその比
    base = np.random.default_rng(20).normal(0, 1, 800)
    a = rets(0.0005, 0.005, n=800, base=base)
    b = rets(0.0005, 0.005, n=800, base=base + np.random.default_rng(21).normal(0, 1, 800))
    c = rets(0.0005, 0.005, n=800, seed=22)
    rs = {"a": item(a), "b": item(b), "c": item(c)}
    res = portfolio.build(rs, mk())
    df = pd.concat({"a": a, "b": b, "c": c}, axis=1)
    inv = 1.0 / (df.std(ddof=1) * np.sqrt(252))
    w = inv / inv.sum()
    pos = df.corr().clip(lower=0.0).to_numpy().copy()
    np.fill_diagonal(pos, 0.0)
    w = w / (1.0 + pos.sum(axis=1))
    w = w / w.sum()
    for k in ("a", "b", "c"):
        assert res["weights"][k] == pytest.approx(float(w[k]), rel=1e-9)


def test_flat_strategy_in_common_period_does_not_make_nan_weights():
    # b は全期間では動くが、3本がそろう期間（c がある後半 100 日）には 0 のまま＝その期間の相関が測れない
    a = rets(0.0005, 0.005, n=400, seed=1)
    b = rets(0.0005, 0.005, n=400, seed=2)
    b.iloc[300:] = 0.0
    c = rets(0.0005, 0.005, n=100, seed=3, start=a.index[300])
    res = portfolio.build({"a": item(a), "b": item(b), "c": item(c)}, mk())
    assert all(np.isfinite(v) for v in res["weights"].values())
    assert sum(res["weights"].values()) == pytest.approx(1.0)


def test_half_brake_between_thresholds_from_next_day():
    # 下げが 8%〜12% の間なら翌日から建玉を半分にする（倍率を手計算で固定）
    r = pd.Series([0.01] * 10 + [-0.10] + [0.001] * 5, index=pd.bdate_range("2021-01-01", periods=16))
    r = r + np.random.default_rng(0).normal(0, 1e-6, 16)
    cfg = mk(QUANT_PORT_KELLY_FRACTION=1000.0, QUANT_PORT_MAX_SCALE=1.0, QUANT_PORT_TARGET_VOL=100.0)
    res = portfolio.build({"a": item(r)}, cfg)
    assert res["scale"] == pytest.approx(1.0)
    out = [v for _, v in res["returns"]]
    assert out[10] == pytest.approx(r.iloc[10])                  # 下げの当日はそのまま受ける
    assert out[11] == pytest.approx(0.5 * r.iloc[11])            # 翌日から半分
    assert res["brake_now"] == 0.5


def test_no_brake_in_calm_series():
    res = portfolio.build({"a": item(rets(0.001, 0.002))}, mk())
    assert res["brake_now"] == 1.0


def test_ensemble_skips_single_cap_but_class_cap_follows_member_mix():
    """組み合わせは1戦略の上限（35%）を受けない。資産クラスの上限（50%）は中身の内訳で数える。"""
    import numpy as np
    import pandas as pd
    from quant import portfolio
    idx = pd.bdate_range("2015-01-01", periods=1500)
    r = pd.Series(np.random.default_rng(1).normal(0.0006, 0.005, 1500), index=idx)
    plain = portfolio.build({"s": {"asset_class": "fx", "returns": r}}, None)
    assert plain["weights"]["s"] == pytest.approx(0.35) and plain["cash"] == pytest.approx(0.65)
    # 中身が 3 クラスに 1/3 ずつ → どのクラスも上限の内なので全額
    mixed = portfolio.build({"e": {"asset_class": "ensemble", "ensemble": True, "returns": r,
                                   "class_mix": {"fx": 1 / 3, "index": 1 / 3, "options": 1 / 3}}}, None)
    assert mixed["weights"]["e"] == pytest.approx(1.0) and mixed["cash"] == pytest.approx(0.0)
    # 中身がオプションだけ → クラスの上限 50% で止まる（組み合わせだからといって上限を抜けない）
    single = portfolio.build({"e": {"asset_class": "ensemble", "ensemble": True, "returns": r,
                                    "class_mix": {"options": 1.0}}}, None)
    assert single["weights"]["e"] == pytest.approx(0.5) and single["cash"] == pytest.approx(0.5)
    # オプション 80%・FX 20% → オプションの持ち分 0.8 を 0.5 に縮める＝全体は 0.7
    part = portfolio.build({"e": {"asset_class": "ensemble", "ensemble": True, "returns": r,
                                  "class_mix": {"options": 0.8, "fx": 0.2}}}, None)
    assert part["weights"]["e"] == pytest.approx(0.7)
