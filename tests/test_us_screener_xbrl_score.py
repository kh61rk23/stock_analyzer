"""米国スクリーナー：XBRL の FCF で欠けを埋め、EPS の加速 × 重みを素点に足す。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import us_stock_screener as us


class Cfg:
    US_SCREENER_EPS_ACCEL_WEIGHT = 0.3
    US_SCREENER_FCF_FILL_XBRL = True


def _c(**kw):
    base = {"ticker": "X", "business_model": "mixed", "roe": 0.2, "op_margin": 0.2, "per": 15, "pbr": 2.0}
    base.update(kw)
    return base


def test_accel_bonus_added_to_score():
    plain = _c()
    us._apply_xbrl_complement(plain, Cfg)
    up = _c(eps_accel_score_xbrl=10.0)
    us._apply_xbrl_complement(up, Cfg)
    down = _c(eps_accel_score_xbrl=-10.0)
    us._apply_xbrl_complement(down, Cfg)
    assert plain["eps_accel_bonus"] == 0.0
    assert up["eps_accel_bonus"] == 3.0 and down["eps_accel_bonus"] == -3.0
    assert us._calc_score(up) == us._calc_score(plain) + 3
    assert us._calc_score(down) == us._calc_score(plain) - 3


def test_weight_zero_turns_it_off():
    class Off(Cfg):
        US_SCREENER_EPS_ACCEL_WEIGHT = 0.0
    c = _c(eps_accel_score_xbrl=10.0)
    us._apply_xbrl_complement(c, Off)
    assert c["eps_accel_bonus"] == 0.0


def test_fcf_filled_only_when_yfinance_missing():
    miss = _c(free_cashflow=None, latest_fcf_xbrl=5e8)
    us._apply_xbrl_complement(miss, Cfg)
    assert miss["free_cashflow"] == 5e8 and miss["fcf_source"] == "xbrl"
    have = _c(free_cashflow=1e8, latest_fcf_xbrl=-5e8)
    us._apply_xbrl_complement(have, Cfg)
    assert have["free_cashflow"] == 1e8 and "fcf_source" not in have


def test_filled_negative_fcf_hits_the_filter():
    model = next(k for k, v in us.BUSINESS_MODEL_FILTERS.items() if v.get("fcf_positive"))
    c = _c(business_model=model, free_cashflow=None, latest_fcf_xbrl=-1e8)
    us._apply_xbrl_complement(c, Cfg)
    assert any("FCF<0" in f for f in us._apply_filter(c)[1])


def test_report_cell():
    assert us._accel_cell({"eps_accel_score_xbrl": None}) == "—"
    assert us._accel_cell({"eps_accel_score_xbrl": 8.0, "eps_accel_bonus": 2.4}) == "+2.4点（+8.0）"
