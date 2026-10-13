from datetime import date

from backtest import factor_ic as F


def test_spearman_and_ties():
    assert F.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert F.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0
    assert F.spearman([1, 1, 1], [1, 2, 3]) is None
    assert abs(F.spearman([1, 2, 2, 3], [1, 3, 2, 4]) - 0.9487) < 1e-3


def test_nw_tstat_shrinks_with_autocorrelation():
    import math
    xs = [0.03 + 0.05 * math.sin(i / 6) for i in range(60)]      # ゆっくり動く（正の自己相関）系列
    assert F.nw_tstat(xs, 11) < F.nw_tstat(xs, 0)


def test_rows_used_only_after_disclosure_and_not_stale():
    panel = [{"code4": "1234", "fiscal_year": "2023", "disclosure_date": "2023-06-25", "fiscal_year_end": "2023-03-31"},
             {"code4": "1234", "fiscal_year": "2024", "disclosure_date": "", "fiscal_year_end": "2024-03-31"}]
    assert F.latest_rows(panel, date(2023, 6, 24)) == {}
    assert F.latest_rows(panel, date(2023, 7, 31))["1234"]["fiscal_year"] == "2023"
    # 2024年度は提出日が無いので決算期末＋3か月（2024-07-01）から
    assert F.latest_rows(panel, date(2024, 6, 30))["1234"]["fiscal_year"] == "2023"
    assert F.latest_rows(panel, date(2024, 7, 31))["1234"]["fiscal_year"] == "2024"
    assert F.latest_rows(panel[:1], date(2025, 1, 31)) == {}     # 提出から15か月超


def test_mcap_restores_later_splits():
    """yfinance の終値は後の分割で遡って割られるため、期末より後の分割倍率を掛け戻す。"""
    row = {"float_shares": "1000000", "equity": "2e9", "gain_m_yen": "1000", "fiscal_year_end": "2023-03-31",
           "cash_and_equiv": "5e8", "interest_bearing_debt": "1e8"}
    f = F.factors_for(row, 400.0, [("2024-04-01", 5.0), ("2022-01-01", 2.0)])
    assert f["mcap"] == 400.0 * 1e6 * 5
    after = 1e9 * (1 - 0.306)
    assert abs(f["adj_pbr"] - 2e9 / (2e9 + after)) < 1e-12
    assert abs(f["asset_value_ratio"] - (4e8 + after) / 2e9) < 1e-12
    assert F.factors_for({**row, "gain_m_yen": "1e7"}, 400.0, []) is None     # 含み益が時価総額の3倍超


def test_month_helpers():
    assert F.month_ends(date(2024, 1, 15), date(2024, 3, 31)) == [date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 31)]
    assert F.add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert F.add_months(date(2024, 11, 30), 12) == date(2025, 11, 30)
    s = [("2024-01-31", 100.0, 90.0), ("2024-02-29", 110.0, 99.0)]
    assert F.price_at(s, date(2024, 3, 5))[0] == "2024-02-29"
    assert F.price_at(s, date(2024, 3, 31)) is None
