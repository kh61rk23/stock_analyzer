"""tools/run_gate.py: 定時・連鎖の回の見送り判定（GitHub の API には触らない）。"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import run_gate as g  # noqa: E402


def test_jst_midnight():
    # 2026-10-22 23:30 UTC は JST 10-23 08:30 → JST 10-23 0:00 = UTC 10-22 15:00
    assert g.jst_midnight_utc(datetime(2026, 10, 22, 23, 30, tzinfo=timezone.utc)) == "2026-10-22T15:00:00Z"


def test_job_state_reads_the_named_job_not_the_workflow():
    jobs = [{"name": "🚦 今日の実行が要るか", "status": "completed", "conclusion": "success"},
            {"name": "📈 アナライザー実行", "status": "completed", "conclusion": "skipped"}]
    assert g.job_state(jobs, "📈 アナライザー実行") == "skipped"     # gate が見送った回は「済み」にしない
    assert g.job_state([{"name": "x", "status": "in_progress"}], "x") == "active"
    assert g.job_state([{"name": "x", "status": "completed", "conclusion": "timed_out"}], "x") == "failure"
    assert g.job_state([], "x") is None


def test_decide():
    d = lambda own=(), ev="schedule", wd=4, od=None, orun=None, sw=7: g.decide(ev, wd, list(own), 2, od or {}, orun or {}, sw)
    assert d(ev="workflow_dispatch", own=["success"])[0] is True        # 手動は必ず
    assert d(wd=7)[0] is False                                          # 日曜は動かさない
    assert d(own=["skipped", "skipped"])[0] is True                     # 見送っただけの回は数えない
    assert d(own=["success"])[0] is False and d(own=["active"])[0] is False
    assert d(own=["failure"])[0] is True and d(own=["failure", "failure"])[0] is False
    assert d(od={"daily_run.yml": 1})[0] is False                        # 分析が済んだ日は取得しない
    assert d(orun={"fetch_jp_filings.yml": 1})[0] is False               # 取得の途中なら連鎖を待つ
    assert d(ev="workflow_run")[0] is True
