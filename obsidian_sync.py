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
#   deliberations  クラウド審議の結果を vault（30_レポート/20_審議/40_判断台帳）へ新規分だけミラー
#   approvals      台帳の承認を invest_approvals.json に書いて push（DB へは Actions が反映）
#   all       pull → deliberations → mirror → approvals → holdings → queue → journal → feedback → rules
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

_OFFLINE = False


def _git_with_retry(args: list, timeout: int, tries: int = 6, wait: int = 30):
    """ネットワーク未接続（スリープ復帰直後）を待って git を再試行する。

    launchd はスリープ中に過ぎた 10:30 のジョブを復帰直後に走らせるが、
    その時点では Wi-Fi が未接続で「Could not resolve host: github.com」になり
    やすい。再試行しないと pull がほぼ毎回失敗し、同期用cloneが古いまま残る。
    """
    import time as _t
    global _OFFLINE
    r = None
    if _OFFLINE:
        tries = 1
    for i in range(tries):
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return r
        transient = any(k in (r.stderr or "") for k in (
            "Could not resolve host", "unable to access", "Connection", "timed out"))
        if not transient or i == tries - 1:
            if transient and tries > 1:
                # 待ってもつながらなかった。この回の残りの git は再試行せずに次回へ回す
                # （工程ごとに 30秒×6回 待つと、オフラインの日は同期が何時間も終わらない）
                _OFFLINE = True
                print("  📴 ネットワークにつながらないため、この回の残りの git は待たずに次回へ回す")
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
        # 同期用 clone には mirror / approvals / holdings の自動コミットが積まれる。ネットワーク断で push できなかった
        # コミットが残ると早送りできず、以後の pull が毎回失敗する（審議結果が vault に届かなくなる）。
        # rebase で GitHub の後ろに並べ直し、取り残したコミットはここで送り出す。
        g = ["git", "-C", str(SYNC_CACHE)]
        r = _git_with_retry(g + ["pull", "--rebase", "--autostash", "-q", "origin", "main"], 300)
        if r.returncode != 0:
            subprocess.run(g + ["rebase", "--abort"], capture_output=True, text=True, timeout=60)
        else:
            ahead = subprocess.run(g + ["rev-list", "--count", "origin/main..HEAD"], capture_output=True, text=True, timeout=60)
            if (ahead.stdout or "0").strip() not in ("", "0"):
                pr = _git_with_retry(g + ["push", "-q", "origin", "HEAD:main"], 120)
                print(f"{'✅' if pr.returncode == 0 else '⚠️'} 取り残した自動コミット {ahead.stdout.strip()}件の push"
                      + ("" if pr.returncode == 0 else f" 失敗（次回再試行）: {(pr.stderr or '')[:120]}"))
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
# mirror: 審議済みティッカーを GitHub へ
# ==============================================================
MIRROR_REL = Path("stock_data") / "data" / "deliberated_tickers.json"


