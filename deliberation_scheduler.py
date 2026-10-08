# ==============================================================
# deliberation_scheduler.py — B部門隔週自動審議の準備（E3）
# ==============================================================
# 【役割】隔週日曜のCowork審議を「承認だけで走る」状態にする軽量版
#   （DR3-1推奨案: 手順書＋プロンプトテンプレ＋packet連携まで。
#   スケジュール登録＝Coworkの定期実行設定は人が手動で行う）。
# 【処理】
#   ① 審議対象1銘柄を選定（screener上位×未審議＝40_判断台帳.md 除外）
#   ② agent_packet_exporter v2 で審議パケット生成（ontology＋E8込み）
#   ③ B1〜B4（ブル/ベア/デビル/レポーター）の起動プロンプトを
#      20_審議/{case_id}/_起動プロンプト.md に出力（相互不可視の指示込み）
#   ④ 次アクション（Coworkでサブエージェント起動）を提示
# 【モデル既定 D-12】ブル/ベア=Sonnet 5、デビル/レポーター=Opus 4.8（Fable温存）。
#   コスト＝Coworkサブスク内・API課金なし（費用ゼロ原則）。
# 使い方（Coworkスケジュール or 手動）:
#   python3 deliberation_scheduler.py            # 対象選定→packet→プロンプト生成
#   python3 deliberation_scheduler.py --ticker 7203.T   # 対象を明示指定
#   python3 deliberation_scheduler.py --dry-run  # 選定のみ（vault書込なし）
# ==============================================================

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


# ── 純粋ロジック（pytest対象） ────────────────────────────────

def build_deliberation_queue(candidates: list, already: set,
                             top_n: int = 5) -> list:
    """
    composite_score 降順で「未審議」の候補を最大 top_n 件返す（審議キュー）。

    日次・週次メールに「次に審議へ上げる候補」を一覧で出すために使う。
    **select_deliberation_target はこの関数の先頭要素を返すだけの薄い
    ラッパーなので、メールに出る並びと隔週ジョブが実際に選ぶ銘柄は
    必ず一致する。**

    Args:
        candidates: [{"ticker","composite_score","sector_code",...}]
        already:    既に審議済みの ticker 集合
        top_n:      返す最大件数

    Returns:
        composite_score 降順のリスト（0件なら空リスト）
    """
    ranked = sorted(
        (c for c in candidates
         if c.get("ticker") and c.get("composite_score") is not None
         and c["ticker"] not in already),
        # 同点（100点張り付き等）は加点前の点（base_score）で並べる
        key=lambda c: (-float(c["composite_score"]),
                       -float(c.get("base_score") or c["composite_score"])))
    return ranked[:max(0, int(top_n))]


def select_deliberation_target(candidates: list, already: set) -> dict | None:
    """
    composite_score 降順で「未審議」の先頭候補を返す。
    candidates: [{"ticker","composite_score","sector_code",...}]
    already: 既に審議済みの ticker 集合（40_判断台帳から抽出）
    Returns: 選定候補dict / なければ None

    ※ 実体は build_deliberation_queue の先頭。メール表示と選定を
      同一ロジックに揃えるための委譲。
    """
    queue = build_deliberation_queue(candidates, already, top_n=1)
    return queue[0] if queue else None


def parse_deliberated_tickers(ledger_text: str) -> set:
    """40_判断台帳.md から既審議 ticker を抽出（wikilink/コード表記の両対応）。"""
    if not ledger_text:
        return set()
    # 表の ticker 列（見出し行で位置を特定）だけを読む。本文全体から
    # 「4桁数字・大文字1〜5字」を拾うと、状態欄の "2026"・"DV-8"・"D-03" の
    # "DEPT" などを審議済みと誤認し、英字入りの証券コード（543A.T・646A.T）を
    # 取りこぼす。
    out: set = set()
    # 見出し行が無い表（旧形式・テスト）は「2列目＝ticker」とみなす
    col = None if re.search(r"\|\s*ticker\s*\|", ledger_text, re.I) else 1
    code_re = re.compile(r"^(?:\d{3}[0-9A-Z]\.T|\d{3}[0-9A-Z]|[A-Z]{1,5})$")
    for line in ledger_text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if "ticker" in [c.lower() for c in cells]:
            col = [c.lower() for c in cells].index("ticker")
            continue
        if col is None or col >= len(cells):
            continue
        raw = cells[col].replace("[[", "").replace("]]", "").strip()
        if code_re.match(raw):
            out.add(raw if not re.match(r"^\d{3}[0-9A-Z]$", raw) else raw + ".T")
    return out


