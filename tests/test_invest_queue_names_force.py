"""審議キュー CLI の会社名（日本語）付与・due --force、パケット見出しの会社名、fundamental_packet の DB 取得の受入テスト。
BigQuery には接続せず、クエリ結果を差し替えて検証する。"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

import invest_queue as iq  # noqa: E402


class _Row(dict):
    def items(self):
        return super().items()


class _Job:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class FakeClient:
    project = "p"

    def __init__(self, master_rows):
        self.master_rows = master_rows
        self.sqls = []

    def query(self, sql, job_config=None, location=None):
        self.sqls.append(sql)
        return _Job(self.master_rows)


def test_japanese_names_maps_sec_code_to_ticker(monkeypatch):
    c = FakeClient([_Row(sec_code="64480", name="ブラザー工業株式会社"), _Row(sec_code="285A0", name="キオクシア")])
    monkeypatch.setattr(iq, "_q", lambda client, sql, params=None: client.query(sql))
    out = iq.japanese_names(c, ["6448.T", "285A.T"])
    assert out == {"6448.T": "ブラザー工業株式会社", "285A.T": "キオクシア"}


def test_japanese_names_failure_returns_empty(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bq down")
    monkeypatch.setattr(iq, "_q", boom)
    assert iq.japanese_names(FakeClient([]), ["6448.T"]) == {}


def test_due_force_ignores_interval():
    out = iq.due(None, interval_days=6, force=True)
    assert out["due"] is True and out["forced"] is True


def test_due_cli_accepts_force(monkeypatch, capsys):
    monkeypatch.setattr(iq, "_client", lambda: None)
    assert iq.main(["due", "--force"]) == 0
    assert '"due": true' in capsys.readouterr().out


def test_packet_header_has_company_name(monkeypatch, tmp_path):
    import agent_packet_exporter as ape
    monkeypatch.setattr(ape, "_company_name", lambda t: "ブラザー工業株式会社")
    for fn in ("collect_screener", "collect_sector_context", "collect_midterm", "collect_risk_signals",
               "collect_alt_policy", "collect_ontology_events", "collect_valuation", "collect_freshness",
               "collect_holdings", "collect_existing_notes"):
        monkeypatch.setattr(ape, fn, lambda *a, **k: [])
    md = ape.build_packet(type("C", (), {"DATA_DIR": tmp_path})(), "6448.T")
    assert "# 審議パケット: ブラザー工業株式会社（6448.T）" in md
    assert 'company: "ブラザー工業株式会社"' in md


_TAIL = Path("Vault2_ClaudeCode_Local/CEO室/departments/B-investment/tools/fundamental_packet.py")
FP = next((b / _TAIL for b in (Path.home() / "Documents" / "Obsidian_Local", Path.home() / "mnt" / "Obsidian_Local")
           if (b / _TAIL).exists()), Path("/nonexistent/fundamental_packet.py"))


@pytest.mark.skipif(not FP.exists(), reason="vault の fundamental_packet.py が無い環境")
def test_fundamental_packet_prefers_db(monkeypatch):
    spec = importlib.util.spec_from_file_location("fundamental_packet", FP)
    fp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fp)
    from datetime import date, datetime
    fin = [{"fiscal_year": y, "fiscal_year_end": date(y, 3, 31), "net_sales": 1e11 * (y - 2020),
            "operating_income": 1e10, "net_income": 7e9, "total_assets": 2e11, "equity": 1e11,
            "interest_bearing_debt": 1e9, "cash_and_equiv": 3e10, "operating_cf": 1.5e10,
            "investing_cf": -5e9, "capex": 5e9, "depreciation": 4e9, "updated_at": datetime(2026, 10, 3)}
           for y in range(2022, 2027)]

    def fake_rows(client, sql, params=None):
        if "company_master" in sql:
            return [{"edinet_code": "E1", "name": "テスト工業株式会社", "name_en": "TEST", "industry": "機械",
                     "accounting_standard": "JP"}]
        if "edinet.financials" in sql:
            return fin
        if "financial_items_q" in sql:
            return [{"fiscal_year": 2027, "quarter": "1", "item": "revenue", "value": 3e10},
                    {"fiscal_year": 2027, "quarter": "1", "item": "operating_income", "value": 3e9}]
        if "financial_items" in sql:
            return [{"fiscal_year": 2026, "item": "shares_issued", "value": 1e8},
                    {"fiscal_year": 2026, "item": "treasury_shares_count", "value": 1e7},
                    {"fiscal_year": 2026, "item": "dividend_per_share", "value": 50.0}]
        if "price_daily_latest" in sql:
            return [{"trade_date": date(2026, 11, 12), "close": 1000.0}]
        return []
    monkeypatch.setattr(fp, "_bq_client", lambda: object())
    monkeypatch.setattr(fp, "_bq_rows", fake_rows)
    d = fp.build("1234", "1234.T")
    assert d["ok"] and d["company"] == "テスト工業株式会社"
    assert len(d["rows"]) == 5 and d["rows"][-1]["sales"] == 6000.0       # 億円換算
    assert d["rows"][-1]["capex"] == -50.0 and d["rows"][-1]["fcf"] == 100.0
    assert d["mcap"] == pytest.approx(1000.0 * 9e7 / 1e8)                 # 終値×(発行済−自己株式)
    assert d["quarters"][0]["revenue"] == 300.0
    md = fp.md_fund(d) + fp.md_kpi(d)
    assert "テスト工業株式会社" in md and "BigQuery" in md and "要AI補完" in md  # F4 は残る
    assert "**取得不可**" not in md
