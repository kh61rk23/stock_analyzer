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


def test_parse_date_iso_rfc1123_and_invalid():
    assert si.parse_date("2026-04-01T00:00:00+09:00") == "2026-04-01"
    assert si.parse_date("Thu, 14 May 2026 00:00:00 GMT") == "2026-05-14"
    assert si.parse_date("Fri, 22 May 2020 00:00:00 GMT") == "2020-05-22"
    assert si.parse_date("令和7年") is None
    assert si.parse_date("2026-13-45") is None
    assert si.parse_date(None) is None


def test_normalize_real_shapes():
    rec = {"amount": 3800000, "award_date": "Thu, 14 May 2026 00:00:00 GMT", "funding_type": "procurement",
           "listed_name": "株式会社ＦＲＯＮＴＥＯ", "ministry_name": "国税庁", "sec_code": "21580", "title": "t"}
    r = si.normalize(rec, "funding_recent", None, "t")
    assert (r["company_code"], r["event_date"], r["company_name"], r["ministry"], r["kind"]) == \
        ("2158", "2026-05-14", "株式会社ＦＲＯＮＴＥＯ", "国税庁", "procurement")
    cert = {"certification_date": "Thu, 07 Aug 2025 00:00:00 GMT", "certification_id": "meti-es-0114",
            "company_name": "日東紡績株式会社", "critical_material": "半導体"}
    c = si.normalize(cert, "economic_security_cert", None, "t")
    assert (c["event_date"], c["kind"], c["company_code"]) == ("2025-08-07", "半導体", None)
    assert c["record_id"] != si.normalize({**cert, "certification_id": "x"}, "economic_security_cert", None, "t")["record_id"]


def test_company_api_path_uses_five_digits():
    assert si.company_api_path("7011") == "/v1/companies/70110/public-funding"
    assert si.code4_of("70110") == "7011" and si.code4_of(None) is None


def test_due_companies_priority_skip_same_day_and_oldest_first():
    today = date(2026, 10, 8)
    meta = {"7011": {"fetched": "2026-10-08"}, "8306": {"fetched": "2026-09-01"},
            "9432": {"fetched": "2026-08-01"}, "1301": {"fetched": "2026-10-01"}, "6758": {"fetched": "2026-10-08"}}
    got = si.due_companies(["8306", "7011", "8306", "2802"], ["1301", "7011", "9432", "8306", "6758", "3000"],
                           meta, today)
    # 優先: 8306, 2802（7011は同日取得済み）→ 残り: 未取得3000, 古い順9432, 1301（6758は同日）
    assert got == ["8306", "2802", "3000", "9432", "1301"]


def test_map_company_codes_normalizes_names():
    companies = [{"name": "日東紡績株式会社", "sec_code": "3110"}, {"name": "ＡＢＣ(株)", "sec_code": "12340"},
                 {"name": "旧社株式会社", "sec_code": "9999", "is_delisted": True},
                 {"name": "同名株式会社", "sec_code": "1111"}, {"name": "同名 株式会社", "sec_code": "2222"}]
    got = si.map_company_codes(["株式会社日東紡績", "ABC株式会社", "旧社", "同名", "存在しない"], companies)
    assert got == {"株式会社日東紡績": "3110", "ABC株式会社": "1234"}


def test_rank_row_converts_top_record():
    rec = {"first_award": "Fri, 22 May 2020 00:00:00 GMT", "funding_events": 13, "funding_types": 1,
           "industry": "電気機器", "last_award": "Thu, 14 May 2026 00:00:00 GMT", "listed_name": "X", "ministries": 6,
           "procurement_oku": 0.0, "sec_code": "24330", "subsidy_oku": 1.5, "total_oku": 33170.4,
           "via_subsidiary_oku": 33170.4}
    r = si.rank_row(rec, 3, date(2026, 10, 8), "now")
    assert (r["company_code"], r["rank_no"], r["first_award"], r["last_award"], r["fetched_date"]) == \
        ("2433", 3, "2020-05-22", "2026-05-14", "2026-10-08")
    assert (r["total_oku"], r["funding_events"], r["ministries"], r["updated_at"]) == (33170.4, 13, 6, "now")
    assert si.rank_row({"listed_name": "no code"}, 1, date(2026, 10, 8), "n") is None


def test_open_subsidy_rows_sorted_and_expired_dropped():
    recs = [{"title": "late", "acceptance_end_datetime": "2026-12-01T00:00:00"},
            {"title": "past", "acceptance_end_datetime": "2026-10-01T00:00:00"},
            {"title": "soon", "acceptance_end_datetime": "Sat, 31 Oct 2026 00:00:00 GMT"},
            {"title": "none"}]
    assert [x["title"] for x in si.open_subsidy_rows(recs, date(2026, 10, 8))] == ["soon", "late", "none"]


def test_run_skips_without_key(monkeypatch):
    monkeypatch.delenv("SEISAKUDB_API_KEY", raising=False)
    assert si.run(object(), ["7011.T"])["skipped"] is True
