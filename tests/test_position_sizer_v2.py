# ==============================================================
# tests/test_position_sizer_v2.py — E5サイジングv2の受入テスト（R1-5）
# ==============================================================
# 受入条件（要件定義書 §R1-5）:
#   各ステップの単体テスト＋境界
#   （相関行列が特異・データ欠損時はステップ3,4をスキップし警告）
# ==============================================================
import random
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from position_sizer import _corr60, _portfolio_vol_annual, calc_position_size_v2  # noqa: E402


def _returns(n=60, mu=0.0, sigma=0.01, seed=1):
    rng = random.Random(seed)
    return [rng.gauss(mu, sigma) for _ in range(n)]


BASE = dict(total_assets=10_000_000, price=1000.0, atr14=50.0, lot_size=100)


class TestStep1Base:
    def test_base_size(self):
        # risk = 1000万×1% = 10万 / (2×50) = 1000株 → conviction3(×1.0)
        r = calc_position_size_v2(**BASE)
        s1 = r["steps"][0]
        assert s1["qty"] == pytest.approx(1000.0)

    def test_invalid_inputs(self):
        assert "error" in calc_position_size_v2(0, 1000, 50)
        assert "error" in calc_position_size_v2(1e7, -1, 50)
        assert "error" in calc_position_size_v2(1e7, 1000, 0)


class TestStep2Conviction:
    @pytest.mark.parametrize("level,mult", [(1, 0.5), (2, 0.75), (3, 1.0),
                                            (4, 1.25), (5, 1.5)])
    def test_conviction_multipliers(self, level, mult):
        r = calc_position_size_v2(**BASE, conviction=level)
        assert r["steps"][1]["qty"] == pytest.approx(1000.0 * mult)

    def test_no_deliberation_defaults_to_3(self):
        r = calc_position_size_v2(**BASE, conviction=None)
        assert "conviction=3" in r["steps"][1]["label"]
        assert r["steps"][1]["qty"] == pytest.approx(1000.0)


class TestStep3Correlation:
    def test_high_corr_penalty(self):
        base = _returns(seed=10)
        # ほぼ同一系列 → ρ̄ ≈ 1 > 0.6 → ×0.7
        r = calc_position_size_v2(
            **BASE, candidate_returns=base,
            portfolio_returns={"A.T": base[:], "B.T": base[:]},
        )
        s3 = next(s for s in r["steps"] if s["step"] == 3)
        assert "ペナルティ" in s3["label"] and "×0.7" in s3["label"]
        assert s3["qty"] == pytest.approx(700.0)

    def test_low_corr_no_penalty(self):
        r = calc_position_size_v2(
            **BASE, candidate_returns=_returns(seed=1),
            portfolio_returns={"A.T": _returns(seed=2)},
        )
        s3 = next(s for s in r["steps"] if s["step"] == 3)
        assert "なし" in s3["label"]
        assert s3["qty"] == pytest.approx(1000.0)

    def test_missing_data_skips_with_warning(self):
        r = calc_position_size_v2(**BASE)     # リターン未指定
        assert any("step3スキップ" in w for w in r["warnings"])

    def test_singular_zero_variance_skips(self):
        # 全ホールディングが分散ゼロ（特異）→ 相関Noneでスキップ
        r = calc_position_size_v2(
            **BASE, candidate_returns=_returns(),
            portfolio_returns={"A.T": [0.0] * 60},
        )
        assert any("step3スキップ" in w for w in r["warnings"])


class TestStep4VolTarget:
    def test_high_vol_scales_down(self):
        # 高ボラ（日次3%≒年率48%）の候補+保有 → σ_p > 15% → 縮小
        vol_rets = _returns(sigma=0.03, seed=3)
        r = calc_position_size_v2(
            **BASE, candidate_returns=vol_rets,
            portfolio_returns={"A.T": _returns(sigma=0.03, seed=4)},
            portfolio_weights={"A.T": 0.5},
        )
        s4 = next(s for s in r["steps"] if s["step"] == 4)
        assert "ボラ制約" in s4["label"] and "×" in s4["label"]
        s3 = next(s for s in r["steps"] if s["step"] == 3)
        assert s4["qty"] < s3["qty"]

    def test_low_vol_passes(self):
        r = calc_position_size_v2(
            **BASE, candidate_returns=_returns(sigma=0.001, seed=5),
            portfolio_returns={"A.T": _returns(sigma=0.001, seed=6)},
            portfolio_weights={"A.T": 0.1},
        )
        s4 = next(s for s in r["steps"] if s["step"] == 4)
        assert "OK" in s4["label"]

    def test_missing_weights_skips_with_warning(self):
        r = calc_position_size_v2(
            **BASE, candidate_returns=_returns(),
            portfolio_returns={"A.T": _returns(seed=2)},
        )                                     # weights欠損
        assert any("step4スキップ" in w for w in r["warnings"])


class TestStep5Caps:
    def test_max_single_position_10pct(self):
        # ATRが小さい→巨大サイズ→総資産10%にクランプ
        r = calc_position_size_v2(total_assets=10_000_000, price=1000.0,
                                  atr14=1.0, lot_size=100)
        assert r["position_value"] <= 10_000_000 * 0.10 + 1e-6
        s5 = next(s for s in r["steps"] if s["step"] == 5)
        assert "クランプ" in s5["label"]

    def test_casino_cap_25pct_of_budget(self):
        # カジノ枠 = min(1000万×15%, 200万) = 150万 → 上限 = ×25% = 37.5万
        r = calc_position_size_v2(total_assets=10_000_000, price=1000.0,
                                  atr14=1.0, is_casino=True, lot_size=100)
        assert r["position_value"] <= 1_500_000 * 0.25 + 1e-6

    def test_lot_rounding(self):
        r = calc_position_size_v2(**BASE)
        assert r["qty"] % 100 == 0


class TestHelpers:
    def test_corr60_needs_20_obs(self):
        assert _corr60([0.01] * 10, [0.01] * 10) is None

    def test_portfolio_vol_positive(self):
        rets = [_returns(sigma=0.02, seed=7), _returns(sigma=0.02, seed=8)]
        v = _portfolio_vol_annual([0.5, 0.5], rets)
        assert v is not None and v > 0

    def test_portfolio_vol_mismatch(self):
        assert _portfolio_vol_annual([0.5], []) is None


class TestBreakdownOutput:
    def test_steps_are_traceable(self):
        """調整内訳が5ステップ全て記録される（注文票reason転記用）。"""
        r = calc_position_size_v2(**BASE, conviction=4)
        assert [s["step"] for s in r["steps"]] == [1, 2, 3, 4, 5]
        assert all("label" in s and "qty" in s for s in r["steps"])
