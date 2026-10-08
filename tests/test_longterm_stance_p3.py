# ==============================================================
# tests/test_longterm_stance_p3.py
# [P-3] 長期保有候補が投資スタンス判定に反映されること
# ==============================================================
# 【防ぐもの】determine_investment_stance() が is_longterm_hold_flag を
# 受け取っても判定に使わない／呼び出し側が常に False を渡す、という不作動。
#
# 仕様: P-3 候補がいる業種は長期側の閾値（long_high）を
# LONGTERM_HOLD_STANCE_RELAX_PT だけ緩める。短期側（short_high）には影響させない。
# ==============================================================
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

import longterm_scenario as ls                                  # noqa: E402


def _cfg():
    import importlib.util
    spec = importlib.util.spec_from_file_location("config", str(REPO / "config.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _stance(lt, st, has_p3, n=0, cfg=None):
    return ls.determine_investment_stance(
        "1016", lt, st, has_p3, longterm_hold_count=n, cfg=cfg or _cfg())


class TestP3AffectsStance:
    def test_promotes_trade_to_core_hold(self):
        """長期が閾値の少し下＋短期高＋P-3あり → trade から core_hold へ。"""
        assert _stance(62, 65, False)["stance"] == "trade"
        assert _stance(62, 65, True, 2)["stance"] == "core_hold"

    def test_promotes_avoid_to_accumulate(self):
        """長期が閾値の少し下＋短期低＋P-3あり → avoid から accumulate へ。"""
        assert _stance(62, 50, False)["stance"] == "avoid"
        assert _stance(62, 50, True, 1)["stance"] == "accumulate"

    def test_no_p3_is_unchanged(self):
        """P-3 が無い業種は緩和なしの判定と完全に同じであること。"""
        for lt, st in ((70, 65), (62, 65), (62, 50), (55, 50), (70, 50)):
            got = _stance(lt, st, False)["stance"]
            lt_high = lt >= ls.STANCE_THRESHOLDS["long_high"]
            st_high = st >= ls.STANCE_THRESHOLDS["short_high"]
            expected = ("core_hold" if lt_high and st_high else
                        "trade" if st_high else
                        "accumulate" if lt_high else "avoid")
            assert got == expected

    def test_relax_does_not_reach_far_below(self):
        """緩和幅を超えて低い長期スコアは昇格しないこと（無制限に緩めない）。"""
        assert _stance(55, 50, True, 5)["stance"] == "avoid"
        assert _stance(59, 65, True, 5)["stance"] == "trade"

    def test_short_side_is_untouched(self):
        """P-3 は短期側の閾値には影響しないこと。"""
        # 長期高・短期低 → P-3 があっても core_hold にはならない
        assert _stance(70, 50, True, 3)["stance"] == "accumulate"

    def test_boost_flag_only_when_it_changed_the_outcome(self):
        """緩和が結論を動かした時だけ boost=True になること。"""
        assert _stance(62, 65, True, 2)["longterm_hold_boost"] is True
        assert _stance(70, 65, True, 2)["longterm_hold_boost"] is False   # 元から高い
        assert _stance(62, 65, False)["longterm_hold_boost"] is False

    def test_reason_records_evidence(self):
        """昇格した時は理由に根拠（社数と緩和幅）を残すこと。"""
        r = _stance(62, 65, True, 2)["reason"]
        assert "P-3" in r and "2社" in r
        # 昇格していない時はノイズを出さない
        assert "P-3" not in _stance(70, 65, True, 2)["reason"]

    def test_applied_threshold_is_reported(self):
        cfg = _cfg()
        relax = cfg.LONGTERM_HOLD_STANCE_RELAX_PT
        base = ls.STANCE_THRESHOLDS["long_high"]
        assert _stance(62, 65, True, 1, cfg)["long_high_applied"] == base - relax
        assert _stance(62, 65, False, 0, cfg)["long_high_applied"] == base


class TestRelaxIsConfigurable:
    def test_zero_relax_restores_previous_behaviour(self):
        """緩和幅0なら P-3 は判定に影響しない（緩和を無効化できる）。"""
        class NoRelax:
            LONGTERM_HOLD_STANCE_RELAX_PT = 0.0

        assert _stance(62, 65, True, 2, NoRelax)["stance"] == "trade"

    def test_config_defines_the_relax(self):
        assert hasattr(_cfg(), "LONGTERM_HOLD_STANCE_RELAX_PT")


class TestSectorCountDerivation:
    def test_counts_only_passing_longterm_candidates(self):
        results = {"screener": {"candidates": [
            {"sector_code": "1016", "is_longterm_hold": True,  "pass_filters": True},
            {"sector_code": "1016", "is_longterm_hold": True,  "pass_filters": True},
            {"sector_code": "1016", "is_longterm_hold": False, "pass_filters": True},
            {"sector_code": "1017", "is_longterm_hold": True,  "pass_filters": False},
        ]}}
        assert ls._count_longterm_holds_by_sector(results) == {"1016": 2}

    def test_empty_inputs_are_safe(self):
        assert ls._count_longterm_holds_by_sector({}) == {}
        assert ls._count_longterm_holds_by_sector({"screener": {}}) == {}
        assert ls._count_longterm_holds_by_sector({"screener": None}) == {}
