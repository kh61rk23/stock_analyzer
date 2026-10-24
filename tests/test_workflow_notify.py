"""workflow_notify: 取得系 workflow の終了通知（メールの組み立て・行数の増減・配線）。送信はしない。"""
from datetime import datetime
from pathlib import Path

import pytest
import yaml

import actions_log
import workflow_notify as wn

WF = Path(__file__).resolve().parent.parent / ".github" / "workflows"
NOTIFIED = ("fetch_jp_filings", "edinet_gov_backfill", "price_universe", "us_company_facts", "manual_task",
            "edinetdb_web_backfill", "sec_filings", "market_positioning")


def test_step_record_keeps_highlights_only():
    lines = ["途中の行", "✅ edinet_gov_docs: {\"days\": 3}", "  ⏩ 何もしない", "x ❌ 混ざった行", "❌ 失敗の要約"]
    r = actions_log.step_record(lines, 0, 12.4, "edinet_gov_docs")
    assert r["highlights"] == ['✅ edinet_gov_docs: {"days": 3}', "⏩ 何もしない", "❌ 失敗の要約"]
    assert (r["ok"], r["err"], r["seconds"]) == (1, 2, 12)


def test_deltas_and_mail():
    now = {"edinet.gov_documents": {"rows": 3400, "mb": 0.6}, "edinet.x": {"error": "NotFound"}}
    d = wn.deltas(now, {"edinet.gov_documents": {"rows": 3191, "mb": 0.5}})
    assert d == {"edinet.gov_documents": 209, "edinet.x": None}
    steps = [{"step": "jp_filings", "rc": 0, "seconds": 160, "ok": 13, "warn": 3, "err": 0, "highlights": ["✅ jp_filings: 記録 5件"]},
             {"step": "edinet_gov_docs", "rc": 1, "seconds": 9, "ok": 0, "warn": 0, "err": 1, "highlights": ["❌ 一覧 2026-10-22: 503"]}]
    subj, body = wn.build_mail("日本企業の開示データ取得", "fetch_jp_filings", steps, now, d,
                               {"total_gb": 3.5, "measured_at": "2026-10-23T05:30"}, "https://example/run/1",
                               datetime(2026, 10, 23, 5, 40))
    assert subj.startswith("【取得】❌ 日本企業の開示データ取得 10/23 05:40")   # 失敗が1つでもあれば ❌
    assert "+209" in body and "読めない" in body and "3.5GB" in body and "実行ページ" in body
    assert "記録 5件" in body


def test_mail_without_steps_says_so():
    subj, body = wn.build_mail("t", "w", [], {}, {}, None, "", datetime(2026, 10, 23))
    assert "ステップの記録が無い" in body


@pytest.mark.parametrize("name", NOTIFIED)
def test_notify_step_runs_always_before_saving_state(name):
    d = yaml.safe_load((WF / f"{name}.yml").read_text(encoding="utf-8"))
    (job,) = [j for k, j in d["jobs"].items() if k != "gate"]
    names = [s.get("name", "") for s in job["steps"]]
    i_n = names.index("Notify")
    i_s = next(i for i, n in enumerate(names) if "Save state to BigQuery" in n)
    assert i_n < i_s                                  # 前回の行数（data/workflow_notify_counts.json）も保存される
    st = job["steps"][i_n]
    assert st["if"] == "always()" and st["continue-on-error"] is True
    assert f"--workflow {name}" in st["run"] and "ALERT_GMAIL_TO" in st["env"]


def test_daily_run_is_not_notified_twice():
    assert "workflow_notify.py" not in (WF / "daily_run.yml").read_text(encoding="utf-8")


def test_step_json_and_run_start():
    steps = [{"step": "sec_filings", "seconds": 100, "at": "2026-10-24 02:30:00",
              "highlights": ['✅ sec_filings: {"range": ["2026-10-20", "2026-10-23"], "days": 4}']}]
    assert wn.step_json(steps, "sec_filings")["range"] == ["2026-10-20", "2026-10-23"]
    assert wn.step_json(steps, "edinet_gov_docs") == {}
    st = wn.run_started(steps)
    assert (st.hour, st.minute, st.second) == (2, 28, 20)


def test_digest_section_rendered():
    subj, body = wn.build_mail("t", "w", [], {}, {}, None, "", datetime(2026, 10, 24),
                               [("13D（経営に関わる目的もありうる5%超の保有）", ["Acme: Fund X（IA）12.0%"])])
    assert "今回取ったもの" in body and "Acme: Fund X" in body
