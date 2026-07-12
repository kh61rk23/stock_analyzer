# ==============================================================
# tests/test_score_calculator.py — R5-2 Phase A JP/US共通スコアの受入テスト
# ==============================================================
# 検証点（要件定義書 §R5-2 Phase A / SPEC_core §8-2）:
#   ・JP/US adapter が既存スコアを共通スキーマへ写像すること（再計算しない）
#   ・対象ユニバース = 保有銘柄 ＋ us_screener上位50（重複除去）
#   ・正規化: JP業種スコア -30..100 → 0..100 / 出力は常に0..100
# ==============================================================
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from score_calculator import (JpSectorAdapter, UsSectorAdapter,   # noqa: E402
                              build_universe, calc_composite_score,
                              normalize_score, run_score_calculator)


def _results():
    return {
        "sectors": {"1006": {"composite_score": 55.0, "risk_level": "low"}},
        "screener": {"candidates": [
            {"ticker": "4005.T", "composite_score": 68.5,
             "sector_score": 55.0, "value_score": 70.0,
             "quality_score": 80.0},
        ]},
        "us_screener": {"top_picks": [
            {"ticker": "NVDA", "composite_score": 82.0, "sector": "Tech",
             "value_score": 60.0, "quality_score": 95.0},
            {"ticker": "KO", "composite_score": 61.0, "sector": "Staples"},
        ]},
    }


class TestAdapters:
    def test_jp_screener_candidate_preferred(self):
        s = JpSectorAdapter().extract("4005.T", "1006", _results())
        assert s["composite_score"] == 68.5
        assert s["score_source"] == "jp_screener"

    def test_jp_sector_fallback(self):
        # screener候補にない保有銘柄 → 業種compositeへフォールバック
        s = JpSectorAdapter().extract("5401.T", "1006", _results())
        assert s["composite_score"] == 55.0
        assert s["score_source"] == "jp_sector_fallback"

    def test_jp_unknown_returns_none(self):
        assert JpSectorAdapter().extract("9999.T", "9999", _results()) is None

    def test_us_adapter_maps_candidate(self):
        s = UsSectorAdapter().extract("NVDA", "Tech", _results())
        assert s["composite_score"] == 82.0
        assert s["score_source"] == "us_screener"

    def test_us_unknown_returns_none(self):
        assert UsSectorAdapter().extract("ZZZZ", "", _results()) is None


class TestNormalize:
    def test_jp_negative_mapped_into_0_100(self):
        # JP業種スコアは -30..100 → 負値も0..100へ収まる
        assert normalize_score(-30.0, "JP") == 0.0
        assert 0.0 < normalize_score(-15.0, "JP") < 23.1

    def test_positive_passthrough_with_clamp(self):
        assert normalize_score(82.0, "US") == 82.0
        assert normalize_score(140.0, "US") == 100.0
        assert normalize_score(-5.0, "US") == 0.0


class TestCalcCompositeScore:
    def test_common_schema(self):
        s = calc_composite_score("NVDA", "US", "Tech", _results())
        assert set(s) == {"ticker", "market", "sector", "composite_score",
                          "score_source", "components"}
        assert 0.0 <= s["composite_score"] <= 100.0

    def test_unknown_market_returns_none(self):
        assert calc_composite_score("X", "EU", "", _results()) is None


class TestUniverse:
    def test_holdings_plus_us_top_dedup(self):
        holdings = [{"ticker": "4005.T", "market": "JP", "sector_code": "1006"},
                    {"ticker": "NVDA", "market": "US", "sector_code": "Tech"}]
        us_top = [{"ticker": "NVDA", "sector": "Tech"},
                  {"ticker": "KO", "sector": "Staples"}]
        uni = build_universe(holdings, us_top, top_n=50)
        tickers = [t for t, _, _ in uni]
        assert tickers == ["4005.T", "NVDA", "KO"]   # NVDA重複なし・順序保持

    def test_top_n_limits_us_side(self):
        us_top = [{"ticker": f"T{i}"} for i in range(60)]
        uni = build_universe([], us_top, top_n=50)
        assert len(uni) == 50


class TestRunScoreCalculator:
    def test_end_to_end(self, tmp_path):
        p = tmp_path / "holdings.csv"
        p.write_text("ticker,market,sector_code\n4005.T,JP,1006\n5401.T,JP,1006\n",
                     encoding="utf-8")

        class Cfg:
            PORTFOLIO_HOLDINGS_PATH = str(p)
            UNIFIED_SCORE_US_TOP_N = 50

        out = run_score_calculator(Cfg(), _results())
        assert not out["skipped"]
        # 保有2 + us_top 2 = 4銘柄全カバー
        assert out["covered"] == 4
        assert out["scores"]["4005.T"]["score_source"] == "jp_screener"
        assert out["scores"]["5401.T"]["score_source"] == "jp_sector_fallback"
        assert out["scores"]["NVDA"]["market"] == "US"

    def test_empty_universe_skips(self):
        class Cfg:
            PORTFOLIO_HOLDINGS_PATH = "/nonexistent/holdings.csv"

        out = run_score_calculator(Cfg(), {})
        assert out["skipped"] is True
