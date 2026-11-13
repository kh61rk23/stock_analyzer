# ==============================================================
# tests/test_quant_gate.py — quant/gate.py の合否の境目と判定の分かれ目を確かめる
# ==============================================================
# 合成の候補 dict だけを使う（ネットワークなし）。基準は settings の既定値。
# ==============================================================
from __future__ import annotations

import copy

import pytest

from quant import gate
from quant import settings as S


def good() -> dict:
    return {
        "id": "x", "external": False,
        "metrics": {"days": 1000, "sharpe": 0.9, "gate_sharpe": 0.9, "dsr": 0.97, "pbo": 0.10, "adj_p": 0.01,
                    "p_profit": {"63": 0.7, "126": 0.8, "252": 0.85}, "max_drawdown": 0.15,
                    "fold_positive": 0.8, "ann_return": 0.08, "ann_vol": 0.09},
        "stress": {"sharpe": 0.6, "ann_return": 0.05},
    }


def _set(c, key, v):
    m = c["metrics"]
    if key == "oos_days":
        m["days"] = v
    elif key == "oos_sharpe":
        m["gate_sharpe"] = v
        if v is None:
            m["sharpe"] = None
    elif key == "dsr":
        m["dsr"] = v
    elif key == "pbo":
        m["pbo"] = v
    elif key == "adj_p":
        m["adj_p"] = v
    elif key == "p_profit_12m":
        m["p_profit"]["252"] = v
    elif key == "stress_sharpe":
        c["stress"]["sharpe"] = v
    elif key == "max_drawdown":
        m["max_drawdown"] = v
    elif key == "fold_positive":
        m["fold_positive"] = v


# key, 設定名, 向き, 基準の少し外側の値の差
CASES = [
    ("oos_days", "QUANT_GATE_MIN_OOS_DAYS", "ge", -1),
    ("oos_sharpe", "QUANT_GATE_MIN_OOS_SHARPE", "ge", -0.01),
    ("dsr", "QUANT_GATE_MIN_DSR", "ge", -0.01),
    ("pbo", "QUANT_GATE_MAX_PBO", "le", 0.01),
    ("adj_p", "QUANT_GATE_MAX_ADJ_P", "le", 0.01),
    ("p_profit_12m", "QUANT_GATE_MIN_P_PROFIT_12M", "ge", -0.01),
    ("stress_sharpe", "QUANT_GATE_MIN_STRESS_SHARPE", "ge", -0.01),
    ("max_drawdown", "QUANT_GATE_MAX_DRAWDOWN", "le", 0.01),
    ("fold_positive", "QUANT_GATE_MIN_FOLD_POSITIVE", "ge", -0.01),
]


def _check(res, key):
    return next(c for c in res["checks"] if c["key"] == key)


def test_all_pass_is_adopted():
    res = gate.evaluate(good(), None)
    assert res["status"] == "adopted"
    assert res["reasons"] == []
    assert [c["key"] for c in res["checks"]] == [c[0] for c in CASES]
    assert all(c["passed"] for c in res["checks"])


@pytest.mark.parametrize("key, setting, op, delta", CASES)
def test_exact_threshold_passes_and_just_outside_fails(key, setting, op, delta):
    th = S.get(None, setting)
    c = good()
    _set(c, key, th)
    assert _check(gate.evaluate(c, None), key)["passed"] is True      # ちょうど基準は合格
    c = good()
    _set(c, key, th + delta)
    res = gate.evaluate(c, None)
    chk = _check(res, key)
    assert chk["passed"] is False
    assert chk["threshold"] == th
    assert len(res["reasons"]) == 1 and chk["label"] in res["reasons"][0]


def test_reason_text_has_value_and_threshold():
    c = good()
    c["metrics"]["dsr"] = 0.5
    res = gate.evaluate(c, None)
    assert "基準に届かない" in res["reasons"][0] and "0.5" in res["reasons"][0] and "0.95" in res["reasons"][0]
    c = good()
    c["metrics"]["max_drawdown"] = 0.4
    assert "基準を超えている" in gate.evaluate(c, None)["reasons"][0]


