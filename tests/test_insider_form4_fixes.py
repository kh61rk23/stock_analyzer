from datetime import datetime

import pandas as pd

import us_insider_form4_scraper as u


def test_raw_form4_doc_strips_xsl_folder():
    assert u.raw_form4_doc("xslF345X06/wk-form4_1791579847.xml") == "wk-form4_1791579847.xml"
    assert u.raw_form4_doc("form4.xml") == "form4.xml"
    assert u.raw_form4_doc("") == ""


XML = """<?xml version="1.0"?><ownershipDocument>
<reportingOwner><reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>CFO</officerTitle></reportingOwnerRelationship></reportingOwner>
<nonDerivativeTable><nonDerivativeTransaction>
<transactionDate><value>2026-10-01</value></transactionDate>
<transactionCoding><transactionFormType>4</transactionFormType><transactionCode>S</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>1000</value></transactionShares><transactionPricePerShare><value>100</value></transactionPricePerShare></transactionAmounts>
</nonDerivativeTransaction><nonDerivativeTransaction>
<transactionDate><value>2026-10-02</value></transactionDate>
<transactionCoding><transactionCode>A</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>500</value></transactionShares></transactionAmounts>
</nonDerivativeTransaction></nonDerivativeTable></ownershipDocument>"""


def test_parse_reads_transaction_code():
    p = u._parse_form4_xml(XML)
    assert [t["type"] for t in p["transactions"]] == ["S"]


def test_yf_sales_are_not_counted_as_buys():
    now = datetime(2026, 10, 19)
    df = pd.DataFrame([
        {"Shares": 1000, "Value": 5_000_000, "Text": "Sale at price 220 per share.", "Position": "Chief Executive Officer", "Start Date": pd.Timestamp("2026-10-01")},
        {"Shares": 500, "Value": 0, "Text": "Stock Award(Grant) at price 0.00 per share.", "Position": "Director", "Start Date": pd.Timestamp("2026-10-02")},
        {"Shares": 300, "Value": 30_000, "Text": "Purchase at price 100 per share.", "Position": "Director", "Start Date": pd.Timestamp("2025-01-01")},
    ])
    s = u.yf_transactions_summary(df, days=30, now=now)
    assert s["sell_count"] == 1 and s["buy_count"] == 0
    assert s["insider_bonus"] < 0


def test_yf_empty_is_neutral():
    assert u.yf_transactions_summary(pd.DataFrame(), days=30) == {"insider_bonus": 0.0, "buy_count": 0, "sell_count": 0}
