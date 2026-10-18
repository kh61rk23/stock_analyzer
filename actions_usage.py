# ==============================================================
# actions_usage.py
# GitHub Actions の当月の使用分数を、このリポジトリの実行履歴から数える
# ==============================================================
# 【役割】
#   private リポジトリの Actions は無料枠（Free 2,000分/月）を超えると月末まで全 workflow が
#   起動直後に失敗し、日次メールも止まる。請求 API（/users/{u}/settings/billing/usage）は
#   GITHUB_TOKEN では読めないので、/repos/{repo}/actions/runs の当月分の
#   「開始〜終了」を run ごとに分単位へ切り上げて合計する（課金はジョブ単位の切り上げなので近似値。
#   このリポジトリの workflow は全部1ジョブなので差は小さい）。
#   結果は LOG_DIR/actions_usage.json に書き、alert_notifier が枠の OPS_ACTIONS_WARN_RATIO を
#   超えたら日次メールに出す。実行中の run（日次本体自身を含む）は数えない。
#
# 【実行】 GH_TOKEN=... GITHUB_REPOSITORY=owner/repo python3 actions_usage.py
#   daily_run.yml で分析より前に呼ぶ（permissions に actions: read が要る）。
#   失敗しても日次は止めない（終了コード0で警告だけ出す）。
# ==============================================================

from __future__ import annotations

import json
import math
import os
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.github.com"
FILENAME = "actions_usage.json"


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def run_minutes(run: dict) -> int:
    """完了した run 1件の分数（開始〜最終更新を分へ切り上げ）。未完了・時刻なしは0（純関数）。"""
    if run.get("status") != "completed":
        return 0
    start, end = _ts(run.get("run_started_at")), _ts(run.get("updated_at"))
    if not start or not end or end <= start:
        return 0
    return int(math.ceil((end - start).total_seconds() / 60.0))


def summarize(runs: list, month: str) -> dict:
    """当月（YYYY-MM）に開始した run の分数を workflow 別に合計する（純関数）。"""
    total, by_wf, n = 0, defaultdict(int), 0
    for r in runs or []:
        if not str(r.get("run_started_at") or "").startswith(month):
            continue
        m = run_minutes(r)
        if m <= 0:
            continue
        n += 1
        total += m
        by_wf[str(r.get("name") or r.get("path") or "?")] += m
    return {"month": month, "minutes": total, "runs": n,
            "by_workflow": dict(sorted(by_wf.items(), key=lambda kv: -kv[1]))}


def fetch_runs(repo: str, token: str, month: str, max_pages: int = 20) -> list:
    """当月に作られた run を全ページ取る（1ページ100件）。"""
    out = []
    for page in range(1, max_pages + 1):
        url = (f"{API}/repos/{repo}/actions/runs?per_page=100&page={page}"
               f"&created=%3E%3D{month}-01")
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "stock-analyzer-actions-usage"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        runs = body.get("workflow_runs") or []
        out.extend(runs)
        if len(runs) < 100:
            break
    return out


def main() -> int:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    base = Path(os.environ.get("STOCK_ANALYZER_BASE", "").strip() or Path(__file__).resolve().parent / "stock_data")
    if not token or not repo:
        print("⏩ actions_usage: GH_TOKEN / GITHUB_REPOSITORY が無いのでスキップ")
        return 0
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    try:
        usage = summarize(fetch_runs(repo, token, month), month)
    except Exception as e:
        print(f"⚠️ actions_usage: 実行履歴を読めない（{type(e).__name__}: {str(e)[:120]}）")
        return 0
    usage["computed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    usage["method"] = "repo の actions/runs の開始〜終了を run ごとに分へ切り上げた合計（近似）"
    out = base / "logs" / FILENAME
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(usage, ensure_ascii=False, indent=2), encoding="utf-8")
    top = ", ".join(f"{k} {v}分" for k, v in list(usage["by_workflow"].items())[:4])
    print(f"⏱️ Actions 使用量（{month}）: {usage['minutes']}分 / {usage['runs']}回 — {top}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