def cmd_mirror(cfg) -> bool:
    """vault の 40_判断台帳.md から審議済みティッカーを書き出し、変わっていれば
    同期用clone（SYNC_CACHE）でその1ファイルだけコミットして push する。

    Actions のメール（審議キュー）は vault を読めないため、このミラーで審議済みを
    除外している。
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
    def _copy_to_worktree() -> None:
        """作業コピー側にも同じ中身を置く（Mac上の queue / board が同じ集合を使う）。
        別の時刻で書き直すと GitHub 側と食い違い、作業コピーの pull が毎回衝突するため、clone の版をそのまま写す。"""
        try:
            dst = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / MIRROR_REL.name
            if target.exists() and dst.parent.exists():
                dst.write_bytes(target.read_bytes())
        except OSError:
            pass
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        prev = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prev = {}
    # 審議済みが変わらなくても1日1回は照合日（verified_at）を上げる。Actions のメールは
    # これを見て、ミラーを正本と同じに扱えるか（台帳と最近照合されているか）を判断する。
    if current == tickers and str(prev.get("verified_at", ""))[:10] == today:
        _copy_to_worktree()
        print(f"✅ mirror: 変更なし（審議済み{len(tickers)}銘柄・本日照合済み）")
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "tickers": tickers,
        "exported_at": (prev.get("exported_at") if current == tickers and prev.get("exported_at")
                        else datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        "verified_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "40_判断台帳.md",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    g = ["git", "-C", str(SYNC_CACHE)]
    added = sorted(set(tickers) - set(current or []))
    msg = (f"auto: 審議済みミラー更新（{len(tickers)}銘柄" + (f"・追加 {', '.join(added)}" if added else "") + "）"
           if current != tickers else f"auto: 審議済みミラー照合（{len(tickers)}銘柄・変更なし）")
    for step in (g + ["add", str(MIRROR_REL)], g + ["commit", "-q", "-m", msg]):
        r = subprocess.run(step, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            print(f"❌ mirror: {' '.join(step[3:5])} 失敗: {(r.stderr or r.stdout)[:200]}")
            return False
    for attempt in range(2):
        r = _git_with_retry(g + ["push", "-q", "origin", "HEAD:main"], 120)
        if r.returncode == 0:
            _copy_to_worktree()
            print(f"✅ mirror: push 完了 — {msg}")
            return True
        # Actions の auto コミットと競合したら取り込み直して再送
        _git_with_retry(g + ["pull", "--rebase", "-q", "origin", "main"], 300)
    print(f"❌ mirror: push 失敗: {(r.stderr or '')[:200]}")
    return False


# ==============================================================
# deliberations: クラウド審議の結果 → vault（30_レポート / 20_審議 / 40_判断台帳）
# approvals    : 台帳の承認 → invest_approvals.json → GitHub（Actions が DB へ反映）
# ==============================================================
DELIB_JSON = "invest_deliberations.json"
APPROVALS_REL = Path("stock_data") / "data" / "invest_approvals.json"
APPROVED_BY = "健太（台帳）"
_APPROVAL_RE = re.compile(r"^(承認待ち|承認済み?|承認|否認|却下|保留|見送り)")
_DATE_RE = re.compile(r"^\s*[（(]\s*(\d{4}-\d{2}-\d{2})")


def parse_approval(status: str) -> tuple:
    """台帳の状態列 → (approval_status, approved_at)。承認待ち・解釈不能は (None, None)。

    「**承認（2026-09-29）**＝…」「否認」「保留」等。承認済・承認済みは「承認」、却下は「否認」に寄せる。"""
    t = (status or "").replace("*", "").strip()
    m = _APPROVAL_RE.match(t)
    if not m or m.group(1) == "承認待ち":
        return None, None
    word = {"承認済": "承認", "承認済み": "承認", "却下": "否認"}.get(m.group(1), m.group(1))
    d = _DATE_RE.match(t[m.end():m.end() + 40])
    return word, (d.group(1) if d else None)


def build_approvals(ledger_text: str) -> dict:
    """台帳全体 → {case_id: {approval_status, approved_at}}（承認待ちの行は含めない）。"""
    out = {}
    for r in _parse_ledger_rows(ledger_text):
        st, day = parse_approval(r["status"])
        if st:
            out[r["case_id"]] = {"approval_status": st, "approved_at": day}
    return out


def _cell(v) -> str:
    """表のセルに入れられる1行の文字列にする（| と改行を除く）。"""
    return re.sub(r"\s+", " ", str(v if v is not None else "").replace("|", "／")).strip()


def ledger_row_for(rec: dict) -> str:
    """DB の審議1件 → 台帳の1行（既存行と同じ8列）。状態は approval_status（日付つき）か「承認待ち」。"""
    st = rec.get("approval_status") or "承認待ち"
    day = str(rec.get("approved_at") or "")[:10]
    if rec.get("approval_status") and day:
        st = f"{st}（{day}）"
    if rec.get("holding_intent"):
        st += f"／保有意図: {rec['holding_intent']}"
    cells = [rec.get("case_id"), rec.get("ticker"), rec.get("verdict"), rec.get("conviction"),
             rec.get("layer"), rec.get("devil_unresolved") if rec.get("devil_unresolved") is not None else 0,
             st]
    return "| " + " | ".join(_cell(c) for c in cells) + " | |"


def mirror_deliberations_to_vault(records: list, bdir: Path, today: str | None = None) -> dict:
    """DB の審議結果のうち vault に無いものだけ書く。既存のファイル・台帳行は触らない。

    書くもの: 30_レポート/<case>_判断レポート.md、20_審議/<case>/{bull,bear,devil,packet}.md、
    40_判断台帳.md の末尾表に1行。台帳を書き換える前に .bak_YYYYMMDD を同フォルダに1度だけ作る。
    人間が台帳や報告書に書いた承認を壊さないため、上書きはしない（冪等）。"""
    today = today or datetime.now().strftime("%Y%m%d")
    ledger = bdir / "40_判断台帳.md"
    text = ledger.read_text(encoding="utf-8") if ledger.exists() else ""
    known = {r["case_id"] for r in _parse_ledger_rows(text)}
    stats = {"reports": 0, "memos": 0, "ledger_rows": 0, "skipped": 0}
    new_lines = []
    for rec in records:
        cid = str(rec.get("case_id") or "").strip()
        if not cid or "/" in cid or cid.startswith("."):
            continue
        wrote = False
        rp = bdir / "30_レポート" / f"{cid}_判断レポート.md"
        if rec.get("report_md") and not rp.exists():
            rp.parent.mkdir(parents=True, exist_ok=True)
            rp.write_text(rec["report_md"], encoding="utf-8")
            stats["reports"] += 1
            wrote = True
        for key, name in (("bull_memo", "bull"), ("bear_memo", "bear"),
                          ("devil_memo", "devil"), ("packet_md", "packet")):
            mp = bdir / "20_審議" / cid / f"{name}.md"
            if rec.get(key) and not mp.exists():
                mp.parent.mkdir(parents=True, exist_ok=True)
                mp.write_text(rec[key], encoding="utf-8")
                stats["memos"] += 1
                wrote = True
        if cid not in known:
            new_lines.append(ledger_row_for(rec))
            known.add(cid)
            stats["ledger_rows"] += 1
            wrote = True
        if not wrote:
            stats["skipped"] += 1
    if new_lines:
        bak = ledger.with_name(f"{ledger.name}.bak_{today}")
        if ledger.exists() and not bak.exists():
            shutil.copy2(ledger, bak)
        lines = text.splitlines()
        last = max((i for i, l in enumerate(lines) if l.strip().startswith("|")), default=None)
        if last is None:
            lines = lines + [""] + new_lines
        else:
            lines[last + 1:last + 1] = new_lines
        ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return stats


def _find_deliberations_json(cfg) -> Path | None:
    for p in (SYNC_CACHE / "stock_data" / "data" / DELIB_JSON, Path(cfg.DATA_DIR) / DELIB_JSON):
        if p.exists():
            return p
    return None


def cmd_deliberations(cfg) -> bool:
    """クラウド審議の結果（invest_deliberations.json）を vault へミラーする。pull の後に実行する。"""
    src = _find_deliberations_json(cfg)
    if src is None:
        print("⏩ deliberations: invest_deliberations.json が無い（pull 後に再実行）")
        return True
    data = _load_json(src) or {}
    recs = data.get("deliberations") or []
    st = mirror_deliberations_to_vault(recs, _vault(cfg))
    print(f"✅ deliberations: 全{len(recs)}件 → レポート{st['reports']}・メモ{st['memos']}"
          f"・台帳行{st['ledger_rows']}を新規書き込み（既存{st['skipped']}件は触らない）")
    return True


def cmd_approvals(cfg) -> bool:
    """台帳の承認状態を stock_data/data/invest_approvals.json に書き、変化があるときだけ
    同期用clone（SYNC_CACHE）でその1ファイルを commit して push する（BQ 認証は Mac に置かない）。
    Actions の deliberation_queue.apply_approvals が差分だけ invest.deliberations に反映する。"""
    lp = _vault(cfg) / "40_判断台帳.md"
    approvals = build_approvals(lp.read_text(encoding="utf-8")) if lp.exists() else {}
    if not approvals:
        print("⏩ approvals: 台帳に承認済みの行が無い／読めない（push しない）")
        return True
    if not (SYNC_CACHE / ".git").exists():
        print("⏩ approvals: 同期用cloneが無い（先に pull が必要）")
        return False
    target = SYNC_CACHE / APPROVALS_REL
    prev = (_load_json(target) or {}).get("approvals")
    if prev == approvals:
        print(f"✅ approvals: 変更なし（{len(approvals)}件）")
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "approvals": approvals, "approved_by": APPROVED_BY,
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "40_判断台帳.md"}, ensure_ascii=False, indent=1), encoding="utf-8")
    g = ["git", "-C", str(SYNC_CACHE)]
    changed = sorted(c for c in approvals if (prev or {}).get(c) != approvals[c])
    msg = f"auto: 承認ミラー更新（{len(approvals)}件・変更 {len(changed)}件）"
    for step in (g + ["add", str(APPROVALS_REL)], g + ["commit", "-q", "-m", msg]):
        r = subprocess.run(step, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            print(f"❌ approvals: {' '.join(step[3:5])} 失敗: {(r.stderr or r.stdout)[:200]}")
            return False
    for _ in range(2):
        r = _git_with_retry(g + ["push", "-q", "origin", "HEAD:main"], 120)
        if r.returncode == 0:
            print(f"✅ approvals: push 完了 — {msg}")
            return True
        _git_with_retry(g + ["pull", "--rebase", "-q", "origin", "main"], 300)
    print(f"❌ approvals: push 失敗: {(r.stderr or '')[:200]}")
    return False


def cmd_holdings(cfg) -> bool:
    """保有銘柄.csv（投資部が手で更新）→ data/portfolio_holdings.csv / portfolio_summary.json を
    同期用clone（SYNC_CACHE）で作り、変わっていればその2ファイルだけ commit & push する。

    週次の総評価額・ポジションサイジング・ストレステストはリポジトリ側の
    保有CSVで計算するため、これが更新されないと古い保有で計算される。
    """
    if not (SYNC_CACHE / ".git").exists():
        print("⏩ holdings: 同期用cloneが無い（先に pull が必要）")
        return False
    script = SYNC_CACHE / "sync_holdings.py"
    if not script.exists():
        print("⏩ holdings: sync_holdings.py が clone に無い（pull 後に再実行）")
        return True
    r = subprocess.run([sys.executable, str(script), "--commit"], cwd=SYNC_CACHE,
                       capture_output=True, text=True, timeout=300)
    print((r.stdout or "").strip() or (r.stderr or "").strip()[:300])
    return r.returncode == 0


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
            # 正本の形式は {date: [候補, ...]}（リスト）。.get("candidates") は
            # 使えない。また append_screener_history は通過銘柄だけを保存し
            # pass_filters キーを持たせないため、pass_filters で絞ると全件落ちる。
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
    # BLOCK R-3 の ENABLE_INVESTMENT_JOURNAL=False で記帳を止める。
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
    # ENABLE_INVESTMENT_JOURNAL で止める（cmd_journal と同じフラグ）。
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


def cmd_rules() -> bool:
    """投資部の部則・審議スキル（vault が正本）に変更があれば GitHub へ同期・push する。

    クラウドの定時審議はリポジトリ内のコピー（investment_dept/・.claude/skills/）しか読めないため、
    vault で部則を直したら次の定期実行でここが反映する。差分が無ければ commit も push もしない。
    """
    import subprocess
    r = subprocess.run([sys.executable, str(REPO_ROOT / "tools" / "sync_invest_rules.py"), "--apply"],
                       cwd=REPO_ROOT, text=True, capture_output=True)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.strip()[-1500:])
    if r.returncode != 0:
        print(f"⚠️ 部則の同期に失敗（exit {r.returncode}）")
        return False
    return True


# ==============================================================

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Obsidian 同期ブリッジ（DEPT-2）")
    ap.add_argument("command", choices=["pull", "mirror", "approvals", "deliberations", "holdings", "queue", "journal",
                                        "feedback", "rules", "all"])
    ap.add_argument("--repo", default="", help="GitHub リポジトリURL")
    args = ap.parse_args()
    cfg = _load_config()
    ok = True
    if args.command in ("pull", "all"):
        ok = (cmd_pull(cfg, args.repo) is not False) and ok
    if args.command in ("deliberations", "all"):
        ok = cmd_deliberations(cfg) and ok
    if args.command in ("mirror", "all"):
        ok = cmd_mirror(cfg) and ok
    if args.command in ("approvals", "all"):
        ok = cmd_approvals(cfg) and ok
    if args.command in ("holdings", "all"):
        ok = cmd_holdings(cfg) and ok
    if args.command in ("queue", "all"):
        cmd_queue(cfg)
    if args.command in ("journal", "all"):
        cmd_journal(cfg)
    if args.command in ("feedback", "all"):
        cmd_feedback(cfg)
    if args.command in ("rules", "all"):
        ok = cmd_rules() and ok
    # pull / mirror の失敗は非0で返す（rc=0 だと run_apps.log 上は「成功」に見える）。
    sys.exit(0 if ok else 1)
