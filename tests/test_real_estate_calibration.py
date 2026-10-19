"""推定時価の補正係数（config.RE_EST_CALIBRATION）と開示時価の置き換え（blend_disclosed_gain）の検査。

補正係数・三大都市圏の都府県コード・時価÷簿価の範囲は、Python（real_estate_signal）とマートSQL・検証パネルSQLに
同じ値を書いているので、ずれたら落ちるようにする（スコアとマートの定義を一致させるため）。
"""
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
from real_estate_signal import (FV_BV_RANGE, METRO_PREF_CODES, blend_disclosed_gain, calc_real_estate_bonus,
                                calibration_factor, region_of, summarize_holdings)

REPO = Path(__file__).resolve().parent.parent
MARTS = REPO / "bi" / "marts"
CAL_SQLS = {
    "m_re_holdings": (MARTS / "m_re_holdings.sql").read_text(encoding="utf-8"),
    "m_re_holdings_pref": (MARTS / "m_re_holdings_pref.sql").read_text(encoding="utf-8"),
}
SCREENER_SQL = (MARTS / "m_screener.sql").read_text(encoding="utf-8")


def _cal_from_sql(sql: str) -> dict:
    rows = re.findall(r"STRUCT\('(\w+)' AS price_kind, '(\w+)' AS region, ([0-9.]+) AS k\)", sql)
    return {f"{kind}|{region}": float(k) for kind, region, k in rows}


@pytest.mark.parametrize("name", sorted(CAL_SQLS))
def test_sql_calibration_matches_config(name):
    assert _cal_from_sql(CAL_SQLS[name]) == {k: float(v) for k, v in config.RE_EST_CALIBRATION.items()}


@pytest.mark.parametrize("name", sorted(CAL_SQLS))
def test_sql_metro_codes_match_python(name):
    m = re.search(r"SUBSTR\(municipality_code, 1, 2\) IN \(([^)]*)\)", CAL_SQLS[name])
    assert m, name
    assert tuple(c.strip().strip("'") for c in m.group(1).split(",")) == METRO_PREF_CODES


@pytest.mark.parametrize("name", sorted(CAL_SQLS))
def test_sql_judges_exclusion_before_calibration(name):
    """20倍超の除外は補正前の est_raw で判定し、係数は除外されなかった値にだけ掛ける。"""
    sql = CAL_SQLS[name]
    assert re.search(r"est_raw > 20 \* [\w.]+, NULL, [\w.]*est_raw \* ", sql), name


def test_screener_fv_bv_range_matches_python():
    lo, hi = FV_BV_RANGE
    assert f"fair_value_m_yen / book_value_m_yen BETWEEN {lo:g} AND {hi:g}" in SCREENER_SQL


def test_calibration_keys_are_known_strata():
    kinds = {"industrial", "commercial", "residential"}
    for key, k in config.RE_EST_CALIBRATION.items():
        kind, region = key.split("|")
        assert kind in kinds and region in {"metro", "other"}
        assert 0.2 <= float(k) <= 5.0


def test_region_of():
    assert region_of("13103") == "metro"
    assert region_of("01401") == "other"
    assert region_of(None) == "other"


def _row(code, price_key, price, area=10_000, book=1_000):
    return {"municipality_code": code, price_key: price, "land_area_m2": area,
            "book_value_land_m_yen": book, "book_value_buildings_m_yen": 0}


CAL = {"commercial|metro": 0.5, "industrial|metro": 2.0}


def test_calibration_factor_by_stratum():
    assert calibration_factor(_row("13103", "commercial_land_price_per_m2", 300_000), CAL) == 0.5
    assert calibration_factor(_row("01401", "commercial_land_price_per_m2", 300_000), CAL) == 1.0
    # 工業地の値があれば工業地が参照地価（マートの COALESCE と同じ順）
    both = _row("27100", "commercial_land_price_per_m2", 300_000)
    both["industrial_land_price_per_m2"] = 100_000
    assert calibration_factor(both, CAL) == 2.0
    assert calibration_factor(_row("13103", "residential_land_price_per_m2", 300_000), CAL) == 1.0
    assert calibration_factor(_row("13103", "commercial_land_price_per_m2", 300_000), None) == 1.0
    assert calibration_factor({"municipality_code": "13103"}, CAL) == 1.0


