# ==============================================================
# tests/test_quant_research.py — quant/research.py のテスト（合成データ・ネットワーク不使用）
# ==============================================================
# 偽の系統（設定ごとに平均の違う乱数の損益を返す）で、区間割りの境目・学習だけでの設定の選択・
# 検証の情報を使っていないこと・DSR・試行の数・Holm・理論価格の割引・落とす理由・例外の扱い・外部の実績を確かめる。
# ==============================================================
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from quant import research
from quant.types import Family, MarketData, SimResult

N_DAYS = 1500

CFG = SimpleNamespace(
    QUANT_WFO_FOLDS=4,
    QUANT_WFO_MIN_TRAIN_DAYS=300,
    QUANT_WFO_EMBARGO_DAYS=10,
    QUANT_BOOT_N=300,
    QUANT_PBO_BLOCKS=6,
)


def _idx(n=N_DAYS):
    return pd.bdate_range("2010-01-01", periods=n)


def _data(n=N_DAYS, rates_source="bis"):
    idx = _idx(n)
    ret = pd.DataFrame({"A": 0.0}, index=idx)
    return MarketData(close=ret + 100.0, returns=ret, rates=pd.DataFrame({"USD": 0.0}, index=idx),
                      instruments={}, quality={"rates_source": rates_source, "as_of": "2015-09-30"})


def _sim(net: pd.Series) -> SimResult:
    z = pd.Series(0.0, index=net.index)
    return SimResult(net=net, gross=net, cost=z, financing=z, turnover=z,
                     leverage=pd.Series(1.0, index=net.index), weights=pd.DataFrame(index=net.index))


class FakeFamily:
    """設定 {"mu": 年率シャープの目安} ごとに、seed 固定の乱数の損益を返す系統。"""

    def __init__(self, fid, mus, asset_class="fx", model_priced=False, needs_rate_history=False,
                 seed=0, override=None, fail_on=None, n=N_DAYS):
        self.family_id = fid
        self.asset_class = asset_class
        self.label_ja = f"偽の系統 {fid}"
        self.model_priced = model_priced
        self.needs_rate_history = needs_rate_history
        self._mus = list(mus)
        self._seed = seed
        self._override = override or {}
        self._fail_on = fail_on
        self._n = n
        self.calls = 0

    def param_grid(self, cfg):
        return [{"mu": m, "j": j} for j, m in enumerate(self._mus)]

    def _series(self, params):
        idx = _idx(self._n)
        rng = np.random.default_rng(self._seed * 1000 + params["j"])
        sd = 0.01
        s = pd.Series(rng.normal(params["mu"] * sd / np.sqrt(252), sd, len(idx)), index=idx)
        if params["j"] in self._override:
            lo, hi, val = self._override[params["j"]]
            s.iloc[lo:hi + 1] = val
        return s

    def run(self, data, params, cfg, cost_mult=1.0):
        self.calls += 1
        if self._fail_on is not None and params["j"] == self._fail_on:
            raise RuntimeError("偽の失敗")
        return _sim(self._series(params) - 0.0001 * cost_mult)

    def targets(self, data, params, cfg):
        return []


def _cand(res, cid):
    return next(c for c in res["candidates"] if c["id"] == cid)


def test_fake_family_satisfies_protocol():
    assert isinstance(FakeFamily("x", [0.0]), Family)


# ── 区間割り ──

def test_wfo_splits_boundaries():
    idx = _idx(1000)
    sp = research.wfo_splits(idx, 6, 300, 10)
    assert len(sp) == 6
    assert sp[0]["oos_start_pos"] >= 310
    assert sp[0]["train_end_pos"] == 299
    for s in sp:
        assert s["oos_start_pos"] - s["train_end_pos"] - 1 == 10      # embargo ぶん空く
        assert s["train_end_pos"] < s["oos_start_pos"]               # 重ならない
        assert s["oos_end_pos"] - s["oos_start_pos"] + 1 >= 40
    for a, b in zip(sp, sp[1:]):
        assert b["oos_start_pos"] == a["oos_end_pos"] + 1             # 隙間なくつながる
    assert sp[-1]["oos_end_pos"] == len(idx) - 1                       # 最後まで覆う