def test_gate_sharpe_is_used_instead_of_raw_sharpe():
    c = good()
    c["metrics"]["sharpe"] = 2.0
    c["metrics"]["gate_sharpe"] = 0.3      # 理論価格の割引後は基準 0.5 未満
    res = gate.evaluate(c, None)
    assert _check(res, "oos_sharpe")["passed"] is False
    assert _check(res, "oos_sharpe")["value"] == 0.3


def test_gate_sharpe_falls_back_to_sharpe():
    c = good()
    del c["metrics"]["gate_sharpe"]
    c["metrics"]["sharpe"] = 0.7
    assert _check(gate.evaluate(c, None), "oos_sharpe")["value"] == 0.7


def test_none_pbo_and_fold_positive_pass_with_note():
    c = good()
    c["metrics"]["pbo"] = None
    c["metrics"]["fold_positive"] = None
    res = gate.evaluate(c, None)
    assert res["status"] == "adopted"
    assert _check(res, "pbo")["passed"] and _check(res, "pbo")["note"]
    assert _check(res, "fold_positive")["passed"] and _check(res, "fold_positive")["note"]


@pytest.mark.parametrize("key", ["oos_days", "oos_sharpe", "dsr", "adj_p", "p_profit_12m", "stress_sharpe", "max_drawdown"])
def test_none_in_other_checks_fails(key):
    c = good()
    _set(c, key, None)
    res = gate.evaluate(c, None)
    assert _check(res, key)["passed"] is False
    assert "計算できない" in res["reasons"][0]


def test_nan_is_treated_as_none():
    c = good()
    c["metrics"]["dsr"] = float("nan")
    assert _check(gate.evaluate(c, None), "dsr")["passed"] is False


def test_external_skips_fold_positive():
    c = good()
    c["external"] = True
    c["metrics"]["fold_positive"] = 0.0
    res = gate.evaluate(c, None)
    assert res["status"] == "adopted"
    assert _check(res, "fold_positive")["passed"] is True


def test_missing_p_profit_key_fails():
    c = good()
    c["metrics"]["p_profit"] = {"63": 0.9}
    assert _check(gate.evaluate(c, None), "p_profit_12m")["passed"] is False


def test_watch_when_few_noncritical_fail():
    c = good()
    c["metrics"]["dsr"] = 0.5
    c["metrics"]["adj_p"] = 0.5
    res = gate.evaluate(c, None)          # 落ちた2個（上限 2）・必須は通っている
    assert res["status"] == "watch"
    assert len(res["reasons"]) == 2


def test_rejected_when_too_many_fail():
    c = good()
    c["metrics"]["dsr"] = 0.5
    c["metrics"]["adj_p"] = 0.5
    c["metrics"]["max_drawdown"] = 0.5
    assert gate.evaluate(c, None)["status"] == "rejected"


@pytest.mark.parametrize("key", ["oos_days", "oos_sharpe", "stress_sharpe"])
def test_critical_failure_is_rejected_even_with_one_fail(key):
    c = good()
    _set(c, key, 0 if key == "oos_days" else -1.0)
    res = gate.evaluate(c, None)
    assert sum(1 for x in res["checks"] if not x["passed"]) == 1
    assert res["status"] == "rejected"


def test_reject_reason_short_circuits():
    c = good()
    c["reject_reason"] = "日数不足"
    res = gate.evaluate(c, None)
    assert res == {"status": "rejected", "checks": [], "reasons": ["日数不足"]}


def test_missing_metrics_is_rejected_not_error():
    res = gate.evaluate({"id": "x"}, None)
    assert res["status"] == "rejected"


def test_does_not_mutate_candidate():
    c = good()
    before = copy.deepcopy(c)
    gate.evaluate(c, None)
    assert c == before


def test_cfg_overrides_threshold():
    class Cfg:
        QUANT_GATE_MIN_DSR = 0.99
    assert _check(gate.evaluate(good(), Cfg()), "dsr")["passed"] is False
