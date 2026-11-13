# ==============================================================
# tools/self_heal.py — 定時 workflow の失敗の分類と自動の対処（self_heal.yml から呼ぶ）
# ==============================================================
# 使い方（GITHUB_TOKEN と GITHUB_REPOSITORY が要る。標準ライブラリだけ・素の python3）:
#   python3 tools/self_heal.py handle --run-id <実行ID>   # workflow_run（completed）の回。失敗なら分類して対処、成功なら Issue を閉じる
#   python3 tools/self_heal.py watchdog                   # 1時間おき。予定を過ぎても動いていない workflow を起こす
# 対処（decide）:
#   transient（ランナーの喪失・setup/checkout/pip の失敗・ネットワーク系）
#       → 失敗したジョブだけを再実行する。ただし ① 1回目の実行（run_attempt<2） ② 今日（JST）の自動の再実行が
#         workflow ごと・全体とも上限未満 ③ 有料 API を呼ぶ段より前の失敗、のときだけ（API の日次枠を二重に使わないため）。
#   timeout・unknown・code → Issue（ラベル「自己修復」）。 notify_only（通知の段だけの失敗）→ 何もしない。
#   cancelled（timeout 以外の取り消し）→ 何もしない。人が止めた回を自動で起こし直さないため（ランナーの喪失は注記で見分けて transient にする）。
# 再実行の回数は GitHub の実行履歴（run_attempt>1 の回）から数える。状態ファイルは持たない。
# Issue は workflow ごとに1本。開いていれば追記し、成功の回が来たら閉じる。本文は workflow 名・ジョブ名・段の名前・
#   分類・実行へのリンク・時刻だけ（public リポジトリなので、ログ・注記の本文・数値・銘柄は書かない）。
#   注記（annotations）の本文は分類の判定にだけ使い、どこにも出さない。
# watchdog は WATCH の表の予定＋猶予を過ぎても本処理ジョブが一度も動いていない workflow を workflow_dispatch で起こす
#   （1 workflow につき1日1回まで・今日の dispatch の履歴で数える）。各 workflow の gate があるので二重には動かない。
# GitHub API は _api だけが呼ぶ。テストはここを差し替える。
# ==============================================================
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
API_ROOT = "https://api.github.com"
API_TIMEOUT_SEC = 30

LABEL = "自己修復"
ISSUE_TITLE_PREFIX = "[自己修復] "

# 自動の再実行の上限（既定）。1日は日本時間で数える
MAX_RERUNS_PER_WORKFLOW = 1
MAX_RERUNS_TOTAL = 3
# この回数未満の run_attempt のときだけ再実行する（再実行した回の失敗は人に回す）
MAX_ATTEMPTS = 2