def test_wfo_splits_reduce_and_empty():
    sp = research.wfo_splits(_idx(310 + 100), 6, 300, 10)
    assert len(sp) == 2
    assert research.wfo_splits(_idx(310 + 79), 6, 300, 10) == []


# ── 選択 ──

def test_best_training_setting_is_chosen():
    fam = FakeFamily("f", [-1.0, 0.0, 4.0], seed=1)
    res = research.run(_data(), CFG, families=[fam])
    c = _cand(res, "f")
    assert len(c["folds"]) == 4
    assert all(f["params"]["j"] == 2 for f in c["folds"])
    assert c["current_params"]["j"] == 2
    assert fam.calls == 6                                               # 設定ごとに通常＋コスト2倍の2回だけ
    assert c["metrics"]["fold_positive"] is not None
    assert set(c["metrics"]["p_profit"]) == {"63", "126", "252"}


def test_oos_change_does_not_change_selection():
    base = research.run(_data(), CFG, families=[FakeFamily("f", [0.5, 1.0, 1.5], seed=2)])
    folds = _cand(base, "f")["folds"]
    idx = _idx()
    sp = research.wfo_splits(idx, 4, 300, 10)
    k = 1
    lo, hi = sp[k]["oos_start_pos"], sp[k]["oos_end_pos"]
    loser = next(j for j in range(3) if j != folds[k]["params"]["j"])
    hacked = research.run(_data(), CFG, families=[
        FakeFamily("f", [0.5, 1.0, 1.5], seed=2, override={loser: (lo, hi, 0.05)})])
    hf = _cand(hacked, "f")["folds"]
    assert hf[k]["params"] == folds[k]["params"]
    assert hf[0]["params"] == folds[0]["params"]
    assert hf[k]["oos_return"] == pytest.approx(folds[k]["oos_return"])


def test_negative_training_sharpe_still_trades():
    res = research.run(_data(), CFG, families=[FakeFamily("neg", [-3.0, -2.0], seed=3)])
    c = _cand(res, "neg")
    assert "reject_reason" not in c
    assert len(c["oos_returns"]) > 0
    assert c["metrics"]["ann_return"] < 0


# ── DSR・試行の数・Holm ──

def test_dsr_noise_low_and_strong_high():
    noise = FakeFamily("noise", [0.0] * 8, seed=4)
    strong = FakeFamily("strong", [0.0, 0.0, 0.0, 4.0], seed=5)
    res = research.run(_data(), CFG, families=[noise, strong])
    assert res["n_trials"] == 12
    assert len([x for v in res["trial_sharpes"].values() for x in v]) == 12
    assert _cand(res, "noise")["metrics"]["dsr"] < 0.5
    assert _cand(res, "strong")["metrics"]["dsr"] > 0.95
    assert _cand(res, "noise")["metrics"]["pbo"] is not None


def test_n_trials_counts_rejected_and_external():
    fams = [FakeFamily("a", [0.0, 1.0], seed=6),
            FakeFamily("b", [0.0, 1.0, 2.0], needs_rate_history=True, seed=7)]
    ext = {"paper": {"label": "仮想", "asset_class": "equity",
                     "returns": pd.Series(np.random.default_rng(8).normal(0, 0.01, 500), index=_idx(500))}}
    res = research.run(_data(rates_source="fallback"), CFG, families=fams, external=ext)
    assert res["n_trials"] == 2 + 3 + 1


def test_holm_not_below_raw():
    fams = [FakeFamily("a", [0.0, 0.5], seed=9), FakeFamily("b", [1.0, 2.0], seed=10),
            FakeFamily("c", [0.0], seed=11)]
    res = research.run(_data(), CFG, families=fams)
    for c in res["candidates"]:
        if "reject_reason" in c:
            continue
        assert c["metrics"]["adj_p"] >= c["metrics"]["raw_p"] - 1e-12
    assert _cand(res, "c")["metrics"]["pbo"] is None                    # 設定が1個


# ── 理論価格の割引 ──

