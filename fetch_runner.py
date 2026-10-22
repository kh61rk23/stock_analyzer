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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("group", choices=sorted(fetch_stages.GROUPS))
    a = ap.parse_args(argv)
    apply_env_overrides(cfg)
    results: dict = {}
    rec = fetch_stages.LedgerRecorder()
    fetch_stages.RUNNERS[a.group](cfg, results, rec)
    for e in rec.entries:
        print(f"  [{e['status']}] {e['module']}: {e['note'][:160]}")
    if not rec.entries or all(e["status"] == "error" for e in rec.entries):
        print(f"❌ {a.group}: 取得できなかったので結果を残さない（日次本体が取り直す）")
        return 1
    p = fetch_stages.save_external(cfg, a.group, results, rec.entries)
    print(f"✅ {a.group}: 記録 {len(rec.entries)}件 → {p.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