# 見張る workflow の表。cron は UTC・ここは JST（曜日は 1=月〜7=日。UTC と日付がずれる cron は曜日も直してある）。
#   time      … 予定の時刻（JST）。daily_run は取得の予定＋取得の timeout（08:17＋90分＝09:47）より後に置く
#   grace_min … 予定からこの分数が過ぎても本処理ジョブが一度も動いていなければ起こす
#   main_job  … 本処理のジョブ名（gate が見送った回は workflow としては success になるので、ジョブの結論で見る）
#   paid_step … 有料・上限つき API を呼ぶ段の名前。この段が始まった後の失敗は再実行しない（None は該当なし）
#   wait_for  … これらの workflow が実行中のあいだは起こさない（終われば連鎖して起動するため）
#   inputs_by_weekday … 起こすときに渡す入力（曜日別）。daily_run は月曜に weekly へ上げる
WATCH = (
    {"name": "全上場株価の日次取得", "file": "price_universe.yml", "weekdays": (2, 3, 4, 5, 6),
     "time": "06:47", "grace_min": 90, "main_job": "run", "paid_step": None, "wait_for": ()},
    {"name": "日本企業の開示データ取得", "file": "fetch_jp_filings.yml", "weekdays": (1, 2, 3, 4, 5, 6),
     "time": "08:17", "grace_min": 60, "main_job": "📥 取得", "paid_step": "Fetch", "wait_for": ()},
    {"name": "📊 Stock Analyzer 自律実行", "file": "daily_run.yml", "weekdays": (1, 2, 3, 4, 5, 6),
     "time": "10:00", "grace_min": 120, "main_job": "📈 アナライザー実行", "paid_step": "🚀 Run analyzer",
     "wait_for": ("fetch_jp_filings.yml",), "inputs_by_weekday": {1: {"mode": "weekly"}}},
    {"name": "需給のデータ（FINRA の空売り残・CFTC の建玉明細）", "file": "market_positioning.yml", "weekdays": (6,),
     "time": "10:17", "grace_min": 90, "main_job": "📊 取得", "paid_step": None, "wait_for": ()},
    {"name": "検証ラボの週次検証", "file": "quant_research.yml", "weekdays": (6,),
     "time": "12:17", "grace_min": 90, "main_job": "🔬 週次の検証", "paid_step": None, "wait_for": ()},
    {"name": "米国株の財務・企業情報の更新", "file": "us_company_facts.yml", "weekdays": (2, 3, 4, 5, 6),
     "time": "20:07", "grace_min": 120, "main_job": "run", "paid_step": None, "wait_for": ()},
    {"name": "SEC EDGAR の提出一覧・8-K 項目・13D/13G", "file": "sec_filings.yml", "weekdays": (1, 2, 3, 4, 5, 6, 7),
     "time": "02:17", "grace_min": 120, "main_job": "🇺🇸 取得", "paid_step": None, "wait_for": ()},
)

# 見張りの対象にしない workflow（自分自身の失敗で自分を起こす無限ループを避ける。CI は自動修正のジョブだけが読む）
CI_WORKFLOW_NAME = "CI"
SELF_WORKFLOW_NAME = "自己修復"

ACTIVE = {"queued", "in_progress", "waiting", "requested", "pending"}
FAILED_CONCLUSIONS = ("failure", "timed_out", "cancelled")

NOTIFY_STEP_MARKER = "fail if notification"
# 失敗した段の名前にこれが入っていれば、ランナー・セットアップ側の失敗として扱う（小文字で比べる）
SETUP_STEP_MARKERS = ("set up job", "setup python", "setup-python", "checkout", "install dependencies",
                      "pip install", "restore installed packages", "complete job")
# CI の失敗の段（コードの失敗）
CODE_STEP_MARKERS = ("pytest", "audit")
# 注記の本文に入っていたら一時的な失敗とみなす語（小文字）。有料 API の上限（rate limit・quota）は含めない
RUNNER_LOSS_PHRASES = ("received a shutdown signal", "lost communication with the server",
                       "runner has received", "hosted runner", "did not respond", "the self-hosted runner")
NETWORK_PHRASES = ("connection reset", "connection aborted", "connection refused", "connectionerror",
                   "max retries exceeded", "temporary failure in name resolution", "name or service not known",
                   "network is unreachable", "read timed out", "readtimeout", "connecttimeout", "connection timed out",
                   "bad gateway", "service unavailable", "gateway timeout", "econnreset", "ssl: ", "tls handshake")
TIMEOUT_PHRASES = ("exceeded the maximum execution time",)
_PY_EXCEPTION = re.compile(r"Traceback \(most recent call last\)|\b[A-Z][A-Za-z]*(?:Error|Exception)\b")

KIND_LABELS = {
    "transient": "一時的な失敗",
    "timeout": "ジョブの timeout",
    "code": "コードの失敗",
    "notify_only": "通知の段だけの失敗",
    "cancelled": "取り消し",
    "unknown": "原因不明",
}


class ApiError(RuntimeError):
    """GitHub API が 2xx 以外を返した。本文は持たない（メッセージにトークンや内容が混ざらないように）。"""

    def __init__(self, status: int, method: str, path: str):
        super().__init__(f"{method} {path} → HTTP {status}")
        self.status = status


# ── GitHub API（ここだけがネットワークを使う） ────────────────────

def _api(method: str, path: str, body: dict | None = None):
    """GitHub API を1回呼んで JSON を返す（本文が空なら {}）。path は /repos/... から。2xx 以外は ApiError。"""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        API_ROOT + path, data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_SEC) as r:
            raw = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, method, path) from None
    return json.loads(raw) if raw.strip() else {}