def test_model_priced_haircut_only():
    fams = [FakeFamily("opt", [1.0, 2.0], asset_class="options", model_priced=True, seed=12),
            FakeFamily("fx", [1.0, 2.0], seed=12)]
    res = research.run(_data(), CFG, families=fams)
    o, f = _cand(res, "opt"), _cand(res, "fx")
    assert o["metrics"]["gate_sharpe"] == pytest.approx(o["metrics"]["sharpe"] * 0.70)
    assert o["stress"]["sharpe"] == pytest.approx(o["stress"]["sharpe_raw"] * 0.70)
    assert f["metrics"]["gate_sharpe"] == pytest.approx(f["metrics"]["sharpe"])
    assert f["stress"]["sharpe"] == pytest.approx(f["stress"]["sharpe_raw"])
    assert f["stress"]["sharpe"] < f["metrics"]["sharpe"]               # コスト2倍で下がる


# ── 落とす理由 ──

def test_rate_history_missing_rejects():
    fams = [FakeFamily("carry", [0.0, 1.0], needs_rate_history=True, seed=13),
            FakeFamily("idx", [0.0, 1.0], seed=14)]
    res = research.run(_data(rates_source="partial"), CFG, families=fams)
    assert "金利の履歴なし" in _cand(res, "carry")["reject_reason"]
    assert fams[0].calls == 0
    assert "reject_reason" not in _cand(res, "idx")


def test_too_few_days_rejects():
    res = research.run(_data(380), CFG, families=[FakeFamily("short", [0.0, 1.0], n=380)])
    assert "日数不足" in _cand(res, "short")["reject_reason"]


def test_exception_family_rejected_others_remain():
    fams = [FakeFamily("bad", [0.0, 1.0], fail_on=1, seed=15), FakeFamily("ok", [0.0, 1.0], seed=16)]
    res = research.run(_data(), CFG, families=fams)
    bad = _cand(res, "bad")
    assert bad["reject_reason"].startswith("計算に失敗（RuntimeError")
    assert res["errors"] == [{"family_id": "bad", "params": {"mu": 1.0, "j": 1}, "error": "RuntimeError: 偽の失敗"}]
    ok = _cand(res, "ok")
    assert "reject_reason" not in ok and ok["metrics"]["adj_p"] is not None
    assert res["n_trials"] == 4


def test_leading_idle_period_dropped():
    idx = _idx()

    class Late(FakeFamily):
        def run(self, data, params, cfg, cost_mult=1.0):
            r = super().run(data, params, cfg, cost_mult)
            lev = pd.Series(1.0, index=idx)
            lev.iloc[:200] = 0.0
            net = r.net.copy()
            net.iloc[:200] = 0.0
            return SimResult(net=net, gross=net, cost=r.cost, financing=r.financing,
                             turnover=r.turnover, leverage=lev, weights=r.weights)

    res = research.run(_data(), CFG, families=[Late("late", [0.0, 1.0], seed=17)])
    c = _cand(res, "late")
    assert c["folds"][0]["train_end"] == idx[200 + 299].strftime("%Y-%m-%d")


# ── 外部の実績 ──

def test_external_is_oos_as_is():
    ret = pd.Series(np.random.default_rng(18).normal(0.0005, 0.01, 600), index=_idx(600))
    ext = {"paper_jp": {"label": "仮想トレード", "asset_class": "equity", "returns": ret}}
    res = research.run(_data(), CFG, families=[FakeFamily("a", [0.0, 1.0], seed=19)], external=ext)
    c = _cand(res, "paper_jp")
    assert c["external"] is True and c["folds"] == []
    assert c["metrics"]["fold_positive"] is None
    assert len(c["oos_returns"]) == 600
    assert c["oos_returns"][0] == (ret.index[0].strftime("%Y-%m-%d"), pytest.approx(ret.iloc[0]))
    assert c["metrics"]["days"] == 600
    assert res["trial_sharpes"]["paper_jp"][0] == pytest.approx(c["metrics"]["sharpe"])
    assert res["as_of"] == "2015-09-30"


# ── 手計算・性質で確かめる追加の検査 ──

