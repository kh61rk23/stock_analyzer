# ==============================================================
# tests/test_config_sync.py
# モジュール定数の config 移設（SSOT第1弾）
# ==============================================================
# 【防ぐもの】各モジュールが自分用の閾値定数を持つため
# 「config を編集しても効かない」状態。参照箇所を書き換えず config を
# 正本にするため、cfg を持つ入口で一度だけモジュール定数を上書きする
# 共通ヘルパー（config_sync）の動作を確認する。
# ==============================================================
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from config_sync import sync_module_constants                   # noqa: E402

# (モジュール名, config側プレフィックス)
SYNCED_MODULES = [
    ("market_breadth", "MB_"),
    ("volume_profile", "VP_"),
    ("support_resistance", "SR_"),
    ("technical_scorer", "TS_"),
    ("rule_optimizer", "RO_"),
    ("earnings_surprise", "ES_"),
    ("latent_pearl_manager", "LATENT_PEARL_"),
    ("anomaly_detector", "ANOMALY_"),
    ("insider_monitor", "IM_"),
    ("politician_signal_scorer", "PSS_"),
    ("screener_feedback_loop", "SFL_"),
    ("options_monitor", "OM_"),
    ("credit_data", "CREDIT_"),
    ("ignition_detector", "IGNITION_"),
    ("academic_conf_tracker", "ACT_"),
    ("alpha_attributer", "AA_"),
    ("bond_commodity_pipeline", "BCP_"),
    ("cache_freshness_manager", "CFM_"),
    ("catalyst_calendar", "CC_"),
    ("ceo_evaluator", "CEO_"),
    ("congress_monitor", "CM_"),
    ("correlation_heatmap", "CH_"),
    ("country_risk_updater", "CRU_"),
    ("cross_size_signal", "CSS_"),
    ("dividend_calendar", "DC_"),
    ("drift_detector", "DD_"),
    ("factor_crowding_monitor", "FCM_"),
    ("google_trends_pipeline", "GTP_"),
    ("independence_score_calculator", "ISC_"),
    ("inference_engine", "IE_"),
    ("institutional_13f_monitor", "I13F_"),
    ("jp_us_sector_correlation", "JUSC_"),
    ("longterm_scenario", "LS_"),
    ("midterm_plan_tracker", "MPT_"),
    ("ml_score_corrector", "MLSC_"),
    ("nedo_selection_scraper", "NSS_"),
    ("pead_monitor", "PM_"),
    ("performance_tracker", "PTR_"),
    ("policy_radar", "PR_"),
    ("policy_tracker", "PT_"),
    ("politician_monitor", "PMON_"),
    ("portfolio_diversity_filter", "PDF_"),
    ("portfolio_optimizer", "PO_"),
    ("position_sizer", "PS_"),
    ("regime_detector", "RD_"),
    ("screener_candidate_builder", "SCB_"),
    ("screener_sector_heatmap", "SSH_"),
    ("self_evaluator", "SE_"),
    ("shareholder_analyzer", "SHA_"),
    ("signal_dedup", "SD_"),
    ("size_decay_manager", "SDM_"),
    ("size_tier_classifier", "STC_"),
    ("stock_screener", "SS_"),
    ("stress_tester", "ST_"),
    ("taxonomy_hotness", "TH_"),
    ("thematic_etf_tracker", "TET_"),
    ("business_model_detector", "BMD_"),
    ("business_model_normalizer", "BMN_"),
    ("learning_audit_log", "LAL_"),
    ("pearl_score_calculator", "PSC_"),
    ("text_signal", "TXS_"),
    ('anomaly_season', "B4_"),
    ('backtester', "B4_"),
    ('comps_analyzer', "B4_"),
    ('dcf_valuation', "B4_"),
    ('emerging_technology_detector', "B4_"),
    ('fed_calendar_fetcher', "B4_"),
    ('finbert_analyzer', "B4_"),
    ('jp_us_sector_bridge', "B4_"),
    ('momentum_factor', "B4_"),
    ('multi_currency_monitor', "B4_"),
    ('policy_signal_applier', "B4_"),
    ('sector_rotation_detector', "B4_"),
    ('signal_culling_report', "B4_"),
    ('us_8k_monitor', "B4_"),
    ('us_fundamental_rotator', "B4_"),
    ('us_insider_form4_scraper', "B4_"),
    ('us_sector_bridge_enhanced', "B4_"),
    ('weekly_feedback_runner', "B4_"),
    ('anomaly_season', "B5_"),
    ('api_key_health', "B5_"),
    ('arxiv_monitor', "B5_"),
    ('backtester', "B5_"),
    ('comps_analyzer', "B5_"),
    ('data_reliability', "B5_"),
    ('db_rest_client', "B5_"),
    ('dcf_valuation', "B5_"),
    ('edinet_client', "B5_"),
    ('executive_transition_tracker', "B5_"),
    ('facility_expansion_monitor', "B5_"),
    ('finbert_analyzer', "B5_"),
    ('industry_standard_tracker', "B5_"),
    ('job_posting_monitor', "B5_"),
    ('jogmec_scraper', "B5_"),
    ('paid_media_searcher', "B5_"),
    ('sec_geographic_extractor', "B5_"),
    ('stock_detail_report', "B5_"),
    ('us_8k_monitor', "B5_"),
    ('us_fundamental_rotator', "B5_"),
    ('us_insider_form4_scraper', "B5_"),
    ('us_sector_bridge_enhanced', "B5_"),
    ('anomaly_season', "B6_"),
    ('arxiv_monitor', "B6_"),
    ('backtester', "B6_"),
    ('dcf_valuation', "B6_"),
    ('deep_dive_trigger', "B6_"),
    ('edinet_direct_fetcher', "B6_"),
    ('emerging_technology_detector', "B6_"),
    ('executive_transition_tracker', "B6_"),
    ('facility_expansion_monitor', "B6_"),
    ('finbert_analyzer', "B6_"),
    ('imf_pipeline', "B6_"),
    ('investment_journal', "B6_"),
    ('job_posting_monitor', "B6_"),
    ('jogmec_scraper', "B6_"),
    ('jp_us_sector_bridge', "B6_"),
    ('momentum_factor', "B6_"),
    ('multi_currency_monitor', "B6_"),
    ('notion_obsidian_writer', "B6_"),
    ('policy_signal_applier', "B6_"),
    ('score_prior_manager', "B6_"),
    ('sec_geographic_extractor', "B6_"),
    ('sector_rotation_detector', "B6_"),
    ('signal_culling_report', "B6_"),
    ('strategy_output_schema', "B6_"),
    ('us_8k_monitor', "B6_"),
    ('us_insider_form4_scraper', "B6_"),
]