def _repo() -> str:
    return os.environ["GITHUB_REPOSITORY"]


# ── 純関数 ───────────────────────────────────────────────────

def job_state(jobs: list, name: str) -> str | None:
    """jobs（API の jobs 配列）→ そのジョブの状態（active / success / failure / cancelled / skipped / None＝無い）。"""
    for j in jobs or []:
        if j.get("name") == name:
            if j.get("status") in ACTIVE:
                return "active"
            c = j.get("conclusion")
            return "failure" if c in ("failure", "timed_out") else c
    return None


def _messages(annotations: list) -> list[str]:
    """注記 → 判定に使う本文（小文字）。失敗レベルのものだけ。本文は返り値の判定にしか使わない。"""
    out = []
    for a in annotations or []:
        if isinstance(a, str):
            out.append(a.lower())
        elif a.get("annotation_level", "failure") == "failure":
            out.append(str(a.get("message") or "").lower())
    return out


def _failed_jobs(jobs: list) -> list[dict]:
    return [j for j in jobs or [] if j.get("conclusion") in FAILED_CONCLUSIONS]


def _failed_steps(job: dict) -> list[dict]:
    return [s for s in job.get("steps") or [] if s.get("conclusion") in ("failure", "timed_out")]


def classify(jobs: list, annotations: list) -> dict:
    """失敗した run の jobs と注記 → {"kind", "reason", "jobs", "steps"}（jobs・steps は名前の一覧）。

    kind: notify_only → timeout → transient → code → unknown の順に判定する。
    reason は固定の短い日本語で、注記の本文は含めない。
    """
    failed = _failed_jobs(jobs)
    job_names = [j.get("name", "") for j in failed]
    step_names = [s.get("name", "") for j in failed for s in _failed_steps(j)]

    def out(kind: str, reason: str) -> dict:
        return {"kind": kind, "reason": reason, "jobs": job_names, "steps": step_names}

    if not failed:
        return out("unknown", "失敗したジョブが見つからない")
    msgs = _messages(annotations)

    if step_names and all(NOTIFY_STEP_MARKER in s.lower() for s in step_names):
        return out("notify_only", "通知を送る段だけが失敗")
    if any(j.get("conclusion") == "timed_out" for j in failed) or any(p in m for m in msgs for p in TIMEOUT_PHRASES):
        return out("timeout", "ジョブが実行時間の上限に達した")
    if any(p in m for m in msgs for p in RUNNER_LOSS_PHRASES):
        return out("transient", "ランナーが失われた")
    if any(j.get("conclusion") == "cancelled" for j in failed):
        return out("cancelled", "ジョブが取り消された（timeout 以外）")
    if any(any(k in s.lower() for k in SETUP_STEP_MARKERS) for s in step_names):
        return out("transient", "セットアップ・checkout・依存の段の失敗")
    if any(p in m for m in msgs for p in NETWORK_PHRASES):
        return out("transient", "ネットワーク系の失敗")
    if any(any(k in s.lower() for k in CODE_STEP_MARKERS) for s in step_names):
        return out("code", "テスト・監査の失敗")
    if any(_PY_EXCEPTION.search(m_raw) for m_raw in _raw_messages(annotations)):
        return out("code", "Python の例外")
    return out("unknown", "分類できない失敗")


def _raw_messages(annotations: list) -> list[str]:
    """例外名の大文字小文字を見るための、小文字にしていない本文（判定専用）。"""
    out = []
    for a in annotations or []:
        if isinstance(a, str):
            out.append(a)
        elif a.get("annotation_level", "failure") == "failure":
            out.append(str(a.get("message") or ""))
    return out


def failed_before_paid(jobs: list, paid_step: str | None) -> bool:
    """有料 API の段がまだ始まっていないか。段が無い workflow は常に True。

    段が始まった（結論が skipped・未定以外）なら False。ジョブごと走らなかった・その前の段で落ちたなら True。
    """
    if not paid_step:
        return True
    for j in jobs or []:
        for s in j.get("steps") or []:
            if s.get("name") == paid_step and s.get("conclusion") not in (None, "skipped"):
                return False
    return True


