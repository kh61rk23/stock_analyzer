# ==============================================================
# idea_evaluator.py  v1.0  (4-B)
# アイデアのスコアリング・状態管理・週次集計
# ==============================================================
# 【役割】
#   ① idea_generator.py が生成したアイデアに人間評価を付与・記録
#      状態: "pending"(未評価) / "active"(継続追跡) /
#            "watching"(様子見) / "rejected"(棄却)
#
#   ② 週次サマリーを自動生成
#      - active/watching アイデアを idea_score 降順で集計
#      - 週次変化（スコア推移・状態変更）を記録
#
#   ③ INSIGHT_DIR/ideas/evaluations.json に永続化
#      INSIGHT_DIR/ideas/weekly_summary_YYYY-WXX.md を出力
#
# 【永続化スキーマ】evaluations.json
#   {
#     "idea_id": {                  # idea_id = YYYY-MM-DD_rank{N}
#       "idea_id":      str,
#       "title":        str,
#       "sector_name":  str,
#       "sector_code":  str,
#       "idea_score":   float,
#       "status":       "pending"|"active"|"watching"|"rejected",
#       "user_note":    str,         # 人間が入力するメモ
#       "created_at":   "YYYY-MM-DD",
#       "updated_at":   "YYYY-MM-DD HH:MM:SS",
#       "score_history":[{"date":"YYYY-MM-DD","score":float}, ...],
#     },
#     ...
#   }
#
# 【依存】config.py / json / pathlib / datetime
# 【呼出】from idea_evaluator import (
#           load_evaluations, upsert_evaluation,
#           run_weekly_summary, get_active_ideas )
# ==============================================================

import json
from datetime import datetime, timedelta
from pathlib import Path

# 有効な状態値
VALID_STATUSES = {"pending", "active", "watching", "rejected"}

# 状態ラベル（UI 表示用）
STATUS_LABEL = {
    "pending":  "⬜ 未評価",
    "active":   "🟢 継続追跡",
    "watching": "🟡 様子見",
    "rejected": "🔴 棄却",
}


# ==============================================================
# BLOCK 1: ファイルパス解決
# ==============================================================

def _eval_path(cfg) -> Path:
    """evaluations.json のパスを返す。"""
    insight_dir = Path(getattr(cfg, "INSIGHT_DIR", "insights"))
    return insight_dir / "ideas" / "evaluations.json"

def _weekly_dir(cfg) -> Path:
    """週次サマリーの保存ディレクトリを返す。"""
    insight_dir = Path(getattr(cfg, "INSIGHT_DIR", "insights"))
    return insight_dir / "ideas"


# ==============================================================
# BLOCK 2: 永続化 CRUD
# ==============================================================

