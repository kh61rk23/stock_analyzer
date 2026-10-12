from types import SimpleNamespace

from real_estate_signal import calc_real_estate_bonus


CFG = SimpleNamespace(RE_BONUS_MIN=-2.0, RE_BONUS_MAX=3.0)


def test_missing_data_returns_zero():
    assert calc_real_estate_bonus([], CFG) == 0.0
    assert calc_real_estate_bonus([{"facility_name": "本社"}], CFG) == 0.0


def test_hidden_asset_uses_consistent_million_yen_unit():
    rows = [{
        "land_area_m2": 10_000,
        "commercial_land_price_per_m2": 300_000,
        "book_value_land_m_yen": 1_000,
        "book_value_buildings_m_yen": 0,
    }]
    bonus = calc_real_estate_bonus(rows, CFG)
    assert 2.8 < bonus <= 3.0


def test_area_risk_accepts_percentage_and_clips_range():
    rows = [{
        "book_value_land_m_yen": 100,
        "book_value_buildings_m_yen": 100,
        "flood_risk_area_pct": 80,
        "population_change_pct": -50,
    }]
    bonus = calc_real_estate_bonus(rows, CFG)
    assert -2.0 <= bonus <= 0.0
    assert bonus == -1.3


def test_total_bonus_always_within_contract():
    rows = [{
        "land_area_m2": 1_000_000,
        "commercial_land_price_per_m2": 5_000_000,
        "book_value_land_m_yen": 1,
        "book_value_buildings_m_yen": 1,
        "flood_risk_area_pct": 100,
        "population_change_pct": -100,
    }]
    assert -2.0 <= calc_real_estate_bonus(rows, CFG) <= 3.0


from real_estate_signal import calc_stock_real_estate_bonus, stock_valuation, summarize_holdings


def test_unbooked_facility_does_not_inflate_gain():
    """簿価の無い施設の時価は含み益に入れない（BIマートと同じ定義）。"""
    booked = {"land_area_m2": 1_000, "commercial_land_price_per_m2": 200_000, "book_value_land_m_yen": 100}
    unbooked = {"land_area_m2": 100_000, "commercial_land_price_per_m2": 1_000_000}
    s = summarize_holdings([booked, unbooked])
    assert s["unrealized_gain_m_yen"] == 100.0          # 200 − 100
    assert s["land_value_est_m_yen"] == 100_200.0       # 時価の目安は両方の合計
    assert calc_real_estate_bonus([booked], CFG) == calc_real_estate_bonus([booked, unbooked], CFG)


def test_facility_over_20x_book_is_excluded():
    """本社欄に全社の面積が入った施設（簿価の20倍超）は時価・含み益から除く。"""
    hq = {"land_area_m2": 5_690_000, "commercial_land_price_per_m2": 3_900_000, "book_value_land_m_yen": 1_000}
    ok = {"land_area_m2": 1_000, "commercial_land_price_per_m2": 200_000, "book_value_land_m_yen": 100}
    s = summarize_holdings([hq, ok])
    assert s["excluded_count"] == 1 and s["unrealized_gain_m_yen"] == 100.0


def test_reference_price_prefers_industrial_then_commercial_then_residential():
    row = {"land_area_m2": 1_000, "industrial_land_price_per_m2": 50_000,
           "commercial_land_price_per_m2": 500_000, "book_value_land_m_yen": 10}
    assert summarize_holdings([row])["unrealized_gain_m_yen"] == 40.0
    row2 = {"land_area_m2": 1_000, "residential_land_price_per_m2": 80_000, "book_value_land_m_yen": 10}
    assert summarize_holdings([row2])["unrealized_gain_m_yen"] == 70.0


def test_stock_valuation_matches_mart_formula():
    """adj_pbr = 時価総額 ÷（純資産＋税引後含み益）、asset_value_ratio =（現金−負債＋税引後含み益）÷ 時価総額。"""
    s = {"unrealized_gain_m_yen": 10_000}                  # 含み益 100億円
    v = stock_valuation(s, market_cap=20e9, pbr=1.25, total_cash=5e9, total_debt=1e9)
    after_tax = 10e9 * (1 - 0.306)
    assert v["adj_pbr"] == round(20e9 / (16e9 + after_tax), 4)
    assert v["asset_value_ratio"] == round((4e9 + after_tax) / 20e9, 4)
    assert stock_valuation({"unrealized_gain_m_yen": 70_000}, 20e9, 1.0)["flag"] == "gt_3x_mcap"


def test_stock_bonus_rewards_hidden_cheap_stock():
    holdings = [{"land_area_m2": 10_000, "commercial_land_price_per_m2": 1_500_000, "book_value_land_m_yen": 1_000}]
    cheap = calc_stock_real_estate_bonus(holdings, {"market_cap": 20e9, "pbr": 1.1,
                                                    "total_cash": 5e9, "total_debt": 0}, CFG)
    pricey = calc_stock_real_estate_bonus(holdings, {"market_cap": 500e9, "pbr": 4.0,
                                                     "total_cash": 5e9, "total_debt": 0}, CFG)
    assert cheap["basis"] == "valuation" and cheap["adj_pbr"] < 1.0
    assert cheap["bonus"] > pricey["bonus"] >= 0.0
    assert -2.0 <= cheap["bonus"] <= 3.0


def test_stock_bonus_falls_back_without_market_cap():
    holdings = [{"land_area_m2": 10_000, "commercial_land_price_per_m2": 300_000, "book_value_land_m_yen": 1_000}]
    r = calc_stock_real_estate_bonus(holdings, {}, CFG)
    assert r["basis"] == "gain_rate" and r["bonus"] == calc_real_estate_bonus(holdings, CFG)


def test_no_pbr_credit_when_land_is_underwater():
    holdings = [{"land_area_m2": 1_000, "commercial_land_price_per_m2": 50_000, "book_value_land_m_yen": 100,
                 "book_value_buildings_m_yen": 0}]
    r = calc_stock_real_estate_bonus(holdings, {"market_cap": 1e9, "pbr": 0.5}, CFG)
    assert r["adj_pbr"] < 1.0 and r["bonus"] == 0.0