def _cfg():
    import importlib.util
    spec = importlib.util.spec_from_file_location("config", str(REPO / "config.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestHelperSafety:
    """設定ミスでスコアが壊れないことを保証する。"""

    def test_none_cfg_keeps_defaults(self):
        g = {"__name__": "t", "TH": 1.5}
        sync_module_constants(g, None, {"X_TH": "TH"})
        assert g["TH"] == 1.5

    def test_missing_attr_keeps_default(self):
        class Cfg:
            pass
        g = {"__name__": "t", "TH": 1.5}
        sync_module_constants(g, Cfg, {"X_TH": "TH"})
        assert g["TH"] == 1.5

    def test_applies_valid_value(self):
        class Cfg:
            X_TH = 2.5
        g = {"__name__": "t", "TH": 1.5}
        sync_module_constants(g, Cfg, {"X_TH": "TH"})
        assert g["TH"] == 2.5

    def test_rejects_wrong_type(self):
        """文字列を数値定数に入れさせない。"""
        class Cfg:
            X_TH = "2.5"
        g = {"__name__": "t", "TH": 1.5}
        sync_module_constants(g, Cfg, {"X_TH": "TH"})
        assert g["TH"] == 1.5

    def test_rejects_bool_for_numeric(self):
        """True/1 の混同で閾値が 1 になる事故を防ぐ。"""
        class Cfg:
            X_TH = True
        g = {"__name__": "t", "TH": 1.5}
        sync_module_constants(g, Cfg, {"X_TH": "TH"})
        assert g["TH"] == 1.5

    def test_bool_default_requires_bool(self):
        class Cfg:
            X_FLAG = 1
        g = {"__name__": "t", "FLAG": False}
        sync_module_constants(g, Cfg, {"X_FLAG": "FLAG"})
        assert g["FLAG"] is False

    def test_int_is_widened_to_float(self):
        """既定が float なら int 設定でも float へ寄せる（計算式を壊さない）。"""
        class Cfg:
            X_TH = 3
        g = {"__name__": "t", "TH": 1.5}
        sync_module_constants(g, Cfg, {"X_TH": "TH"})
        assert isinstance(g["TH"], float) and g["TH"] == 3.0

    def test_dict_and_list_types(self):
        class Cfg:
            X_W = {"a": 1.0}
            X_L = [1, 2]
        g = {"__name__": "t", "W": {"a": 0.5}, "L": [9]}
        sync_module_constants(g, Cfg, {"X_W": "W", "X_L": "L"})
        assert g["W"] == {"a": 1.0}
        assert g["L"] == [1, 2]


@pytest.mark.parametrize("mod_name,prefix", SYNCED_MODULES)
class TestMigratedModules:
    def test_map_exists(self, mod_name, prefix):
        mod = __import__(mod_name)
        assert hasattr(mod, "_CFG_CONST_MAP"), f"{mod_name} に対応表が無い"
        assert mod._CFG_CONST_MAP, "対応表が空"

    def test_config_defines_every_mapped_name(self, mod_name, prefix):
        """対応表の config 名がすべて config.py に実在すること。

        書き忘れると黙って既定値のままになり、移設した気になって
        config を編集しても効かない状態に戻る。
        """
        cfg = _cfg()
        mod = __import__(mod_name)
        missing = [c for c in mod._CFG_CONST_MAP if not hasattr(cfg, c)]
        assert not missing, f"config.py に無い: {missing}"

    def test_values_match_module_defaults(self, mod_name, prefix):
        """config の値が移設時点の実装値と一致すること（挙動不変の保証）。"""
        cfg = _cfg()
        mod = __import__(mod_name)
        for cfg_name, mod_name_ in mod._CFG_CONST_MAP.items():
            assert getattr(cfg, cfg_name) == getattr(mod, mod_name_), (
                f"{cfg_name} と {mod_name}.{mod_name_} の値が食い違っている"
            )

    def test_sync_is_noop_with_current_config(self, mod_name, prefix):
        """現行 config を取り込んでも値が変わらないこと。"""
        cfg = _cfg()
        mod = __import__(mod_name)
        before = {k: getattr(mod, k) for k in mod._CFG_CONST_MAP.values()}
        sync_module_constants(mod.__dict__, cfg, mod._CFG_CONST_MAP)
        after = {k: getattr(mod, k) for k in mod._CFG_CONST_MAP.values()}
        assert before == after

    def test_prefix_convention(self, mod_name, prefix):
        """config 名がモジュール別プレフィックスを持つこと（衝突防止）。"""
        mod = __import__(mod_name)
        # 第4弾は多モジュール同時移設のため、衝突を避ける
        # B4_<MODULE>_<CONSTANT> 形式も正式な名前空間として許可する。
        bad = [c for c in mod._CFG_CONST_MAP
               if not c.startswith((prefix, "B4_", "B5_", "B6_"))]
        assert not bad, f"{prefix} または B4_/B5_/B6_ 接頭辞が無い: {bad}"

    def test_run_function_calls_sync(self, mod_name, prefix):
        """run_* が同期を呼んでいること（呼び忘れ検出）。"""
        src = (REPO / f"{mod_name}.py").read_text(encoding="utf-8")
        assert "sync_module_constants(globals(), cfg, _CFG_CONST_MAP)" in src, (
            f"{mod_name} の run_* で同期が呼ばれていない"
        )


def test_stock_analyzer_main_sync_is_wired_without_importing():
    """起動時に依存パッケージを導入する主モジュールは、ソースを静的検査する。"""
    src = (REPO / "stock_analyzer_main.py").read_text(encoding="utf-8")
    assert "_CFG_CONST_MAP" in src
    assert "sync_module_constants(globals(), cfg, _CFG_CONST_MAP)" in src
    assert hasattr(_cfg(), "SAM_DELTA_THRESHOLDS")
