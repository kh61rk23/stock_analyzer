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


from real_estate_signal import stock_bonus, summarize_holdings


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


def test_stock_bonus_rewards_hidden_cheap_stock():
    cheap, basis = stock_bonus(0.6, 1.2, 5e10, None, 0.1, CFG)
    pricey, _ = stock_bonus(1.8, 0.05, 5e10, None, 0.1, CFG)
    assert basis == "valuation"
    assert cheap == round(1.5 * 0.8 + 1.5 - 0.1, 4)
    assert cheap > pricey and -2.0 <= pricey <= 3.0


def test_no_pbr_credit_when_land_is_underwater():
    bonus, _ = stock_bonus(0.5, None, -1e9, None, 0.0, CFG)
    assert bonus == 0.0


def test_flagged_or_missing_valuation_uses_area_risk_only():
    assert stock_bonus(None, None, 9e12, "gt_3x_mcap", 0.4, CFG) == (-0.4, "area_only")
    assert stock_bonus(None, None, None, None, 0.3, CFG) == (-0.3, "area_only")
    assert stock_bonus(None, None, None, None, None, CFG) == (0.0, "none")
    assert stock_bonus(0.2, 5.0, 1e12, None, 0.0, CFG)[0] == 3.0
