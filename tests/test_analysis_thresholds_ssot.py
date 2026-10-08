# ==============================================================
# tests/test_analysis_thresholds_ssot.py
# 分析系の閾値が config を正本として読まれること
# ==============================================================
# 【防ぐもの】実装側が別名のモジュール定数を持つ（A型）か値を直書き（B型）して、
# config.py の定数が誰にも読まれなくなること。対象:
#   BLOCK A-1  IC_STABLE_MIN_RATE / IC_REVIEW_CONSECUTIVE_NEG
#   BLOCK 9    SHORT_TERM_DAYS
#   BLOCK 12   CREDIT_SQUEEZE_THRESH / CREDIT_OVERHEAT_THRESH
#   BLOCK M-3  ACTIVIST_TOP_N
#   BLOCK GAP-8 ATTRIBUTION_MIN_FIRE / ATTRIBUTION_LOOKBACK_W
#   BLOCK P-4  FOREIGN_INVESTOR_SPIKE_PCT / SHAREHOLDER_SCORE_TOP_N
#   BLOCK M-4  TDNET_MONITOR_CATEGORIES
#   BLOCK SCORE-CAP SCORE_MIN_COMPOSITE / SCORE_MAX_COMPOSITE
#   BLOCK D-1  CYCLE_PHASE_HISTORY_FILE
# config の値は実装側の既定値と一致させてある。
# ==============================================================
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))


