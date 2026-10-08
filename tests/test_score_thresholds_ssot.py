# ==============================================================
# tests/test_score_thresholds_ssot.py
# スコア系（I-1 / I-2 / I-3）の閾値が config を正本として読まれること
# ==============================================================
# 【防ぐもの】実装側の直書きで config.py の定数が読まれず、
# config を編集してもスコアが変わらない状態。
#   I-1 ACCEL_*        : stock_screener（0.30/0.15/0.05）
#   I-2 保守度の四半期数: earnings_surprise（8）
#   I-3 CEO補正量      : ceo_evaluator（10.0/5.0）
# config の値は実装値（上記）に揃えてある。
# ==============================================================
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

import stock_screener as ss                                     # noqa: E402


def _cfg():
    import importlib.util
    spec = importlib.util.spec_from_file_location("config", str(REPO / "config.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _data(accel):
    # 加速度は profit_accel（四半期前年比 − 通期前年比）から読む
    return {"profit_accel": None if accel is None else {"accel": accel},
            "per": 15.0, "pbr": 1.2, "roe": 0.12, "sector_code": "1016"}


class TestEpsAccelerationI1:
    """I-1: EPS加速度の閾値が config から読まれること。"""

    def test_config_matches_implementation_defaults(self):
        cfg = _cfg()
        assert cfg.ACCEL_STRONG_UP == 0.30
        assert cfg.ACCEL_MID_UP == 0.15
        assert cfg.ACCEL_MILD_UP == 0.05
        assert cfg.ACCEL_MILD_DOWN == -0.05
        assert cfg.ACCEL_MID_DOWN == -0.15
        assert cfg.ACCEL_STRONG_DOWN == -0.30

    def test_wiring_does_not_change_behaviour(self):
        cfg = _cfg()
        for a in (0.40, 0.20, 0.10, 0.0, -0.10, -0.20, -0.40, None):
            d = _data(a)
            assert (ss._calc_scores(d, 60.0, cfg=cfg).get("acc_bonus")
                    == ss._calc_scores(d, 60.0, cfg=None).get("acc_bonus"))

    def test_threshold_is_actually_read(self):
        """閾値を厳しくすればボーナスが下がること（＝本当に読んでいる証拠）。"""
        class Strict:
            ACCEL_STRONG_UP = 5.00      # 到達不能
            ACCEL_MID_UP = 4.00
            ACCEL_MILD_UP = 3.00
            ACCEL_MILD_DOWN = -3.00
            ACCEL_MID_DOWN = -4.00
            ACCEL_STRONG_DOWN = -5.00

        d = _data(0.40)   # 既定では強加速 +10pt
        assert ss._calc_scores(d, 60.0, cfg=None).get("acc_bonus") == 10.0
        assert ss._calc_scores(d, 60.0, cfg=Strict).get("acc_bonus") == 0.0

    def test_growth_without_acceleration_gets_no_bonus(self):
        """増益率だけでは加点しない（加点は加速度のみ）。"""
        d = {"earnings_growth": 0.20, "earnings_qgrowth": 0.20,
             "per": 15.0, "pbr": 1.2, "roe": 0.12, "sector_code": "1016"}
        assert ss._calc_scores(d, 60.0, cfg=None).get("acc_bonus") == 0.0
        assert ss._calc_scores(_data(-0.02), 60.0, cfg=None).get("acc_bonus") == 0.0


class TestConservatismQuartersI2:
    """I-2: 保守度計算の四半期数が config を正本とすること。"""

    def test_config_adopts_implementation_value(self):
        """config が実装値 8 を採用していること。"""
        import earnings_surprise as es
        assert _cfg().CONSERVATISM_MIN_QUARTERS == 8
        assert es.CONSERVATISM_QUARTERS == 8, "モジュール既定も8のままであること"

    def test_module_reads_config(self):
        import earnings_surprise as es
        assert int(getattr(es._cfg_es(), "CONSERVATISM_MIN_QUARTERS")) == 8


class TestCeoQualityBonusI3:
    """I-3: 経営者スコアの quality_score 補正量が config から読まれること。"""

    def test_config_matches_implementation_defaults(self):
        cfg = _cfg()
        assert cfg.CEO_QUALITY_BONUS_HIGH == 10.0
        assert cfg.CEO_QUALITY_BONUS_MID == 5.0

    def test_ceo_evaluator_reads_config(self):
        src = (REPO / "ceo_evaluator.py").read_text(encoding="utf-8")
        assert "CEO_QUALITY_BONUS_HIGH" in src
        assert "CEO_QUALITY_BONUS_MID" in src


class TestNegativeRiskAlertFlag:
    """ALERT_ON_NEGATIVE_RISK がネガ側アラートを実際に止められること。"""

    def test_flag_is_read_by_alert_notifier(self):
        src = (REPO / "alert_notifier.py").read_text(encoding="utf-8")
        assert "ALERT_ON_NEGATIVE_RISK" in src, (
            "ネガティブリスク通知の専用フラグが読まれていない"
            "（ALERT_ON_HIGH_PROB_SURPRISE だけで守られた状態に戻っている）"
        )


class TestRetiredConstantIsGone:
    def test_m_a_roic_threshold_removed(self):
        """M&A成否はROICではなく「のれん減損比率」で判定するため削除済み。"""
        assert not hasattr(_cfg(), "M_A_ROIC_THRESHOLD"), (
            "M_A_ROIC_THRESHOLD が復活している。"
            "軸③の実装は goodwill_impairment / total_assets を使う"
        )