def test_wfo_splits_hand_computed():
    # n=1000・min_train=300・embargo=10・6区間 → 先頭310・残り690 を6等分（115日ずつ）
    sp = research.wfo_splits(_idx(1000), 6, 300, 10)
    assert [s["oos_start_pos"] for s in sp] == [310, 425, 540, 655, 770, 885]
    assert [s["oos_end_pos"] for s in sp] == [424, 539, 654, 769, 884, 999]
    assert [s["train_end_pos"] for s in sp] == [299, 414, 529, 644, 759, 874]


def test_wfo_splits_uneven_remainder_covers_to_end():
    sp = research.wfo_splits(_idx(1003), 6, 300, 10)     # 残り693は割り切れない
    assert sp[0]["oos_start_pos"] == 310 and sp[-1]["oos_end_pos"] == 1002
    for a, b in zip(sp, sp[1:]):
        assert b["oos_start_pos"] == a["oos_end_pos"] + 1


class _Const(FakeFamily):
    """設定ごとに決まった損益（日ごとに交互に上下）を返す系統。学習のシャープが手計算で決まる。"""

    def _series(self, params):
        idx = _idx(self._n)
        amp = params["mu"]
        base = np.where(np.arange(len(idx)) % 2 == 0, amp + 0.001, -amp + 0.001)
        return pd.Series(base, index=idx)


def test_selection_is_highest_train_sharpe_by_hand():
    # 振れ幅の小さい設定ほど同じ平均で高シャープ → 振れ幅 0.002 の設定（j=1）が毎区間で選ばれる
    fam = _Const("c", [0.01, 0.002, 0.02], seed=0)
    res = research.run(_data(), CFG, families=[fam])
    c = _cand(res, "c")
    assert all(f["params"]["j"] == 1 for f in c["folds"])
    assert all(f["train_sharpe"] > 5 for f in c["folds"])


def test_stress_uses_setting_chosen_at_normal_cost():
    # j=1 は通常コストでは最良だが、コスト2倍の損益だけを悪化させても選択は変わらず、stress に反映される
    class CostHeavy(FakeFamily):
        def run(self, data, params, cfg, cost_mult=1.0):
            self.calls += 1
            pen = 0.01 if params["j"] == 1 else 0.0
            return _sim(self._series(params) - pen * (cost_mult - 1.0))

    res = research.run(_data(), CFG, families=[CostHeavy("h", [0.0, 5.0], seed=21)])
    c = _cand(res, "h")
    assert all(f["params"]["j"] == 1 for f in c["folds"])
    assert c["stress"]["sharpe"] < 0 < c["metrics"]["sharpe"]


def test_never_active_setting_is_not_a_trial_sharpe_but_counts_and_keeps_pbo():
    idx = _idx()

    class Idle(FakeFamily):
        def run(self, data, params, cfg, cost_mult=1.0):
            r = super().run(data, params, cfg, cost_mult)
            if params["j"] == 2:
                z = pd.Series(0.0, index=idx)
                return SimResult(net=z, gross=z, cost=z, financing=z, turnover=z, leverage=z,
                                 weights=pd.DataFrame(index=idx))
            return r

    res = research.run(_data(), CFG, families=[Idle("idle", [0.0, 1.0, 2.0], seed=22)])
    assert res["n_trials"] == 3
    assert len(res["trial_sharpes"]["idle"]) == 2
    assert _cand(res, "idle")["metrics"]["pbo"] is not None


def test_duplicate_ids_raise():
    with pytest.raises(ValueError):
        research.run(_data(), CFG, families=[FakeFamily("a", [0.0], seed=1), FakeFamily("a", [1.0], seed=2)])


# ── DSR の基準と、あらかじめ決めた組み合わせ ──

def test_dsr_benchmark_comes_from_days_and_candidate_count_not_from_spread():
    """基準は「検証の日数」と「候補の本数」だけで決まる。成績の離れた系統を足しても、本数が同じなら基準は変わらない。"""
    import math
    from quant import stats
    a = research.run(_data(), CFG, families=[FakeFamily("x", [0.5, 0.6], seed=1), FakeFamily("y", [0.5, 0.6], seed=2)])
    b = research.run(_data(), CFG, families=[FakeFamily("x", [0.5, 0.6], seed=1), FakeFamily("y", [-9.0, 9.0], seed=2)])
    ma, mb = _cand(a, "x")["metrics"], _cand(b, "x")["metrics"]
    assert ma["dsr_benchmark"] == pytest.approx(mb["dsr_benchmark"])
    n_live = a["n_candidates"]
    assert ma["dsr_benchmark"] == pytest.approx(
        stats.expected_max_sharpe(n_live, 1.0 / (ma["days"] - 1)) * math.sqrt(252))
    # 候補が増えれば基準は上がる
    c = research.run(_data(), CFG, families=[FakeFamily(f"f{i}", [0.5, 0.6], seed=i) for i in range(8)])
    assert _cand(c, "f1")["metrics"]["dsr_benchmark"] > ma["dsr_benchmark"]