# ── プロンプトテンプレ（D-12モデル既定） ─────────────────────

_COMMON = ("あなたはB投資部の担当アナリストです。審議パケット（唯一の数値正本）と"
           "自分の部則のみを読み、他アナリストのメモは見ないこと（相互不可視）。"
           "結論は末尾YAML（conviction 1-5 / 確度% / 主要論点ID）で構造化する。")

PROMPTS = {
    "bull": {
        "model": "Sonnet 5",
        "role": "①ブルアナリスト",
        "prompt": _COMMON + " あなたは強気仮説のみを構築する。リスクは書かない"
                  "（それはベア/デビルの職掌）。packetの数値を根拠に上昇シナリオを提示。"
                  "出力: 20_審議/{case_id}/01_ブル.md",
    },
    "bear": {
        "model": "Sonnet 5",
        "role": "②ベアアナリスト",
        "prompt": _COMMON + " あなたは弱気仮説のみを構築する。強気材料は書かない。"
                  "packetの数値を根拠に下落・停滞シナリオを提示。"
                  "出力: 20_審議/{case_id}/02_ベア.md",
    },
    "devil": {
        "model": "Opus 4.8",
        "role": "③デビルアナリスト",
        "prompt": _COMMON + " あなたは検証者。bull.md と bear.md と packet を突き合わせ、"
                  "両仮説の論証の弱点・反証・データ鮮度/二重計上を指摘し、論点別に信頼度A/B/Cを付す。"
                  "独自の相場観や新規の買い/売り理由は持ち込まない。"
                  "出力: 20_審議/{case_id}/03_デビル.md",
    },
    "reporter": {
        "model": "Opus 4.8",
        "role": "④レポートエージェント",
        "prompt": _COMMON + " あなたは統合者。3メモ＋packet＋strategy分類から最終判断"
                  "（Buy/Watch/Avoid＋conviction）・両論要約・デビル指摘の解消状況表・"
                  "推奨サイズ（position_sizer v2の内訳参照）・人間承認欄を作る。"
                  "3メモにない新規論拠は追加しない。"
                  "出力: 30_レポート/{case_id}_判断レポート.md ＋ 40_判断台帳.md 追記",
    },
}


def build_launch_prompts(case_id: str, ticker: str) -> str:
    """B1〜B4の起動プロンプトMDを組み立てる（純粋関数）。"""
    lines = [f"# 審議起動プロンプト: {ticker}（case: {case_id}）",
             "",
             "> 隔週自動審議（E3）の起動手順。**Coworkで下記を順に実行**。",
             "> ①②は相互不可視で**並列**起動可（各エージェントに自分の部則＋packetのみ渡す）。",
             "> ③は①②完了後、④は③完了後（順序厳守）。API課金なし＝Coworkサブスク内。",
             ""]
    order = [("bull", "並列可"), ("bear", "並列可"),
             ("devil", "①②完了後"), ("reporter", "③完了後")]
    for key, timing in order:
        p = PROMPTS[key]
        lines += [f"## {p['role']}  （モデル: **{p['model']}** / {timing}）",
                  "```",
                  p["prompt"].replace("{case_id}", case_id),
                  "```",
                  ""]
    lines += ["## 完了後",
              "- ⑤ 人間承認欄付きレポートを健太が確認（Buy/Watch/Avoid）",
              "- ⑥ 完了を翌朝ブリーフに掲載（未完了なら『審議未完了』表示）",
              "- 途中停止時は途中成果物を 20_審議/ に残し、翌日再開する"]
    return "\n".join(lines) + "\n"


# ── I/O層 ────────────────────────────────────────────────────

