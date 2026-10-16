"""オントロジー：全上場を土台にした企業エンティティ・注意項目のシグナル・書き込みの形。"""
from datetime import date
from types import SimpleNamespace

import ontology_exporter as oe


def test_company_entities_cover_listed_and_overlay_portfolio(tmp_path):
    cfg = SimpleNamespace(DATA_DIR=tmp_path)
    listed = [{"ticker": "7203.T", "company_name": "トヨタ自動車", "market": "JP", "sector_code": "1017"},
              {"ticker": "130A.T", "company_name": "新規", "market": "JP", "sector_code": "1029"},
              {"ticker": "MSFT", "company_name": "Microsoft", "market": "US", "sector_code": "7372"}]
    rows = {r["ticker"]: r for r in oe.build_company_entities(cfg, listed)}
    assert {"7203.T", "130A.T", "MSFT"} <= set(rows)
    assert rows["7203.T"]["sector_code"] == "1017" and rows["MSFT"]["market"] == "US"


class _Client:
    project = "p"

    def __init__(self, rows):
        self.rows, self.sql = rows, []

    def query(self, sql):
        self.sql.append(sql)
        return SimpleNamespace(result=lambda: self.rows)


def test_alert_items_become_signal_events_with_stable_ids():
    r = SimpleNamespace(report_date=date(2026, 10, 15), section_key="buyback", severity="medium",
                        ticker="6143.T", market="JP", line="💴 ソディック（6143.T）: 自己株式取得")
    d = SimpleNamespace(report_date=date(2026, 10, 15), section_key="earnings_surprise_down", severity="medium",
                        ticker="6902.T", market="JP", line="🔴 デンソー（6902.T）")
    ev = oe.alert_signal_events(_Client([r, d]))
    assert [e["signal_category"] for e in ev] == ["alert_buyback", "alert_earnings_surprise_down"]
    assert ev[0]["fired_at"] == "2026-10-15" and ev[1]["direction"] == "bearish"
    assert ev[0]["event_id"] == oe.alert_signal_events(_Client([r]))[0]["event_id"]
