# ==============================================================
# obsidian_sync.py  v1.0  (Wave DEPT-2)
# GitHub Actions 出力 → iCloud Obsidian vault 同期ブリッジ
# ==============================================================
# 【背景 — なぜこの方式か】
#   GitHub Actions のランナーから iCloud Drive へ直接書き込む方法は
#   存在しない（iCloud にサーバーAPIが無い）。一方、daily_run.yml は
#   実行結果を「リポジトリへの git commit」と「GDrive バックアップ」の
#   両方に出力済みである。よって Mac 側の定期ジョブ（launchd/cron）が
#   git pull → vault へ転記する「ブリッジ」が最小・確実な解になる。
#   vault は iCloud 配下のローカルフォルダなので、Mac が書けば
#   iPhone/iPad へは iCloud が自動同期する。
#
# 【サブコマンド】
#   pull      GitHub リポジトリから stock_data 出力を取得し、
#             ① ローカル作業コピーの stock_data へミラー
#             ② vault の 05_最新データ/ へ主要成果物を転記
#   queue     15_審議キュー.md の自動セクションを再生成
#             （screener上位 + ドリフト + Ignition収束）[DEPT-2①]
#   journal   40_判断台帳.md の「承認」済み Buy 行を
#             investment_journal.json に記帳 [DEPT-2②]
#   feedback  investment_journal の成績を 40_判断台帳.md の
#             成績列に還流 [DEPT-2③]
#   all       pull → queue → journal → feedback を順に実行
#
# 【定期実行（launchd 例・毎朝9時）】
#   ~/Library/LaunchAgents/com.stockanalyzer.obsidian-sync.plist に
#   ProgramArguments: [python3, <本ファイル絶対パス>, all] /
#   StartCalendarInterval: {Hour: 9, Minute: 0} を設定して
#   `launchctl load` する（具体例は SPEC_investment_dept.md §4-2）。
#
# 【設定】
#   STOCK_ANALYZER_REPO_URL 環境変数 or --repo : GitHub リポジトリURL
#   config.py BLOCK 39 INVESTMENT_DEPT_VAULT_DIR : vault パス
# ==============================================================

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).parent
SYNC_CACHE = Path.home() / ".stock_analyzer_sync" / "repo"

AUTO_BEGIN = "<!-- AUTO-QUEUE:BEGIN（obsidian_sync.py が再生成。手動編集禁止） -->"
AUTO_END   = "<!-- AUTO-QUEUE:END -->"


def _load_config():
    spec = importlib.util.spec_from_file_location(
        "config", str(REPO_ROOT / "config.py"))
    cfg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cfg)
    base_env = os.environ.get("STOCK_ANALYZER_BASE", "")
    base = Path(base_env) if base_env else (
        Path(cfg.DRIVE_BASE) if Path(cfg.DRIVE_BASE).exists()
        else REPO_ROOT / "stock_data")
    cfg.DRIVE_BASE, cfg.DATA_DIR = base, base / "data"
    cfg.INSIGHT_DIR, cfg.LOG_DIR = base / "insights", base / "logs"
    cfg.CACHE_DIR = base / "cache"
    return cfg


def _vault(cfg) -> Path:
    v = Path(getattr(cfg, "INVESTMENT_DEPT_VAULT_DIR", ""))
    if not v.exists():
        raise SystemExit(f"❌ vault が見つかりません: {v}")
    return v


def _load_json(p: Path):
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


# ==============================================================
# pull: GitHub → ローカル stock_data + vault 05_最新データ/
# ==============================================================

def _git_with_retry(args: list, timeout: int, tries: int = 6, wait: int = 30):
    """ネットワーク未接続（スリープ復帰直後）を待って git を再試行する。

    2026-09-29: launchd はスリープ中に過ぎた 10:30 のジョブを復帰直後に走らせるが、
    その時点では Wi-Fi が未接続で「Could not resolve host: github.com」になり、
    30回中28回 pull に失敗していた（同期用cloneは 8/27 のまま）。
    """
    import time as _t
    r = None
    for i in range(tries):
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return r
        transient = any(k in (r.stderr or "") for k in (
            "Could not resolve host", "unable to access", "Connection", "timed out"))
        if not transient or i == tries - 1:
            break
        print(f"  ⏳ ネットワーク待ち（{i + 1}/{tries}）: {(r.stderr or '').strip()[:80]}")
        _t.sleep(wait)
    return r


