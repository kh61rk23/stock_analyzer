# ==============================================================
# tests/test_wave7_wiring.py
# WAVE7 系7モジュールの稼働と、WAVE6/WAVE7 定数の config 正本化を固定する
# ==============================================================
# 防ぐこと:
#   - ENABLE_* が False に戻り、7モジュールが動かなくなる
#   - size_decay_manager / cross_size_signal は _in_mode("size_tier") で
#     ゲートされるため、"size_tier" が DAILY/WEEKLY に無いと
#     ENABLE が True でも daily/weekly で実行されない
#   - WAVE7_WEEKLY_MODULE_ORDER の宣言順が実コードの実行順とずれる
# ==============================================================
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

WAVE7_FLAGS = [
    "ENABLE_STANDARDIZATION_TRACKER",
    "ENABLE_ACADEMIC_CONF_TRACKER",
    "ENABLE_MIDTERM_PLAN_TRACKER",
    "ENABLE_SIZE_DECAY_MANAGER",
    "ENABLE_CROSS_SIZE_SIGNAL",
    "ENABLE_INDEPENDENCE_SCORE",
    "ENABLE_LATENT_PEARL_MANAGER",
]


def _cfg():
    import importlib.util
    spec = importlib.util.spec_from_file_location("config", str(REPO / "config.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestWave7IsEnabled:
    def test_all_seven_modules_enabled(self):
        cfg = _cfg()
        off = [f for f in WAVE7_FLAGS if not getattr(cfg, f, False)]
        assert not off, f"Wave 7 が無効に戻っている: {off}"

    def test_size_tier_mode_key_registered(self):
        """size_decay / cross_size が日次で実行されるための mode-key。

        これが無いと ENABLE を True にしても _in_mode("size_tier") が
        daily/weekly で False になり、all モードでしか動かない。
        """
        cfg = _cfg()
        assert "size_tier" in cfg.DAILY_MODULES, (
            "DAILY_MODULES に 'size_tier' が無い。"
            "size_decay_manager / cross_size_signal が日次で動かなくなる"
        )


class TestWave7ModuleOrderDeclaration:
    """宣言された実行順が実コードと一致すること。"""

    @staticmethod
    def _order_map():
        import json
        reg = json.loads((REPO / "pipeline_registry.json").read_text(encoding="utf-8"))
        order = {}
        for s in reg["stages"]:
            for m in s.get("modules", []):
                order.setdefault(m, s["order"])
        return order

    def test_declared_modules_are_wired(self):
        cfg = _cfg()
        order = self._order_map()
        for name in ("WAVE7_DAILY_MODULE_ORDER", "WAVE7_WEEKLY_MODULE_ORDER"):
            for m in getattr(cfg, name):
                assert m in order, f"{name} の {m} がパイプライン未配線"

    def test_declared_order_matches_code_order(self):
        cfg = _cfg()
        order = self._order_map()
        for name in ("WAVE7_DAILY_MODULE_ORDER", "WAVE7_WEEKLY_MODULE_ORDER"):
            declared = list(getattr(cfg, name))
            assert declared == sorted(declared, key=lambda m: order[m]), (
                f"{name} の宣言順が実コードの実行順と不一致。"
                f"宣言={declared} / 実際={sorted(declared, key=lambda m: order[m])}"
            )

    def test_midterm_runs_before_d3_consumers(self):
        """中計は D3ボーナスが参照するため screener 段で先に確定すること。"""
        order = self._order_map()
        assert order["midterm_plan_tracker"] < order["industry_standard_tracker"]
        assert order["midterm_plan_tracker"] < order["academic_conf_tracker"]


class TestWave7ConstantsAreRead:
    """config の定数が実装から実際に効くこと（値を変えれば結果が変わる）。"""

    def test_independence_tier_thresholds(self):
        import independence_score_calculator as isc
        cfg = _cfg()
        assert isc._judge_independence_tier(70.0, cfg) == "HIGH"

        class Strict:
            INDEPENDENCE_TIER_THRESHOLDS = {"HIGH": 99.0, "MID": 98.0}

        assert isc._judge_independence_tier(70.0, Strict) == "LOW"

    def test_independence_window_is_read(self):
        src = (REPO / "independence_score_calculator.py").read_text(encoding="utf-8")
        assert "INDEPENDENCE_ROLLING_WINDOW_DAYS" in src

    def test_latent_pearl_weights(self):
        import latent_pearl_manager as lpm
        cfg = _cfg()
        base = lpm._calc_latent_pearl_score(70, 6, 4, 80, ["A", "B"], cfg)

        class W:
            LATENT_PEARL_SCORE_WEIGHTS = {
                "independence": 1.0, "cross_signal": 0.0,
                "catalyst": 0.0, "midterm": 0.0,
            }

        assert lpm._calc_latent_pearl_score(70, 6, 4, 80, ["A", "B"], W) != base

    def test_d3_bonus_caps(self):
        import pearl_score_calculator as psc
        cfg = _cfg()
        data = {"catalyst_score": 10.0}
        assert psc._calc_catalyst_bonus(data, cfg) == cfg.D3_CATALYST_BONUS_MAX

        class Low:
            D3_CATALYST_BONUS_MAX = 2.0

        assert psc._calc_catalyst_bonus(data, Low) == 2.0

    def test_config_values_match_module_defaults(self):
        """config と実装既定値が一致していること（ズレると挙動が変わる）。"""
        import independence_score_calculator as isc
        import latent_pearl_manager as lpm
        import pearl_score_calculator as psc
        cfg = _cfg()
        assert cfg.INDEPENDENCE_ROLLING_WINDOW_DAYS == isc._ROLLING_WINDOW_DAYS
        assert cfg.LATENT_PEARL_SCORE_WEIGHTS == lpm._LATENT_SCORE_WEIGHTS
        assert cfg.D3_EDINET_SEGMENT_BONUS_MAX == psc._D3_EDINET_SEGMENT_BONUS_MAX
        assert cfg.D3_MIDTERM_BONUS_MAX == psc._D3_MIDTERM_BONUS_MAX
        assert cfg.D3_CATALYST_BONUS_MAX == psc._D3_CATALYST_BONUS_MAX


class TestWave6Constants:
    def test_geo_cache_ttl_is_read(self):
        src = (REPO / "geo_signal.py").read_text(encoding="utf-8")
        assert "GEO_RISK_CACHE_TTL_SEC" in src

    def test_sec_target_sections_narrowing(self):
        """対象セクションで絞り込めること。該当なしなら全文に戻ること。"""
        import sec_geographic_extractor as sge
        cfg = _cfg()
        text = ("HEADER " * 500) + "Geographic Segments " + ("BODY " * 2000)
        narrowed = sge._narrow_to_target_sections(text, cfg)
        assert len(narrowed) < len(text), "絞り込みが効いていない"

        # 見出しが無い文書は全文のまま（後退しない）
        plain = "NO MATCHING HEADINGS " * 500
        assert sge._narrow_to_target_sections(plain, cfg) == plain

    def test_narrowing_falls_back_when_too_short(self):
        """絞り込み結果が短すぎる場合は全文へ戻すこと。"""
        import sec_geographic_extractor as sge
        cfg = _cfg()
        text = "Risk Factors"          # 見出しはあるが本文が無い
        assert sge._narrow_to_target_sections(text, cfg) == text

    def test_exposure_ui_port_has_a_consumer(self):
        """起動導線が定数を読むこと（Streamlit はポートを実行時に渡すため）。"""
        launcher = REPO / "setup" / "権益レビューUI起動.command"
        assert launcher.exists(), "権益レビューUIの起動スクリプトが無い"
        assert "EXPOSURE_REVIEW_UI_PORT" in launcher.read_text(encoding="utf-8")


class TestRetiredConstant:
    def test_size_decay_ttl_removed(self):
        """TTLは指数減衰（decay_factor）に置き換わっているため削除済み。"""
        assert not hasattr(_cfg(), "SIZE_DECAY_TTL_DAYS"), (
            "SIZE_DECAY_TTL_DAYS が復活している。"
            "古さの扱いは calc_decay_factor() の e^(-λt) が担う"
        )
