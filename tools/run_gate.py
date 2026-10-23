# ==============================================================
# tools/run_gate.py — 定時・連鎖で起動した workflow が「今日はもう要らない」かを決める（gate ジョブから呼ぶ）
# ==============================================================
# 使い方（gate ジョブの1ステップ。GH_TOKEN に GITHUB_TOKEN を渡す）:
#   python tools/run_gate.py --workflow daily_run.yml --job "📈 アナライザー実行" \
#       --skip-if-running fetch_jp_filings.yml --skip-weekday 7
# 判定（手動実行 workflow_dispatch は必ず走らせる）:
#   ・「今日」は日本時間の日付で数える（定時は UTC で書くが、取得の終わりに連鎖して起動すると UTC では前日になる）
#   ・自分の workflow の今日の回のうち、本処理のジョブ（--job）が実行中・待ち・成功の回があれば見送る
#     （gate が見送った回も workflow としては success で終わるので、workflow の結論ではなくジョブの結論で数える）
#   ・本処理のジョブの失敗が --max-failures 回あれば見送る（同じ失敗の繰り返しで API の日次枠を使い切らないため）
#   ・--skip-if-done W:JOB … 別の workflow のそのジョブが今日 実行中・待ち・成功なら見送る
#   ・--skip-if-running W … 別の workflow に今日 終わっていない回があれば見送る（その終わりに連鎖して起動するため）
#   ・--skip-weekday N … 日本時間の曜日（1=月〜7=日）が N なら見送る
#   ・履歴を読めない時は走らせる（止まるより二重に動く方が害が小さい）
# 結果は GITHUB_OUTPUT に run=true/false。理由を1行出す。
# ==============================================================
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
ACTIVE = {"queued", "in_progress", "waiting", "requested", "pending"}


def jst_midnight_utc(now: datetime) -> str:
    """日本時間の今日 0:00 を UTC の ISO 文字列で（GitHub の created フィルタ用）。"""
    d = now.astimezone(JST).replace(hour=0, minute=0, second=0, microsecond=0)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def job_state(jobs: list, name: str) -> str | None:
    """runs/{id}/jobs の jobs → そのジョブの状態（active / success / failure / skipped / None）。"""
    for j in jobs or []:
        if j.get("name") == name:
            if j.get("status") in ACTIVE:
                return "active"
            c = j.get("conclusion")
            return "failure" if c in ("failure", "timed_out") else c
    return None


def decide(event: str, weekday: int, own: list[str], failures_max: int, others_done: dict, others_running: dict,
           skip_weekday: int | None) -> tuple[bool, str]:
    """(走らせるか, 理由)（純関数）。own は自分の今日の回の本処理ジョブの状態の列。"""
    if event == "workflow_dispatch":
        return True, "手動実行"
    if skip_weekday and weekday == skip_weekday:
        return False, f"曜日 {weekday} は動かさない"
    done = sum(1 for s in own if s in ("active", "success"))
    failed = sum(1 for s in own if s == "failure")
    if done:
        return False, f"今日は済み（実行中・成功 {done}回）"
    if failed >= failures_max:
        return False, f"今日は失敗が {failed}回（打ち止め）"
    for w, n in others_done.items():
        if n:
            return False, f"{w} が今日 済み・実行中"
    for w, n in others_running.items():
        if n:
            return False, f"{w} が実行中（終わったら連鎖して起動する）"
    return True, f"今日はまだ（失敗 {failed}回）"


def _api(path: str) -> dict:
    req = urllib.request.Request(f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/{path}",
                                 headers={"Authorization": f"Bearer {os.environ['GH_TOKEN']}",
                                          "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _runs_today(workflow: str, since: str, exclude: str) -> list[dict]:
    runs = _api(f"actions/workflows/{workflow}/runs?created=%3E%3D{since}&per_page=50").get("workflow_runs") or []
    return [r for r in runs if str(r.get("id")) != str(exclude)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflow", required=True)
    ap.add_argument("--job", required=True)
    ap.add_argument("--max-failures", type=int, default=2)
    ap.add_argument("--skip-if-done", action="append", default=[], help="workflow.yml:ジョブ名")
    ap.add_argument("--skip-if-running", action="append", default=[])
    ap.add_argument("--skip-weekday", type=int)
    a = ap.parse_args(argv)
    event, run_id = os.environ.get("GITHUB_EVENT_NAME", ""), os.environ.get("GITHUB_RUN_ID", "")
    now = datetime.now(timezone.utc)
    since = jst_midnight_utc(now)
    try:
        own = [job_state(_api(f"actions/runs/{r['id']}/jobs").get("jobs"), a.job)
               for r in _runs_today(a.workflow, since, run_id)]
        others_done = {}
        for spec in a.skip_if_done:
            w, job = spec.split(":", 1)
            others_done[w] = sum(1 for r in _runs_today(w, since, run_id)
                                 if job_state(_api(f"actions/runs/{r['id']}/jobs").get("jobs"), job) in ("active", "success"))
        others_running = {w: sum(1 for r in _runs_today(w, since, run_id) if r.get("status") in ACTIVE)
                          for w in a.skip_if_running}
        ok, why = decide(event, now.astimezone(JST).isoweekday(), own, a.max_failures, others_done, others_running,
                         a.skip_weekday)
    except Exception as e:
        ok, why = True, f"履歴を読めなかったので走らせる（{type(e).__name__}）"
    print(f"{'▶️' if ok else '⏩'} {a.workflow}（{event}・JST {now.astimezone(JST):%Y-%m-%d %H:%M}）: {why}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"run={'true' if ok else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
