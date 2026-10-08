# ==============================================================
# tests/test_batch6_ssot.py
# R系・ML系・単発の config 正本化と、淘汰判定・USマクロ反映の検証
# ==============================================================
# 【最重要】SIGNAL_WEIGHTS は config が正本（23カテゴリ）。実装だけが
# 独自の重みを持つと、カテゴリ追加時に config が更新されず乖離に気づけない。
# ==============================================================
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))


def _cfg():
    import importlib.util
    spec = importlib.util.spec_from_file_location("config", str(REPO / "config.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestSignalWeightsSSOT:
    """シグナル重みはスコアの根幹。config と実装の一致を強制する。"""

    def test_config_matches_implementation(self):
        import signal_aggregator as sa
        assert _cfg().SIGNAL_WEIGHTS == sa.DEFAULT_WEIGHTS, (
            "config.SIGNAL_WEIGHTS と signal_aggregator.DEFAULT_WEIGHTS が"
            "乖離している。実装値を config へ写すこと"
        )

    def test_has_all_23_categories(self):
        cfg = _cfg()
        assert len(cfg.SIGNAL_WEIGHTS) == 23, "カテゴリ数が23でない"

    def test_weights_sum_to_one(self):
        import pytest
        assert sum(_cfg().SIGNAL_WEIGHTS.values()) == pytest.approx(1.0)

    def test_wave67_categories_present(self):
        """geo_signal〜academic_signal 等の追加カテゴリが config にもあること。"""
        cfg = _cfg()
        for k in ("geo_signal", "independence_signal", "taxonomy_signal",
                  "midterm_signal", "standardization_signal", "academic_signal"):
            assert k in cfg.SIGNAL_WEIGHTS, f"{k} が config に無い"

    def test_sync_applies_config(self):
        import signal_aggregator as sa
        saved = dict(sa.DEFAULT_WEIGHTS)
        try:
            sa.sync_default_weights_from_config(_cfg())
            assert sa.DEFAULT_WEIGHTS == _cfg().SIGNAL_WEIGHTS
        finally:
            sa.DEFAULT_WEIGHTS = saved

    def test_sync_rejects_broken_weights(self):
        """合計が1.00から外れる設定は取り込まないこと（スコア崩壊の防止）。"""
        import signal_aggregator as sa
        saved = dict(sa.DEFAULT_WEIGHTS)
        try:
            class Bad:
                SIGNAL_WEIGHTS = {"sector_score": 0.5}

            sa.sync_default_weights_from_config(Bad)
            assert sa.DEFAULT_WEIGHTS == saved, "壊れた重みを取り込んでしまった"

            class NotDict:
                SIGNAL_WEIGHTS = "invalid"

            sa.sync_default_weights_from_config(NotDict)
            assert sa.DEFAULT_WEIGHTS == saved
            sa.sync_default_weights_from_config(None)
            assert sa.DEFAULT_WEIGHTS == saved
        finally:
            sa.DEFAULT_WEIGHTS = saved


class TestAllocationCulling:
    """S11 の淘汰判定。PF/p が None 固定で判定が働かない状態を防ぐ。"""

    def test_profit_factor(self):
        import allocation_meta as am
        assert am.calc_profit_factor([0.1, -0.05, 0.05], 6) == 3.0
        assert am.calc_profit_factor([], 6) is None
        # 損失月なし＝負けていないので判定不要
        assert am.calc_profit_factor([0.1, 0.2], 6) is None

    def test_profit_factor_respects_window(self):
        """CULL_MONTHS で観測期間が変わること。"""
        import allocation_meta as am
        rets = [-0.5] * 6 + [0.1] * 6      # 前半が大負け・後半が勝ち
        assert am.calc_profit_factor(rets, 6) is None       # 直近6ヶ月は勝ちのみ
        assert am.calc_profit_factor(rets, 12) < 1.0        # 12ヶ月なら負け

    def test_bootstrap_p_is_deterministic(self):
        import allocation_meta as am
        rets = [0.05, -0.02, 0.03, -0.01, 0.04, 0.02]
        assert am.bootstrap_p_value(rets, 6) == am.bootstrap_p_value(rets, 6)

    def test_culling_actually_triggers(self):
        """負け続けた戦略が淘汰され、勝ち戦略は残ること。"""
        import allocation_meta as am
        win = [0.05, 0.03, -0.01, 0.04, 0.02, 0.03]
        lose = [-0.05, -0.03, 0.01, -0.04, -0.02, 0.03]
        for rets, expected in ((win, False), (lose, True)):
            pf = am.calc_profit_factor(rets, 6)
            p = am.bootstrap_p_value(rets, 6)
            assert am.cull_check(pf, p, 1.0, 0.5) is expected

    def test_insufficient_data_is_safe(self):
        import allocation_meta as am
        assert am.bootstrap_p_value([0.1], 6) is None
        assert am.cull_check(None, None) is False


class TestMlMetaDefaults:
    """Purged CV のパラメータが config を正本とすること。"""

    def test_config_values(self):
        cfg = _cfg()
        assert cfg.ML_META_CV_FOLDS == 5
        assert cfg.ML_META_EMBARGO_D == 20
        assert cfg.ML_META_TARGET_HORIZON_D == 20

    def test_splits_use_config_when_omitted(self):
        from ml.purged_cv import purged_walk_forward_splits
        dates = [f"2026-{m:02d}-{d:02d}" for m in range(1, 13) for d in range(1, 21)]
        assert len(purged_walk_forward_splits(dates)) == \
               len(purged_walk_forward_splits(dates, 5, 20, 20))

    def test_embargo_still_removes_train_rows(self):
        """embargo がリーク防止として効いていること（既存挙動の維持）。"""
        from ml.purged_cv import purged_walk_forward_splits
        dates = [f"2026-{m:02d}-{d:02d}" for m in range(1, 13) for d in range(1, 21)]
        with_e = purged_walk_forward_splits(dates, 5, 20, 20)
        without_e = purged_walk_forward_splits(dates, 5, 0, 0)
        assert sum(len(tr) for tr, _ in with_e) < sum(len(tr) for tr, _ in without_e)


class TestPromotionM2:
    """実弾昇格M2基準。レポートの文言だけでなく判定が実際に働くこと。"""

    @staticmethod
    def _acc(fills, monthly_factor):
        return {"total_fills": fills,
                "equity_history": [
                    {"date": f"2026-{m:02d}-28",
                     "equity": 1_000_000 * (monthly_factor ** m)}
                    for m in range(1, 13)]}

    def test_winner_is_eligible(self):
        from paper.monthly_report import judge_promotion
        j = judge_promotion(self._acc(40, 1.03), _cfg())
        assert j["eligible"] is True
        assert j["reasons"] == []

    def test_too_few_trades_blocks(self):
        from paper.monthly_report import judge_promotion
        j = judge_promotion(self._acc(5, 1.03), _cfg())
        assert j["eligible"] is False
        assert any("取引数" in r for r in j["reasons"])

    def test_loser_is_blocked(self):
        from paper.monthly_report import judge_promotion
        j = judge_promotion(self._acc(40, 0.97), _cfg())
        assert j["eligible"] is False
        assert any("PF" in r for r in j["reasons"])

    def test_no_loss_months_count_as_passing_pf(self):
        """損失月ゼロは最良ケース。PF が None でも昇格を妨げないこと。

        calc_profit_factor は「損失なし」を None で返す（淘汰判定では
        対象外の意味）。昇格判定でそのまま不合格にすると逆になる。
        """
        from paper.monthly_report import judge_promotion
        j = judge_promotion(self._acc(40, 1.03), _cfg())
        assert j["pf"] is None
        assert not any("PF" in r for r in j["reasons"])

    def test_thresholds_come_from_config(self):
        from paper.monthly_report import judge_promotion

        class Strict:
            PROMOTION_MIN_TRADES = 999
            PROMOTION_MIN_PF = 1.3
            PROMOTION_MAX_P = 0.10

        assert judge_promotion(self._acc(40, 1.03), Strict)["eligible"] is False

    def test_report_includes_judgement_section(self):
        from paper.monthly_report import build_report
        md = build_report({"s1": self._acc(40, 1.03)}, "202607", _cfg())
        assert "実弾昇格M2判定" in md
        assert "昇格候補" in md


class TestTaxonomyHotness:
    def test_constants_are_read(self):
        src = (REPO / "taxonomy_hotness.py").read_text(encoding="utf-8")
        assert "TAXONOMY_HOTNESS_BASELINE_DAYS" in src
        assert "TAXONOMY_LEADING_ONLY_TH" in src

    def test_leading_threshold_is_configurable(self):
        from taxonomy_hotness import leading_only_nodes
        hot = {"n1": {"leading": 70.0, "semi_leading": 10.0}}
        assert "n1" in leading_only_nodes(hot, cfg=_cfg())

        class Strict:
            TAXONOMY_LEADING_ONLY_TH = 99.0

        assert "n1" not in leading_only_nodes(hot, cfg=Strict)


class TestMiscWiring:
    def test_edinet_skip_sectors_filters_representatives(self):
        import edinetdb_ingestor as ei

        class Cfg:
            SECTOR_REPRESENTATIVE_TICKERS = {"1001": ["1332.T"], "1016": ["6758.T"]}
            EDINET_SKIP_SECTORS = ["1001"]
            DATA_DIR = "/nonexistent"

        _, _, reps = ei._priority_tickers(Cfg)
        assert "6758.T" in reps
        assert "1332.T" not in reps, "スキップ業種が除外されていない"

    def test_skip_sectors_empty_keeps_all(self):
        import edinetdb_ingestor as ei

        class Cfg:
            SECTOR_REPRESENTATIVE_TICKERS = {"1001": ["1332.T"], "1016": ["6758.T"]}
            EDINET_SKIP_SECTORS = []
            DATA_DIR = "/nonexistent"

        _, _, reps = ei._priority_tickers(Cfg)
        assert {"1332.T", "6758.T"} <= set(reps)

    def test_wired_constants_are_read(self):
        checks = [
            ("TREND_MOM_THRESHOLD", "google_trends_pipeline.py"),
            ("US_SECTOR_MAPPING_CACHE_DAYS", "us_sector_bridge_enhanced.py"),
            ("NISA_LONG_HOLD_MONTHS", "tax_optimizer.py"),
            ("CULL_MONTHS", "allocation_meta.py"),
            ("B1_KESSAN_CATEGORIES", "stock_analyzer_main.py"),
            ("CONTRACTION_RESULT_KEY", "stock_analyzer_main.py"),
            ("US_MACRO_MAX_SECTOR_ADJ", "stock_analyzer_main.py"),
            ("EDINET_SKIP_SECTORS", "edinetdb_ingestor.py"),
            ("SIGNAL_WEIGHTS", "signal_aggregator.py"),
        ]
        for const, filename in checks:
            src = (REPO / filename).read_text(encoding="utf-8")
            assert const in src, f"{const} が {filename} から読まれていない"


class TestRetiredConstants:
    def test_removed(self):
        cfg = _cfg()
        for name in ("FUDOSANDB_P1_TICKERS", "PEARL_ENABLED_DIMENSIONS"):
            assert not hasattr(cfg, name), f"{name} が復活している"

    def test_unimplemented_are_documented(self):
        """実装が無いフラグは config にその旨が書かれていること。"""
        src = (REPO / "config.py").read_text(encoding="utf-8")
        for flag in ("GARCH_ENABLED", "IGNITION_CATEGORY_D_ENABLED"):
            idx = src.index(flag)
            assert "未実装" in src[max(0, idx - 600):idx + 100], (
                f"{flag} は実装が無いため、その旨を config に明記すること"
            )
