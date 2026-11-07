"""DCF の前提（資本コストの下限・業種別成長率・ターミナル成長率）と、順位で付ける加点。"""
import pytest

import config
import dcf_valuation as dv


def _res(discounts):
    return {f"{i:04d}.T": {"discount_pct": d} for i, d in enumerate(discounts)}


def test_floor_is_effective_cost_of_equity_for_low_beta_jp():
    # Rf1.5%・β0.8 の式の値は 6.4% 前後。下限（config）がそのまま資本コストになる
    assert dv.calc_wacc(0.8, 0.015, floor=config.DCF_WACC_FLOOR) == pytest.approx(config.DCF_WACC_FLOOR)
    assert config.DCF_WACC_FLOOR >= 0.08


def test_beta_is_pulled_toward_one():
    lo = dv.calc_wacc(0.0, 0.015, 0.06, floor=0.0)
    assert lo == pytest.approx(0.015 + 0.33 * 0.06, abs=1e-4)
    assert dv.calc_wacc(1.0, 0.015, 0.06, floor=0.0) == pytest.approx(0.075, abs=1e-4)


def test_growth_by_sector_is_capped_and_defaults():
    assert dv.fcf_growth_for("Technology") == pytest.approx(0.04)
    assert dv.fcf_growth_for("Utilities") < dv.fcf_growth_for("Technology")
    assert dv.fcf_growth_for("") == pytest.approx(dv.FCF_GROWTH_RATE)
    assert dv.fcf_growth_for("Technology") <= dv.DCF_FCF_GROWTH_CAP


def test_terminal_growth_jp_lower_than_us():
    assert dv.terminal_growth_for("7203.T") == pytest.approx(config.DCF_TERMINAL_GROWTH_JP)
    assert dv.terminal_growth_for("AAPL") == pytest.approx(dv.DEFAULT_GROWTH)
    assert dv.terminal_growth_for("7203.T") < dv.terminal_growth_for("AAPL")


def test_sensitivity_uses_given_fcf_growth():
    a = dv.calc_sensitivity(100.0, 0.09, 0.01, 1.0, 0.0, fcf_growth=0.0)["base_price"]
    b = dv.calc_sensitivity(100.0, 0.09, 0.01, 1.0, 0.0, fcf_growth=0.05)["base_price"]
    assert b > a


def test_rank_bonus_median_zero_top_max_bottom_min():
    r = dv.apply_rank_bonus(_res([-60 + 4 * i for i in range(41)]))   # -60〜+100 の41銘柄（中央値 +20）
    b = {t: d["dcf_bonus"] for t, d in r.items()}
    assert b["0040.T"] == pytest.approx(dv.DCF_RANK_BONUS_MAX)     # 最上位
    assert b["0000.T"] == pytest.approx(-dv.DCF_RANK_BONUS_MIN)    # 最下位
    assert b["0020.T"] == 0.0                                      # 中央値
    assert r["0040.T"]["dcf_bonus_basis"] == "rank"
    assert r["0040.T"]["dcf_rank_pct"] == 100.0


def test_rank_bonus_never_flips_sign_with_discount():
    # 全銘柄が割安（乖離率が正）でも、下位は減点でなく 0。全銘柄が割高なら上位は加点でなく 0
    cheap = dv.apply_rank_bonus(_res([10 + i for i in range(30)]))
    assert min(d["dcf_bonus"] for d in cheap.values()) == 0.0
    rich = dv.apply_rank_bonus(_res([-50 + i for i in range(30)]))
    assert max(d["dcf_bonus"] for d in rich.values()) == 0.0


def test_rank_bonus_does_not_saturate_when_everything_looks_cheap():
    r = dv.apply_rank_bonus(_res([150 + 5 * i for i in range(100)]))   # 全銘柄が +150% 超でも
    at_cap = sum(1 for d in r.values() if d["dcf_bonus"] >= dv.DCF_RANK_BONUS_MAX - 1e-9)
    assert at_cap <= 1


def test_invalid_and_skipped_are_outside_the_population():
    res = _res([float(i) for i in range(30)])
    res["bad.T"] = {"discount_pct": 500.0}                        # モデル破綻
    res["bank.T"] = {"discount_pct": 40.0, "dcf_skip": "金融"}
    res["none.T"] = {"discount_pct": None}
    out = dv.apply_rank_bonus(res)
    for k in ("bad.T", "bank.T", "none.T"):
        assert out[k]["dcf_bonus"] == 0.0 and out[k]["dcf_bonus_basis"] == "none"
    assert out["0029.T"]["dcf_rank_pct"] == 100.0                 # 母集団は30銘柄だけ


def test_small_population_falls_back_to_absolute():
    r = dv.apply_rank_bonus(_res([5.0, 30.0, 120.0]))
    assert r["0001.T"]["dcf_bonus"] == pytest.approx(dv.calc_dcf_bonus(30.0))
    assert r["0001.T"]["dcf_bonus_basis"] == "absolute"


def test_ties_share_average_rank():
    r = dv.apply_rank_bonus(_res([10.0] * 20 + [20.0] * 20))
    assert len({d["dcf_bonus"] for t, d in r.items() if t < "0020.T"}) == 1


def test_config_values_are_synced_into_module():
    dv.sync_module_constants(dv.__dict__, config, dv._CFG_CONST_MAP)
    dv.sync_module_constants(dv.__dict__, config, dv._CFG_VALUATION_MAP)
    assert dv.DCF_WACC_FLOOR == config.DCF_WACC_FLOOR
    assert dv.DCF_RANK_BONUS_MAX == config.DCF_RANK_BONUS_MAX
    assert dv.MARKET_RISK_PREM == config.B6_DCF_VALUATION_MARKET_RISK_PREM
