"""企業保有不動産から含み資産・エリアリスク補正を算出する純関数。"""

from __future__ import annotations

import math


def _number(value) -> float | None:
    try:
        return float(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _fraction(value) -> float:
    """APIの百分率(例: 12.5)と比率(例: 0.125)を同じ比率へ揃える。"""
    number = _number(value)
    if number is None:
        return 0.0
    return number / 100.0 if abs(number) > 1.0 else number


def calc_real_estate_bonus(holdings: list[dict], cfg) -> float:
    """確定済みP1式を適用し ``RE_BONUS_MIN..MAX`` に収める。

    金額はBQ契約どおり百万円、土地単価は円/m2のため、推定時価を百万円へ
    揃えてから簿価と比較する。算出可能な保有不動産が無ければ0.0。
    """
    valid = [row for row in (holdings or []) if isinstance(row, dict)]
    if not valid:
        return 0.0

    market_land_m_yen = 0.0
    book_land_m_yen = 0.0
    weighted_flood = 0.0
    weighted_population_decline = 0.0
    total_book_value = 0.0
    has_signal_data = False

    for row in valid:
        land_area = _number(row.get("land_area_m2"))
        land_price = _number(row.get("commercial_land_price_per_m2"))
        book_land = max(0.0, _number(row.get("book_value_land_m_yen")) or 0.0)
        book_building = max(
            0.0, _number(row.get("book_value_buildings_m_yen")) or 0.0
        )
        book_total = book_land + book_building

        if land_area is not None and land_price is not None:
            market_land_m_yen += max(0.0, land_area) * max(0.0, land_price) / 1_000_000.0
            has_signal_data = True
        if book_land > 0:
            has_signal_data = True
        book_land_m_yen += book_land

        if book_total > 0:
            flood = max(0.0, _fraction(row.get("flood_risk_area_pct")))
            population_change = _number(row.get("population_change_pct"))
            population_decline = (
                max(0.0, -population_change) / 100.0
                if population_change is not None else 0.0
            )
            weighted_flood += book_total * flood
            weighted_population_decline += book_total * population_decline
            total_book_value += book_total
            if flood > 0 or population_decline > 0:
                has_signal_data = True

    if not has_signal_data:
        return 0.0

    hidden_rate = (market_land_m_yen - book_land_m_yen) / max(1.0, book_land_m_yen)
    hidden_asset = max(0.0, min(3.0, math.tanh(hidden_rate) * 3.0))
    if total_book_value > 0:
        area_risk_raw = (
            weighted_flood + weighted_population_decline
        ) / total_book_value
    else:
        area_risk_raw = 0.0
    area_risk = -max(0.0, min(2.0, area_risk_raw))

    minimum = float(getattr(cfg, "RE_BONUS_MIN", -2.0))
    maximum = float(getattr(cfg, "RE_BONUS_MAX", 3.0))
    return round(max(minimum, min(maximum, hidden_asset + area_risk)), 4)
