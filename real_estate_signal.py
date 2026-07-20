"""Score corporate property data for the base-agent composite score."""
from __future__ import annotations

import math


def calc_real_estate_bonus(estimated_land_value, market_value, book_value,
                           flood_risk=0.0, population_decline=0.0) -> float:
    """Return D-②-2 bonus in [-2, +3]; absent/invalid data returns neutral 0."""
    try:
        book = float(book_value)
        if book <= 0:
            return 0.0
        hidden_asset = max(0.0, min(3.0, math.tanh((float(estimated_land_value) - book) / book) * 3.0))
        area_risk = -max(0.0, min(2.0, float(flood_risk) + float(population_decline)))
        return round(max(-2.0, min(3.0, hidden_asset + area_risk)), 2)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def get_real_estate_bonus(company_code: str) -> float:
    try:
        from edinet_bq_client import EDINETBQClient
        bq = EDINETBQClient()
        code = str(company_code).replace("'", "")
        sql = f"""
        SELECT h.estimated_land_value_jpy, h.book_value_jpy,
               AVG(COALESCE(p.flood_risk_score, 0)) flood, AVG(COALESCE(p.population_decline_score, 0)) pop
        FROM `{bq.project}.real_estate.corporate_holdings` h
        LEFT JOIN `{bq.project}.real_estate.area_profile` p
          ON p.area_code = CONCAT(COALESCE(h.prefecture,''), COALESCE(h.municipality,''))
        WHERE h.company_code = '{code}'
        GROUP BY h.estimated_land_value_jpy, h.book_value_jpy
        ORDER BY h.updated_at DESC LIMIT 1"""
        frame = bq.query_to_df(sql)
        if frame.empty:
            return 0.0
        r = frame.iloc[0]
        return calc_real_estate_bonus(r.estimated_land_value_jpy, r.estimated_land_value_jpy,
                                      r.book_value_jpy, r.flood, r.pop)
    except Exception:
        return 0.0
