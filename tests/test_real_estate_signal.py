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
