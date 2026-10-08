"""seisakudb_ingestor: 応答の取り出し・正規化・取り直し対象の判定。"""
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import seisakudb_ingestor as si  # noqa: E402


def test_results_of_accepts_count_results_and_list():
    assert si.results_of({"count": 1, "results": [{"a": 1}]}) == [{"a": 1}]
    assert si.results_of([{"a": 1}, "x"]) == [{"a": 1}]
    assert si.results_of({"error": "x"}) == []


def test_normalize_picks_known_fields_and_keeps_raw():
    rec = {"sec_code": "7011", "company_name": "三菱重工業", "fiscal_year": "2025", "amount": "1,200,000",
           "funding_type": "調達", "ministry": "防衛省", "title": "X", "date": "2026-04-01"}
    r = si.normalize(rec, "funding_recent", None, "2026-10-08T00:00:00+00:00")
    assert (r["company_code"], r["fiscal_year"], r["amount_yen"], r["kind"], r["ministry"]) == \
        ("7011", 2025, 1200000.0, "調達", "防衛省")
    assert r["event_date"] == "2026-04-01"
    assert json.loads(r["record_json"]) == rec


def test_normalize_rejects_non_iso_date_and_uses_given_code():
    r = si.normalize({"date": "令和7年", "amount": None}, "company_public_funding", "8306", "t")
    assert r["event_date"] is None and r["company_code"] == "8306" and r["amount_yen"] is None


def test_due_companies_respects_ttl_and_order():
    meta = {"7011": {"fetched": "2026-10-01"}, "8306": {"fetched": "2026-08-01"}}
    assert si.due_companies(["7011", "8306", "9432", "8306"], meta, date(2026, 10, 8), 30) == ["8306", "9432"]


def test_run_skips_without_key(monkeypatch):
    monkeypatch.delenv("SEISAKUDB_API_KEY", raising=False)
    assert si.run(object(), ["7011.T"])["skipped"] is True