def decide(kind: str, run_attempt: int, workflow: str, reruns_today: dict, cfg: dict | None = None,
           before_paid: bool = True) -> str:
    """分類 → 対処（rerun_failed / issue / none）。

    reruns_today は {"by_workflow": {workflow名: 回数}, "total": 回数}。cfg は上限の上書き
    （max_reruns_per_workflow・max_reruns_total・max_attempts）。
    再実行するのは transient で、1回目の実行・上限未満・有料 API の段より前のときだけ。
    """
    cfg = cfg or {}
    if kind in ("notify_only", "cancelled"):
        return "none"
    if kind != "transient":
        return "issue"
    if not before_paid:
        return "issue"
    per_wf = int(cfg.get("max_reruns_per_workflow", MAX_RERUNS_PER_WORKFLOW))
    total = int(cfg.get("max_reruns_total", MAX_RERUNS_TOTAL))
    attempts = int(cfg.get("max_attempts", MAX_ATTEMPTS))
    n_wf = int((reruns_today.get("by_workflow") or {}).get(workflow, 0))
    n_all = int(reruns_today.get("total", 0))
    if run_attempt < attempts and n_wf < per_wf and n_all < total:
        return "rerun_failed"
    return "issue"


def _parse_time(s) -> datetime:
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def jst_day(dt) -> str:
    """時刻 → 日本時間の日付（YYYY-MM-DD）。"""
    return _parse_time(dt).astimezone(JST).strftime("%Y-%m-%d")


def count_reruns(now, runs_by_file: dict, table=WATCH) -> dict:
    """実行履歴 → 今日（JST）の自動の再実行の回数 {"by_workflow": {名前: n}, "total": n}。

    runs_by_file は {workflow ファイル: [run]}。run_attempt>1 で、最新の試行が今日始まった回を1回と数える
    （人が手で再実行した回も数える。上限に余裕を持たせる側に倒れる）。
    """
    today = jst_day(now)
    by_wf, total = {}, 0
    for entry in table:
        n = 0
        for r in runs_by_file.get(entry["file"], []):
            started = r.get("run_started_at") or r.get("created_at")
            if int(r.get("run_attempt") or 1) > 1 and started and jst_day(started) == today:
                n += 1
        by_wf[entry["name"]] = n
        total += n
    return {"by_workflow": by_wf, "total": total}


def watchdog_due(now, history: dict, table=WATCH) -> list[dict]:
    """今の時刻と実行履歴 → 起こすべき workflow（表の行）の一覧。

    history は {workflow ファイル: [{"event", "created_at", "main_state", "active"}]}。
      main_state … その回の本処理ジョブの状態（job_state の値。ジョブが無い・見送りは None か skipped）
      active     … その回がまだ終わっていない
    起こす条件（全部そろったとき）:
      ・今日（JST）の曜日が表にあり、予定時刻＋猶予を過ぎている
      ・今日の回の本処理ジョブが一度も動いていない（実行中・成功・失敗・取り消しが無い。gate の見送りだけは動いていないとみなす）
      ・今日すでに workflow_dispatch の回がない（1日1回まで）
      ・wait_for の workflow が実行中でない
    """
    now_jst = _parse_time(now).astimezone(JST)
    today = now_jst.strftime("%Y-%m-%d")
    due = []
    for entry in table:
        if now_jst.isoweekday() not in entry["weekdays"]:
            continue
        h, m = (int(x) for x in entry["time"].split(":"))
        deadline = now_jst.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(minutes=entry["grace_min"])
        if now_jst < deadline or deadline.strftime("%Y-%m-%d") != today:
            continue
        runs_today = [r for r in history.get(entry["file"], []) if jst_day(r["created_at"]) == today]
        if any(r.get("main_state") in ("active", "success", "failure", "cancelled") for r in runs_today):
            continue
        if any(r.get("event") == "workflow_dispatch" for r in runs_today):
            continue
        if any(r.get("active") for w in entry.get("wait_for", ()) for r in history.get(w, [])):
            continue
        due.append(entry)
    return due


