"""2026-10-06 の修正（出口戦略の取得単価・金利の取得元・13F差分・メールのファイルリンク）。"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import file_links  # noqa: E402
import institutional_13f_monitor as m13  # noqa: E402
import rates_monitor as rm  # noqa: E402
import sync_holdings as sh  # noqa: E402


def test_cost_from_trades_moving_average_excludes_realized_gain():
    # 三菱UFJの実例: 特定100株を売却（益74,700円）・NISAで2回買付 → NISA の原価は 284,000円
    tx = [
        {"Date": "2023-04-11", "Name": "MUFG", "Asset_Class": "日本株", "Action": "株式現物買",
         "Account": "特定", "Quantity": "100", "Total_JPY": "85300"},
        {"Date": "2024-01-15", "Name": "MUFG", "Asset_Class": "日本株", "Action": "株式現物買",
         "Account": "NISA", "Quantity": "100", "Total_JPY": "128950"},
        {"Date": "2024-05-13", "Name": "MUFG", "Asset_Class": "日本株", "Action": "株式現物売",
         "Account": "特定", "Quantity": "100", "Total_JPY": "160000"},
        {"Date": "2024-05-16", "Name": "MUFG", "Asset_Class": "日本株", "Action": "株式現物買",
         "Account": "NISA", "Quantity": "100", "Total_JPY": "155050"},
    ]
    pos = sh.cost_from_trades(tx)
    assert pos[("MUFG", "NISA")] == (200.0, 284000.0)
    assert pos[("MUFG", "特定")][0] == 0


def test_convert_uses_trade_cost_when_quantity_matches():
    rows = [{"Code": "8306:XJP", "Name": "MUFG", "Asset_Class": "日本株", "Account": "NISA",
             "Quantity": "200", "Total_Cost_JPY": "209300", "Current_Price": "3652",
             "Market_Value_JPY": "730400", "Price_Date": "2026-09-17"}]
    out, _ = sh.convert(rows, trades={("MUFG", "NISA"): (200.0, 284000.0)})
    assert out[0]["avg_cost_jpy"] == 1420.0
    assert out[0]["excel_avg_cost_jpy"] == 1046.5
    assert out[0]["account_type"] == "NISA"


def test_price_in_jpy_converts_only_us():
    assert sh.price_in_jpy({"ticker": "NVDA", "market": "US"}, 200.0, 150.0) == 30000.0
    assert sh.price_in_jpy({"ticker": "8306.T", "market": "JP"}, 3652.0, 150.0) == 3652.0
    assert sh.price_in_jpy({"ticker": "NVDA"}, None, 150.0) is None


def test_parse_ust_csv():
    text = ('Date,"1 Mo","3 Yr","5 Yr","10 Yr"\n'
            "10/05/2026,4.05,4.97,5.06,5.31\n10/02/2026,4.04,4.96,5.06,5.28\n")
    s = rm.parse_ust_csv(text)
    assert s["10年"] == [(date(2026, 10, 2), 5.28), (date(2026, 10, 5), 5.31)]
    assert rm.change_summary(s["10年"])["d1"] == 0.03


def test_parse_fred_json_skips_missing():
    t = '{"observations":[{"date":"2026-09-17","value":"4.00"},{"date":"2026-09-18","value":"."}]}'
    assert rm.parse_fred_json(t) == [(date(2026, 9, 17), 4.0)]


def test_13f_filings_skip_amendments_and_duplicate_periods():
    subs = {"filings": {"recent": {
        "form": ["13F-HR/A", "13F-HR", "13F-HR", "13F-HR"],
        "accessionNumber": ["a", "b", "c", "d"],
        "reportDate": ["2026-06-30", "2026-06-30", "2026-06-30", "2026-03-31"]}}}
    assert m13._get_13f_filings(subs, 2) == [("b", "2026-06-30"), ("d", "2026-03-31")]
    assert m13._period_to_quarter("2026-06-30") == "2026Q2"


def test_diff_holdings_compares_previous_filing_not_empty():
    prev = [{"cusip": "A", "shares": 100, "value_usd": 10, "name": "a"},
            {"cusip": "B", "shares": 100, "value_usd": 10, "name": "b"}]
    curr = [{"cusip": "A", "shares": 105, "value_usd": 20, "name": "a"},   # 株数+5% → 変化なし
            {"cusip": "C", "shares": 50, "value_usd": 5, "name": "c"}]
    d = {c["cusip"]: c["change_type"] for c in m13.diff_holdings(curr, prev, {"name": "x", "tier": 1})}
    assert d == {"B": "sold", "C": "new_position"}


def test_infotable_thousands_detected_by_price():
    xml = ("<informationTable><infoTable><nameOfIssuer>APPLE</nameOfIssuer><cusip>037833100</cusip>"
           "<value>46243</value><shrsOrPrnAmt><sshPrnamt>159811</sshPrnamt></shrsOrPrnAmt></infoTable>"
           "</informationTable>")
    r = m13.parse_infotable_xml(xml)
    assert r[0]["value_usd"] == 46243000


def test_linkify_only_known_files(tmp_path):
    p = tmp_path / "screener_2026-10-07.md"
    p.write_text("x")
    html = file_links.linkify("保存: screener_2026-10-07.md / other_2026-10-07.md", {p.name: p})
    assert html.count("<a href=") == 1
    assert "other_2026-10-07.md" in html


def test_drive_search_url_quotes_name():
    assert file_links.drive_search_url("a b.md").endswith("%22a%20b.md%22")
