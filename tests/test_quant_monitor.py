# ==============================================================
# tests/test_quant_monitor.py — quant/monitor.py の3つの状態を確かめる
# ==============================================================
# 合成の日次損益だけを使う（ネットワークなし）。乱数は seed 固定。
# ==============================================================
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant import monitor


def series(mean, sd, n, seed):
    rng = np.random.default_rng(seed)
    return pd.Series(rng.normal(mean, sd, n), index=pd.bdate_range("2020-01-01", periods=n))


OOS = series(0.0006, 0.006, 750, 1)


def test_insufficient_below_min_days():
    res = monitor.check(series(0.0006, 0.006, 39, 2), OOS, None)
    assert res["state"] == "insufficient"
    assert res["live_days"] == 39
    assert res["expected_q05"] is None
    assert res["live_return"] is not None


def test_empty_track_is_insufficient():
    res = monitor.check(pd.Series([], dtype=float), OOS, None)
    assert res["state"] == "insufficient" and res["live_days"] == 0 and res["live_return"] is None


def test_exactly_min_days_is_judged():
    assert monitor.check(series(0.0006, 0.006, 40, 3), OOS, None)["state"] in ("ok", "suspend")


def test_same_distribution_is_ok():
    res = monitor.check(series(0.0006, 0.006, 60, 4), OOS, None)
    assert res["state"] == "ok"
    assert res["live_days"] == 60
    assert res["expected_q05"] < 0.1


def test_big_loss_is_suspended():
    live = series(-0.004, 0.006, 60, 5)
    res = monitor.check(live, OOS, None)
    assert res["state"] == "suspend"
    assert res["live_return"] < res["expected_q05"]
    assert "下回っている" in res["reason"]


def test_drawdown_excess_suspends_even_if_total_is_positive():
    oos = series(0.001, 0.002, 750, 6)                      # 検証の下げは小さい
    live = pd.Series([0.01] * 20 + [-0.015] * 15 + [0.012] * 25,
                     index=pd.bdate_range("2024-01-01", periods=60))
    res = monitor.check(live, oos, None)
    assert res["live_return"] > res["expected_q05"]         # 累積は悪くない
    assert res["state"] == "suspend"
    assert "最大の下げ" in res["reason"]


def test_deterministic():
    live = series(0.0006, 0.006, 60, 7)
    assert monitor.check(live, OOS, None) == monitor.check(live, OOS, None)


def test_nan_in_track_is_dropped():
    live = series(0.0006, 0.006, 60, 8)
    live.iloc[3] = np.nan
    assert monitor.check(live, OOS, None)["live_days"] == 59


def test_too_short_oos_raises():
    with pytest.raises(ValueError):
        monitor.check(series(0.0, 0.01, 50, 9), pd.Series([0.01]), None)
