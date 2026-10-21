# ==============================================================
# tests/test_commodity_rotation.py — R4-2 コモディティローテーションの受入テスト
# ==============================================================
# 受入条件（要件定義書 §R4-2）:
#   （S1で過去3年月次リバランスバックテストは backtest.cli で利用者が実行）
#   ここでは純粋ロジック（12-1モメンタム・象限別配分・絶対モメンタム）を検証。
# ==============================================================
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from commodity_rotation import (build_order_tickets, calc_12_1_momentum,   # noqa: E402
                                select_allocations)

UNIVERSE = {"1326.T": "gold", "1671.T": "energy", "1687.T": "grains",
            "1684.T": "broad"}
REGIME_ALLOW = {"A": ["energy", "grains", "broad"], "C": ["gold", "broad"],
                "B": [], "D": []}


class TestMomentum:
    def test_12_1_momentum(self):
        # 252日前100→今120 (long +20%), 21日前115→今120 (short +4.35%)
        closes = [100.0] * 1 + [None] * 0
        closes = [0.0] * 300
        closes[-1 - 252] = 100.0
        closes[-1 - 21] = 115.0
        closes[-1] = 120.0
        m = calc_12_1_momentum(closes)
        assert m == pytest.approx((120/100 - 1) - (120/115 - 1), abs=1e-3)

    def test_insufficient_data(self):
        assert calc_12_1_momentum([100.0] * 100) is None

    def test_positive_when_long_beats_short(self):
        closes = [0.0] * 300
        closes[-1 - 252] = 80.0    # 大きく上昇（long +50%）
        closes[-1 - 21] = 118.0    # 直近はほぼ横ばい
        closes[-1] = 120.0
        assert calc_12_1_momentum(closes) > 0


class TestSelectAllocations:
    def test_regime_A_allows_energy_grains(self):
        mom = {"1671.T": 0.10, "1687.T": 0.05, "1326.T": 0.20, "1684.T": -0.02}
        r = select_allocations(mom, "A", UNIVERSE, REGIME_ALLOW, 10.0)
        # A象限=energy/grains/broad許可。gold(1326)は許可外。broad(1684)はmom<0で除外
        assert set(r["allocations"].keys()) == {"1671.T", "1687.T"}
        assert r["total_pct"] == pytest.approx(10.0)
        assert r["allocations"]["1671.T"] == pytest.approx(5.0)

    def test_regime_C_allows_gold(self):
        mom = {"1326.T": 0.15, "1671.T": 0.10, "1684.T": 0.05}
        r = select_allocations(mom, "C", UNIVERSE, REGIME_ALLOW, 10.0)
        # C象限=gold/broad。energy(1671)は許可外
        assert set(r["allocations"].keys()) == {"1326.T", "1684.T"}

    def test_regime_B_is_cash(self):
        mom = {"1326.T": 0.15, "1671.T": 0.10}
        r = select_allocations(mom, "B", UNIVERSE, REGIME_ALLOW, 10.0)
        assert r["allocations"] == {} and r["cash_pct"] == 100.0

    def test_absolute_momentum_filter(self):
        """許可カテゴリでも momentum<=0 は除外（絶対モメンタム）。"""
        mom = {"1671.T": -0.05, "1687.T": -0.10, "1684.T": -0.01}
        r = select_allocations(mom, "A", UNIVERSE, REGIME_ALLOW, 10.0)
        assert r["allocations"] == {} and r["cash_pct"] == 100.0

    def test_total_never_exceeds_max(self):
        mom = {t: 0.10 for t in UNIVERSE}
        r = select_allocations(mom, "A", UNIVERSE, REGIME_ALLOW, 10.0)
        assert r["total_pct"] <= 10.0 + 1e-6


class TestOrderTickets:
    def test_build_tickets(self):
        tickets = build_order_tickets(
            {"1326.T": 5.0}, total_assets=10_000_000,
            prices={"1326.T": 5000}, today="2026-07-10")
        assert len(tickets) == 1
        t = tickets[0]
        # 目標 50万 / 5000円 = 100口
        assert t["qty"] == 100
        assert t["strategy"] == "commodity" and t["side"] == "BUY"
        assert t["status"] == "pending"        # 発注は利用者手動

    def test_zero_price_skipped(self):
        tickets = build_order_tickets({"X.T": 5.0}, 10_000_000,
                                      {"X.T": 0}, "2026-07-10")
        assert tickets == []
