# numpy型が isinstance(v, (int,float)) で弾かれる問題の再発防止
#
# pandas 由来の値（.sum()/.mean() の戻り値など）は
# numpy.float32 / numpy.int64 になることがあり、これらは Python の
# int/float のサブクラスではないため isinstance が False になる。
# 弾かれると ml/dataset_builder の特徴量が全業種で空になる。
import numpy as np
import pytest

from finance_logic import as_number


class TestAsNumber:
    @pytest.mark.parametrize("value,expected", [
        (np.float64(55.0), 55.0),
        (np.float32(55.0), 55.0),
        (np.int64(61), 61.0),
        (np.int32(61), 61.0),
        (55.0, 55.0),
        (61, 61.0),
    ])
    def test_numeric_values_are_converted(self, value, expected):
        assert as_number(value) == pytest.approx(expected)

    def test_numeric_looking_strings_are_rejected(self):
        """"80" のような文字列を通すと型の取り違えを隠してしまう"""
        assert as_number("80") is None
        assert as_number("42.5") is None

    def test_numpy_types_would_fail_naive_isinstance(self):
        """この前提が崩れたら本テストの存在意義がなくなるので固定する"""
        assert isinstance(np.float32(1.0), (int, float)) is False
        assert isinstance(np.int64(1), (int, float)) is False

    @pytest.mark.parametrize("value", [
        None, True, False, "abc", "", [], {},
        float("nan"), float("inf"), float("-inf"),
        np.float64("nan"), np.float32("inf"),
    ])
    def test_non_numeric_and_boolean_are_rejected(self, value):
        assert as_number(value) is None

    def test_bool_is_rejected_even_though_it_is_an_int(self):
        """True/False が 1.0/0.0 に化けてスコアへ混入するのを防ぐ"""
        assert as_number(True) is None
        assert isinstance(True, int) is True


class TestDatasetBuilderAcceptsNumpy:
    def test_numpy_components_produce_features(self):
        import importlib.util

        import config

        spec = importlib.util.spec_from_file_location(
            "dsb_guard", "ml/dataset_builder.py")
        dsb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dsb)

        results = {"signal_aggregator": {"sector_signals": {
            "1028": {"components": {"A": np.float32(55.0),
                                    "B": np.int64(61),
                                    "C": np.float64(70.5),
                                    "D": None}},
        }}}
        rows = dsb.build_feature_rows(config, results, "2026-09-09")

        assert rows, "numpy型のcomponentsから特徴量が作られていない"
        feats = {k: v for k, v in rows[0].items() if k.startswith("f_")}
        assert set(feats) == {"f_A", "f_B", "f_C"}, "None以外が全て拾えていない"
        assert all(isinstance(v, float) for v in feats.values()), \
            "BQ/JSONへ書けるようPython floatに正規化されていること"


class TestScoreCalculatorAcceptsNumpy:
    def test_numpy_composite_score_is_extracted(self):
        import score_calculator as sc

        results = {"screener": {"candidates": [
            {"ticker": "6501.T", "composite_score": np.float32(72.5)},
        ]}}
        got = sc.JpSectorAdapter().extract("6501.T", "1016", results)
        assert got is not None, "numpy型のcomposite_scoreが未算出扱いされている"
        assert got["composite_score"] == pytest.approx(72.5)