def _load_screener_candidates(cfg) -> list:
    data_dir = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
    sh = data_dir / "screener_history.json"
    try:
        data = json.loads(sh.read_text(encoding="utf-8"))
    except Exception as e:
        # 握り潰さずに出す（形式不一致などの原因が「候補が空」に埋もれないように）。
        print(f"  ⚠️ screener_history 読込失敗: {str(e)[:80]}")
        return []
    from performance_tracker import latest_candidates
    _, cands = latest_candidates(data)
    return cands


def _load_ledger_tickers(cfg) -> set:
    vault = Path(getattr(cfg, "INVESTMENT_DEPT_VAULT_DIR", ""))
    ledger = vault / "40_判断台帳.md"
    try:
        return parse_deliberated_tickers(ledger.read_text(encoding="utf-8"))
    except Exception:
        return set()


# ── 審議済みティッカーの解決（メール表示用）────────────
#
# 【なぜ複数ソースが要るか】
#   審議済みの正本は Obsidian vault の 40_判断台帳.md だが、これは Mac 上
#   にしか無い。日次・週次メールを生成する GitHub Actions からは読めない。
#   除外集合が空のままだと「審議済みの銘柄がキュー先頭に出続ける」ので、
#   リポジトリ内にある記録で補完する。
#
# 優先順:
#   ① vault の 40_判断台帳.md（正本。Mac 実行時のみ読める）
#   ② stock_data/data/deliberated_tickers.json
#      （obsidian_sync が vault から書き出すリポジトリ側ミラー）
#   ③ investment_journal.json のティッカー
#      （※承認済みBuy行のみ＝審議したが見送った銘柄は含まれない部分集合）

_DELIBERATED_MIRROR = "deliberated_tickers.json"


def _load_journal_tickers(cfg) -> set:
    data_dir = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
    try:
        raw = json.loads(
            (data_dir / "investment_journal.json").read_text(encoding="utf-8"))
    except Exception:
        return set()
    entries = raw.get("entries", raw) if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return set()
    return {str(e.get("ticker")) for e in entries
            if isinstance(e, dict) and e.get("ticker")}


def _load_mirror_tickers(cfg) -> set:
    data_dir = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
    try:
        raw = json.loads(
            (data_dir / _DELIBERATED_MIRROR).read_text(encoding="utf-8"))
    except Exception:
        return set()
    tickers = raw.get("tickers", []) if isinstance(raw, dict) else raw
    return {str(t) for t in tickers if t} if isinstance(tickers, list) else set()


def resolve_deliberated(cfg) -> tuple[set, str]:
    """審議済みティッカー集合と、その出所ラベルを返す。

    Returns:
        (tickers, source)  source は "台帳(正本)" / "ミラー" / "journal(部分)" /
                           "なし" のいずれか。メールに但し書きを出すために使う。
    """
    ledger = _load_ledger_tickers(cfg)
    if ledger:
        return ledger, "台帳(正本)"
    mirror = _load_mirror_tickers(cfg)
    if mirror:
        return mirror, "ミラー"
    journal = _load_journal_tickers(cfg)
    if journal:
        return journal, "journal(部分)"
    return set(), "なし"