def cmd_pull(cfg, repo_url: str) -> bool:
    repo_url = repo_url or os.environ.get("STOCK_ANALYZER_REPO_URL", "")
    if not repo_url:
        print("⏩ pull スキップ: リポジトリ未指定"
              "（--repo か STOCK_ANALYZER_REPO_URL を設定）")
        print("   ※ GDrive 同期クライアント等で stock_data を取得済みなら"
              " pull なしでも queue 以降は動作する")
        return
    SYNC_CACHE.parent.mkdir(parents=True, exist_ok=True)
    if (SYNC_CACHE / ".git").exists():
        r = _git_with_retry(["git", "-C", str(SYNC_CACHE), "pull", "--ff-only"], 300)
    else:
        r = _git_with_retry(["git", "clone", "--depth", "1", repo_url,
                             str(SYNC_CACHE)], 600)
    if r.returncode != 0:
        print(f"❌ git 失敗: {r.stderr[:200]}")
        return False
    print(f"✅ リポジトリ同期: {SYNC_CACHE}")

    # ① ローカル作業コピーの stock_data へミラー（パケットが濃くなる）
    src = SYNC_CACHE / "stock_data"
    if src.exists():
        for sub in ("data", "insights", "logs", "cache"):
            s = src / sub
            if s.exists():
                shutil.copytree(s, Path(cfg.DRIVE_BASE) / sub,
                                dirs_exist_ok=True)
        print(f"✅ stock_data ミラー: {cfg.DRIVE_BASE}")

    # ② vault 05_最新データ/ へ主要成果物を転記
    dest = _vault(cfg) / "05_最新データ"
    dest.mkdir(exist_ok=True)
    copied = []
    dash = Path(cfg.DATA_DIR) / "dashboard" / "dashboard_latest.csv"
    if dash.exists():
        shutil.copy2(dash, dest / dash.name)
        copied.append(dash.name)
    wr_dir = Path(cfg.INSIGHT_DIR) / "weekly"
    if wr_dir.exists():
        mds = sorted(wr_dir.glob("*.md"))
        if mds:
            shutil.copy2(mds[-1], dest / f"最新週次レポート_{mds[-1].name}")
            copied.append(mds[-1].name)
    ledger = Path(cfg.LOG_DIR) / "module_ledger_latest.json"
    if ledger.exists():
        shutil.copy2(ledger, dest / ledger.name)
        copied.append(ledger.name)
    (dest / "データ同期状況.md").write_text(
        f"# データ同期状況\n\n- 最終同期: "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"- 取得元: `{repo_url}`\n"
        f"- 転記: {', '.join(copied) if copied else 'なし'}\n",
        encoding="utf-8")
    print(f"✅ vault 転記: {dest}（{len(copied)}ファイル）")
    return True


# ==============================================================
# mirror: 審議済みティッカーを GitHub へ（2026-09-29・健太の要望で自動化）
# ==============================================================
MIRROR_REL = Path("stock_data") / "data" / "deliberated_tickers.json"


