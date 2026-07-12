# ==============================================================
# tests/test_us_macro.py — US-2 米国マクロ拡張パイプラインの受入テスト
# ==============================================================
# ネットワークゼロ（FRED未取得＝空データでも壊れない）＋純粋ロジック検証。
# ==============================================================
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from us_macro_pipeline import (build_leading_signals, composite_us_macro_score,  # noqa: E402
                               cpi_yoy, latest, pct_change, run_us_macro_pipeline)


def _series(values):
    return [{"date": f"2026-01-{i+1:02d}", "value": v}
            for i, v in enumerate(values)]


class TestPureCalc:
    def test_cpi_yoy(self):
        vals = [100.0] + [None] * 0
        s = _series([100 + i for i in range(13)])   # 100..112
        assert cpi_yoy(s) == pytest.approx((112/100 - 1) * 100, abs=0.01)

    def test_cpi_yoy_insufficient(self):
        assert cpi_yoy(_series([100, 101])) is None

    def test_pct_change(self):
        assert pct_change(_series([100, 110]), 1) == pytest.approx(10.0)

    def test_latest(self):
        assert latest(_series([1, 2, 3])) == 3
        assert latest([]) is None


class TestLeadingSignals:
    def test_hy_spread_headwind(self):
        sigs = build_leading_signals({"hy_spread": 6.0})
        assert any(s["factor"] == "us_hy_spread" and s["signal"] == "headwind"
                   for s in sigs)

    def test_yield_inversion(self):
        sigs = build_leading_signals({"t10y2y": -0.3})
        assert any(s["factor"] == "us_yield_inversion" for s in sigs)

    def test_jobless_spike(self):
        sigs = build_leading_signals({"icsa_mom_pct": 20.0})
        assert any(s["factor"] == "us_jobless_claims" for s in sigs)

    def test_permits_tailwind(self):
        sigs = build_leading_signals({"permit_mom_pct": 5.0})
        assert any(s["signal"] == "tailwind" for s in sigs)

    def test_calm_no_signals(self):
        sigs = build_leading_signals({"hy_spread": 3.0, "t10y2y": 1.0,
                                      "icsa_mom_pct": 2.0, "permit_mom_pct": 0.5})
        assert sigs == []


class TestCompositeScore:
    def test_good_macro_high_score(self):
        snap = {"unrate": 3.5, "t10y2y": 1.5, "hy_spread": 3.2,
                "permit_mom_pct": 5.0}
        score = composite_us_macro_score(snap, [])
        assert score > 60

    def test_bad_macro_low_score(self):
        snap = {"unrate": 7.5, "t10y2y": -0.5, "hy_spread": 7.0,
                "permit_mom_pct": -8.0}
        sigs = build_leading_signals(snap)
        score = composite_us_macro_score(snap, sigs)
        assert score < 40

    def test_empty_is_neutral(self):
        assert composite_us_macro_score({}, []) == 50.0


class TestRunPipeline:
    def test_no_fred_key_graceful(self, tmp_path):
        """FREDキー無し（ネットワークゼロ）でも契約キーを返す。"""
        class Cfg:
            CACHE_DIR = tmp_path / "cache"
            POLICY_FRED_API_KEY = ""
            US_MACRO_FRED_SERIES = {"unrate": ("UNRATE", "失業率", "%")}
        out = run_us_macro_pipeline(Cfg(), {})
        # main が参照する契約キーが全て存在
        for k in ("macro_snapshot_ext", "leading_signals", "d2_supplement",
                  "composite_us_macro_score", "cached"):
            assert k in out
        assert out["composite_us_macro_score"] == 50.0   # データ無し=中立