def test_ensemble_weights_use_only_past_volatility():
    idx = _idx(400)
    rng = np.random.default_rng(0)
    s = {"a": pd.Series(rng.normal(0, 0.01, 400), index=idx), "b": pd.Series(rng.normal(0, 0.02, 400), index=idx)}
    w1 = research.ensemble_weights(s, 60, 2)
    s2 = {k: v.copy() for k, v in s.items()}
    s2["a"].iloc[300:] *= 50                              # 300日目以降を変えても、300日目までの配分は変わらない
    w2 = research.ensemble_weights(s2, 60, 2)
    pd.testing.assert_frame_equal(w1.iloc[:301], w2.iloc[:301])
    live = w1.dropna()
    assert np.allclose(live.sum(axis=1), 1.0)
    assert (live["a"] > live["b"]).mean() > 0.95          # 変動の小さい方に多く配る
    assert w1.iloc[:30].isna().all().all()                # 変動を測れない序盤は建てない


def test_ensemble_candidate_is_built_from_member_oos_and_listed():
    cfg = SimpleNamespace(**vars(CFG), QUANT_ENSEMBLES={"ens_ab": {"label": "組", "members": ("a*", "b")},
                                                         "ens_none": {"label": "無", "members": ("zzz",)}},
                          QUANT_ENSEMBLE_VOL_LOOKBACK=60)
    fams = [FakeFamily("a1", [1.0, 1.5], seed=1), FakeFamily("b", [1.0, 1.5], seed=2), FakeFamily("c", [0.0], seed=3)]
    res = research.run(_data(), cfg, families=fams)
    e = _cand(res, "ens_ab")
    assert e["ensemble"] is True and e["asset_class"] == "ensemble"
    assert set(e["members"]) == {"a1", "b"} and sum(e["members"].values()) == pytest.approx(1.0)
    # 損益は「前日までの変動の逆数で配った、各戦略の検証区間の損益の加重和」
    oos = {k: pd.Series(dict(_cand(res, k)["oos_returns"])) for k in ("a1", "b")}
    for v in oos.values():
        v.index = pd.to_datetime(v.index)
    w = research.ensemble_weights(oos, 60, 2)
    want = (w * pd.DataFrame(oos)).sum(axis=1)[w.notna().any(axis=1)]
    got = pd.Series(dict(e["oos_returns"]))
    assert np.allclose(got.to_numpy(), want.to_numpy())
    assert e["metrics"]["adj_p"] is not None and e["metrics"]["fold_positive"] is not None and e["metrics"]["pbo"] is None
    assert "reject_reason" in _cand(res, "ens_none")
    assert res["n_candidates"] == 3 + 2                    # 系統 3 本＋定義した組み合わせ 2 本（成立しなかった組も数える）


def test_ensemble_applies_haircut_per_member_for_gate_only():
    cfg = SimpleNamespace(**vars(CFG), QUANT_ENSEMBLES={"e": {"label": "組", "members": ("*",)}},
                          QUANT_ENSEMBLE_VOL_LOOKBACK=60)
    fams = [FakeFamily("opt", [2.0, 2.5], asset_class="options", model_priced=True, seed=5),
            FakeFamily("fx", [2.0, 2.5], seed=6)]
    e = _cand(research.run(_data(), cfg, families=fams), "e")
    assert e["model_priced"] is True
    assert e["metrics"]["gate_sharpe"] < e["metrics"]["sharpe"]
    assert e["stress"]["sharpe"] <= e["stress"]["sharpe_raw"] + 1e-12