def test_summarize_holdings_applies_calibration():
    row = _row("13103", "commercial_land_price_per_m2", 300_000)   # 推定 3,000 百万円・簿価 1,000
    assert summarize_holdings([row])["unrealized_gain_m_yen"] == 2000.0
    s = summarize_holdings([row], CAL)
    assert s["land_value_est_m_yen"] == 1500.0 and s["unrealized_gain_m_yen"] == 500.0
    assert summarize_holdings([_row("01401", "commercial_land_price_per_m2", 300_000)], CAL)["unrealized_gain_m_yen"] == 2000.0


def test_exclusion_uses_uncalibrated_value():
    # 推定 25,000 は簿価 1,000 の25倍 → 補正（×0.5 で 12,500）しても除外したまま
    hq = _row("13103", "commercial_land_price_per_m2", 2_500_000)
    s = summarize_holdings([hq], CAL)
    assert s["excluded_count"] == 1 and s["unrealized_gain_m_yen"] is None
    # 推定 15,000（15倍）は除外されず、×2.0 で 30,000 になっても残る
    ok = _row("27100", "industrial_land_price_per_m2", 1_500_000)
    s = summarize_holdings([ok], CAL)
    assert s["excluded_count"] == 0 and s["land_value_est_m_yen"] == 30_000.0


def test_calc_bonus_reads_calibration_from_cfg():
    row = _row("13103", "commercial_land_price_per_m2", 300_000)
    plain = SimpleNamespace(RE_BONUS_MIN=-2.0, RE_BONUS_MAX=3.0)
    cal = SimpleNamespace(RE_BONUS_MIN=-2.0, RE_BONUS_MAX=3.0, RE_EST_CALIBRATION=CAL)
    assert calc_real_estate_bonus([row], cal) < calc_real_estate_bonus([row], plain)


def test_blend_without_disclosure_keeps_estimate():
    assert blend_disclosed_gain(100.0, 1000.0, None, None) == (100.0, None, "estimate")
    assert blend_disclosed_gain(None, None, None, None) == (None, None, None)


def test_blend_replaces_rental_share_of_estimate():
    # 賃貸分の簿価 400 ÷ 保有不動産の簿価 1,000 = 0.4 → 推定 100 の6割 + 開示 (700 − 400)
    gain, share, basis = blend_disclosed_gain(100.0, 1000.0, 400.0, 700.0)
    assert share == pytest.approx(0.4) and gain == pytest.approx(60.0 + 300.0) and basis == "estimate+disclosed"


def test_blend_uses_disclosed_only_when_rental_covers_holdings():
    assert blend_disclosed_gain(100.0, 300.0, 400.0, 700.0) == (300.0, 1.0, "disclosed")
    assert blend_disclosed_gain(100.0, None, 400.0, 700.0) == (300.0, 1.0, "disclosed")
    assert blend_disclosed_gain(None, 1000.0, 400.0, 300.0) == (-100.0, pytest.approx(0.4), "disclosed")


def test_blend_ignores_implausible_disclosure():
    lo, hi = FV_BV_RANGE
    assert blend_disclosed_gain(100.0, 1000.0, 100.0, 100.0 * hi * 1.1) == (100.0, None, "estimate")
    assert blend_disclosed_gain(100.0, 1000.0, 100.0, 100.0 * lo * 0.9) == (100.0, None, "estimate")
    assert blend_disclosed_gain(100.0, 1000.0, 0.0, 50.0) == (100.0, None, "estimate")