def load_evaluations(cfg) -> dict:
    """
    evaluations.json を読み込んで返す。
    存在しない場合は {} を返す。
    """
    p = _eval_path(cfg)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_evaluations(cfg, evals: dict) -> None:
    """evaluations.json に書き込む。"""
    p = _eval_path(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(evals, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        print(f"    ⚠️  評価保存失敗: {str(e)[:60]}")


def upsert_evaluation(
    cfg,
    idea_id:    str,
    title:      str,
    sector_name: str,
    sector_code: str,
    idea_score:  float,
    status:      str = "pending",
    user_note:   str = "",
) -> dict:
    """
    アイデア評価を新規登録または更新する。

    - 初回登録: status="pending", score_history に初期スコアを追加
    - 更新時  : status / user_note を更新、score_history に追記

    Returns:
        更新後のエントリ dict
    """
    if status not in VALID_STATUSES:
        status = "pending"

    evals   = load_evaluations(cfg)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today   = datetime.now().strftime("%Y-%m-%d")

    if idea_id in evals:
        entry = evals[idea_id]
        entry["status"]     = status
        entry["user_note"]  = user_note
        entry["updated_at"] = now_str
        # スコア履歴（同日重複を避ける）
        history = entry.get("score_history", [])
        if not history or history[-1]["date"] != today:
            history.append({"date": today, "score": idea_score})
        entry["score_history"] = history
    else:
        entry = {
            "idea_id":      idea_id,
            "title":        title,
            "sector_name":  sector_name,
            "sector_code":  sector_code,
            "idea_score":   idea_score,
            "status":       status,
            "user_note":    user_note,
            "created_at":   today,
            "updated_at":   now_str,
            "score_history": [{"date": today, "score": idea_score}],
        }
    evals[idea_id] = entry
    save_evaluations(cfg, evals)
    return entry


def update_status(cfg, idea_id: str, new_status: str, note: str = "") -> bool:
    """
    指定 idea_id の状態を更新する。
    存在しない場合は False を返す。
    """
    if new_status not in VALID_STATUSES:
        return False
    evals = load_evaluations(cfg)
    if idea_id not in evals:
        return False
    evals[idea_id]["status"]     = new_status
    evals[idea_id]["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if note:
        evals[idea_id]["user_note"] = note
    save_evaluations(cfg, evals)
    return True


# ==============================================================
# BLOCK 3: クエリ
# ==============================================================

def get_active_ideas(cfg) -> list:
    """
    status が active / watching のアイデアを idea_score 降順で返す。
    """
    evals = load_evaluations(cfg)
    active = [e for e in evals.values()
              if e.get("status") in ("active", "watching")]
    return sorted(active, key=lambda x: x.get("idea_score", 0), reverse=True)


def get_all_ideas_by_status(cfg) -> dict:
    """
    status ごとにグループ化した辞書を返す。

    Returns:
        {"pending": [...], "active": [...], "watching": [...], "rejected": [...]}
    """
    evals = load_evaluations(cfg)
    grouped = {s: [] for s in VALID_STATUSES}
    for e in evals.values():
        s = e.get("status", "pending")
        grouped.setdefault(s, []).append(e)
    for lst in grouped.values():
        lst.sort(key=lambda x: x.get("idea_score", 0), reverse=True)
    return grouped


def sync_new_ideas(cfg, ideas: list) -> int:
    """
    idea_generator の出力リストを evaluations.json に同期する。
    すでに登録済みのアイデアはスキップ（上書きしない）。

    Returns:
        新規登録件数
    """
    added = 0
    for idea in ideas:
        idea_id = f"{idea.get('generated_at', 'nodate')}_rank{idea.get('rank', 0)}"
        evals   = load_evaluations(cfg)
        if idea_id not in evals:
            upsert_evaluation(
                cfg,
                idea_id     = idea_id,
                title       = idea.get("title", ""),
                sector_name = idea.get("sector_name", ""),
                sector_code = idea.get("sector_code", ""),
                idea_score  = idea.get("idea_score", 0),
                status      = "pending",
            )
            added += 1
    return added


# ==============================================================
# BLOCK 4: 週次集計
# ==============================================================

def _week_label() -> str:
    """現在の週ラベルを返す（例: 2026-W09）。"""
    now = datetime.now()
    return f"{now.year}-W{now.isocalendar()[1]:02d}"


def run_weekly_summary(cfg) -> dict:
    """
    週次集計を実行し、Markdown レポートを保存する。

    Returns:
        {
          "week":       "2026-W09",
          "active":     [...],   # active アイデア一覧
          "watching":   [...],   # watching アイデア一覧
          "new_this_week": [...],# 今週追加されたアイデア
          "top3":       [...],   # idea_score TOP3
          "total":      int,
          "timestamp":  str,
        }
    """
    print("\n  📊 週次アイデア集計 開始...")
    evals   = load_evaluations(cfg)
    week    = _week_label()
    today   = datetime.now().strftime("%Y-%m-%d")
    week_ago= (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    active   = [e for e in evals.values() if e.get("status") == "active"]
    watching = [e for e in evals.values() if e.get("status") == "watching"]
    new_week = [e for e in evals.values() if e.get("created_at", "") >= week_ago]

    all_tracked = active + watching
    all_tracked.sort(key=lambda x: x.get("idea_score", 0), reverse=True)
    top3 = all_tracked[:3]

    result = {
        "week":          week,
        "active":        sorted(active,   key=lambda x: x.get("idea_score", 0), reverse=True),
        "watching":      sorted(watching, key=lambda x: x.get("idea_score", 0), reverse=True),
        "new_this_week": sorted(new_week, key=lambda x: x.get("idea_score", 0), reverse=True),
        "top3":          top3,
        "total":         len(evals),
        "timestamp":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    print(f"    → {week}: active={len(active)} / watching={len(watching)} / 今週新規={len(new_week)}")
    _save_weekly_report(result, cfg)
    return result


def _save_weekly_report(result: dict, cfg) -> None:
    """insights/ideas/weekly_summary_YYYY-WXX.md に保存する。"""
    week = result["week"]
    out  = _weekly_dir(cfg) / f"weekly_summary_{week}.md"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# 週次アイデアサマリー（{week}）", "",
            f"**生成日時**: {result['timestamp']}  |  **総アイデア数**: {result['total']}",
            "",
            f"## 🟢 継続追跡（{len(result['active'])}件）", "",
            "| # | タイトル | 業種 | スコア | 更新日 |",
            "|---|---------|------|-------|-------|",
        ]
        for i, e in enumerate(result["active"], 1):
            lines.append(
                f"| {i} | {e['title'][:40]} | {e['sector_name']} "
                f"| {e.get('idea_score', 0):.1f} | {e.get('updated_at','')[:10]} |"
            )
        lines += [
            "",
            f"## 🟡 様子見（{len(result['watching'])}件）", "",
            "| # | タイトル | 業種 | スコア |",
            "|---|---------|------|-------|",
        ]
        for i, e in enumerate(result["watching"], 1):
            lines.append(
                f"| {i} | {e['title'][:40]} | {e['sector_name']} "
                f"| {e.get('idea_score', 0):.1f} |"
            )
        if result["top3"]:
            lines += ["", "## 🏆 スコア TOP3", ""]
            for i, e in enumerate(result["top3"], 1):
                lines.append(
                    f"{i}. **[{e['sector_name']}]** {e['title'][:50]} — "
                    f"スコア: {e.get('idea_score', 0):.1f} / {STATUS_LABEL.get(e.get('status',''), e.get('status',''))}"
                )
        if result["new_this_week"]:
            lines += ["", f"## 🆕 今週の新規アイデア（{len(result['new_this_week'])}件）", ""]
            for e in result["new_this_week"]:
                lines.append(f"- {e['title'][:50]} [{e.get('sector_name','')}]")

        out.write_text("\n".join(lines), encoding="utf-8")
        print(f"    💾 週次サマリー保存: weekly_summary_{week}.md")
    except Exception as ex:
        print(f"    ⚠️  週次サマリー保存失敗: {str(ex)[:60]}")


# ==============================================================
# BLOCK 5: run_all_tasks 用エントリポイント
# ==============================================================

def run_idea_evaluator(cfg, ideas: list = None) -> dict:
    """
    run_all_tasks から呼ばれるエントリポイント。
    ① idea_generator の最新結果を evaluations.json に同期
    ② 週次集計を実行（月曜日のみ、または EVAL_FORCE_WEEKLY=True）
    """
    print("\n  📊 アイデアエバリュエーター 開始...")

    # 同期
    added = sync_new_ideas(cfg, ideas or [])
    print(f"    → 新規登録: {added}件")

    # 週次集計（月曜日 or 強制フラグ）
    force   = getattr(cfg, "EVAL_FORCE_WEEKLY", False)
    is_mon  = datetime.now().weekday() == 0  # 0 = Monday
    summary = {}
    if is_mon or force:
        summary = run_weekly_summary(cfg)
    else:
        print("    → 週次集計: スキップ（月曜日以外）")

    evals = load_evaluations(cfg)
    return {
        "added":   added,
        "total":   len(evals),
        "summary": summary,
    }


# 単体テスト
if __name__ == "__main__":
    import importlib.util
    for p in ["/content/config.py", str(Path(__file__).parent / "config.py")]:
        if Path(p).exists():
            spec = importlib.util.spec_from_file_location("config", p)
            cfg  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cfg)
            break
    # テスト用ダミーアイデア
    dummy = [
        {"title": "銀行業 × LLM信用リスク", "sector_name": "銀行業",
         "sector_code": "1028", "idea_score": 72.0,
         "generated_at": "2026-02-27", "rank": 1},
    ]
    result = run_idea_evaluator(cfg, dummy)
    print(f"登録済み: {result['total']}件")
    active = get_active_ideas(cfg)
    print(f"active: {len(active)}件")