def issue_title(workflow_name: str) -> str:
    return f"{ISSUE_TITLE_PREFIX}{workflow_name}"


def issue_body(workflow_name: str, cls: dict, run_url: str, now) -> str:
    """Issue（と追記）の本文。入れるのは workflow 名・ジョブ名・段の名前・分類・実行のリンク・時刻だけ。"""
    jobs = "、".join(cls.get("jobs") or []) or "（なし）"
    steps = "、".join(cls.get("steps") or []) or "（なし）"
    when = _parse_time(now).astimezone(JST).strftime("%Y-%m-%d %H:%M JST")
    return "\n".join([
        f"- workflow: {workflow_name}",
        f"- 失敗したジョブ: {jobs}",
        f"- 失敗した段: {steps}",
        f"- 分類: {KIND_LABELS.get(cls.get('kind'), cls.get('kind'))}（{cls.get('reason', '')}）",
        f"- 実行: {run_url}",
        f"- 時刻: {when}",
    ])


# ── 実行履歴の読み出し ─────────────────────────────────────────

def _list_runs(file: str) -> list[dict]:
    q = urllib.parse.urlencode({"per_page": 30})
    return _api("GET", f"/repos/{_repo()}/actions/workflows/{file}/runs?{q}").get("workflow_runs") or []


def _jobs_of(run_id) -> list[dict]:
    return _api("GET", f"/repos/{_repo()}/actions/runs/{run_id}/jobs?per_page=100").get("jobs") or []


def _annotations_of(jobs: list) -> list[dict]:
    out = []
    for j in _failed_jobs(jobs):
        out.extend(_api("GET", f"/repos/{_repo()}/check-runs/{j['id']}/annotations?per_page=100") or [])
    return out


def _entry_for(run: dict) -> dict | None:
    """run の path（.github/workflows/x.yml@ref）→ 表の行。表に無い workflow は None。"""
    base = os.path.basename(str(run.get("path") or "").split("@")[0])
    return next((e for e in WATCH if e["file"] == base), None)


def _history_for_watchdog(now) -> dict:
    """watchdog 用の履歴。今日の回だけ、本処理ジョブの状態を引く（API を節約するため時刻と曜日で絞った workflow だけ）。"""
    now_jst = _parse_time(now).astimezone(JST)
    today = now_jst.strftime("%Y-%m-%d")
    files = set()
    for e in WATCH:
        if now_jst.isoweekday() in e["weekdays"]:
            files.add(e["file"])
            files.update(e.get("wait_for", ()))
    history = {}
    for f in sorted(files):
        entry = next((e for e in WATCH if e["file"] == f), None)
        rows = []
        for r in _list_runs(f):
            today_run = jst_day(r["created_at"]) == today
            main = None
            if today_run and entry:
                main = job_state(_jobs_of(r["id"]), entry["main_job"])
            rows.append({"event": r.get("event"), "created_at": r["created_at"], "main_state": main,
                         "active": r.get("status") in ACTIVE})
        history[f] = rows
    return history


# ── Issue ─────────────────────────────────────────────────────

def _open_issue(title: str) -> dict | None:
    q = urllib.parse.urlencode({"labels": LABEL, "state": "open", "per_page": 100})
    for it in _api("GET", f"/repos/{_repo()}/issues?{q}") or []:
        if "pull_request" not in it and it.get("title") == title:
            return it
    return None


def _ensure_label() -> None:
    """ラベルが無ければ作る。すでにある（422）のは正常。"""
    try:
        _api("POST", f"/repos/{_repo()}/labels", {"name": LABEL, "color": "d93f0b",
                                                  "description": "workflow の失敗の自動対処"})
    except ApiError as e:
        if e.status != 422:
            raise


def upsert_issue(workflow_name: str, body: str) -> str:
    """workflow ごとに1本の Issue を作る。開いていれば追記する。戻り値は created / commented。"""
    title = issue_title(workflow_name)
    found = _open_issue(title)
    if found:
        _api("POST", f"/repos/{_repo()}/issues/{found['number']}/comments", {"body": body})
        return "commented"
    _ensure_label()
    _api("POST", f"/repos/{_repo()}/issues", {"title": title, "body": body, "labels": [LABEL]})
    return "created"


