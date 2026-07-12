# ==============================================================
# tests/test_allocation_meta.py — R4-4 マルチ戦略資金配分メタの受入テスト
# ==============================================================
# 受入条件（要件定義書 §R4-4）:
#   ・合成リターン系列でのケリー計算 pytest
#   ・制約充足（カジノ枠 ≤ min(15%,200万)）の検証テスト
# 確定式（変更禁止）:
#   f_s = μ/σ² → f_s' = 0.25×f_s（負は0）／ w_s = f_s'/σ を正規化
#   淘汰: PF<1.0 かつ bootstrap p>0.5 → 配分0
# ==============================================================
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from allocation_meta import (apply_casino_constraint, build_allocation,   # noqa: E402
                             build_report, cull_check, kelly_fraction,
                             risk_parity_weights)


class TestKellyFraction:
    def test_positive_mu_gives_positive_fraction(self):
        # μ>0・分散あり → f' = 0.25 × μ/σ² > 0
        rets = [0.02, 0.03, 0.01, 0.04, 0.00]
        out = kelly_fraction(rets, kelly_frac=0.25)
        mu = sum(rets) / len(rets)
        var = sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)
        assert out["mu"] == pytest.approx(mu, abs=1e-5)
        assert out["var"] == pytest.approx(var, abs=1e-7)
        assert out["kelly_frac"] == pytest.approx(max(0.0, 0.25 * mu / var), abs=1e-3)

    def test_negative_mu_clamped_to_zero(self):
        # μ<0 → 分数ケリーは0にクランプ（負のベットはしない）
        rets = [-0.03, -0.01, -0.02, -0.04]
        out = kelly_fraction(rets, kelly_frac=0.25)
        assert out["kelly_frac"] == 0.0

    def test_zero_variance_returns_zero(self):
        # 分散0（定数系列）→ ゼロ除算せず0
        out = kelly_fraction([0.01, 0.01, 0.01], kelly_frac=0.25)
        assert out["var"] == 0.0
        assert out["kelly_frac"] == 0.0

    def test_kelly_fraction_scales_linearly(self):
        rets = [0.02, 0.03, 0.01, 0.04]
        half = kelly_fraction(rets, kelly_frac=0.25)["kelly_frac"]
        full = kelly_fraction(rets, kelly_frac=0.50)["kelly_frac"]
        assert full == pytest.approx(2 * half, abs=1e-3)


class TestRiskParityWeights:
    def test_weights_sum_to_one(self):
        stats = {
            "s1": {"kelly_frac": 0.4, "sigma": 0.02},
            "s2": {"kelly_frac": 0.2, "sigma": 0.05},
        }
        w = risk_parity_weights(stats)
        assert sum(w.values()) == pytest.approx(1.0, abs=1e-3)

    def test_lower_sigma_gets_higher_weight(self):
        # 同じf'なら σ が小さい戦略ほど w が大きい（リスクパリティ）
        stats = {
            "low_vol": {"kelly_frac": 0.3, "sigma": 0.01},
            "high_vol": {"kelly_frac": 0.3, "sigma": 0.03},
        }
        w = risk_parity_weights(stats)
        assert w["low_vol"] > w["high_vol"]

    def test_zero_fraction_gets_zero_weight(self):
        stats = {
            "on": {"kelly_frac": 0.3, "sigma": 0.02},
            "off": {"kelly_frac": 0.0, "sigma": 0.02},
        }
        w = risk_parity_weights(stats)
        assert w["off"] == 0.0
        assert w["on"] == pytest.approx(1.0, abs=1e-3)

    def test_all_zero_returns_all_zero(self):
        stats = {"a": {"kelly_frac": 0.0, "sigma": 0.0}}
        assert risk_parity_weights(stats) == {"a": 0.0}


class TestCasinoConstraint:
    def test_allocation_never_exceeds_budget(self):
        weights = {"s1": 0.6, "s2": 0.4}
        budget = 2_000_000
        alloc = apply_casino_constraint(weights, total_assets=100_000_000,
                                        casino_budget=budget)
        assert sum(alloc.values()) <= budget + 1  # 丸め誤差許容

    def test_proportional_scaling_preserves_ratio(self):
        weights = {"s1": 0.75, "s2": 0.25}
        alloc = apply_casino_constraint(weights, 100_000_000, 1_000_000)
        assert alloc["s1"] == pytest.approx(3 * alloc["s2"], abs=1.0)


class TestCullCheck:
    def test_culled_when_pf_low_and_p_high(self):
        assert cull_check(pf=0.8, bootstrap_p=0.7) is True

    def test_not_culled_when_pf_ok(self):
        assert cull_check(pf=1.5, bootstrap_p=0.7) is False

    def test_not_culled_when_p_low(self):
        assert cull_check(pf=0.8, bootstrap_p=0.2) is False

    def test_none_inputs_never_cull(self):
        assert cull_check(None, 0.9) is False
        assert cull_check(0.5, None) is False


class TestBuildAllocation:
    def test_end_to_end_constraint_satisfied(self):
        strategy_returns = {
            "event": [0.03, 0.02, 0.04, 0.01, 0.02],
            "commodity": [0.01, 0.02, 0.00, 0.03, 0.01],
            "loser": [-0.05, -0.03, -0.04, -0.02],   # 淘汰対象
        }
        strategy_pf = {"event": 1.6, "commodity": 1.2, "loser": 0.7}
        strategy_p = {"event": 0.05, "commodity": 0.2, "loser": 0.8}
        budget = 2_000_000
        plan = build_allocation(strategy_returns, strategy_pf, strategy_p,
                                total_assets=100_000_000, casino_budget=budget)
        # 制約: カジノ枠を超えない
        assert sum(plan["allocations"].values()) <= budget + 1
        # loser は淘汰される
        assert "loser" in plan["culled"]
        assert "loser" not in plan["allocations"] or plan["allocations"].get("loser", 0) == 0

    def test_insufficient_history_excluded(self):
        # 最低3ヶ月未満はデータ不足で保留（min_months=3）
        plan = build_allocation(
            {"newbie": [0.02, 0.03]},   # 2ヶ月のみ
            {"newbie": 1.5}, {"newbie": 0.1},
            total_assets=100_000_000, casino_budget=2_000_000)
        assert "newbie" in plan["insufficient"]
        assert plan["min_months"] == 3

    def test_report_renders_without_placeholder_bug(self):
        # build_report が {getattr} 等の未解決プレースホルダを残さないこと（回帰）
        plan = build_allocation(
            {"newbie": [0.02, 0.03]},
            {"newbie": 1.5}, {"newbie": 0.1},
            total_assets=100_000_000, casino_budget=2_000_000)
        md = build_report(plan, "202607")
        assert "getattr" not in md
        assert "最低3ヶ月未満" in md
