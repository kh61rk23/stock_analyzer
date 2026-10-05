# 2026-10-05: 月次メール
from pathlib import Path

import monthly_mail as mm


def test_build_collects_reports(tmp_path):
    (tmp_path / "strategy_review_202609.md").write_text("# 月次戦略レビュー\n| a | b |\n|---|---|\n| 1 | **2** |\n- 項目", encoding="utf-8")
    (tmp_path / "commodity_alloc_202610.md").write_text("# コモディティ", encoding="utf-8")
    subject, body, n = mm.build("202609", tmp_path)
    assert n == 2 and "2026-09" in subject
    assert "<table" in body and "<b>2</b>" in body and "・項目" in body


def test_monthly_runner_has_mail_step_last():
    import monthly_runner as mr
    from datetime import date
    steps = mr.build_steps(None, "202609", date(2026, 10, 2))
    assert steps[-1][0] == "monthly_mail"


def test_13f_infotable_parse():
    import institutional_13f_monitor as m
    xml = ('<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">'
           '<infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><cusip>037833100</cusip><value>1000</value>'
           '<shrsOrPrnAmt><sshPrnamt>10</sshPrnamt></shrsOrPrnAmt></infoTable>'
           '<infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><cusip>037833100</cusip><value>500</value>'
           '<shrsOrPrnAmt><sshPrnamt>5</sshPrnamt></shrsOrPrnAmt></infoTable></informationTable>')
    r = m.parse_infotable_xml(xml)
    assert r == [{"name": "APPLE INC", "cusip": "037833100", "value_usd": 1500, "shares": 15, "ticker": ""}]
    assert m.parse_infotable_xml(xml, value_in_thousands=True)[0]["value_usd"] == 1500000


def test_holding_value_jpy():
    from sync_holdings import holding_value_jpy
    assert holding_value_jpy({"market": "US", "quantity": 2, "avg_cost_jpy": 5000}, 10.0, 150.0) == 3000.0
    assert holding_value_jpy({"market": "US", "quantity": 2, "avg_cost_jpy": 5000}, None, 150.0) == 10000.0
    assert holding_value_jpy({"market": "US", "quantity": 2, "market_value_jpy": 7000}, None, 150.0) == 7000.0
