# ==============================================================
# tests/test_tax_optimizer.py — R3-5 税・NISA最適化の受入テスト
# ==============================================================
# 受入条件（要件定義書 §R3-5）:
#   損益通算計算のpytest（複数実現益・複数含み損の組合せ）。
# ==============================================================
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from tax_optimizer import (build_report, calc_tax_harvest,             # noqa: E402
                           nisa_recommendation, nisa_usage,
                           old_nisa_countdown)

RATE = 0.20315


class TestTaxHarvest:
    def test_multiple_losses_offset_gain(self):
        """複数含み損 × 実現益: min(Σ損, 益)×税率。"""
        positions = [
            {"ticker": "A.T", "unrealized_pnl": -100_000, "account_type": "特定"},
            {"ticker": "B.T", "unrealized_pnl": -50_000, "account_type": "特定"},
            {"ticker": "C.T", "unrealized_pnl": 30_000, "account_type": "特定"},
        ]
        r = calc_tax_harvest(positions, realized_gain_ytd=120_000, rate=RATE)
        assert r["total_loss"] == 150_000
        assert r["harvestable"] == 120_000            # min(150k, 120k)
        assert r["tax_saving"] == pytest.approx(120_000 * RATE)
        # 含み損の大きい順
        assert [c["ticker"] for c in r["candidates"]] == ["A.T", "B.T"]

    def test_gain_exceeds_loss(self):
        positions = [{"ticker": "A.T", "unrealized_pnl": -40_000,
                      "account_type": "特定"}]
        r = calc_tax_harvest(positions, 200_000, RATE)
        assert r["harvestable"] == 40_000             # 損失側が上限
        assert r["tax_saving"] == pytest.approx(40_000 * RATE)

    def test_no_realized_gain_no_saving(self):
        positions = [{"ticker": "A.T", "unrealized_pnl": -40_000,
                      "account_type": "特定"}]
        r = calc_tax_harvest(positions, 0, RATE)
        assert r["harvestable"] == 0 and r["tax_saving"] == 0

    def test_nisa_losses_excluded(self):
        """NISA口座の含み損は損益通算対象外（非課税口座）。"""
        positions = [
            {"ticker": "A.T", "unrealized_pnl": -100_000, "account_type": "NISA"},
            {"ticker": "B.T", "unrealized_pnl": -30_000, "account_type": "特定"},
        ]
        r = calc_tax_harvest(positions, 100_000, RATE)
        assert r["total_loss"] == 30_000              # NISA分は除外
        assert [c["ticker"] for c in r["candidates"]] == ["B.T"]

    def test_no_losers(self):
        positions = [{"ticker": "A.T", "unrealized_pnl": 50_000,
                      "account_type": "特定"}]
        r = calc_tax_harvest(positions, 50_000, RATE)
        assert r["candidates"] == [] and r["tax_saving"] == 0


class TestNisaRecommendation:
    def test_recommends_nisa_for_longterm(self):
        assert "NISA推奨" in nisa_recommendation(
            order_hold_months=24, nisa_remaining=1_000_000, order_value=500_000)

    def test_tokutei_when_short_hold(self):
        assert "特定口座" in nisa_recommendation(6, 1_000_000, 500_000)

    def test_tokutei_when_no_room(self):
        assert "不足" in nisa_recommendation(24, 100_000, 500_000)


class TestOldNisaCountdown:
    def test_countdown(self):
        positions = [
            {"ticker": "A.T", "account_type": "旧NISA"},
            {"ticker": "B.T", "account_type": "特定"},
        ]
        out = old_nisa_countdown(positions, current_year=2026, expiry_year=2040)
        assert len(out) == 1 and out[0]["ticker"] == "A.T"
        assert out[0]["years_left"] == 14


class TestNisaUsage:
    def test_usage(self):
        positions = [
            {"ticker": "A.T", "account_type": "NISA", "cost_basis": 3_000_000},
            {"ticker": "B.T", "account_type": "特定", "cost_basis": 1_000_000},
        ]
        u = nisa_usage(positions, annual_limit=2_400_000, total_limit=12_000_000)
        assert u["used"] == 3_000_000                 # NISA分のみ
        assert u["total_remaining"] == 9_000_000


class TestReport:
    def test_build_report_complete(self):
        harvest = calc_tax_harvest(
            [{"ticker": "A.T", "unrealized_pnl": -100_000, "account_type": "特定"}],
            120_000, RATE)
        md = build_report(harvest, [], nisa_usage([], 2_400_000, 12_000_000), 2026)
        assert "税・NISA最適化レポート 2026年" in md
        assert "税務の最終判断は利用者" in md            # 免責明記
        assert "損出し候補" in md and "wash sale" in md
