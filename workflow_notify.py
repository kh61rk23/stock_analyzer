# ==============================================================
# workflow_notify.py — 取得系の workflow が終わったら、取り込んだ内容をメールで知らせる
# ==============================================================
# 使い方（workflow の最後のステップ。if: always()）:
#   python workflow_notify.py --workflow fetch_jp_filings --title "日本企業の開示データ取得" \
#       --tables edinet.gov_documents,edinet.gov_doc_facts
# 中身:
#   ・各ステップの成否・秒数・✅⚠️❌の数と要点の行（actions_log.py が logs/step_results_<workflow>_<実行ID>.jsonl に残したもの）
#   ・--tables の表の行数と、前回この workflow が知らせた時からの増減（表のメタデータを読むだけ＝課金されない）
#   ・BigQuery の保存量（logs/bq_storage.json があれば）と、実行ページへのリンク
# 送り先は日次メールと同じ（ALERT_GMAIL_FROM / ALERT_GMAIL_PASSWORD / ALERT_GMAIL_TO）。WORKFLOW_NOTIFY_ENABLED=False で止める。
# メールの中身は Actions のログに出さない（public リポジトリのため）。ログには送ったかどうかだけを出す。
# 前回の行数は stock_data/data/workflow_notify_counts.json（状態として BigQuery に保存される）。
# ==============================================================
from __future__ import annotations

import argparse
import html
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

import actions_log


def read_steps(path: Path) -> list[dict]:
    try:
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    except (OSError, ValueError):
        return []


def table_counts(client, tables: list[str]) -> dict:
    out = {}
    for t in tables:
        try:
            x = client.get_table(f"{client.project}.{t}")
            out[t] = {"rows": int(x.num_rows or 0), "mb": round((x.num_bytes or 0) / 1e6, 2)}
        except Exception as e:
            out[t] = {"error": type(e).__name__}
    return out


def deltas(now: dict, before: dict) -> dict:
    """{表: 行数の増減}（前回が無い・読めない表は None）。"""
    out = {}
    for t, v in now.items():
        b = (before or {}).get(t) or {}
        out[t] = (v["rows"] - b["rows"]) if "rows" in v and "rows" in b else None
    return out


def build_mail(title: str, workflow: str, steps: list[dict], counts: dict, delta: dict,
               storage: dict | None, run_url: str, now: datetime) -> tuple[str, str]:
    """(件名, HTML 本文)（純関数）。"""
    failed = [s for s in steps if s.get("rc")]
    mark = "❌" if failed else ("⚠️" if any(s.get("err") for s in steps) else "✅")
    subject = f"📥 {mark} {title} {now:%m/%d %H:%M}"
    parts = [f"<h3>{html.escape(title)}（{html.escape(workflow)}）{mark}</h3>"]
    if not steps:
        parts.append("<div>ステップの記録が無い（取り込みの前に止まった可能性。実行ページを見る）</div>")
    for s in steps:
        head = (f"{'✅' if not s.get('rc') else '❌'} <b>{html.escape(s['step'])}</b>"
                f"（{s.get('seconds', 0)}秒・✅{s.get('ok', 0)} ⚠️{s.get('warn', 0)} ❌{s.get('err', 0)}）")
        items = "".join(f"<li>{html.escape(h)}</li>" for h in s.get("highlights") or [])
        parts.append(f"<div style='margin-top:8px'>{head}<ul style='margin:2px 0'>{items}</ul></div>")
    if counts:
        rows = ""
        for t, v in counts.items():
            d = delta.get(t)
            dtxt = "—" if d is None else f"{d:+,}"
            body = (f"<td>{v['rows']:,}</td><td>{dtxt}</td><td>{v['mb']:,}</td>" if "rows" in v
                    else f"<td colspan=3>読めない（{html.escape(v.get('error', ''))}）</td>")
            rows += f"<tr><td>{html.escape(t)}</td>{body}</tr>"
        parts.append("<h4>取り込み先の表</h4><table border=1 cellpadding=4 style='border-collapse:collapse;font-size:12px'>"
                     "<tr><th>表</th><th>行数</th><th>前回からの増減</th><th>MB</th></tr>" + rows + "</table>")
    if storage and storage.get("total_gb") is not None:
        parts.append(f"<div style='margin-top:8px'>BigQuery の保存量 {storage['total_gb']}GB（無料枠 10GB/月・"
                     f"測った時刻 {html.escape(str(storage.get('measured_at', '')))}）</div>")
    if run_url:
        parts.append(f"<div style='margin-top:8px'><a href='{html.escape(run_url)}'>実行ページ</a></div>")
    return subject, "\n".join(parts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflow", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--tables", default="", help="行数の増減を見る表（dataset.table をカンマ区切り）")
    ap.add_argument("--base", default=os.environ.get("STOCK_ANALYZER_BASE") or "stock_data")
    a = ap.parse_args(argv)
    import config as cfg
    if not getattr(cfg, "WORKFLOW_NOTIFY_ENABLED", True):
        print("⏩ 通知: WORKFLOW_NOTIFY_ENABLED=False")
        return 0
    base = Path(a.base)
    steps = read_steps(actions_log.results_path(base, a.workflow))
    tables = [t.strip() for t in a.tables.split(",") if t.strip()]
    counts, delta = {}, {}
    state_p = base / "data" / "workflow_notify_counts.json"
    try:
        state = json.loads(state_p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if tables:
        try:
            import state_store
            counts = table_counts(state_store.make_client(cfg), tables)
            delta = deltas(counts, state.get(a.workflow))
            state[a.workflow] = counts
            state_p.parent.mkdir(parents=True, exist_ok=True)
            state_p.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            print(f"⚠️ 通知: 表の行数を読めない（{type(e).__name__}）")
    try:
        storage = json.loads((base / "logs" / "bq_storage.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        storage = None
    srv, repo, rid = (os.environ.get(k, "") for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    run_url = f"{srv}/{repo}/actions/runs/{rid}" if srv and repo and rid else ""
    subject, body = build_mail(a.title, a.workflow, steps, counts, delta, storage, run_url,
                               datetime.now(ZoneInfo("Asia/Tokyo")))
    from alert_notifier import send_gmail
    ok, _err = send_gmail(os.environ.get("ALERT_GMAIL_FROM", ""), os.environ.get("ALERT_GMAIL_PASSWORD", ""),
                          os.environ.get("ALERT_GMAIL_TO", ""), subject, body)
    # 送れなかった理由はアドレスを含むことがあるので出さない（鍵の死活確認の Gmail 行で原因を見る）
    print("✅ 通知メールを送った" if ok else "⚠️ 通知メールを送れなかった")
    return 0


if __name__ == "__main__":
    sys.exit(main())
