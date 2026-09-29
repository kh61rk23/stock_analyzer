# 2026-09-29: EDINET DB 取り込みの見える化
#   ・その日取り込んだ銘柄をメール（Gmail）に載せる
#   ・対象があるのに取り込み0社の日を success と記録していた
import alert_notifier as an


def test_ingest_alert_lists_companies():
    al = an.build_edinetdb_ingest_alert({
        "success": 2, "targets": ["E1", "E2", "E3"], "failure": 0,
        "ingested": [{"sec_code": "3983", "name": "オロ", "fiscal_year": 2025, "financial_rows": 5},
                     {"sec_code": "7203", "name": "トヨタ自動車"}],
        "missing_companies": [{"sec_code": "5866", "name": "新規上場A"}],
        "doc_index_rows": 14, "api_calls": 20})
    assert al["channel"] == "gmail" and al["severity"] == "info"
    assert "3983 オロ" in al["body"] and "7203 トヨタ自動車" in al["body"]
    assert "5866 新規上場A" in al["body"]
    assert al["title"] == "🗂 EDINET DB 取り込み 2社"


def test_zero_ingest_is_flagged():
    al = an.build_edinetdb_ingest_alert({"success": 0, "targets": ["E1"], "ingested": [],
                                         "missing_companies": [], "failure": 0})
    assert "要確認" in al["title"] and al["severity"] == "medium"


def test_extract_alerts_includes_ingest(monkeypatch):
    import types
    cfg = types.SimpleNamespace(ALERT_ON_EDINETDB_INGEST=True)
    res = {"edinetdb_ingest": {"skipped": False, "success": 1, "targets": ["E1"],
                               "ingested": [{"sec_code": "1332", "name": "ニッスイ"}]}}
    alerts = an.extract_alerts(cfg, res)
    assert any(a["trigger"] == "edinetdb_ingest" for a in alerts)
