# ==============================================================
# fetch_runner.py — 取得専用 workflow の入口
#   python fetch_runner.py jp_filings
# fetch_stages の取得グループを1つ動かし、結果の要約と実行台帳の記録を
# stock_data/data/fetch_external/<group>_<日付>.json に残す（日次本体が読んで取得を省く）。
# 全部が error の時はファイルを残さず終了コード 1（日次本体がその回で取り直す）。
# ==============================================================
from __future__ import annotations

import argparse
import sys

import config as cfg
import fetch_stages
from run_headless import apply_env_overrides


_MARKS = {"success": "✅", "skipped": "⏩", "warn": "⚠️", "alert": "❌", "error": "❌"}


def entry_line(e: dict) -> str:
    """台帳の1件を通知が拾える印つきの行にする（印の無い行は通知の要点に出ない）。"""
    return f"  {_MARKS.get(e['status'], '⚠️')} [{e['status']}] {e['module']}: {str(e['note'])[:160]}"


def summary_line(group: str, entries: list, filename: str) -> str:
    """結果の1行。成功だけのときだけ ✅。警告・失敗があれば件数と対象のモジュールを出し、印は ⚠️（全部が失敗なら呼び出し側が終了する）。"""
    count = {k: sum(1 for e in entries if e["status"] == k) for k in ("success", "skipped", "warn", "alert", "error")}
    bad = [e["module"] for e in entries if e["status"] in ("warn", "alert", "error")]
    body = (f"{len(entries)}モジュール（成功{count['success']}・見送り{count['skipped']}・警告{count['warn']}"
            f"・失敗{count['alert'] + count['error']}）")
    if bad:
        return f"⚠️ {group}: {body}・要確認 {'、'.join(bad)} → {filename}"
    return f"✅ {group}: {body} → {filename}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("group", choices=sorted(fetch_stages.GROUPS))
    a = ap.parse_args(argv)
    apply_env_overrides(cfg)
    results: dict = {}
    rec = fetch_stages.LedgerRecorder()
    fetch_stages.RUNNERS[a.group](cfg, results, rec)
    for e in rec.entries:
        print(entry_line(e))
    if not rec.entries or all(e["status"] == "error" for e in rec.entries):
        print(f"❌ {a.group}: 取得できなかったので結果を残さない（日次本体が取り直す）")
        return 1
    p = fetch_stages.save_external(cfg, a.group, results, rec.entries)
    print(summary_line(a.group, rec.entries, p.name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
