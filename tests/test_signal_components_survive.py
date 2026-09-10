# signal_aggregator の components が算出値を保持すること
#
# 2026-09-10: calc_signal_score の
#     if score is not None:
#         components[cat] = round(score, 1)
#         ...
#         components[cat] = None   # データなし   ← else が無く同じブロック内
# により、値が入った直後に必ず None で上書きされていた（2026-03-30 初版以来）。
# signal_score は weighted_sum から先に算出されるため表向きは正常に見え、
# components を読む側だけが全滅していた:
#   ml/dataset_builder  → 「sector_signals=33 / 特徴量あり業種=0」で1行も蓄積されず
#   _pick_geo_alert     → components["geo_signal"] が float にならず未発火
# 「スコアは出ているのに内訳が空」を検出できるよう固定する。
import types

import pytest

import signal_aggregator as sa


def _signal(sector_score=45.0):
    results = {"sectors": {"1016": {"composite_score": sector_score}}}
    return sa.calc_signal_score("1016", results, sa.DEFAULT_WEIGHTS)


class TestComponentsRetained:
    def test_at_least_one_component_is_numeric(self):
        comps = _signal()["components"]
        numeric = [k for k, v in comps.items() if v is not None]
        assert numeric, "算出できたカテゴリが None で上書きされている"

    def test_sector_score_round_trips(self):
        comps = _signal(sector_score=45.0)["components"]
        assert comps["sector_score"] == 45.0

    def test_data_coverage_matches_numeric_components(self):
        """coverage が正なら内訳も空であってはならない"""
        out = _signal()
        if out.get("data_coverage", 0) > 0:
            assert any(v is not None for v in out["components"].values())

    def test_categories_without_data_stay_none(self):
        """値が取れなかったカテゴリは None のままであること（潰さない）"""
        comps = _signal()["components"]
        assert any(v is None for v in comps.values()), \
            "データが無いカテゴリまで数値化されている"

    def test_every_extractor_category_has_a_key(self):
        comps = _signal()["components"]
        assert len(comps) >= 20, "カテゴリのキーが欠けている"


class TestMlMetaConsumesComponents:
    """ml/dataset_builder が実際に特徴量行を作れること（配線の端から端まで）"""

    def test_feature_rows_are_built_from_real_signals(self):
        import ml.dataset_builder as db
        results = {"sectors": {"1016": {"composite_score": 45.0}}}
        sigs = {"1016": sa.calc_signal_score("1016", results, sa.DEFAULT_WEIGHTS)}
        cfg = types.SimpleNamespace(
            SECTOR_REPRESENTATIVE_TICKERS={"1016": ["6501.T"]},
            CACHE_DIR="/nonexistent")
        rows = db.build_feature_rows(
            cfg,
            {"signal_aggregator": {"sector_signals": sigs},
             "economic_cycle": {"phase": "B"}},
            "2026-09-10")
        assert rows, "sector_signals があるのに特徴量行が0件"
        assert any(k.startswith("f_") for k in rows[0]), "f_ 列が無い"
