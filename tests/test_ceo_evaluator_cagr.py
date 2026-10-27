"""ceo_evaluator: 営業利益が黒字から赤字に転じた会社で CAGR が複素数になり評価が落ちていた。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ceo_evaluator as ce


def _db(values):
    return {"X.T": {"financials": {"operating_income": values}}}


def test_turned_to_loss_is_worst_not_error(monkeypatch):
    monkeypatch.setattr(ce, "_get_edinet_financials", lambda t, db: db["X.T"]["financials"])
    score, detail = ce._axis1_performance("X.T", 3.0, _db([100.0, 80.0, 50.0, -20.0]))
    assert detail["op_income_cagr"] == -100.0 and detail["cagr_score"] == 0.0
    assert isinstance(score, float)


def test_normal_growth(monkeypatch):
    monkeypatch.setattr(ce, "_get_edinet_financials", lambda t, db: db["X.T"]["financials"])
    _s, detail = ce._axis1_performance("X.T", 2.0, _db([100.0, 110.0, 121.0]))
    assert detail["op_income_cagr"] == 10.0
