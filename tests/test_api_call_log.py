"""EdinetDB の呼び出し記録・枠の内訳・候補の計画・日次メールの説明行。"""
from datetime import date

import api_call_log as acl
import alert_notifier as an
import edinetdb_ingestor as ei


def test_normalize_path_hides_codes_and_query():
    assert acl.normalize_path("https://edinetdb.jp/v1/companies/E03606/analysis?x=1") == "/companies/{code}/analysis"
    assert acl.normalize_path("/companies/E00001/text-blocks") == "/companies/{code}/text-blocks"
    assert acl.normalize_path("/usage") == "/usage"


def test_note_and_summarize_count_by_caller_path_status(tmp_path):
    acl.note("edinetdb", "/companies/E00001/financials", 200, caller="edinetdb_ingestor:run", data_dir=tmp_path)
    acl.note("edinetdb", "/companies/E00002/financials", 404, caller="edinetdb_ingestor:run", data_dir=tmp_path)
    acl.note("edinetdb", "/usage", 200, caller="api_key_health:probe", data_dir=tmp_path)
    sm = acl.summarize("edinetdb", data_dir=tmp_path)
    assert sm["total"] == 3
    assert sm["by_caller"][0] == ("edinetdb_ingestor:run", 2)
    assert dict(sm["by_path"])["/companies/{code}/financials"] == 2
    assert dict(sm["by_status"]) == {"200": 2, "404": 1}


def test_note_never_raises(tmp_path):
    bad = tmp_path / "file"
    bad.write_text("x")
    acl.note("edinetdb", "/usage", 200, caller="x", data_dir=bad)   # ディレクトリにできない場所でも例外を出さない


def test_caller_of_skips_client_frames():
    class F:
        def __init__(self, filename, function):
            self.filename, self.function = filename, function
    st = [F("/a/db_rest_client.py", "_request"), F("/a/api_call_log.py", "note"), F("/a/edinetdb_ingestor.py", "run_edinetdb_ingest")]
    assert acl.caller_of(st) == "edinetdb_ingestor:run_edinetdb_ingest"


def test_budget_cap_breakdown_names_the_limiter():
    b = ei.budget_cap_breakdown(97, {"daily_remaining": 100, "monthly_remaining": 900, "monthly_used": 3080}, 3100)
    assert b["final"] == 20 and b["limiter"] == "monthly_guard"
    b = ei.budget_cap_breakdown(97, {"daily_remaining": 100}, 3100)
    assert b["final"] == 97 and b["limiter"] == "config"
    assert ei.budget_cap_breakdown(97, None, 3100)["final"] == 97


def test_recent_web_nodata_only_within_recheck_window():
    meta = {"E1": {"web_check": {"date": "2026-10-08", "has": False}},
            "E2": {"web_check": {"date": "2026-09-20", "has": False}},
            "E3": {"web_check": {"date": "2026-10-08", "has": True}}}
    assert ei.recent_web_nodata(meta, 7, date(2026, 10, 10)) == {"E1"}


def test_plan_targets_excludes_recent_nodata(monkeypatch):
    import types
    cfg = types.SimpleNamespace(EDINETDB_WEB_PRECHECK=True, EDINETDB_WEB_RECHECK_DAYS=7)
    monkeypatch.setattr(ei, "_priority_tickers", lambda cfg: ([], [], []))
    companies = [{"edinet_code": f"E{i:05d}", "sec_code": f"{1000 + i}0"} for i in range(10)]
    today = date.today().isoformat()
    meta = {f"E{i:05d}": {"web_check": {"date": today, "has": False}} for i in range(5)}
    got = ei._plan_targets(companies, cfg, meta, 30)
    assert got and not set(got) & set(meta)


def test_usage_lines_explain_monthly_cap_and_underuse():
    caps = {"caps": {"config": 97, "monthly_guard": 20}, "final": 20, "limiter": "monthly_guard",
            "monthly_used": 3080, "monthly_guard": 3100}
    lines, warns = an.api_usage_lines({"total": 2, "by_caller": [("a:b", 2)], "by_path": [("/usage", 2)],
                                       "by_status": [("200", 1), ("429", 1)], "first": "09:00", "last": "09:01"},
                                      caps, 5, {"web_skipped": ["E1"] * 30, "targets": ["E2"]})
    text = "\n".join(lines)
    assert "月の上限" in text and "a:b 2回" in text
    assert any("429" in w for w in warns) and any("月の上限" in w for w in warns) and any("しか使っていない" in w for w in warns)
    alert = an.build_edinetdb_ingest_alert({"ingested": [], "targets": ["E2"], "budget_caps": caps}, {}, {"calls_used": 5}, 100,
                                           {"total": 0})
    assert "API 5/100回" in alert["title"] and alert["severity"] == "medium"
