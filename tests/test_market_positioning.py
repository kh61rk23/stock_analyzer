"""market_positioning: FINRA の空売り残・CFTC の建玉明細の行づくりと、取り込む月・週の決め方。"""
from datetime import date

import market_positioning as mp


def test_short_rows_drop_otc():
    recs = [{"settlementDate": "2026-09-30", "symbolCode": "A", "issueName": "Agilent", "marketClassCode": "NYSE",
             "currentShortPositionQuantity": 5, "previousShortPositionQuantity": 4, "changePercent": 25.0,
             "averageDailyVolumeQuantity": 2, "daysToCoverQuantity": 2.5},
            {"settlementDate": "2026-09-30", "symbolCode": "XYZQ", "marketClassCode": "OTC"}]
    rows = mp.short_rows(recs, "now")
    assert [r["symbol"] for r in rows] == ["A"] and rows[0]["short_qty"] == 5.0 and rows[0]["days_to_cover"] == 2.5


def test_cot_rows_keep_numbers_in_json():
    rec = {"report_date_as_yyyy_mm_dd": "2026-10-20T00:00:00.000", "cftc_contract_market_code": "099741 ",
           "market_and_exchange_names": "EURO FX", "commodity_name": "CURRENCY", "open_interest_all": "700000",
           "lev_money_positions_long": "120000", "yyyy_report_week_ww": "2026 Report Week 42"}
    r = mp.cot_rows([rec], "tff", "now")[0]
    assert (r["report_date"], r["market_code"], r["open_interest"]) == ("2026-10-20", "099741", 700000.0)
    assert r["positions"]["lev_money_positions_long"] == 120000.0 and "yyyy_report_week_ww" not in r["positions"]


def test_periods_recent_always_then_missing():
    st = {"months": ["2026-08-01", "2026-07-01"], "cot": {"legacy": ["2026-10-12"]}}
    ms = mp.months_to_run(st, date(2026, 10, 24), 2, date(2026, 7, 1))
    assert ms == [date(2026, 10, 1), date(2026, 9, 1)]          # 直近2か月は毎回・8月7月は済み
    ws = mp.weeks_to_run(st, "legacy", date(2026, 10, 24), 1, date(2026, 10, 5))
    assert ws == [date(2026, 10, 19), date(2026, 10, 5)]         # 10/12 は済み