def close_issue(workflow_name: str, run_url: str) -> bool:
    """開いている Issue があれば、成功の回のリンクを付けて閉じる。"""
    found = _open_issue(issue_title(workflow_name))
    if not found:
        return False
    n = found["number"]
    _api("POST", f"/repos/{_repo()}/issues/{n}/comments", {"body": f"成功の回が来たので閉じる: {run_url}"})
    _api("PATCH", f"/repos/{_repo()}/issues/{n}", {"state": "closed", "state_reason": "completed"})
    return True


# ── 入口 ──────────────────────────────────────────────────────

def handle(run_id, now=None, cfg: dict | None = None) -> str:
    """workflow_run（completed）の1回を処理する。戻り値は取った対処（closed / rerun_failed / issue / none / skip）。"""
    now = now or datetime.now(timezone.utc)
    run = _api("GET", f"/repos/{_repo()}/actions/runs/{run_id}")
    entry = _entry_for(run)
    if entry is None:
        print(f"⏩ 見張りの対象外: {run.get('name')}")
        return "skip"
    name, url, conclusion = entry["name"], run.get("html_url", ""), run.get("conclusion")
    jobs = _jobs_of(run_id)

    if conclusion == "success":
        if job_state(jobs, entry["main_job"]) == "success" and close_issue(name, url):
            print(f"✅ {name}: 成功の回が来たので Issue を閉じた")
            return "closed"
        print(f"⏩ {name}: 成功（閉じる Issue なし、または本処理ジョブは見送り）")
        return "skip"
    if conclusion not in FAILED_CONCLUSIONS:
        print(f"⏩ {name}: 結論 {conclusion}（対処なし）")
        return "skip"

    cls = classify(jobs, _annotations_of(jobs))
    attempt = int(run.get("run_attempt") or 1)
    reruns = {"by_workflow": {}, "total": 0}
    if cls["kind"] == "transient" and attempt < MAX_ATTEMPTS:
        reruns = count_reruns(now, {e["file"]: _list_runs(e["file"]) for e in WATCH})
    action = decide(cls["kind"], attempt, name, reruns, cfg, before_paid=failed_before_paid(jobs, entry["paid_step"]))
    print(f"{name}: 分類 {cls['kind']}（{cls['reason']}）・試行 {attempt}・今日の再実行 {reruns['total']}回 → {action}")
    if action == "rerun_failed":
        _api("POST", f"/repos/{_repo()}/actions/runs/{run_id}/rerun-failed-jobs")
    elif action == "issue":
        print(f"  Issue: {upsert_issue(name, issue_body(name, cls, url, now))}")
    return action


def watchdog(now=None) -> list[str]:
    """予定を過ぎても動いていない workflow を起こす。戻り値は起こした workflow のファイル名。"""
    now = now or datetime.now(timezone.utc)
    ref = os.environ.get("GITHUB_REF_NAME") or "main"
    woken = []
    for e in watchdog_due(now, _history_for_watchdog(now)):
        body = {"ref": ref}
        inputs = (e.get("inputs_by_weekday") or {}).get(_parse_time(now).astimezone(JST).isoweekday())
        if inputs:
            body["inputs"] = inputs
        _api("POST", f"/repos/{_repo()}/actions/workflows/{e['file']}/dispatches", body)
        print(f"▶️ {e['name']}: 予定 {e['time']}＋{e['grace_min']}分を過ぎても動いていないので起こした")
        woken.append(e["file"])
    if not woken:
        print("⏩ 起こす workflow なし")
    return woken


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Actions の失敗の分類と自動の対処")
    sub = ap.add_subparsers(dest="command", required=True)
    h = sub.add_parser("handle")
    h.add_argument("--run-id", required=True)
    sub.add_parser("watchdog")
    a = ap.parse_args(argv)
    if a.command == "handle":
        handle(a.run_id)
    else:
        watchdog()
    return 0


if __name__ == "__main__":
    sys.exit(main())
