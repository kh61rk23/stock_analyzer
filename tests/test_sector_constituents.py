import sector_constituents as sc
from base_agent import calc_composite_score


def test_group_rows_sorts_by_mcap_and_drops_bad_rows():
    rows = [{"sector_code": "1016", "ticker": "A.T", "market_cap": 1e11},
            {"sector_code": "1016", "ticker": "B.T", "market_cap": 5e12},
            {"sector_code": "1016", "ticker": "C.T", "market_cap": 0},
            {"sector_code": None, "ticker": "D.T", "market_cap": 1e12}]
    g = sc.group_rows(rows)
    assert [r["ticker"] for r in g["1016"]] == ["B.T", "A.T"] and list(g) == ["1016"]


def test_weighted_skips_missing_values():
    rows = [{"market_cap": 3e12, "roe": 0.20}, {"market_cap": 1e12, "roe": 0.0},
            {"market_cap": 6e12, "roe": None}]
    assert abs(sc.weighted(rows, "roe") - 0.15) < 1e-12
    f = sc.fundamentals(rows)
    assert f["n"] == 3 and f["mcap_total"] == 1e13 and f["roa"] is None


def test_sector_fundamentals_override_representative_average():
    """代表銘柄の値が悪くても、業種全体の時価総額加重が良ければそちらで採点する。"""
    weak = [{"fetch_ok": True, "roe": 0.01, "roa": 0.0, "op_margin": 0.01, "market_cap": 1e10}]
    base, _ = calc_composite_score(weak, [])
    good, _ = calc_composite_score(weak, [], sector_fundamentals={
        "roe": 0.12, "roa": 0.06, "op_margin": 0.11, "accrual_ratio": -0.03})
    assert good - base == 15 + 10 + 15 + 8
    # 代表銘柄が1社も取れなくても業種全体の財務で採点できる
    only, _ = calc_composite_score([], [], sector_fundamentals={"roe": 0.12, "roa": None, "op_margin": None, "accrual_ratio": None})
    assert only - calc_composite_score([], [])[0] == 15


def test_analyst_average_is_mcap_weighted():
    cos = [{"fetch_ok": True, "rec_mean": 1.5, "market_cap": 9e12},
           {"fetch_ok": True, "rec_mean": 4.0, "market_cap": 1e12}]
    s, _ = calc_composite_score(cos, [])
    s_eq, _ = calc_composite_score([{**c, "market_cap": None} for c in cos], [])
    assert s - s_eq == 5     # 加重平均1.75→+5点、単純平均2.75→0点