def _cfg():
    import importlib.util
    spec = importlib.util.spec_from_file_location("config", str(REPO / "config.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestIcTrackerA1:
    """IC_STABLE_MIN_RATE は「直近12週で IC>=0 だった週の比率」の閾値。

    ICの絶対値ではなく“プラスであり続けた頻度”で見る。
    """

    def test_config_matches_implementation(self):
        cfg = _cfg()
        assert cfg.IC_STABLE_MIN_RATE == 0.80
        assert cfg.IC_REVIEW_CONSECUTIVE_NEG == 3

    def test_alpha_attributer_reads_config(self):
        src = (REPO / "alpha_attributer.py").read_text(encoding="utf-8")
        assert "IC_STABLE_MIN_RATE" in src
        assert "IC_REVIEW_CONSECUTIVE_NEG" in src

    def test_dead_threshold_removed(self):
        """learning_audit_log の別基準（IC絶対値0.20）は紛らわしいため削除済み。"""
        import learning_audit_log as lal
        assert not hasattr(lal, "IC_STABLE_THRESHOLD"), (
            "IC_STABLE_THRESHOLD が復活している。"
            "安定判定の正本は config.IC_STABLE_MIN_RATE（IC>=0だった週の比率）"
        )


class TestCreditDataBlock12:
    @pytest.fixture(autouse=True)
    def _restore(self):
        import credit_data as cd
        saved = {m: getattr(cd, m) for m in cd._CFG_THRESHOLD_MAP.values()}
        yield
        for k, v in saved.items():
            setattr(cd, k, v)

    def test_config_matches_module_defaults(self):
        import credit_data as cd
        cfg = _cfg()
        assert cfg.CREDIT_SQUEEZE_THRESH == cd.SQUEEZE_THRESHOLD
        assert cfg.CREDIT_OVERHEAT_THRESH == cd.OVERHEAT_THRESHOLD

    def test_sync_applies_config(self):
        import credit_data as cd

        class Cfg:
            CREDIT_SQUEEZE_THRESH = 0.5
            CREDIT_OVERHEAT_THRESH = 20.0

        cd.sync_thresholds_from_config(Cfg)
        assert cd.SQUEEZE_THRESHOLD == 0.5
        assert cd.OVERHEAT_THRESHOLD == 20.0

    def test_bad_values_keep_defaults(self):
        import credit_data as cd
        before = cd.SQUEEZE_THRESHOLD

        class Bad:
            CREDIT_SQUEEZE_THRESH = "低い"

        cd.sync_thresholds_from_config(Bad)
        assert cd.SQUEEZE_THRESHOLD == before
        cd.sync_thresholds_from_config(None)
        assert cd.SQUEEZE_THRESHOLD == before


class TestAlphaAttributionGap8:
    def test_config_matches_module_defaults(self):
        import alpha_attributer as aa
        cfg = _cfg()
        assert cfg.ATTRIBUTION_LOOKBACK_W == aa.LOOKBACK_WEEKS
        assert cfg.ATTRIBUTION_MIN_FIRE == aa.MIN_FIRE_COUNT

    def test_implementation_reads_config(self):
        src = (REPO / "alpha_attributer.py").read_text(encoding="utf-8")
        assert "ATTRIBUTION_LOOKBACK_W" in src
        assert "ATTRIBUTION_MIN_FIRE" in src


class TestShareholderP4:
    def test_config_matches_module_default(self):
        import shareholder_analyzer as sa
        assert _cfg().FOREIGN_INVESTOR_SPIKE_PCT == sa.FOREIGN_SPIKE_PCT

    def test_spike_threshold_is_read(self):
        """閾値を上げれば「発見フェーズ」判定にならないこと。

        前年比は history[-4]（4四半期前）との差で算出するため、
        履歴は4件以上必要。
        """
        import shareholder_analyzer as sa
        hist = [{"foreign_pct": 20.0}, {"foreign_pct": 20.5},
                {"foreign_pct": 21.0}, {"foreign_pct": 22.0}]
        # 現在24.0% - 4期前20.0% = +4.0% → 既定(3.0)で発見フェーズ、
        # SURGE(5.0)には届かない
        _, label_def, _, _ = sa._axis2_foreign_trend(24.0, hist, None)
        assert "発見フェーズ" in label_def

        class Strict:
            FOREIGN_INVESTOR_SPIKE_PCT = 99.0

        _, label_str, _, _ = sa._axis2_foreign_trend(24.0, hist, Strict)
        assert "発見フェーズ" not in label_str

    def test_top_scored_is_produced(self):
        """SHAREHOLDER_SCORE_TOP_N に対応するスコア順ランキングが出ること。"""
        src = (REPO / "shareholder_analyzer.py").read_text(encoding="utf-8")
        assert "SHAREHOLDER_SCORE_TOP_N" in src
        assert '"top_scored"' in src


class TestTdnetM4:
    def test_categories_filter_is_applied(self):
        """config で監視カテゴリを絞れること。"""
        import tdnet_monitor as tm
        title = "自己株式取得に係る事項の決定に関するお知らせ"

        class OnlySplit:
            TDNET_MONITOR_CATEGORIES = ["SPLIT"]

        assert tm._classify_event(title, None) is not None      # cfg 無し: 全件検知
        assert tm._classify_event(title, OnlySplit) is None      # 絞り込みが効く

    def test_config_categories_exist_in_patterns(self):
        """config のカテゴリ名が実装の DETECT_PATTERNS と一致していること。

        綴り違いがあると黙って何も検知しなくなる。
        """
        import tdnet_monitor as tm
        cfg = _cfg()
        unknown = set(cfg.TDNET_MONITOR_CATEGORIES) - set(tm.DETECT_PATTERNS)
        assert not unknown, f"DETECT_PATTERNS に無いカテゴリ: {unknown}"


class TestScoreCapAndMisc:
    def test_composite_bounds_are_read(self):
        src = (REPO / "stock_analyzer_main.py").read_text(encoding="utf-8")
        assert "SCORE_MIN_COMPOSITE" in src
        assert "SCORE_MAX_COMPOSITE" in src

    def test_config_bounds_match_previous_literals(self):
        cfg = _cfg()
        assert cfg.SCORE_MIN_COMPOSITE == 0.0
        assert cfg.SCORE_MAX_COMPOSITE == 100.0

    def test_short_term_days_is_read(self):
        src = (REPO / "timeline_manager.py").read_text(encoding="utf-8")
        assert "SHORT_TERM_DAYS" in src
        assert _cfg().SHORT_TERM_DAYS == 5

    def test_activist_top_n_is_read(self):
        src = (REPO / "stock_screener.py").read_text(encoding="utf-8")
        assert "ACTIVIST_TOP_N" in src

    def test_cycle_phase_history_file_is_read(self):
        src = (REPO / "economic_cycle_detector.py").read_text(encoding="utf-8")
        assert "CYCLE_PHASE_HISTORY_FILE" in src