def cmd_mirror(cfg) -> bool:
    """vault の 40_判断台帳.md から審議済みティッカーを書き出し、変わっていれば
    同期用clone（SYNC_CACHE）でその1ファイルだけコミットして push する。

    Actions のメール（審議キュー）は vault を読めないため、このミラーで審議済みを
    除外している。以前は Mac で書き出したあと手作業でコミットする必要があった。
    作業中のリポジトリ（01_System）には触れない。
    """
    from deliberation_scheduler import _load_ledger_tickers
    tickers = sorted(_load_ledger_tickers(cfg))
    if not tickers:
        print("⏩ mirror: 判断台帳を読めない／審議済み0件（push しない）")
        return True
    if not (SYNC_CACHE / ".git").exists():
        print("⏩ mirror: 同期用cloneが無い（先に pull が必要）")
        return False
    target = SYNC_CACHE / MIRROR_REL
    try:
        current = sorted(json.loads(target.read_text(encoding="utf-8")).get("tickers") or [])
    except (OSError, ValueError):
        current = None
    # 作業コピー側にも書く（Mac上の queue / board が同じ集合を使う）
    try:
        from deliberation_scheduler import export_deliberated_mirror
        export_deliberated_mirror(cfg)
    except Exception:
        pass
    if current == tickers:
        print(f"✅ mirror: 変更なし（審議済み{len(tickers)}銘柄）")
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "tickers": tickers,
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "40_判断台帳.md",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    g = ["git", "-C", str(SYNC_CACHE)]
    added = sorted(set(tickers) - set(current or []))
    msg = f"auto: 審議済みミラー更新（{len(tickers)}銘柄" + (f"・追加 {', '.join(added)}" if added else "") + "）"
    for step in (g + ["add", str(MIRROR_REL)], g + ["commit", "-q", "-m", msg]):
        r = subprocess.run(step, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            print(f"❌ mirror: {' '.join(step[3:5])} 失敗: {(r.stderr or r.stdout)[:200]}")
            return False
    for attempt in range(2):
        r = _git_with_retry(g + ["push", "-q", "origin", "HEAD:main"], 120)
        if r.returncode == 0:
            print(f"✅ mirror: push 完了 — {msg}")
            return True
        # Actions の auto コミットと競合したら取り込み直して再送
        _git_with_retry(g + ["pull", "--rebase", "-q", "origin", "main"], 300)
    print(f"❌ mirror: push 失敗: {(r.stderr or '')[:200]}")
    return False


# ==============================================================
# queue: 審議キュー自動生成 [DEPT-2①]
# ==============================================================

def _build_queue_rows(cfg) -> list[str]:
    today = datetime.now().strftime("%Y-%m-%d")
    rows = []
    # screener 上位5（最新日）
    sh = _load_json(Path(cfg.DATA_DIR) / "screener_history.json")
    if sh:
        try:
            # 2026-07-25: 正本の形式は {date: [候補, ...]}（リスト）。
            # 従来は .get("candidates") をリストに対して呼んでおり
            # AttributeError → 下の except で握り潰され、審議キューの
            # screener 行が常に0件だった。さらに pass_filters で絞っていたが、
            # append_screener_history は通過銘柄だけを保存し、そのキー自体を
            # 持たせないため、形式を直しても全件落ちる二重の誤りだった。
            from performance_tracker import latest_candidates
            latest, cands = latest_candidates(sh)
            cands = sorted(
                cands, key=lambda c: -float(c.get("composite_score", 0) or 0))
            for c in cands[:5]:
                rows.append(f"| {today} | {c.get('ticker','')} "
                            f"| screener上位（{c.get('composite_score','')}点 "
                            f"{latest}） | 高 | 未着手 |")
        except Exception as e:
            print(f"  ⚠️ screener行の生成に失敗: {str(e)[:80]}")
    # ドリフト（直近・業種単位の警報）
    dh = _load_json(Path(cfg.INSIGHT_DIR) / "drift" / "drift_history.json")
    if dh:
        for d in [x for x in dh if isinstance(x, dict)][-3:]:
            rows.append(f"| {today} | （業種{d.get('code','')}） "
                        f"| ドリフト{d.get('drift_type','')} "
                        f"{d.get('delta','')}pt [{d.get('severity','')}] "
                        f"| 中 | 未着手 |")
    # Ignition 収束アラート
    ig = _load_json(Path(cfg.CACHE_DIR) / "ignition_alerts.json")
    if ig:
        alerts = ig.get("alerts", ig) if isinstance(ig, dict) else ig
        if isinstance(alerts, list):
            for a in alerts[:3]:
                if isinstance(a, dict):
                    rows.append(f"| {today} | {a.get('ticker', a.get('sector_code',''))} "
                                f"| Ignition収束 | 高 | 未着手 |")
    return rows


def cmd_queue(cfg) -> None:
    qp = _vault(cfg) / "15_審議キュー.md"
    rows = _build_queue_rows(cfg)
    auto = "\n".join([
        AUTO_BEGIN,
        f"## 自動キュー（最終更新: {datetime.now().strftime('%Y-%m-%d %H:%M')}）",
        "",
        "| 追加日 | ticker | 理由 | 優先度 | 状態 |",
        "|---|---|---|---|---|",
        *(rows if rows else ["| | （データなし — pull 実行または stock_data 同期後に再生成） | | | |"]),
        AUTO_END,
    ])
    src = qp.read_text(encoding="utf-8") if qp.exists() else "# 審議キュー\n"
    if AUTO_BEGIN in src and AUTO_END in src:
        src = re.sub(re.escape(AUTO_BEGIN) + r".*?" + re.escape(AUTO_END),
                     auto, src, flags=re.S)
    else:
        src = src.rstrip() + "\n\n" + auto + "\n"
    qp.write_text(src, encoding="utf-8")
    print(f"✅ 審議キュー更新: {len(rows)}件 → {qp}")


# ==============================================================
# journal / feedback: 判断台帳 ⇔ investment_journal [DEPT-2②③]
# ==============================================================

def _parse_ledger_rows(text: str) -> list[dict]:
    """40_判断台帳.md の表をパースする。"""
    rows = []
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 8 or cells[0] in ("case_id", "---", ""):
            continue
        if set(cells[0]) <= {"-", " "}:
            continue
        rows.append({
            "case_id": cells[0], "ticker": cells[1], "verdict": cells[2],
            "conviction": cells[3], "layer": cells[4], "open_issues": cells[5],
            "status": cells[6], "result": cells[7] if len(cells) > 7 else "",
            "_line": line,
        })
    return rows


def cmd_journal(cfg) -> None:
    """台帳の「承認」済み Buy 行を investment_journal に記帳（冪等）。"""
    # 2026-07-25: BLOCK R-3 の ENABLE_INVESTMENT_JOURNAL を配線。
    # investment_journal.py の実装はあるのに有効フラグが誰にも読まれておらず、
    # False にしても記帳・成績還流が止まらなかった。
    if not getattr(cfg, "ENABLE_INVESTMENT_JOURNAL", True):
        print("⏩ journal スキップ: ENABLE_INVESTMENT_JOURNAL=False")
        return
    lp = _vault(cfg) / "40_判断台帳.md"
    if not lp.exists():
        print("⏩ journal スキップ: 判断台帳なし")
        return
    sys.path.insert(0, str(REPO_ROOT))
    import investment_journal as ij
    journal = ij.load_journal(cfg)
    existing_cases = {e.get("source_case") for e in journal.get("entries", [])}
    added = 0
    # 「承認待ち」を部分一致で拾わないよう完全一致セットで判定する
    APPROVED = {"承認", "承認済", "承認済み"}
    for r in _parse_ledger_rows(lp.read_text(encoding="utf-8")):
        if r["status"].strip() not in APPROVED or r["verdict"] != "Buy":
            continue
        if r["case_id"] in existing_cases:
            continue  # 記帳済み（冪等性）
        today = datetime.now().strftime("%Y-%m-%d")
        journal["entries"].append({
            "id":          ij._make_entry_id(journal, r["ticker"], today),
            "ticker":      r["ticker"],
            "company":     r["ticker"],
            "sector_code": "",
            "source":      "投資部審議",
            "source_case": r["case_id"],
            "status":      "active",
            "entry": {
                "date":  today,
                "price": None,
                "reason": {
                    "conviction_level": r["conviction"],
                    "layer":            r["layer"],
                    "key_factors":      [f"投資部審議 {r['case_id']}"],
                    "report":           f"30_レポート/{r['case_id']}_判断レポート.md",
                },
            },
        })
        added += 1
    if added:
        ij.save_journal(cfg, journal)
    print(f"✅ journal 記帳: 新規{added}件（承認済みBuyのみ・冪等）")


def cmd_feedback(cfg) -> None:
    """journal の成績を台帳の成績列へ還流。"""
    # 2026-07-25: ENABLE_INVESTMENT_JOURNAL を配線（cmd_journal と同じ理由）。
    # feedback は journal の内容を読むため、journal を切ったら道連れで止める。
    if not getattr(cfg, "ENABLE_INVESTMENT_JOURNAL", True):
        print("⏩ feedback スキップ: ENABLE_INVESTMENT_JOURNAL=False")
        return
    lp = _vault(cfg) / "40_判断台帳.md"
    if not lp.exists():
        print("⏩ feedback スキップ: 判断台帳なし")
        return
    sys.path.insert(0, str(REPO_ROOT))
    import investment_journal as ij
    journal = ij.load_journal(cfg)
    by_case = {}
    for e in journal.get("entries", []):
        case = e.get("source_case")
        if not case:
            continue
        res = e.get("result", {}) or {}
        perf = (res.get("alpha_pct") or res.get("return_pct")
                or e.get("status", ""))
        if perf not in ("", None):
            by_case[case] = str(perf)
    src = lp.read_text(encoding="utf-8")
    updated = 0
    for r in _parse_ledger_rows(src):
        perf = by_case.get(r["case_id"])
        if not perf or r["result"]:
            continue
        cells = [c.strip() for c in r["_line"].strip().strip("|").split("|")]
        cells[7] = perf
        new_line = "| " + " | ".join(cells) + " |"
        src = src.replace(r["_line"], new_line)
        updated += 1
    if updated:
        lp.write_text(src, encoding="utf-8")
    print(f"✅ feedback 還流: {updated}件")


# ==============================================================

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Obsidian 同期ブリッジ（DEPT-2）")
    ap.add_argument("command", choices=["pull", "mirror", "queue", "journal",
                                        "feedback", "all"])
    ap.add_argument("--repo", default="", help="GitHub リポジトリURL")
    args = ap.parse_args()
    cfg = _load_config()
    ok = True
    if args.command in ("pull", "all"):
        ok = (cmd_pull(cfg, args.repo) is not False) and ok
    if args.command in ("mirror", "all"):
        ok = cmd_mirror(cfg) and ok
    if args.command in ("queue", "all"):
        cmd_queue(cfg)
    if args.command in ("journal", "all"):
        cmd_journal(cfg)
    if args.command in ("feedback", "all"):
        cmd_feedback(cfg)
    # 2026-09-29: 失敗しても rc=0 で終わっていたため、run_apps.log 上は毎日「成功」に
    # 見えていた。pull / mirror の失敗は非0で返す。
    sys.exit(0 if ok else 1)