def export_deliberated_mirror(cfg) -> dict:
    """vault の判断台帳をリポジトリ側へミラーする（Mac 実行時のみ有効）。

    Actions 側のメール生成が正しい除外集合を使えるようにするための橋渡し。
    vault が読めない環境では何もしない（skipped を返す）。
    """
    tickers = _load_ledger_tickers(cfg)
    if not tickers:
        return {"skipped": True, "reason": "vault の判断台帳を読めない", "count": 0}
    data_dir = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / _DELIBERATED_MIRROR).write_text(
            json.dumps({
                "tickers": sorted(tickers),
                "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "source": "40_判断台帳.md",
            }, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception as e:
        return {"skipped": True, "reason": str(e)[:80], "count": 0}
    return {"skipped": False, "count": len(tickers)}


def get_queue_for_report(cfg, results: dict = None, top_n: int = 5) -> dict:
    """日次・週次メール用に審議キューを組み立てる（副作用なし・読み取りのみ）。

    候補は results["screener"]["candidates"] を優先し、無ければ
    screener_history.json から読む（Actions/ローカルどちらでも動くように）。

    Returns:
        {"queue": [...], "source": str, "already_count": int,
         "partial": bool, "interval_days": int}
        partial=True は除外集合が正本でない（＝審議済みが混じり得る）ことを示す。
    """
    cands = []
    if isinstance(results, dict):
        cands = ((results.get("screener") or {}).get("candidates") or [])
    if not cands:
        cands = _load_screener_candidates(cfg)

    already, source = resolve_deliberated(cfg)
    queue = build_deliberation_queue(cands, already, top_n=top_n)
    return {
        "queue":         queue,
        "source":        source,
        "already_count": len(already),
        "partial":       source != "台帳(正本)",
        "interval_days": int(getattr(cfg, "DELIBERATION_INTERVAL_DAYS", 14)),
    }


def queue_changes(history: dict, today_queue: list, already: set,
                  top_n: int = 5, today: str | None = None, lookback: int = 14,
                  today_cands: list | None = None) -> dict:
    """審議キューの前回からの入れ替わりを返す（純関数）。

    毎日同じ銘柄が並ぶと「ロジックが動いているのか」が分からないため、各銘柄が何日連続で並んでいるかと、前回から外れた銘柄と
    その理由をメールに出す。過去日のキューは同じ除外集合・同じ並べ方で
    screener_history から再計算する。

    Returns:
        {"streak": {ticker: 連続日数}, "new": [ticker],
         "dropped": [{"ticker","name","reason"}], "prev_date": str|None}
    """
    from performance_tracker import latest_candidates
    days = sorted(d for d in (history or {}) if not today or d < today)[-lookback:]
    past = []   # 新しい順の [(date, [ticker...], {ticker: cand})]
    for d in reversed(days):
        _, cands = latest_candidates({d: history[d]})
        q = build_deliberation_queue(cands, already, top_n=top_n)
        past.append((d, [c["ticker"] for c in q], {c.get("ticker"): c for c in cands}))
    today_t = [c.get("ticker") for c in today_queue]
    streak = {}
    for t in today_t:
        n = 1
        for _, qt, _ in past:
            if t in qt:
                n += 1
            else:
                break
        streak[t] = n
    dropped = []
    prev_date = past[0][0] if past else None
    if past:
        _, prev_q, prev_cands = past[0]
        in_queue = {c.get("ticker") for c in today_queue}
        ranked = build_deliberation_queue(today_cands or [], already, top_n=10 ** 6)
        rank = {c.get("ticker"): (i, c) for i, c in enumerate(ranked, 1)}
        for t in prev_q:
            if t in in_queue:
                continue
            if t in already:
                reason = "審議済みになった"
            elif t in rank:
                i, c = rank[t]
                reason = f"順位が下がった（今日{i}位・{float(c.get('composite_score') or 0):.1f}点）"
            else:
                reason = "今日の候補から外れた（スクリーナー不通過）"
            dropped.append({"ticker": t, "name": (prev_cands.get(t) or {}).get("name", ""),
                            "reason": reason})
    return {"streak": streak, "new": [t for t, n in streak.items() if n == 1],
            "dropped": dropped, "prev_date": prev_date}


def run_prep(cfg, ticker: str = "", dry_run: bool = False) -> dict:
    """メインエントリ。Returns: {"ticker","case_id","packet","prompts_path","skipped"}"""
    # vault が読める環境（Mac）でのみ、審議済みティッカーを
    # リポジトリ側へミラーする。GitHub Actions のメール生成が正しい除外集合を
    # 使えるようにするための橋渡し（Actions からは vault を読めないため）。
    if not dry_run:
        _mirror = export_deliberated_mirror(cfg)
        if not _mirror.get("skipped"):
            print(f"  🔁 審議済みミラー更新: {_mirror.get('count', 0)}銘柄")

    if not ticker:
        cands = _load_screener_candidates(cfg)
        already = _load_ledger_tickers(cfg)
        # メールに出したキューと同じ並び。先頭が選定対象になる。
        queue = build_deliberation_queue(
            cands, already,
            top_n=int(getattr(cfg, "DELIBERATION_QUEUE_TOP_N", 5)))
        if not queue:
            print("  ⏩ 審議対象なし（screener候補が空 or 全て審議済み）")
            return {"skipped": True}
        target = queue[0]
        ticker = target["ticker"]
        print(f"  🎯 審議対象選定: {ticker}（composite={target.get('composite_score')}）")
        if len(queue) > 1:
            _rest = " / ".join(
                f"{c.get('ticker')}({c.get('composite_score')})"
                for c in queue[1:])
            print(f"     次点: {_rest}")

    case_id = f"{datetime.now().strftime('%Y-%m-%d')}_{ticker.replace('.', '')}"
    prompts_md = build_launch_prompts(case_id, ticker)

    if dry_run:
        print(f"  ✅ --dry-run: 対象={ticker} case={case_id}（vault書込なし）")
        return {"ticker": ticker, "case_id": case_id, "skipped": False,
                "dry_run": True}

    # packet生成（best-effort）
    packet_path = None
    try:
        import agent_packet_exporter as ape
        packet_path = str(ape.export(ticker))
    except SystemExit as e:
        print(f"  ⚠️ packet生成スキップ（vault未検出）: {str(e)[:60]}")
    except Exception as e:
        print(f"  ⚠️ packet生成エラー: {str(e)[:60]}")

    # 起動プロンプトを vault の審議フォルダへ
    prompts_path = None
    try:
        vault = Path(getattr(cfg, "INVESTMENT_DEPT_VAULT_DIR", ""))
        if vault and vault.exists():
            d = vault / "20_審議" / case_id
            d.mkdir(parents=True, exist_ok=True)
            pp = d / "_起動プロンプト.md"
            pp.write_text(prompts_md, encoding="utf-8")
            prompts_path = str(pp)
            print(f"  ✅ 起動プロンプト生成: {pp}")
    except Exception as e:
        print(f"  ⚠️ プロンプト書込エラー: {str(e)[:60]}")

    return {"ticker": ticker, "case_id": case_id, "packet": packet_path,
            "prompts_path": prompts_path, "skipped": False}


def _state_path(cfg) -> Path:
    base = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
    return base / "deliberation_state.json"


def due_for_run(cfg, today: date = None, interval_days: int = None) -> tuple:
    """隔週判定。Returns: (走らせるか, 理由)

    launchd は毎週日曜に起動する（StartCalendarInterval に隔週指定が無いため）。
    実際に走らせるかはここで前回実行日から判定する。
    前回記録が無い初回は必ず走らせる。
    """
    today = today or date.today()
    interval = int(interval_days if interval_days is not None
                   else getattr(cfg, "DELIBERATION_INTERVAL_DAYS", 14))
    try:
        st = json.loads(_state_path(cfg).read_text(encoding="utf-8"))
        last = date.fromisoformat(str(st.get("last_run", "")))
    except Exception:
        return True, "前回実行の記録なし（初回）"
    elapsed = (today - last).days
    if elapsed >= interval:
        return True, f"前回から{elapsed}日経過（間隔{interval}日）"
    return False, f"前回から{elapsed}日（間隔{interval}日に未達）"


def _record_run(cfg, today: date = None) -> None:
    p = _state_path(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(
            {"last_run": (today or date.today()).isoformat()},
            ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️ 実行日記録に失敗: {str(e)[:60]}")


def main():
    ap = argparse.ArgumentParser(description="B部門隔週自動審議の準備（E3）")
    ap.add_argument("--ticker", default="", help="対象銘柄（省略時は自動選定）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="隔週判定を無視して実行する")
    args = ap.parse_args()
    try:
        import config as cfg
    except Exception:
        cfg = None

    # BLOCK 39 の ENABLE_INVESTMENT_DEPT で無効化できる。
    if not getattr(cfg, "ENABLE_INVESTMENT_DEPT", True):
        print("⏩ 審議準備: スキップ（ENABLE_INVESTMENT_DEPT=False）")
        return

    if not args.force and not args.dry_run:
        ok, why = due_for_run(cfg)
        if not ok:
            print(f"⏩ 審議準備: スキップ（{why}）— 手動実行は --force")
            return
        print(f"🗓️ 審議準備を実行: {why}")

    result = run_prep(cfg, args.ticker, args.dry_run)
    if not args.dry_run and not result.get("skipped"):
        _record_run(cfg)


if __name__ == "__main__":
    main()
