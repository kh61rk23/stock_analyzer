"""一次採点の重み: 逆向き寄りの観点（業種）だけ下げ、符号は反転しない。日米で同じ重みを使う。"""
import pytest

import config
import screener_universe as su
import us_screener_universe as us


def test_weights_sum_to_one_and_stay_nonnegative():
    w = config.SCREEN_UNIVERSE_WEIGHTS
    assert set(w) == set(su.ASPECTS)
    assert sum(w.values()) == pytest.approx(1.0, abs=1e-9)
    assert all(v >= 0 for v in w.values())


def test_sector_weight_is_halved_relative_to_others():
    w = su.normalize_weights(config.SCREEN_UNIVERSE_WEIGHTS)
    assert w["sector"] < 0.03
    assert w["sector"] == pytest.approx(min(w.values()))
    assert w["sector"] * 2 == pytest.approx(w["flow"], rel=0.05)


def test_us_uses_the_same_weights():
    src = open(us.__file__, encoding="utf-8").read()
    assert "SCREEN_UNIVERSE_WEIGHTS" in src
