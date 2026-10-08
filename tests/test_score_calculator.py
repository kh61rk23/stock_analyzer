# ==============================================================
# tests/test_score_calculator.py — R5-2 Phase A JP/US共通スコアの受入テスト
# ==============================================================
# 検証点（要件定義書 §R5-2 Phase A / SPEC_core §8-2）:
#   ・JP/US adapter が既存スコアを共通スキーマへ写像すること（再計算しない）
#   ・対象ユニバース = 保有銘柄 ＋ us_screener上位50（重複除去）
#   ・正規化: JP業種スコア -30..100 → 0..100 / 出力は常に0..100
#
# Phase B（U-9）:
#   ・composite = clamp(base + dcf_adj + tech_adj + geo_adj, 0, 100)
#   ・UNIFIED_SCORE_PHASE_B_ENABLED=False で Phase A と完全一致（非常口）
#   ・成分が取れない銘柄は base のみ（＝Phase A と同値・劣化しない）
#   ・各 *_adj は config の上限でクリップされる
# ==============================================================
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from score_calculator import (JpSectorAdapter, UsSectorAdapter,   # noqa: E402
                              build_universe, calc_composite_score,
                              calc_dcf_adj, calc_geo_adj,
                              calc_phase_b_adjustments, calc_tech_adj,
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


# ==============================================================
# Phase B（U-9）
# ==============================================================

class CfgB:
    """Phase B 有効（config BLOCK 56 の既定値と同じ）。"""
    UNIFIED_SCORE_US_TOP_N = 50
    UNIFIED_SCORE_PHASE_B_ENABLED = True
    UNIFIED_SCORE_DCF_MAX_ADJ = 5.0
    UNIFIED_SCORE_TECH_MAX_ADJ = 5.0
    UNIFIED_SCORE_GEO_MAX_ADJ = 3.0


class CfgA(CfgB):
    """非常口: Phase A 相当へ戻す。"""
    UNIFIED_SCORE_PHASE_B_ENABLED = False


def _results_b():
    """
    Phase B の成分が載る固定 results。
    4005.T = jp_screener（3成分とも base に含む → 加算されない）
    5401.T = jp_sector_fallback（tech/dcf のみ加算・geo は base に含む）
    NVDA   = us_screener（3成分とも加算対象）
    """
    r = _results()
    r["technical_scorer"] = {"results": {
        "4005.T": {"score": 80.0, "ticker": "4005.T"},   # jp_screener → 除外
        "5401.T": {"score": 60.0, "ticker": "5401.T"},   # (60-50)/50*5 = +1.0
        "NVDA":   {"score": 30.0, "ticker": "NVDA"},     # (30-50)/50*5 = -2.0
    }}
    r["dcf_valuation"] = {"results": {
        "4005.T": {"dcf_bonus": 8.0},    # jp_screener → 除外
        "5401.T": {"dcf_bonus": 4.0},    # 上限内 → +4.0
        "NVDA":   {"dcf_bonus": -6.0},   # 下限-5.0でクリップ → -5.0
    }}
    r["sectors"]["1006"]["geo_impact_bonus"] = 4.0       # 上限3.0 → +3.0
    return r


class TestPhaseBEscapeHatch:
    """UNIFIED_SCORE_PHASE_B_ENABLED=False → Phase A と完全一致。"""

    def test_disabled_gives_phase_a_score_exactly(self):
        a = calc_composite_score("4005.T", "JP", "1006", _results_b(), CfgA())
        assert a["composite_score"] == 68.5          # base のみ
        assert a["score_source"] == "jp_screener"    # サフィックスなし
        assert not any(k.endswith("_adj") for k in a["components"])

    def test_disabled_us_side_too(self):
        a = calc_composite_score("NVDA", "US", "Tech", _results_b(), CfgA())
        assert a["composite_score"] == 82.0
        assert a["score_source"] == "us_screener"

    def test_disabled_equals_no_cfg_phase_a_dict(self):
        # 「cfg なし＝Phase A 固定」ではないので、旧挙動は flag=False が正本。
        assert calc_phase_b_adjustments("4005.T", "JP", "1006",
                                        _results_b(), CfgA()) == {}

    def test_run_score_calculator_disabled_matches_phase_a(self, tmp_path):
        p = tmp_path / "h.csv"
        p.write_text("ticker,market,sector_code\n4005.T,JP,1006\n"
                     "5401.T,JP,1006\n", encoding="utf-8")

        class _A(CfgA):
            PORTFOLIO_HOLDINGS_PATH = str(p)

        out = run_score_calculator(_A(), _results_b())
        assert out["phase"] == "A"
        assert out["component_coverage"] == {"dcf_adj": 0, "tech_adj": 0,
                                             "geo_adj": 0}
        # Phase A の実測値（既存 test_end_to_end と同じ写像結果）
        assert out["scores"]["4005.T"]["composite_score"] == 68.5
        assert out["scores"]["5401.T"]["composite_score"] == 55.0
        assert out["scores"]["NVDA"]["composite_score"] == 82.0
        assert out["scores"]["KO"]["composite_score"] == 61.0
        for s in out["scores"].values():
            assert "+phaseB" not in s["score_source"]


class TestPhaseBComponents:
    def test_tech_adj_from_technical_scorer(self):
        assert calc_tech_adj("4005.T", "JP", _results_b(), 5.0) == 3.0
        assert calc_tech_adj("NVDA", "US", _results_b(), 5.0) == -2.0

    def test_tech_adj_falls_back_to_candidate_field(self):
        r = _results()
        r["screener"]["candidates"][0]["technical_score"] = 60.0
        assert calc_tech_adj("4005.T", "JP", r, 5.0) == 1.0

    def test_tech_adj_none_when_unavailable(self):
        assert calc_tech_adj("KO", "US", _results_b(), 5.0) is None

    def test_tech_adj_clipped_at_bounds(self):
        r = _results_b()
        r["technical_scorer"]["results"]["4005.T"]["score"] = 999.0
        assert calc_tech_adj("4005.T", "JP", r, 5.0) == 5.0
        r["technical_scorer"]["results"]["4005.T"]["score"] = -999.0
        assert calc_tech_adj("4005.T", "JP", r, 5.0) == -5.0

    def test_dcf_adj_clipped_at_bounds(self):
        # dcf_bonus は -6..+8 なので上限5では必ずクリップされる
        assert calc_dcf_adj("4005.T", "JP", _results_b(), 5.0) == 5.0
        assert calc_dcf_adj("NVDA", "US", _results_b(), 5.0) == -5.0

    def test_dcf_adj_passthrough_inside_bounds(self):
        r = _results_b()
        r["dcf_valuation"]["results"]["4005.T"]["dcf_bonus"] = 4.0
        assert calc_dcf_adj("4005.T", "JP", r, 5.0) == 4.0

    def test_dcf_adj_falls_back_to_candidate_field(self):
        r = _results()
        r["screener"]["candidates"][0]["dcf_bonus"] = 4.0
        assert calc_dcf_adj("4005.T", "JP", r, 5.0) == 4.0

    def test_dcf_adj_none_when_unavailable(self):
        assert calc_dcf_adj("KO", "US", _results_b(), 5.0) is None

    def test_geo_adj_from_sectors_and_clipped(self):
        assert calc_geo_adj("4005.T", "JP", "1006", _results_b(), 3.0) == 3.0
        r = _results_b()
        r["sectors"]["1006"]["geo_impact_bonus"] = -15.0
        assert calc_geo_adj("4005.T", "JP", "1006", r, 3.0) == -3.0

    def test_geo_adj_candidate_takes_priority_over_sector(self):
        r = _results_b()
        r["screener"]["candidates"][0]["geo_impact_bonus"] = -1.5
        assert calc_geo_adj("4005.T", "JP", "1006", r, 3.0) == -1.5

    def test_geo_adj_none_for_us_without_sector_geo(self):
        assert calc_geo_adj("NVDA", "US", "Tech", _results_b(), 3.0) is None

    def test_non_numeric_values_ignored(self):
        r = _results_b()
        r["technical_scorer"]["results"]["4005.T"]["score"] = "80"
        r["dcf_valuation"]["results"]["4005.T"]["dcf_bonus"] = float("nan")
        r["sectors"]["1006"]["geo_impact_bonus"] = True   # bool は数値扱いしない
        assert calc_tech_adj("4005.T", "JP", r, 5.0) is None
        assert calc_dcf_adj("4005.T", "JP", r, 5.0) is None
        assert calc_geo_adj("4005.T", "JP", "1006", r, 3.0) is None


class TestPhaseBApplicability:
    """
    二重計上の排除。base に含まれる成分は加算しない。
    根拠: stock_screener.apply_valuation_to_candidate（L1162-1179）が
    composite に tech/dcf を織り込み、base_agent が業種 composite に geo を
    織り込む。us_stock_screener._calc_score はいずれも含まない。
    """

    def test_jp_screener_is_identical_to_phase_a(self):
        b = calc_composite_score("4005.T", "JP", "1006", _results_b(), CfgB())
        a = calc_composite_score("4005.T", "JP", "1006", _results_b(), CfgA())
        assert b["composite_score"] == a["composite_score"] == 68.5
        assert b["score_source"] == "jp_screener"      # サフィックスなし
        assert not any(k in b["components"] for k in
                       ("dcf_adj", "tech_adj", "geo_adj"))

    def test_jp_screener_records_skip_reasons(self):
        s = calc_composite_score("4005.T", "JP", "1006", _results_b(), CfgB())
        for k in ("dcf_adj_skipped", "tech_adj_skipped", "geo_adj_skipped"):
            assert s["components"][k] == "already_in_base"

    def test_jp_sector_fallback_gets_tech_and_dcf_only(self):
        s = calc_composite_score("5401.T", "JP", "1006", _results_b(), CfgB())
        # base 55.0 + dcf 4.0 + tech 1.0 = 60.0（geo は base に含むので除外）
        assert s["composite_score"] == 60.0
        assert s["components"]["dcf_adj"] == 4.0
        assert s["components"]["tech_adj"] == 1.0
        assert "geo_adj" not in s["components"]
        assert s["components"]["geo_adj_skipped"] == "already_in_base"
        assert s["score_source"] == "jp_sector_fallback+phaseB"

    def test_us_screener_gets_all_three(self):
        r = _results_b()
        r["us_screener"]["top_picks"][0]["geo_impact_bonus"] = 2.0
        s = calc_composite_score("NVDA", "US", "Tech", r, CfgB())
        # base 82.0 - dcf 5.0 - tech 2.0 + geo 2.0 = 77.0
        assert s["composite_score"] == 77.0
        assert s["components"]["dcf_adj"] == -5.0
        assert s["components"]["tech_adj"] == -2.0
        assert s["components"]["geo_adj"] == 2.0
        assert not any(k.endswith("_skipped") for k in s["components"])
        assert s["score_source"] == "us_screener+phaseB"

    def test_unknown_score_source_adds_nothing(self):
        adj = calc_phase_b_adjustments("X", "JP", "1006", _results_b(),
                                       CfgB(), score_source="future_source")
        assert not any(isinstance(v, float) for v in adj.values())
        assert adj == {"dcf_adj_skipped": "unknown_score_source",
                       "tech_adj_skipped": "unknown_score_source",
                       "geo_adj_skipped": "unknown_score_source"}

    def test_default_score_source_is_treated_as_unknown(self):
        # 呼び出し側が score_source を渡し忘れても安全側（何も加算しない）
        adj = calc_phase_b_adjustments("4005.T", "JP", "1006", _results_b(),
                                       CfgB())
        assert not any(isinstance(v, float) for v in adj.values())


class TestPhaseBComposite:
    def test_us_gets_dcf_and_tech_only(self):
        s = calc_composite_score("NVDA", "US", "Tech", _results_b(), CfgB())
        # base 82.0 - dcf 5.0 - tech 2.0 = 75.0（geo は未算出）
        assert s["composite_score"] == 75.0
        assert "geo_adj" not in s["components"]
        assert s["score_source"] == "us_screener+phaseB"

    def test_no_components_equals_phase_a(self):
        # KO はどの成分も無い → Phase A と同値・サフィックスも付かない
        s = calc_composite_score("KO", "US", "Staples", _results_b(), CfgB())
        assert s["composite_score"] == 61.0
        assert s["score_source"] == "us_screener"

    def test_output_top_level_keys_unchanged(self):
        s = calc_composite_score("4005.T", "JP", "1006", _results_b(), CfgB())
        assert set(s) == {"ticker", "market", "sector", "composite_score",
                          "score_source", "components"}

    def test_clamped_to_100_on_extreme_upside(self):
        # us_screener は3成分とも加算対象なので上振れを作れる
        r = _results_b()
        r["us_screener"]["top_picks"][0]["composite_score"] = 100.0
        r["us_screener"]["top_picks"][0]["geo_impact_bonus"] = 15.0
        r["technical_scorer"]["results"]["NVDA"]["score"] = 100.0
        r["dcf_valuation"]["results"]["NVDA"]["dcf_bonus"] = 8.0
        s = calc_composite_score("NVDA", "US", "Tech", r, CfgB())
        assert s["composite_score"] == 100.0

    def test_clamped_to_0_on_extreme_downside(self):
        r = _results_b()
        r["us_screener"]["top_picks"][0]["composite_score"] = 0.0
        r["us_screener"]["top_picks"][0]["geo_impact_bonus"] = -15.0
        r["technical_scorer"]["results"]["NVDA"]["score"] = 0.0
        r["dcf_valuation"]["results"]["NVDA"]["dcf_bonus"] = -6.0
        s = calc_composite_score("NVDA", "US", "Tech", r, CfgB())
        assert s["composite_score"] == 0.0

    def test_always_within_0_100_for_extreme_inputs(self):
        r = _results_b()
        r["us_screener"]["top_picks"][0]["geo_impact_bonus"] = 15.0
        for raw in (-9999.0, -30.0, 0.0, 55.5, 100.0, 9999.0):
            r["us_screener"]["top_picks"][0]["composite_score"] = raw
            r["screener"]["candidates"][0]["composite_score"] = raw
            r["sectors"]["1006"]["composite_score"] = raw
            for tk, mk, sec in (("NVDA", "US", "Tech"),
                                ("4005.T", "JP", "1006"),
                                ("5401.T", "JP", "1006")):
                s = calc_composite_score(tk, mk, sec, r, CfgB())
                assert 0.0 <= s["composite_score"] <= 100.0

    def test_negative_max_adj_config_is_treated_as_magnitude(self):
        class _Bad(CfgB):
            UNIFIED_SCORE_GEO_MAX_ADJ = -3.0
        r = _results_b()
        r["us_screener"]["top_picks"][0]["geo_impact_bonus"] = 9.0
        s = calc_composite_score("NVDA", "US", "Tech", r, _Bad())
        assert s["components"]["geo_adj"] == 3.0

    def test_default_cfg_none_enables_phase_b_with_defaults(self):
        s = calc_composite_score("5401.T", "JP", "1006", _results_b())
        assert s["composite_score"] == 60.0        # 55.0 + dcf 4.0 + tech 1.0


class TestPhaseBRun:
    def test_coverage_counts_and_phase_flag(self, tmp_path):
        p = tmp_path / "h.csv"
        p.write_text("ticker,market,sector_code\n4005.T,JP,1006\n"
                     "5401.T,JP,1006\n", encoding="utf-8")

        class _B(CfgB):
            PORTFOLIO_HOLDINGS_PATH = str(p)

        out = run_score_calculator(_B(), _results_b())
        assert out["phase"] == "B"
        assert out["covered"] == 4
        # 4005.T: なし（3成分とも base 済） / 5401.T: dcf+tech /
        # NVDA: dcf+tech（geoデータなし） / KO: データなし
        assert out["component_coverage"] == {"dcf_adj": 2, "tech_adj": 2,
                                             "geo_adj": 0}
        assert out["scores"]["4005.T"]["composite_score"] == 68.5   # Phase A
        assert out["scores"]["5401.T"]["composite_score"] == 60.0
        assert out["scores"]["NVDA"]["composite_score"] == 75.0
        assert out["scores"]["KO"]["composite_score"] == 61.0

    def test_jp_screener_tickers_never_move(self, tmp_path):
        """jp_screener 由来の銘柄は Phase B 有効でも Phase A と一致する。"""
        p = tmp_path / "h.csv"
        p.write_text("ticker,market,sector_code\n4005.T,JP,1006\n",
                     encoding="utf-8")

        class _B(CfgB):
            PORTFOLIO_HOLDINGS_PATH = str(p)

        class _A(CfgA):
            PORTFOLIO_HOLDINGS_PATH = str(p)

        b = run_score_calculator(_B(), _results_b())["scores"]
        a = run_score_calculator(_A(), _results_b())["scores"]
        moved = [t for t in a
                 if a[t]["composite_score"] != b[t]["composite_score"]]
        assert "4005.T" not in moved
        assert b["4005.T"]["score_source"] == "jp_screener"

    def test_phase_b_never_degrades_when_no_components(self, tmp_path):
        """成分が全滅しても Phase A から劣化しない（R9-2 の保証）。"""
        p = tmp_path / "h.csv"
        p.write_text("ticker,market,sector_code\n4005.T,JP,1006\n"
                     "5401.T,JP,1006\n", encoding="utf-8")

        class _B(CfgB):
            PORTFOLIO_HOLDINGS_PATH = str(p)

        class _A(CfgA):
            PORTFOLIO_HOLDINGS_PATH = str(p)

        plain = _results()          # technical/dcf/geo なし
        b = run_score_calculator(_B(), plain)["scores"]
        a = run_score_calculator(_A(), plain)["scores"]
        assert ({t: s["composite_score"] for t, s in b.items()}
                == {t: s["composite_score"] for t, s in a.items()})
        for s in b.values():
            assert "+phaseB" not in s["score_source"]
